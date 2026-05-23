/**
 * VenueMap — Intelligent, conversational Google Maps interface.
 *
 * Architecture:
 *   - Google Maps JS API (AdvancedMarkerElement) renders the base map
 *   - AI search overlay accepts free-form natural language queries
 *   - Query router classifies intent into spatial categories:
 *       restaurants, cafes, hiking, parks, offices, bookstores, etc.
 *   - Venue cards displayed as info windows ON the map (Google TOS compliance)
 *   - Datadog Browser RUM traces all interactions
 *
 * Maps Agentic UI Toolkit pattern:
 *   1. User types a natural language query ("find me a cozy reading spot near Midtown")
 *   2. AI classifies the intent and maps it to a place type + filters
 *   3. Results are rendered as dynamic map markers with intelligence overlays
 *   4. Conversational follow-ups refine the search in real time
 */

"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  EnrichedMapMarker,
  GoogleMapsConfig,
  GooglePlaceDetails,
  LatLng,
  MapMarker,
  MapPinColor,
  VenueSignal,
} from "../hooks/useVenueSearch";
import { useVenueSearch } from "../hooks/useVenueSearch";
import {
  rumAction,
  traceMapInteraction,
  tracePlaceDetailsFetch,
  traceSearch,
  traceSSEEvent,
} from "../lib/tracing";

// ─── Google Maps loader (lazy) ────────────────────────────────────────────

declare global {
  interface Window {
    google: typeof google;
    googleMapsLoaded: boolean;
  }
}

