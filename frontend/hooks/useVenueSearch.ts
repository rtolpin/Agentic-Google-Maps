// useVenueSearch.ts — React hook for streaming venue search
// Connects to SSE endpoint, progressively updates map pins and insight cards.

import { useCallback, useReducer, useRef } from "react";

// ─── Domain Types ─────────────────────────────────────────────────────────────

export type NoiseLevel = "very_quiet" | "quiet" | "moderate" | "loud" | "very_loud";
export type WifiQuality = "none" | "poor" | "good" | "excellent";
export type BookingDifficulty = "easy" | "moderate" | "hard";
export type PriceBand = "budget" | "mid" | "upscale" | "luxury";
export type NoisePreference = "quiet" | "moderate" | "lively";
export type FeedbackValue = 1 | -1;

export interface VenueIntelligence {
  why_card: string;
  scenario: string;
  sensitivity_bars: Record<string, number>;
  live_signal: string | null;
  suggestions: string[];
}

export interface VenueSignal {
  venue_id: string;
  name: string;
  city: string;
  neighborhood: string;
  cuisine: string;
  // Google Place ID extracted by Nimble — passed to Google Maps JS API for rendering.
  // Never used to display Google's content data (ratings, photos) outside a Google Map.
  place_id: string;
  address: string;
  latitude: number | null;
  longitude: number | null;
  price_per_head: number;
  has_private_room: boolean;
  max_group_size: number;
  noise_level: NoiseLevel;
  birthday_score: number;
  match_score: number;
  key_quotes: string[];
  scraped_at: string | null;
  intelligence?: VenueIntelligence;
}

// ─── Google Maps Integration Types ───────────────────────────────────────────
// COMPLIANCE (Google TOS):
//   - Display place details only on an official Google Map (Maps JS API)
//   - Do not cache GooglePlaceDetails beyond the session
//   - Only render markers for venues with place_ids from Google identifiers

export interface GooglePlaceDetails {
  place_id: string;
  name: string;
  formatted_address: string;
  rating: number | null;
  user_rating_count: number | null;
  price_level: "PRICE_LEVEL_FREE" | "PRICE_LEVEL_INEXPENSIVE" | "PRICE_LEVEL_MODERATE" | "PRICE_LEVEL_EXPENSIVE" | "PRICE_LEVEL_VERY_EXPENSIVE" | null;
  is_open_now: boolean | null;
  website_uri: string | null;
  phone_number: string | null;
  latitude: number | null;
  longitude: number | null;
  photo_url: string | null;
}

export interface MapMarker {
  venue_id: string;
  place_id: string;
  name: string;
  latitude: number | null;
  longitude: number | null;
  match_score: number;
  has_private_room: boolean;
  price_per_head: number;
}

export interface GoogleMapsConfig {
  apiKey: string;
  mapId: string;
  defaultCenter: LatLng;
  defaultZoom: number;
}

export interface LatLng {
  lat: number;
  lng: number;
}

export interface MapBounds {
  north: number;
  south: number;
  east: number;
  west: number;
}

export type MapPinColor = "primary" | "highlighted" | "dimmed";

export interface EnrichedMapMarker extends MapMarker {
  pinColor: MapPinColor;
  isSelected: boolean;
}

export interface ParsedIntent {
  occasion: string;
  group_size: number;
  cuisine: string | null;
  noise_preference: NoisePreference | null;
  needs_private_room: boolean;
  city: string;
  date: string | null;
  price_band: PriceBand | null;
  dietary_restrictions: string[];
  other_signals: string[];
}

export interface CityBenchmark {
  occasion_score: number;
  avg_price: number;
  private_room_rate: number;
  venue_count: number;
}

export interface ValidationResult {
  valid: boolean;
  warnings: string[];
}

// ─── SSE Event Discriminated Union ────────────────────────────────────────────

