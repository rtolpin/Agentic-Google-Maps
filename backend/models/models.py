"""
Shared Pydantic models for The Right Spot.
All domain types, enums, and serialization helpers live here.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─── Enumerations ─────────────────────────────────────────────────────────────

class NoiseLevel(str, Enum):
    VERY_QUIET = "very_quiet"
    QUIET = "quiet"
    MODERATE = "moderate"
    LOUD = "loud"
    VERY_LOUD = "very_loud"

    @property
    def ch_int(self) -> int:
        return {"very_quiet": 1, "quiet": 2, "moderate": 3, "loud": 4, "very_loud": 5}[self.value]

    @classmethod
    def from_ch_int(cls, value: int) -> "NoiseLevel":
        return {1: cls.VERY_QUIET, 2: cls.QUIET, 3: cls.MODERATE, 4: cls.LOUD, 5: cls.VERY_LOUD}.get(value, cls.MODERATE)


class WifiQuality(str, Enum):
    NONE = "none"
    POOR = "poor"
    GOOD = "good"
    EXCELLENT = "excellent"

    @property
    def ch_int(self) -> int:
        return {"none": 0, "poor": 1, "good": 2, "excellent": 3}[self.value]

    @classmethod
    def from_ch_int(cls, value: int) -> "WifiQuality":
        return {0: cls.NONE, 1: cls.POOR, 2: cls.GOOD, 3: cls.EXCELLENT}.get(value, cls.NONE)


class BookingDifficulty(str, Enum):
    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"

    @property
    def ch_int(self) -> int:
        return {"easy": 1, "moderate": 2, "hard": 3}[self.value]

    @classmethod
    def from_ch_int(cls, value: int) -> "BookingDifficulty":
        return {1: cls.EASY, 2: cls.MODERATE, 3: cls.HARD}.get(value, cls.MODERATE)


class PriceBand(str, Enum):
    BUDGET = "budget"
    MID = "mid"
    UPSCALE = "upscale"
    LUXURY = "luxury"

    @property
    def price_range(self) -> tuple[int, int]:
        return {"budget": (0, 40), "mid": (40, 80), "upscale": (80, 150), "luxury": (150, 999)}[self.value]


class NoisePreference(str, Enum):
    QUIET = "quiet"
    MODERATE = "moderate"
    LIVELY = "lively"

    @property
    def ch_value(self) -> str:
        """SQL-comparable string for the SCORE_QUERY noise scoring branch."""
        return self.value


# ─── Intent ───────────────────────────────────────────────────────────────────

class VenueIntent(BaseModel):
    occasion: str = "dining"
    group_size: int = Field(default=2, ge=1, le=500)
    cuisine: Optional[str] = None
    noise_preference: Optional[NoisePreference] = None
    needs_private_room: bool = False
    city: str = "Unknown"
    date: Optional[str] = None
    price_band: Optional[PriceBand] = None
    dietary_restrictions: list[str] = Field(default_factory=list)
    other_signals: list[str] = Field(default_factory=list)

    @field_validator("group_size", mode="before")
    @classmethod
    def coerce_group_size(cls, v: object) -> object:
        # Coerce null (from LLM output) to the default; let all other values
        # pass through so the int type and ge/le constraints still fire.
        return 2 if v is None else v

    @field_validator("city", mode="before")
    @classmethod
    def city_not_empty(cls, v: object) -> str:
        if v is None:
            return "Unknown"
        if isinstance(v, str) and not v.strip():
            raise ValueError("city must not be empty")
        return str(v).strip()

    @property
    def price_range(self) -> tuple[int, int]:
        return self.price_band.price_range if self.price_band else (0, 999)

    @property
    def noise_sql_value(self) -> str:
        """Noise preference as a ClickHouse-comparable string."""
        if self.noise_preference is None:
            return "moderate"
        return self.noise_preference.ch_value

    def to_score_params(self) -> dict[str, Any]:
        """Produce the parameter dict expected by SCORE_QUERY."""
        price_min, price_max = self.price_range
        return {
            "city": self.city,
            "cuisine": self.cuisine or "",
            "group_size": self.group_size,
            "needs_private_room": self.needs_private_room,
            "noise_pref": self.noise_sql_value,
            "price_min": price_min,
            "price_max": price_max,
            "occasion": self.occasion,
        }


# ─── Raw / Scraped Data ───────────────────────────────────────────────────────

class RawVenueResult(BaseModel):
    name: str
    url: Optional[str] = None
    snippet: str = ""
    source: str = ""
    # Extracted by Nimble google_maps engine — stored in ClickHouse as a string
    # identifier only.  Google's actual data (ratings, photos) is never cached.
    place_id: str = ""
    address: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class ExtractedSignals(BaseModel):
    """Claude-extracted venue attributes from unstructured review text."""
    noise_level: Optional[NoiseLevel] = None
    has_private_room: Optional[bool] = None
    max_group_size: Optional[int] = Field(default=None, ge=0)
    birthday_friendly: Optional[bool] = None
    wifi_quality: Optional[WifiQuality] = None
    dog_friendly: Optional[bool] = None
    outdoor_seating: Optional[bool] = None
    price_per_head_usd: Optional[int] = Field(default=None, ge=0)
    booking_difficulty: Optional[BookingDifficulty] = None
    special_occasion_score: int = Field(default=0, ge=0, le=100)
    birthday_mentions: int = Field(default=0, ge=0)
    key_quotes: list[str] = Field(default_factory=list)

    @field_validator("key_quotes")
    @classmethod
    def cap_quotes(cls, v: list[str]) -> list[str]:
        return v[:3]


class EnrichedVenue(RawVenueResult, ExtractedSignals):
    """Merged raw result + Claude-extracted signals, ready for ClickHouse upsert."""
    neighborhood: str = ""
    cuisine: str = ""

    def to_venue_signal(self, city: str) -> "VenueSignal":
        venue_id = (self.name + city).lower().replace(" ", "_").replace("'", "")
        return VenueSignal(
            venue_id=venue_id,
            name=self.name,
            city=city,
            neighborhood=self.neighborhood,
            cuisine=self.cuisine,
            url=self.url or "",
            noise_level=self.noise_level or NoiseLevel.MODERATE,
            has_private_room=self.has_private_room or False,
            max_group_size=self.max_group_size or 0,
            birthday_score=min(100, (self.birthday_mentions or 0) * 10 + (50 if self.birthday_friendly else 0)),
            wifi_quality=self.wifi_quality or WifiQuality.NONE,
            dog_friendly=self.dog_friendly or False,
            outdoor_seating=self.outdoor_seating or False,
            price_per_head=self.price_per_head_usd or 0,
            booking_difficulty=self.booking_difficulty or BookingDifficulty.MODERATE,
            special_occasion_score=self.special_occasion_score,
            birthday_mentions=self.birthday_mentions,
            key_quotes=self.key_quotes,
        )


# ─── ClickHouse Venue Record ──────────────────────────────────────────────────

class VenueSignal(BaseModel):
    venue_id: str
    name: str
    city: str
    neighborhood: str = ""
    cuisine: str = ""
    url: str = ""
    # Google Place ID — only the identifier is stored, never Google's content data
    place_id: str = ""
    address: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    noise_level: NoiseLevel = NoiseLevel.MODERATE
    has_private_room: bool = False
    max_group_size: int = Field(default=0, ge=0)
    birthday_score: int = Field(default=0, ge=0, le=100)
    wifi_quality: WifiQuality = WifiQuality.NONE
    dog_friendly: bool = False
    outdoor_seating: bool = False
    price_per_head: int = Field(default=0, ge=0)
    booking_difficulty: BookingDifficulty = BookingDifficulty.MODERATE
    special_occasion_score: int = Field(default=0, ge=0, le=100)
    birthday_mentions: int = Field(default=0, ge=0)
    key_quotes: list[str] = Field(default_factory=list)
    scraped_at: Optional[datetime] = None

    CH_COLUMNS: ClassVar[list[str]] = [
        "venue_id", "name", "city", "neighborhood", "cuisine", "url",
        "place_id", "address", "latitude", "longitude",
        "noise_level", "has_private_room", "max_group_size",
        "birthday_score", "wifi_quality", "dog_friendly",
        "outdoor_seating", "price_per_head", "booking_difficulty",
        "special_occasion_score", "birthday_mentions", "key_quotes",
        "scraped_at", "signal_age_hrs",
    ]

    def to_ch_row(self, now: datetime) -> list[Any]:
        """Serialize to a ClickHouse insert row matching CH_COLUMNS order."""
        return [
            self.venue_id,
            self.name,
            self.city,
            self.neighborhood,
            self.cuisine,
            self.url,
            self.place_id,
            self.address,
            self.latitude or 0.0,
            self.longitude or 0.0,
            self.noise_level.ch_int,
            int(self.has_private_room),
            self.max_group_size,
            self.birthday_score,
            self.wifi_quality.ch_int,
            int(self.dog_friendly),
            int(self.outdoor_seating),
            self.price_per_head,
            self.booking_difficulty.ch_int,
            self.special_occasion_score,
            self.birthday_mentions,
            self.key_quotes,
            now,
            0,  # signal_age_hrs — freshness computed at query time via dateDiff
        ]


# ─── Scored / Intelligence Results ───────────────────────────────────────────

class VenueIntelligence(BaseModel):
    why_card: str
    scenario: str
    sensitivity_bars: dict[str, int]
    live_signal: Optional[str] = None
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("sensitivity_bars")
    @classmethod
    def clamp_bars(cls, v: dict[str, int]) -> dict[str, int]:
        return {k: max(0, min(100, val)) for k, val in v.items()}

    @field_validator("suggestions")
    @classmethod
    def cap_suggestions(cls, v: list[str]) -> list[str]:
        return v[:4]


class ScoredVenue(BaseModel):
    venue_id: str
    name: str
    city: str
    neighborhood: str = ""
    cuisine: str = ""
    # place_id stored by Nimble extraction — used by frontend to render Google Maps marker
    place_id: str = ""
    address: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_head: int = 0
    has_private_room: bool = False
    max_group_size: int = 0
    noise_level: str = ""
    birthday_score: int = 0
    key_quotes: list[str] = Field(default_factory=list)
    scraped_at: Optional[str] = None
    match_score: float = 0.0
    intelligence: Optional[VenueIntelligence] = None

    CH_ROW_FIELDS: ClassVar[list[str]] = [
        "venue_id", "name", "city", "neighborhood", "cuisine",
        "place_id", "address", "latitude", "longitude",
        "price_per_head", "has_private_room", "max_group_size",
        "noise_level", "birthday_score", "key_quotes", "scraped_at", "match_score",
    ]

    @classmethod
    def from_ch_row(cls, row: tuple) -> "ScoredVenue":
        return cls(
            venue_id=row[0],
            name=row[1],
            city=row[2],
            neighborhood=row[3],
            cuisine=row[4],
            place_id=row[5] or "",
            address=row[6] or "",
            latitude=float(row[7]) if row[7] else None,
            longitude=float(row[8]) if row[8] else None,
            price_per_head=row[9],
            has_private_room=bool(row[10]),
            max_group_size=row[11],
            noise_level=row[12] if isinstance(row[12], str) else NoiseLevel.from_ch_int(row[12]).value,
            birthday_score=row[13],
            key_quotes=list(row[14]) if row[14] else [],
            scraped_at=row[15].isoformat() if row[15] else None,
            match_score=round(float(row[16]), 1),
        )


# ─── User Preferences & Feedback ──────────────────────────────────────────────

class UserPreferences(BaseModel):
    prefers_quiet: bool = False
    preferred_neighborhoods: list[str] = Field(default_factory=list)
    preferred_cuisines: list[str] = Field(default_factory=list)
    prefers_private_room: bool = False
    price_ceiling: Optional[int] = Field(default=None, ge=0)


class FeedbackSignal(BaseModel):
    user_id: str
    venue_id: str
    query: str
    feedback: int = Field(..., ge=-1, le=1)

    @field_validator("feedback")
    @classmethod
    def only_valid_feedback(cls, v: int) -> int:
        if v not in (-1, 0, 1):
            raise ValueError("feedback must be -1, 0, or 1")
        return v


# ─── API Shapes ───────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=500)
    user_id: Optional[str] = None


class CityBenchmark(BaseModel):
    occasion_score: float = Field(ge=0, le=100)
    avg_price: float = Field(ge=0)
    private_room_rate: float = Field(ge=0, le=1)
    venue_count: int = Field(ge=0)


class HealthResponse(BaseModel):
    status: str = "ok"


# ─── Senso.ai GEO & Governance Types ─────────────────────────────────────────

class SensoEntityType(str, Enum):
    VENUE = "venue"
    CITY = "city"
    CUISINE = "cuisine"
    OCCASION = "occasion"


class SensoClaimType(str, Enum):
    PRICE = "price"
    NOISE = "noise"
    CAPACITY = "capacity"
    PRIVATE_ROOM = "private_room"
    CUISINE = "cuisine"
    BOOKING = "booking"
    OCCASION_FIT = "occasion_fit"
    QUOTE = "quote"


class GapPriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SensoKBEntry(BaseModel):
    """A single verified fact record from Senso's knowledge base."""
    source_id: str
    entity_type: SensoEntityType
    entity_name: str
    verified_facts: dict[str, Any] = Field(default_factory=dict)
    last_verified: Optional[datetime] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    traceable_url: Optional[str] = None