async function loadGoogleMaps(apiKey: string, mapId: string): Promise<void> {
  if (window.googleMapsLoaded) return;
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `https://maps.googleapis.com/maps/api/js?key=${apiKey}&libraries=maps,marker&v=beta&loading=async`;
    script.async = true;
    script.defer = true;
    script.onload = () => { window.googleMapsLoaded = true; resolve(); };
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

// ─── Spatial query categories ─────────────────────────────────────────────

export type PlaceCategory =
  | "restaurants"
  | "cafes"
  | "hiking"
  | "parks"
  | "offices"
  | "bookstores"
  | "libraries"
  | "coworking"
  | "museums"
  | "all";

interface CategoryConfig {
  label: string;
  icon: string;
  description: string;
  placeTypes: string[];
  defaultQuery: string;
}

const CATEGORIES: Record<PlaceCategory, CategoryConfig> = {
  restaurants: {
    label: "Restaurants",
    icon: "🍽️",
    description: "Dining & special occasions",
    placeTypes: ["restaurant"],
    defaultQuery: "best restaurant near me",
  },
  cafes: {
    label: "Cafés",
    icon: "☕",
    description: "Coffee, work, and reading",
    placeTypes: ["cafe", "coffee_shop"],
    defaultQuery: "quiet cafe with fast wifi",
  },
  hiking: {
    label: "Hiking",
    icon: "🥾",
    description: "Trails, parks, nature walks",
    placeTypes: ["park", "natural_feature", "hiking_area"],
    defaultQuery: "hiking trails near the city",
  },
  parks: {
    label: "Parks",
    icon: "🌿",
    description: "Outdoor relaxation spots",
    placeTypes: ["park"],
    defaultQuery: "peaceful parks to relax",
  },
  offices: {
    label: "Offices",
    icon: "🏢",
    description: "Business districts, headquarters",
    placeTypes: ["office", "corporate_office"],
    defaultQuery: "company offices and business district",
  },
  bookstores: {
    label: "Bookstores",
    icon: "📚",
    description: "Independent & chain bookshops",
    placeTypes: ["book_store"],
    defaultQuery: "bookstores with reading areas",
  },
  libraries: {
    label: "Libraries",
    icon: "🏛️",
    description: "Public libraries and archives",
    placeTypes: ["library"],
    defaultQuery: "public libraries near me",
  },
  coworking: {
    label: "Coworking",
    icon: "💻",
    description: "Shared workspaces and hotdesks",
    placeTypes: ["coworking_space"],
    defaultQuery: "coworking spaces day pass",
  },
  museums: {
    label: "Museums",
    icon: "🎨",
    description: "Art, science, history",
    placeTypes: ["museum", "art_gallery"],
    defaultQuery: "museums and galleries open today",
  },
  all: {
    label: "All",
    icon: "🔍",
    description: "Search everything",
    placeTypes: [],
    defaultQuery: "",
  },
};

// ─── Pin colours by state ─────────────────────────────────────────────────

const PIN_STYLES: Record<MapPinColor, { background: string; glyph: string }> = {
  primary:     { background: "#4F46E5", glyph: "#FFFFFF" },
  highlighted: { background: "#F59E0B", glyph: "#1F2937" },
  dimmed:      { background: "#9CA3AF", glyph: "#6B7280" },
};

// ─── AI query classifier ──────────────────────────────────────────────────

function classifyQueryCategory(query: string): PlaceCategory {
  const q = query.toLowerCase();
  if (/hik|trail|mountain|nature walk|trekk/.test(q)) return "hiking";
  if (/park|green space|outdoor|garden/.test(q)) return "parks";
  if (/caf[eé]|coffee|espresso|latte/.test(q)) return "cafes";
  if (/book|read|library|librar/.test(q)) return "bookstores";
  if (/office|work|job|company|headquarters|corp/.test(q)) return "offices";
  if (/cowork|hot ?desk|shared workspace/.test(q)) return "coworking";
  if (/museum|gallery|art|exhibit/.test(q)) return "museums";
  if (/librar/.test(q)) return "libraries";
  if (/restaurant|dinner|lunch|eat|food|dine/.test(q)) return "restaurants";
  return "all";
}

// ─── Props ────────────────────────────────────────────────────────────────

export interface VenueMapProps {
  config: GoogleMapsConfig;
  userId: string;
  initialQuery?: string;
  onVenueSelect?: (venue: VenueSignal | null) => void;
}

// ─── Component ────────────────────────────────────────────────────────────

export function VenueMap({
  config,
  userId,
  initialQuery = "",
  onVenueSelect,
}: VenueMapProps) {
  const mapRef = useRef<HTMLDivElement>(null);
  const mapInstanceRef = useRef<google.maps.Map | null>(null);
  const markersRef = useRef<Map<string, google.maps.marker.AdvancedMarkerElement>>(new Map());
  const infoWindowRef = useRef<google.maps.InfoWindow | null>(null);

  const [mapsReady, setMapsReady] = useState(false);
  const [activeCategory, setActiveCategory] = useState<PlaceCategory>("restaurants");
  const [query, setQuery] = useState(initialQuery);
  const [inputValue, setInputValue] = useState(initialQuery);
  const [selectedPlaceDetails, setSelectedPlaceDetails] = useState<GooglePlaceDetails | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [aiSuggestions, setAiSuggestions] = useState<string[]>([]);

  const { state, search, fetchPlaceDetails, selectVenue, cancel } = useVenueSearch(userId);

  // ── Load Google Maps API ────────────────────────────────────────────────

  useEffect(() => {
    loadGoogleMaps(config.apiKey, config.mapId)
      .then(() => setMapsReady(true))
      .catch(console.error);
  }, [config.apiKey, config.mapId]);

  // ── Initialise map instance ─────────────────────────────────────────────

  useEffect(() => {
    if (!mapsReady || !mapRef.current || mapInstanceRef.current) return;

    mapInstanceRef.current = new google.maps.Map(mapRef.current, {
      center: config.defaultCenter,
      zoom: config.defaultZoom,
      mapId: config.mapId,
      disableDefaultUI: false,
      gestureHandling: "greedy",
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: false,
      styles: [
        { featureType: "poi", elementType: "labels", stylers: [{ visibility: "off" }] },
      ],
    });

    infoWindowRef.current = new google.maps.InfoWindow();

    mapInstanceRef.current.addListener("click", () => {
      infoWindowRef.current?.close();
      selectVenue(null);
      setSidebarOpen(false);
      traceMapInteraction({ action: "pan" }).finish();
    });

    mapInstanceRef.current.addListener("zoom_changed", () => {
      const zoom = mapInstanceRef.current?.getZoom();
      traceMapInteraction({ action: "zoom", zoomLevel: zoom }).finish();
    });
  }, [mapsReady, config, selectVenue]);

  // ── Sync markers when venues arrive ────────────────────────────────────

  const enrichedMarkers = useMemo<EnrichedMapMarker[]>(() => {
    return state.mapMarkers.map((m) => ({
      ...m,
      pinColor: m.venue_id === state.selectedVenueId
        ? "highlighted"
        : state.selectedVenueId
          ? "dimmed"
          : "primary",
      isSelected: m.venue_id === state.selectedVenueId,
    }));
  }, [state.mapMarkers, state.selectedVenueId]);

  useEffect(() => {
    if (!mapsReady || !mapInstanceRef.current) return;

    const currentIds = new Set(enrichedMarkers.map((m) => m.venue_id));

    // Remove stale markers
    markersRef.current.forEach((marker, id) => {
      if (!currentIds.has(id)) {
        marker.map = null;
        markersRef.current.delete(id);
      }
    });

    // Add or update markers
    enrichedMarkers.forEach((m) => {
      if (!m.latitude || !m.longitude) return;
      const pos = { lat: m.latitude, lng: m.longitude };
      const style = PIN_STYLES[m.pinColor];

      const existing = markersRef.current.get(m.venue_id);
      if (existing) {
        existing.position = pos;
        const pin = existing.content as HTMLElement;
        if (pin) pin.style.backgroundColor = style.background;
        return;
      }

      // Build a custom pin element
      const pinEl = document.createElement("div");
      pinEl.className = "venue-pin";
      pinEl.style.cssText = `
        width: 36px; height: 36px; border-radius: 50% 50% 50% 0;
        transform: rotate(-45deg); cursor: pointer;
        background: ${style.background}; border: 2px solid #fff;
        box-shadow: 0 2px 6px rgba(0,0,0,0.3);
        display: flex; align-items: center; justify-content: center;
        transition: transform 0.15s, box-shadow 0.15s;
      `;
      const score = document.createElement("span");
      score.style.cssText = `
        transform: rotate(45deg); font-size: 11px; font-weight: 700;
        color: ${style.glyph}; pointer-events: none;
      `;
      score.textContent = String(Math.round(m.match_score));
      pinEl.appendChild(score);

      const marker = new google.maps.marker.AdvancedMarkerElement({
        map: mapInstanceRef.current!,
        position: pos,
        content: pinEl,
        title: m.name,
      });

      marker.addListener("click", async () => {
        const span = traceMapInteraction({ action: "marker_click", venueId: m.venue_id });
        selectVenue(m.venue_id);
        setSidebarOpen(true);
        rumAction("venue_marker_clicked", { venueId: m.venue_id, venueName: m.name });

        // Fetch live Google Place details (TOS: displayed on this map, not stored)
        const detailSpan = tracePlaceDetailsFetch(m.venue_id);
        const details = await fetchPlaceDetails(m.venue_id);
        setSelectedPlaceDetails(details);
        detailSpan.finish();

        // Show info window on map
        if (infoWindowRef.current && mapInstanceRef.current) {
          infoWindowRef.current.setContent(buildInfoWindowContent(m, details));
          infoWindowRef.current.open({ map: mapInstanceRef.current, anchor: marker });
        }

        const venue = state.venues.find((v) => v.venue_id === m.venue_id) ?? null;
        onVenueSelect?.(venue);
        span.finish();
      });

      markersRef.current.set(m.venue_id, marker);
    });
  }, [enrichedMarkers, mapsReady, fetchPlaceDetails, selectVenue, onVenueSelect, state.venues]);

  // ── Pan map to fit all markers when results arrive ──────────────────────

  useEffect(() => {
    if (!mapsReady || !mapInstanceRef.current || enrichedMarkers.length === 0) return;
    const bounds = new google.maps.LatLngBounds();
    let count = 0;
    enrichedMarkers.forEach((m) => {
      if (m.latitude && m.longitude) {
        bounds.extend({ lat: m.latitude, lng: m.longitude });
        count++;
      }
    });
    if (count > 0) {
      mapInstanceRef.current.fitBounds(bounds, 80);
      if (count === 1) mapInstanceRef.current.setZoom(15);
    }
  }, [enrichedMarkers, mapsReady]);

  // ── SSE event tracing ───────────────────────────────────────────────────

  useEffect(() => {
    if (state.status === "searching") {
      traceSSEEvent("search_started").finish();
    }
    if (state.status === "done") {
      traceSSEEvent("search_done", state.totalVenues ?? 0).finish();
    }
  }, [state.status, state.totalVenues]);

  // ── Handlers ───────────────────────────────────────────────────────────

  const handleSearch = useCallback(async (q: string) => {
    if (!q.trim()) return;
    const span = traceSearch({ query: q, userId });
    rumAction("search_submitted", { query: q, category: activeCategory });
    setQuery(q);

    // Classify the query and update the active category
    const detected = classifyQueryCategory(q);
    if (detected !== "all") setActiveCategory(detected);

    traceMapInteraction({ action: "ai_query" }).finish();
    await search(q);
    span.finish();

    // Generate AI follow-up suggestions based on the query
    setAiSuggestions(generateFollowUps(q, detected));
  }, [search, userId, activeCategory]);

  const handleCategorySwitch = useCallback((cat: PlaceCategory) => {
    setActiveCategory(cat);
    traceMapInteraction({ action: "category_switch", category: cat }).finish();
    rumAction("category_switched", { from: activeCategory, to: cat });
    const cfg = CATEGORIES[cat];
    if (cfg.defaultQuery) {
      setInputValue(cfg.defaultQuery);
    }
  }, [activeCategory]);

  const handleSubmit = useCallback((e: React.FormEvent) => {
    e.preventDefault();
    handleSearch(inputValue);
  }, [inputValue, handleSearch]);

  // ─── Render ─────────────────────────────────────────────────────────────

  return (
    <div className="venue-map-root" style={{ position: "relative", width: "100%", height: "100vh", fontFamily: "system-ui, sans-serif" }}>

      {/* ── Map canvas ── */}
      <div ref={mapRef} style={{ width: "100%", height: "100%" }} />

      {/* ── AI search overlay ── */}
      <div style={{
        position: "absolute", top: 16, left: "50%", transform: "translateX(-50%)",
        width: "min(640px, calc(100% - 32px))",
        zIndex: 10,
      }}>
        <form onSubmit={handleSubmit} style={{ display: "flex", gap: 8 }}>
          <div style={{ flex: 1, position: "relative" }}>
            <input
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder={`Ask anything — "cozy reading café near Central Park" or "hiking trails under 2 hours"`}
              style={{
                width: "100%", padding: "14px 48px 14px 16px",
                borderRadius: 12, border: "none",
                boxShadow: "0 4px 24px rgba(0,0,0,0.18)",
                fontSize: 15, background: "#fff",
                outline: "none", boxSizing: "border-box",
              }}
            />
            <span style={{ position: "absolute", right: 14, top: "50%", transform: "translateY(-50%)", fontSize: 20 }}>
              🔍
            </span>
          </div>
          <button
            type="submit"
            disabled={state.status === "searching"}
            style={{
              padding: "0 20px", borderRadius: 12, border: "none",
              background: "#4F46E5", color: "#fff", fontWeight: 600,
              fontSize: 14, cursor: "pointer", whiteSpace: "nowrap",
              boxShadow: "0 4px 12px rgba(79,70,229,0.35)",
            }}
          >
            {state.status === "searching" ? "Searching…" : "Search"}
          </button>
          {state.status === "searching" && (
            <button
              type="button"
              onClick={cancel}
              style={{
                padding: "0 16px", borderRadius: 12, border: "none",
                background: "#EF4444", color: "#fff", cursor: "pointer",
              }}
            >
              ✕
            </button>
          )}
        </form>

        {/* Status message */}
        {state.statusMessage && state.status === "searching" && (
          <div style={{
            marginTop: 8, padding: "8px 14px", background: "rgba(255,255,255,0.95)",
            borderRadius: 8, fontSize: 13, color: "#6B7280",
            boxShadow: "0 2px 8px rgba(0,0,0,0.1)",
          }}>
            <span style={{ animation: "pulse 1.5s infinite" }}>⟳</span> {state.statusMessage}
          </div>
        )}

        {/* AI follow-up suggestions */}
        {aiSuggestions.length > 0 && state.status === "done" && (
          <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {aiSuggestions.map((s) => (
              <button
                key={s}
                onClick={() => { setInputValue(s); handleSearch(s); }}
                style={{
                  padding: "6px 12px", borderRadius: 20, border: "1px solid #E5E7EB",
                  background: "rgba(255,255,255,0.95)", fontSize: 12, cursor: "pointer",
                  color: "#374151", boxShadow: "0 1px 4px rgba(0,0,0,0.08)",
                }}
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* ── Category pills ── */}
      <div style={{
        position: "absolute", bottom: sidebarOpen ? "calc(40% + 16px)" : 16,
        left: "50%", transform: "translateX(-50%)",
        display: "flex", gap: 8, overflowX: "auto", maxWidth: "calc(100% - 32px)",
        paddingBottom: 4, zIndex: 10,
        transition: "bottom 0.3s ease",
      }}>
        {(Object.entries(CATEGORIES) as [PlaceCategory, CategoryConfig][]).map(([key, cfg]) => (
          <button
            key={key}
            onClick={() => handleCategorySwitch(key)}
            title={cfg.description}
            style={{
              padding: "8px 14px", borderRadius: 24, border: "2px solid",
              borderColor: activeCategory === key ? "#4F46E5" : "transparent",
              background: activeCategory === key ? "#4F46E5" : "rgba(255,255,255,0.95)",
              color: activeCategory === key ? "#fff" : "#374151",
              fontWeight: activeCategory === key ? 600 : 400,
              fontSize: 13, cursor: "pointer", whiteSpace: "nowrap",
              boxShadow: "0 2px 8px rgba(0,0,0,0.1)",
              transition: "all 0.15s ease",
            }}
          >
            {cfg.icon} {cfg.label}
          </button>
        ))}
      </div>

      {/* ── Result count badge ── */}
      {state.status === "done" && state.venues.length > 0 && (
        <div style={{
          position: "absolute", top: 80, left: "50%", transform: "translateX(-50%)",
          padding: "6px 16px", borderRadius: 20,
          background: "rgba(79,70,229,0.9)", color: "#fff",
          fontSize: 13, fontWeight: 600, zIndex: 10,
          boxShadow: "0 2px 8px rgba(79,70,229,0.4)",
        }}>
          {state.venues.length} {CATEGORIES[activeCategory].label.toLowerCase()} found
        </div>
      )}

      {/* ── Venue detail sidebar ── */}
      {sidebarOpen && state.selectedVenueId && (
        <VenueDetailSidebar
          venue={state.venues.find((v) => v.venue_id === state.selectedVenueId) ?? null}
          placeDetails={selectedPlaceDetails}
          onClose={() => {
            setSidebarOpen(false);
            selectVenue(null);
            infoWindowRef.current?.close();
            onVenueSelect?.(null);
          }}
        />
      )}

      {/* ── Loading skeleton ── */}
      {!mapsReady && (
        <div style={{
          position: "absolute", inset: 0, background: "#F3F4F6",
          display: "flex", alignItems: "center", justifyContent: "center",
          zIndex: 20,
        }}>
          <div style={{ textAlign: "center", color: "#6B7280" }}>
            <div style={{ fontSize: 32, marginBottom: 12 }}>🗺️</div>
            <div>Loading intelligent map…</div>
          </div>
        </div>
      )}

      {/* ── Error banner ── */}
      {state.status === "error" && state.error && (
        <div style={{
          position: "absolute", top: 80, left: 16, right: 16,
          padding: "12px 16px", background: "#FEF2F2",
          border: "1px solid #FCA5A5", borderRadius: 10,
          color: "#B91C1C", fontSize: 14, zIndex: 10,
        }}>
          {state.error}
        </div>
      )}
    </div>
  );
}

// ─── Info window HTML (displayed ON the map per Google TOS) ───────────────

function buildInfoWindowContent(
  marker: EnrichedMapMarker,
  details: GooglePlaceDetails | null,
): string {
  const rating = details?.rating ? `⭐ ${details.rating} (${details.user_rating_count?.toLocaleString()})` : "";
  const open = details?.is_open_now === true ? "🟢 Open now" : details?.is_open_now === false ? "🔴 Closed" : "";
  const price = marker.price_per_head ? `~$${marker.price_per_head}/head` : "";
  const room = marker.has_private_room ? "🚪 Private room" : "";

  return `
    <div style="font-family:system-ui;max-width:240px;padding:4px 0">
      <div style="font-weight:700;font-size:15px;color:#111827;margin-bottom:4px">
        ${escapeHtml(marker.name)}
      </div>
      <div style="font-size:12px;color:#6B7280;margin-bottom:8px">
        ${[rating, open].filter(Boolean).join(" · ")}
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${[price, room].filter(Boolean).map((t) =>
          `<span style="background:#F3F4F6;padding:2px 8px;border-radius:12px;font-size:11px;color:#374151">${t}</span>`
        ).join("")}
        <span style="background:#EEF2FF;padding:2px 8px;border-radius:12px;font-size:11px;color:#4F46E5;font-weight:600">
          ${Math.round(marker.match_score)}% match
        </span>
      </div>
    </div>
  `;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ─── Venue detail sidebar ─────────────────────────────────────────────────

interface VenueDetailSidebarProps {
  venue: VenueSignal | null;
  placeDetails: GooglePlaceDetails | null;
  onClose: () => void;
}

function VenueDetailSidebar({ venue, placeDetails, onClose }: VenueDetailSidebarProps) {
  if (!venue) return null;

  const intel = venue.intelligence;
  const openLabel = placeDetails?.is_open_now === true
    ? { label: "Open now", color: "#10B981" }
    : placeDetails?.is_open_now === false
      ? { label: "Closed", color: "#EF4444" }
      : null;

  return (
    <div style={{
      position: "absolute", bottom: 0, left: 0, right: 0,
      height: "40%", background: "#fff",
      borderRadius: "20px 20px 0 0",
      boxShadow: "0 -4px 24px rgba(0,0,0,0.15)",
      overflowY: "auto", zIndex: 10,
      padding: "20px 20px 32px",
    }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: "#111827" }}>
            {venue.name}
          </h2>
          <div style={{ fontSize: 13, color: "#6B7280", marginTop: 2 }}>
            {venue.neighborhood && `${venue.neighborhood} · `}
            {venue.cuisine}
          </div>
        </div>
        <button onClick={onClose} style={{ background: "none", border: "none", fontSize: 20, cursor: "pointer", color: "#9CA3AF", padding: 4 }}>✕</button>
      </div>

      {/* Score + status pills */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
        <Pill color="#4F46E5" label={`${Math.round(venue.match_score)}% match`} />
        {openLabel && <Pill color={openLabel.color} label={openLabel.label} />}
        {venue.has_private_room && <Pill color="#059669" label="Private room" />}
        {venue.price_per_head > 0 && <Pill color="#374151" label={`~$${venue.price_per_head}/head`} />}
        {placeDetails?.rating && (
          <Pill color="#F59E0B" label={`⭐ ${placeDetails.rating} (${placeDetails.user_rating_count?.toLocaleString()})`} />
        )}
      </div>

      {/* Why card */}
      {intel?.why_card && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "#9CA3AF", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 4 }}>Why this spot</div>
          <p style={{ margin: 0, fontSize: 14, color: "#374151", lineHeight: 1.5 }}>{intel.why_card}</p>
        </div>
      )}

      {/* Scenario */}
      {intel?.scenario && (
        <div style={{ marginBottom: 14, background: "#F9FAFB", borderRadius: 10, padding: "10px 14px" }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "#9CA3AF", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 4 }}>Your evening</div>
          <p style={{ margin: 0, fontSize: 13, color: "#4B5563", lineHeight: 1.5, fontStyle: "italic" }}>{intel.scenario}</p>
        </div>
      )}

      {/* Sensitivity bars */}
      {intel?.sensitivity_bars && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "#9CA3AF", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>Dimensions</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 16px" }}>
            {Object.entries(intel.sensitivity_bars).map(([dim, score]) => (
              <SensitivityBar key={dim} label={dim.replace("_", " ")} value={score} />
            ))}
          </div>
        </div>
      )}

      {/* Live signal */}
      {intel?.live_signal && (
        <div style={{
          marginBottom: 14, padding: "8px 12px",
          background: "#FFFBEB", border: "1px solid #FDE68A",
          borderRadius: 8, fontSize: 13, color: "#92400E",
        }}>
          ⚡ {intel.live_signal}
        </div>
      )}

      {/* Key quotes */}
      {venue.key_quotes.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "#9CA3AF", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>What people say</div>
          {venue.key_quotes.slice(0, 3).map((q, i) => (
            <div key={i} style={{ fontSize: 13, color: "#4B5563", marginBottom: 4, paddingLeft: 10, borderLeft: "2px solid #E5E7EB" }}>
              "{q}"
            </div>
          ))}
        </div>
      )}

      {/* Contact links (displayed ON the map — TOS compliant) */}
      {(placeDetails?.website_uri || placeDetails?.phone_number) && (
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          {placeDetails.website_uri && (
            <a href={placeDetails.website_uri} target="_blank" rel="noopener noreferrer"
              style={{ fontSize: 13, color: "#4F46E5", textDecoration: "none" }}>
              🌐 Website
            </a>
          )}
          {placeDetails.phone_number && (
            <a href={`tel:${placeDetails.phone_number}`}
              style={{ fontSize: 13, color: "#4F46E5", textDecoration: "none" }}>
              📞 {placeDetails.phone_number}
            </a>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Micro UI components ──────────────────────────────────────────────────

function Pill({ label, color }: { label: string; color: string }) {
  return (
    <span style={{
      padding: "4px 10px", borderRadius: 20,
      background: `${color}18`,
      color, border: `1px solid ${color}40`,
      fontSize: 12, fontWeight: 600,
    }}>
      {label}
    </span>
  );
}

function SensitivityBar({ label, value }: { label: string; value: number }) {
  const clamped = Math.max(0, Math.min(100, value));
  const hue = Math.round((clamped / 100) * 120); // red=0, green=120
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
        <span style={{ fontSize: 11, color: "#6B7280", textTransform: "capitalize" }}>{label}</span>
        <span style={{ fontSize: 11, fontWeight: 600, color: "#374151" }}>{clamped}</span>
      </div>
      <div style={{ height: 4, background: "#F3F4F6", borderRadius: 2, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${clamped}%`,
          background: `hsl(${hue},70%,45%)`,
          borderRadius: 2,
          transition: "width 0.4s ease",
        }} />
      </div>
    </div>
  );
}

// ─── Follow-up suggestion generator ──────────────────────────────────────

function generateFollowUps(query: string, category: PlaceCategory): string[] {
  const base: Record<PlaceCategory, string[]> = {
    restaurants: [
      "Which have private dining rooms?",
      "Best for groups over 10?",
      "Quietest atmosphere for conversation?",
      "Best birthday dinner options?",
    ],
    cafes: [
      "Which have the fastest WiFi?",
      "Best for all-day working?",
      "Dog-friendly cafés nearby?",
      "Most Instagram-worthy spots?",
    ],
    hiking: [
      "Trails under 2 hours?",
      "Best sunrise hikes?",
      "Dog-friendly trails?",
      "Easiest trails for beginners?",
    ],
    parks: [
      "Best for picnics?",
      "Parks with playgrounds?",
      "Quietest parks to read?",
      "Parks open late?",
    ],
    offices: [
      "Most iconic corporate campuses?",
      "Tech company headquarters?",
      "Offices with public tours?",
      "Financial district landmarks?",
    ],
    bookstores: [
      "Which have reading cafés?",
      "Best for rare books?",
      "Independent bookshops only?",
      "Longest opening hours?",
    ],
    libraries: [
      "Libraries with study rooms?",
      "Best children's sections?",
      "Most architecturally stunning?",
      "Libraries open on weekends?",
    ],
    coworking: [
      "Which offer day passes?",
      "Best phone booths?",
      "24-hour coworking spaces?",
      "Best networking events?",
    ],
    museums: [
      "Free museums nearby?",
      "Best for kids?",
      "Current must-see exhibitions?",
      "Museums open late?",
    ],
    all: [
      "Nearby coffee shops?",
      "What's open right now?",
      "Best-rated places?",
      "Hidden gems in the area?",
    ],
  };
  return (base[category] ?? base.all).slice(0, 4);
}