export type SSEEvent =
  | { event: "status";      data: string }
  | { event: "intent";      data: ParsedIntent }
  | { event: "venues_raw";  data: VenueSignal[] }
  | { event: "validation";  data: ValidationResult }
  | { event: "global_intel"; data: Record<string, CityBenchmark> }
  | { event: "results";     data: VenueSignal[] }
  | { event: "done";        data: { total_venues: number } }
  | { event: "error";       data: string }
  | { event: "end";         data: null };

// ─── State & Actions ──────────────────────────────────────────────────────────

export type SearchStatus = "idle" | "searching" | "done" | "error";

export interface SearchState {
  status: SearchStatus;
  statusMessage: string;
  intent: ParsedIntent | null;
  venues: VenueSignal[];
  mapMarkers: MapMarker[];
  selectedVenueId: string | null;
  globalIntel: Record<string, CityBenchmark> | null;
  totalVenues: number | null;
  error: string | null;
}

type SearchAction =
  | { type: "SEARCH_STARTED" }
  | { type: "STATUS_UPDATE";    message: string }
  | { type: "INTENT_PARSED";    intent: ParsedIntent }
  | { type: "VENUES_RAW";       venues: VenueSignal[] }
  | { type: "RESULTS";          venues: VenueSignal[] }
  | { type: "GLOBAL_INTEL";     data: Record<string, CityBenchmark> }
  | { type: "SEARCH_DONE";      total: number }
  | { type: "SEARCH_ERROR";     error: string }
  | { type: "SEARCH_CANCELLED" }
  | { type: "SELECT_VENUE";     venueId: string | null }
  | { type: "MAP_MARKERS_SET";  markers: MapMarker[] };

const initialState: SearchState = {
  status: "idle",
  statusMessage: "",
  intent: null,
  venues: [],
  mapMarkers: [],
  selectedVenueId: null,
  globalIntel: null,
  totalVenues: null,
  error: null,
};

function searchReducer(state: SearchState, action: SearchAction): SearchState {
  switch (action.type) {
    case "SEARCH_STARTED":
      return { ...initialState, status: "searching", statusMessage: "Starting search..." };
    case "STATUS_UPDATE":
      return { ...state, statusMessage: action.message };
    case "INTENT_PARSED":
      return { ...state, intent: action.intent };
    case "VENUES_RAW":
      return { ...state, venues: action.venues };
    case "RESULTS": {
      // Auto-build map markers from venues that have a place_id
      const markers: MapMarker[] = action.venues
        .filter((v) => v.place_id)
        .map((v) => ({
          venue_id: v.venue_id,
          place_id: v.place_id,
          name: v.name,
          latitude: v.latitude,
          longitude: v.longitude,
          match_score: v.match_score,
          has_private_room: v.has_private_room,
          price_per_head: v.price_per_head,
        }));
      return {
        ...state,
        venues: action.venues,
        mapMarkers: markers,
        statusMessage: `Found ${action.venues.length} matches`,
      };
    }
    case "MAP_MARKERS_SET":
      return { ...state, mapMarkers: action.markers };
    case "SELECT_VENUE":
      return { ...state, selectedVenueId: action.venueId };
    case "GLOBAL_INTEL":
      return { ...state, globalIntel: action.data };
    case "SEARCH_DONE":
      return { ...state, status: "done", totalVenues: action.total };
    case "SEARCH_ERROR":
      return { ...state, status: "error", error: action.error };
    case "SEARCH_CANCELLED":
      return { ...state, status: "idle" };
    default:
      return state;
  }
}

// ─── Hook ─────────────────────────────────────────────────────────────────────

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface UseVenueSearchReturn {
  state: SearchState;
  search: (query: string, userCity?: string, userCoords?: { lat: number; lng: number; radiusM?: number }) => Promise<void>;
  sendFeedback: (venueId: string, query: string, feedback: FeedbackValue) => Promise<void>;
  /**
   * Fetch real-time Google Place details for a venue.
   * Pass placeId when available to skip the ClickHouse lookup (faster, always works).
   * COMPLIANCE: Display the result on a Google Map only. Do not persist.
   */
  fetchPlaceDetails: (venueId: string, placeId?: string) => Promise<GooglePlaceDetails | null>;
  selectVenue: (venueId: string | null) => void;
  cancel: () => void;
}