class SensoKBResult(BaseModel):
    """Response from a Senso knowledge-base query."""
    entries: list[SensoKBEntry] = Field(default_factory=list)
    query_id: str = ""
    total_entries: int = 0

    def get_verified_facts_for(self, entity_name: str) -> dict[str, Any]:
        """Return merged verified facts for a named entity, highest confidence first."""
        relevant = sorted(
            [e for e in self.entries if e.entity_name.lower() == entity_name.lower()],
            key=lambda e: e.confidence,
            reverse=True,
        )
        merged: dict[str, Any] = {}
        for entry in relevant:
            merged.update(entry.verified_facts)
        return merged


class VenueCitation(BaseModel):
    """Links a venue claim to its traceable Senso source IDs."""
    venue_name: str
    claim_type: SensoClaimType
    claim_value: str
    source_ids: list[str] = Field(default_factory=list)
    verified: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class GovernanceScore(BaseModel):
    """Senso governance evaluation result for published content."""
    overall_score: float = Field(ge=0, le=100)
    hallucination_risk: float = Field(ge=0.0, le=1.0)
    compliance_flags: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)

    @property
    def is_compliant(self) -> bool:
        return self.overall_score >= 70 and self.hallucination_risk < 0.2


class ContentGapReport(BaseModel):
    """Tells Senso what verified data is missing so its remediation engine can act."""
    entity_name: str
    entity_type: SensoEntityType
    missing_fields: list[str]
    priority: GapPriority = GapPriority.MEDIUM
    context: str = ""
    suggested_sources: list[str] = Field(default_factory=list)