export function useVenueSearch(userId: string): UseVenueSearchReturn {
  const [state, dispatch] = useReducer(searchReducer, initialState);
  const abortRef = useRef<AbortController | null>(null);

  const handleEvent = useCallback((msg: SSEEvent): void => {
    switch (msg.event) {
      case "status":
        dispatch({ type: "STATUS_UPDATE", message: msg.data });
        break;
      case "intent":
        dispatch({ type: "INTENT_PARSED", intent: msg.data });
        break;
      case "venues_raw":
        dispatch({ type: "VENUES_RAW", venues: msg.data });
        break;
      case "results":
        dispatch({ type: "RESULTS", venues: msg.data });
        break;
      case "global_intel":
        dispatch({ type: "GLOBAL_INTEL", data: msg.data });
        break;
      case "done":
        dispatch({ type: "SEARCH_DONE", total: msg.data.total_venues });
        break;
      case "error":
        dispatch({ type: "SEARCH_ERROR", error: msg.data });
        break;
    }
  }, []);

  const search = useCallback(
    async (query: string, userCity?: string, userCoords?: { lat: number; lng: number; radiusM?: number }): Promise<void> => {
      abortRef.current?.abort();
      abortRef.current = new AbortController();
      dispatch({ type: "SEARCH_STARTED" });

      try {
        const resp = await fetch(`${API_BASE}/api/search/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query,
            user_id: userId,
            user_city: userCity || undefined,
            user_lat: userCoords?.lat ?? undefined,
            user_lng: userCoords?.lng ?? undefined,
            user_radius_m: userCoords?.radiusM ?? undefined,
          }),
          signal: abortRef.current.signal,
        });

        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (!resp.body) throw new Error("No response body");

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const raw = line.slice(6).trim();
            if (!raw || raw === "[DONE]") continue;
            try {
              handleEvent(JSON.parse(raw) as SSEEvent);
            } catch {
              // Partial JSON — accumulate in buffer for next chunk
            }
          }
        }
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") return;
        dispatch({
          type: "SEARCH_ERROR",
          error: err instanceof Error ? err.message : "Unknown error",
        });
      }
    },
    [userId, handleEvent],
  );

  const sendFeedback = useCallback(
    async (venueId: string, query: string, feedback: FeedbackValue): Promise<void> => {
      await fetch(`${API_BASE}/api/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: userId,
          venue_id: venueId,
          query,
          feedback,
        }),
      });
    },
    [userId],
  );

  /**
   * Fetch real-time Google Place details from the backend.
   * Uses /api/place/{placeId} when placeId is provided (skips ClickHouse lookup).
   * Falls back to /api/venue/{venueId}/place for backwards compatibility.
   * COMPLIANCE: Do NOT store the returned object. Display on a Google Map only.
   */
  const fetchPlaceDetails = useCallback(
    async (venueId: string, placeId?: string): Promise<GooglePlaceDetails | null> => {
      try {
        const url = placeId
          ? `${API_BASE}/api/place/${encodeURIComponent(placeId)}`
          : `${API_BASE}/api/venue/${encodeURIComponent(venueId)}/place`;
        const resp = await fetch(url);
        if (!resp.ok) return null;
        return (await resp.json()) as GooglePlaceDetails;
      } catch {
        return null;
      }
    },
    [],
  );

  const selectVenue = useCallback((venueId: string | null): void => {
    dispatch({ type: "SELECT_VENUE", venueId });
  }, []);

  const cancel = useCallback((): void => {
    abortRef.current?.abort();
    dispatch({ type: "SEARCH_CANCELLED" });
  }, []);

  return { state, search, sendFeedback, fetchPlaceDetails, selectVenue, cancel };
}