class GEOMetadata(BaseModel):
    """
    Generative Engine Optimization metadata attached to every Senso publish.
    Structures content so it's discoverable and citable by LLMs.
    """
    city: str
    occasion: str
    cuisine: Optional[str] = None
    entities: list[str] = Field(default_factory=list)          # venue names, neighborhoods
    keywords: list[str] = Field(default_factory=list)          # searchable terms
    compliance_domain: str = "hospitality"
    agent: str = "the-right-spot"
    grounded: bool = True
    citation_count: int = 0
    verified_claim_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    data_freshness_hours: int = 0


class SensoPublishResult(BaseModel):
    """Full result of a Senso publish operation including governance outcome."""
    slug: str
    url: Optional[str] = None
    version_id: str = ""
    status: str = "published"
    governance_score: Optional[GovernanceScore] = None
    citations_registered: int = 0
    gaps_reported: int = 0


class PublishedGuide(BaseModel):
    """Returned to the orchestrator after a successful Senso publish cycle."""
    slug: str
    url: Optional[str] = None
    status: str = "published"
    governance_score: Optional[GovernanceScore] = None
    citations_registered: int = 0
    gaps_reported: int = 0
    is_compliant: bool = True


# ─── Google Maps Platform Types ───────────────────────────────────────────────
# COMPLIANCE RULE: These models hold REAL-TIME Google data only.
# They must NEVER be persisted to ClickHouse or any long-term store.
# Google restricts caching. Store only place_id (a string identifier)
# and display everything else fresh from the Google Maps Platform APIs.

class GooglePriceLevel(str, Enum):
    FREE = "PRICE_LEVEL_FREE"
    INEXPENSIVE = "PRICE_LEVEL_INEXPENSIVE"
    MODERATE = "PRICE_LEVEL_MODERATE"
    EXPENSIVE = "PRICE_LEVEL_EXPENSIVE"
    VERY_EXPENSIVE = "PRICE_LEVEL_VERY_EXPENSIVE"


class GooglePlaceDetails(BaseModel):
    """
    Real-time Google Maps place data.  NOT FOR LONG-TERM STORAGE.
    Results from the Google Maps Platform must be displayed on a Google Map
    (TOS requirement) and may not be cached beyond the session.
    """
    place_id: str
    name: str = ""
    formatted_address: str = ""
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    user_rating_count: Optional[int] = Field(default=None, ge=0)
    price_level: Optional[GooglePriceLevel] = None
    is_open_now: Optional[bool] = None
    website_uri: Optional[str] = None
    phone_number: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class MapMarker(BaseModel):
    """
    Minimal map pin payload sent to the frontend for Google Maps rendering.
    Only place_id + coordinates + display name — no cached Google content.
    The frontend passes place_id to the Google Maps JS API for interactive display.
    """
    venue_id: str
    place_id: str
    name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    match_score: float = 0.0
    has_private_room: bool = False
    price_per_head: int = 0


class GeocodeResult(BaseModel):
    """Latitude/longitude from a Google Maps geocoding call."""
    latitude: float
    longitude: float
    formatted_address: str = ""
    place_id: str = ""


class NimbleMapsResult(BaseModel):
    """
    Structured result from Nimble's google_maps search engine.
    Nimble uses computer vision to extract local pack data including Place IDs.
    """
    name: str
    place_id: str = ""
    address: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    review_count: Optional[int] = Field(default=None, ge=0)
    snippet: str = ""
    url: Optional[str] = None
    phone: Optional[str] = None
    business_type: Optional[str] = None
