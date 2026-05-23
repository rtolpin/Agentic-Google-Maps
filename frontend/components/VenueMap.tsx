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

import { Loader } from "@googlemaps/js-api-loader";
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
    googleMapsLoaded: boolean;
  }
}

// The Loader implements Google's Bootstrap Loader pattern and deduplicates
// concurrent calls automatically — no manual script injection needed.
let _loader: Loader | null = null;

function getLoader(apiKey: string): Loader {
  if (!_loader) {
    _loader = new Loader({ apiKey, version: "weekly" });
  }
  return _loader;
}

async function loadGoogleMaps(
  apiKey: string,
  _mapId: string,
  onStep?: (step: number) => void,
): Promise<void> {
  if (window.googleMapsLoaded) return;
  onStep?.(1); // 12%
  const loader = getLoader(apiKey);
  await loader.importLibrary("maps");
  onStep?.(3); // 75%
  await loader.importLibrary("marker");
  onStep?.(4); // 95%
  window.googleMapsLoaded = true;
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
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadStep, setLoadStep] = useState(0);
  const [activeCategory, setActiveCategory] = useState<PlaceCategory>("restaurants");
  const [query, setQuery] = useState(initialQuery);
  const [inputValue, setInputValue] = useState(initialQuery);
  const [selectedPlaceDetails, setSelectedPlaceDetails] = useState<GooglePlaceDetails | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [aiSuggestions, setAiSuggestions] = useState<string[]>([]);

  const { state, search, fetchPlaceDetails, selectVenue, cancel } = useVenueSearch(userId);

  // ── Load Google Maps API ────────────────────────────────────────────────

  useEffect(() => {
    if (!config.apiKey) {
      setLoadError("Missing NEXT_PUBLIC_GOOGLE_MAPS_KEY — add it to frontend/.env.local and restart the dev server.");
      return;
    }
    if (window.googleMapsLoaded) { setMapsReady(true); return; }

    const timeout = setTimeout(() => {
      setLoadError(
        "Map timed out after 8 s. In Google Cloud Console: enable Maps JavaScript API, add http://localhost:3000/* to the key's HTTP referrer allowlist, and ensure billing is active.",
      );
    }, 8000);

    loadGoogleMaps(config.apiKey, config.mapId, setLoadStep)
      .then(() => { clearTimeout(timeout); setMapsReady(true); })
      .catch((err) => {
        clearTimeout(timeout);
        setLoadError(`Map failed to load: ${err instanceof Error ? err.message : "check API key and billing in Google Cloud Console."}`);
      });

    return () => clearTimeout(timeout);
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

  // ── Derived agent step statuses ────────────────────────────────────────
  const agentSteps = useMemo(() => [
    {
      id: "intent", name: "Intent Agent", icon: "🧠",
      desc: state.intent
        ? `${state.intent.occasion} · ${state.intent.city}${state.intent.group_size > 1 ? ` · ${state.intent.group_size} people` : ""}`
        : "Understanding your request…",
      done: !!state.intent,
    },
    {
      id: "scraper", name: "Scraper Agent", icon: "🔍",
      desc: state.venues.length > 0 ? `Found ${state.venues.length} candidates` : "Searching Nimble SERP + maps…",
      done: state.venues.length > 0,
    },
    {
      id: "validator", name: "Validator Agent", icon: "✅",
      desc: state.status === "done" ? "Quality checks passed" : "Checking relevance & quality…",
      done: state.status === "done",
    },
    {
      id: "scorer", name: "Score Engine", icon: "⚡",
      desc: state.status === "done" ? `Ranked ${state.venues.length} venues` : "Scoring & personalising results…",
      done: state.status === "done",
    },
    {
      id: "global", name: "Global Intel Agent", icon: "🌍",
      desc: state.globalIntel ? `${Object.keys(state.globalIntel).length} city benchmarks loaded` : "Loading city benchmarks…",
      done: !!state.globalIntel,
    },
  ], [state.intent, state.venues.length, state.status, state.globalIntel]);

  const showLeftPanel = state.status === "searching" || state.status === "done" || state.status === "error";
  const leftPanelW = 320;

  // ─── Render ─────────────────────────────────────────────────────────────

  return (
    <div className="venue-map-root" style={{ position: "relative", width: "100%", height: "100vh", fontFamily: "system-ui, -apple-system, sans-serif" }}>
      <style>{`
        @keyframes dotBounce {
          0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
          40%            { transform: scale(1);   opacity: 1;   }
        }
        @keyframes chipIn {
          from { opacity: 0; transform: scale(0.78) translateY(10px); }
          to   { opacity: 1; transform: scale(1)    translateY(0);    }
        }
      `}</style>

      {/* ── Map canvas (shifts right when panel is open) ── */}
      <div ref={mapRef} style={{
        position: "absolute", top: 0, bottom: 0,
        left: showLeftPanel ? leftPanelW : 0,
        right: 0,
        transition: "left 0.3s ease",
      }} />

      {/* ════════════════════════════════════════════════════════
          LEFT PANEL — Agent Activity (searching) / Results (done)
          ════════════════════════════════════════════════════════ */}
      {showLeftPanel && (
        <div style={{
          position: "absolute", top: 0, left: 0, bottom: 0,
          width: leftPanelW,
          background: "linear-gradient(180deg, #0f172a 0%, #1a2236 100%)",
          boxShadow: "4px 0 24px rgba(0,0,0,0.35)",
          display: "flex", flexDirection: "column",
          zIndex: 15, overflow: "hidden",
        }}>
          {/* Panel header */}
          <div style={{
            padding: "20px 20px 14px",
            borderBottom: "1px solid rgba(255,255,255,0.07)",
            flexShrink: 0,
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <div style={{
                width: 32, height: 32, borderRadius: 8,
                background: "linear-gradient(135deg, #3B82F6, #8B5CF6)",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 16,
              }}>✨</div>
              <div>
                <div style={{ color: "#F1F5F9", fontWeight: 700, fontSize: 15 }}>The Right Spot AI</div>
                <div style={{ color: "#64748B", fontSize: 11 }}>
                  {state.status === "searching" ? "Agents working…" : state.status === "done" ? `${state.venues.length} venues found` : "Search error"}
                </div>
              </div>
              {state.status === "searching" && (
                <div style={{ marginLeft: "auto", display: "flex", gap: 3 }}>
                  {[0,1,2].map(i => (
                    <div key={i} style={{
                      width: 6, height: 6, borderRadius: "50%", background: "#3B82F6",
                      animation: `dotBounce 1.2s ease-in-out ${i * 0.2}s infinite`,
                    }} />
                  ))}
                </div>
              )}
            </div>
            {query && (
              <div style={{
                marginTop: 10, padding: "8px 12px",
                background: "rgba(255,255,255,0.05)", borderRadius: 8,
                fontSize: 12, color: "#94A3B8", fontStyle: "italic",
                border: "1px solid rgba(255,255,255,0.07)",
              }}>
                "{query}"
              </div>
            )}
          </div>

          {/* Agent steps */}
          <div style={{ padding: "14px 20px 10px", flexShrink: 0 }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 10 }}>
              AI Agents
            </div>
            {agentSteps.map((step, i) => {
              const isActive = state.status === "searching" && !step.done && (i === 0 || agentSteps[i-1]?.done);
              return (
                <div key={step.id} style={{
                  display: "flex", alignItems: "flex-start", gap: 10,
                  marginBottom: 10, opacity: step.done || isActive ? 1 : 0.4,
                  transition: "opacity 0.3s",
                }}>
                  {/* Status dot */}
                  <div style={{
                    width: 22, height: 22, borderRadius: "50%", flexShrink: 0,
                    marginTop: 1,
                    background: step.done ? "rgba(16,185,129,0.15)" : isActive ? "rgba(59,130,246,0.15)" : "rgba(255,255,255,0.05)",
                    border: `1.5px solid ${step.done ? "#10B981" : isActive ? "#3B82F6" : "rgba(255,255,255,0.1)"}`,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    fontSize: 11,
                    animation: isActive ? "agentPulse 1.5s ease-in-out infinite" : "none",
                  }}>
                    {step.done ? "✓" : step.icon}
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: step.done ? "#10B981" : isActive ? "#60A5FA" : "#64748B" }}>
                      {step.name}
                    </div>
                    <div style={{ fontSize: 11, color: "#475569", marginTop: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {step.desc}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Status message bar */}
          {state.statusMessage && state.status === "searching" && (
            <div style={{
              margin: "0 20px 12px",
              padding: "8px 12px",
              background: "rgba(59,130,246,0.1)", borderRadius: 8,
              fontSize: 11, color: "#60A5FA",
              border: "1px solid rgba(59,130,246,0.2)",
              flexShrink: 0,
            }}>
              ⟳ {state.statusMessage}
            </div>
          )}

          {/* ── Results list (status === done) ── */}
          {state.status === "done" && state.venues.length > 0 && (
            <div style={{ flex: 1, overflowY: "auto", padding: "0 12px 20px" }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", padding: "4px 8px 10px" }}>
                Top Matches
              </div>
              {state.venues.map((venue, idx) => (
                <div
                  key={venue.venue_id}
                  onClick={() => {
                    selectVenue(venue.venue_id);
                    setSidebarOpen(true);
                    fetchPlaceDetails(venue.venue_id).then(setSelectedPlaceDetails);
                    onVenueSelect?.(venue);
                  }}
                  style={{
                    padding: "12px", borderRadius: 12, marginBottom: 8, cursor: "pointer",
                    background: state.selectedVenueId === venue.venue_id
                      ? "linear-gradient(135deg, rgba(59,130,246,0.2), rgba(139,92,246,0.15))"
                      : "rgba(255,255,255,0.04)",
                    border: `1px solid ${state.selectedVenueId === venue.venue_id ? "rgba(59,130,246,0.5)" : "rgba(255,255,255,0.07)"}`,
                    transition: "all 0.15s ease",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                        <span style={{
                          fontSize: 10, fontWeight: 700, color: "#64748B",
                          background: "rgba(255,255,255,0.06)", borderRadius: 4,
                          padding: "1px 5px",
                        }}>#{idx + 1}</span>
                        <span style={{ fontSize: 13, fontWeight: 700, color: "#F1F5F9", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                          {venue.name}
                        </span>
                      </div>
                      <div style={{ fontSize: 11, color: "#64748B" }}>
                        {[venue.neighborhood, venue.cuisine].filter(Boolean).join(" · ")}
                      </div>
                    </div>
                    {/* Match score badge */}
                    <div style={{
                      flexShrink: 0, width: 42, height: 42, borderRadius: 10,
                      background: `conic-gradient(#3B82F6 ${venue.match_score * 3.6}deg, rgba(255,255,255,0.08) 0deg)`,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      position: "relative",
                    }}>
                      <div style={{
                        width: 34, height: 34, borderRadius: 8,
                        background: "#1a2236",
                        display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
                      }}>
                        <span style={{ fontSize: 11, fontWeight: 800, color: "#60A5FA", lineHeight: 1 }}>
                          {Math.round(venue.match_score)}
                        </span>
                        <span style={{ fontSize: 8, color: "#475569" }}>%</span>
                      </div>
                    </div>
                  </div>
                  {/* Venue tags */}
                  <div style={{ display: "flex", gap: 4, marginTop: 8, flexWrap: "wrap" }}>
                    {venue.has_private_room && (
                      <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(16,185,129,0.12)", color: "#34D399", border: "1px solid rgba(16,185,129,0.2)" }}>
                        🚪 Private room
                      </span>
                    )}
                    {venue.price_per_head > 0 && (
                      <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(255,255,255,0.06)", color: "#94A3B8", border: "1px solid rgba(255,255,255,0.08)" }}>
                        ~${venue.price_per_head}/head
                      </span>
                    )}
                    {venue.intelligence?.why_card && (
                      <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(139,92,246,0.12)", color: "#A78BFA", border: "1px solid rgba(139,92,246,0.2)" }}>
                        ✨ AI analysed
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* ── AI follow-up suggestions ── */}
          {aiSuggestions.length > 0 && state.status === "done" && (
            <div style={{ padding: "0 12px 16px", flexShrink: 0, borderTop: "1px solid rgba(255,255,255,0.07)" }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", padding: "10px 8px 8px" }}>
                Refine with AI
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                {aiSuggestions.slice(0, 3).map((s) => (
                  <button
                    key={s}
                    onClick={() => { setInputValue(s); handleSearch(s); }}
                    style={{
                      padding: "8px 12px", borderRadius: 8, border: "1px solid rgba(59,130,246,0.25)",
                      background: "rgba(59,130,246,0.08)", fontSize: 12, cursor: "pointer",
                      color: "#93C5FD", textAlign: "left", transition: "all 0.15s",
                    }}
                  >
                    ↩ {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Cancel button */}
          {state.status === "searching" && (
            <div style={{ padding: "0 12px 16px", flexShrink: 0 }}>
              <button onClick={cancel} style={{
                width: "100%", padding: "9px", borderRadius: 8,
                background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.25)",
                color: "#F87171", fontSize: 13, cursor: "pointer", fontWeight: 600,
              }}>
                ✕ Cancel search
              </button>
            </div>
          )}

          <style>{`
            @keyframes dotBounce {
              0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
              40%            { transform: scale(1);   opacity: 1;   }
            }
            @keyframes agentPulse {
              0%, 100% { box-shadow: 0 0 0 0 rgba(59,130,246,0.4); }
              50%       { box-shadow: 0 0 0 5px rgba(59,130,246,0); }
            }
            @keyframes chipIn {
              from { opacity: 0; transform: scale(0.78) translateY(10px); }
              to   { opacity: 1; transform: scale(1)    translateY(0);    }
            }
          `}</style>
        </div>
      )}

      {/* ════════════════════════════════════════════════════════
          HEADER — Full-width AI search + intent chips
          ════════════════════════════════════════════════════════ */}
      <div style={{
        position: "absolute",
        top: 0,
        left: showLeftPanel ? leftPanelW : 0,
        right: 0,
        zIndex: 10,
        transition: "left 0.3s ease",
        background: "rgba(7,11,24,0.96)",
        backdropFilter: "blur(22px)",
        WebkitBackdropFilter: "blur(22px)",
        borderBottom: "1px solid rgba(255,255,255,0.07)",
        boxShadow: "0 6px 32px rgba(0,0,0,0.5)",
        padding: state.intent ? "14px 20px 18px" : "14px 20px 14px",
      }}>

        {/* Row 1 — logo + agent status */}
        <div style={{ display: "flex", alignItems: "center", marginBottom: 12 }}>
          <div style={{
            display: "inline-flex", alignItems: "center", gap: 7,
            padding: "5px 14px", borderRadius: 24,
            background: "linear-gradient(135deg, rgba(37,99,235,0.2), rgba(124,58,237,0.2))",
            border: "1px solid rgba(99,179,237,0.2)",
            fontSize: 12, fontWeight: 800, color: "#93C5FD",
            letterSpacing: "0.07em",
          }}>
            ✦ THE RIGHT SPOT
          </div>

          <div style={{
            marginLeft: "auto",
            display: "inline-flex", alignItems: "center", gap: 6,
            padding: "5px 12px", borderRadius: 20,
            background: state.status === "searching"
              ? "rgba(59,130,246,0.12)"
              : "rgba(255,255,255,0.04)",
            border: `1px solid ${state.status === "searching" ? "rgba(59,130,246,0.3)" : "rgba(255,255,255,0.07)"}`,
            fontSize: 11, fontWeight: 600,
            color: state.status === "searching" ? "#60A5FA" : "#475569",
            transition: "all 0.3s",
          }}>
            {state.status === "searching" ? (
              <>
                {[0, 1, 2].map(i => (
                  <span key={i} style={{
                    display: "inline-block", width: 4, height: 4, borderRadius: "50%",
                    background: "#3B82F6",
                    animation: `dotBounce 1.2s ease-in-out ${i * 0.15}s infinite`,
                  }} />
                ))}
                <span style={{ marginLeft: 4 }}>5 agents running</span>
              </>
            ) : (
              <span>5 AI agents</span>
            )}
          </div>
        </div>

        {/* Row 2 — search form */}
        <form onSubmit={handleSubmit} style={{ display: "flex", gap: 10 }}>
          <div style={{
            flex: 1, display: "flex", alignItems: "center",
            background: "rgba(255,255,255,0.06)",
            borderRadius: 14,
            border: "1.5px solid rgba(255,255,255,0.11)",
            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
          }}>
            <span style={{ padding: "0 14px", fontSize: 17, flexShrink: 0, opacity: 0.55 }}>🔍</span>
            <input
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder='Try "birthday dinner for 8, quiet Italian NYC" or "cosy café to work from"'
              style={{
                flex: 1, padding: "14px 0",
                border: "none", background: "transparent",
                fontSize: 14, color: "#E2E8F0", outline: "none",
                caretColor: "#3B82F6",
              }}
            />
            {inputValue && (
              <button
                type="button"
                onClick={() => setInputValue("")}
                style={{
                  background: "none", border: "none", color: "#475569",
                  cursor: "pointer", padding: "0 12px", fontSize: 18, lineHeight: 1,
                }}
              >×</button>
            )}
          </div>
          <button
            type="submit"
            disabled={state.status === "searching"}
            style={{
              padding: "0 26px", borderRadius: 14, border: "none",
              background: state.status === "searching"
                ? "rgba(59,130,246,0.22)"
                : "linear-gradient(135deg, #2563EB 0%, #7C3AED 100%)",
              color: state.status === "searching" ? "#60A5FA" : "#fff",
              fontWeight: 700, fontSize: 14,
              cursor: state.status === "searching" ? "not-allowed" : "pointer",
              whiteSpace: "nowrap",
              boxShadow: state.status === "searching"
                ? "none"
                : "0 4px 18px rgba(37,99,235,0.5), inset 0 1px 0 rgba(255,255,255,0.15)",
              transition: "all 0.2s",
              letterSpacing: "0.02em",
            }}
          >
            {state.status === "searching" ? "Searching…" : "Ask AI →"}
          </button>
        </form>

        {/* Row 3 — intent chips: BIG, colorful, animated */}
        {state.intent && (() => {
          const chips = [
            { val: state.intent!.city,                icon: "📍", label: state.intent!.city,                        color: "#60A5FA", bg: "rgba(59,130,246,0.13)",  border: "rgba(59,130,246,0.38)"  },
            { val: state.intent!.occasion,            icon: "🎉", label: state.intent!.occasion?.replace(/_/g, " "), color: "#A78BFA", bg: "rgba(139,92,246,0.13)", border: "rgba(139,92,246,0.38)" },
            { val: state.intent!.cuisine,             icon: "🍽️", label: state.intent!.cuisine,                    color: "#FBBF24", bg: "rgba(245,158,11,0.13)", border: "rgba(245,158,11,0.38)" },
            { val: state.intent!.group_size > 1,      icon: "👥", label: `${state.intent!.group_size} people`,     color: "#34D399", bg: "rgba(16,185,129,0.13)", border: "rgba(16,185,129,0.38)" },
            { val: state.intent!.needs_private_room,  icon: "🚪", label: "private room",                           color: "#22D3EE", bg: "rgba(6,182,212,0.13)",  border: "rgba(6,182,212,0.38)"  },
            { val: state.intent!.noise_preference,    icon: "🔊", label: state.intent!.noise_preference,           color: "#F472B6", bg: "rgba(236,72,153,0.13)", border: "rgba(236,72,153,0.38)" },
            { val: state.intent!.price_band,          icon: "💎", label: state.intent!.price_band,                 color: "#A3E635", bg: "rgba(132,204,22,0.13)", border: "rgba(132,204,22,0.38)" },
          ].filter(c => c.val);
          return chips.length === 0 ? null : (
            <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              {chips.map((chip, idx) => (
                <div
                  key={String(chip.label)}
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 7,
                    padding: "8px 16px", borderRadius: 100,
                    background: chip.bg,
                    border: `1.5px solid ${chip.border}`,
                    color: chip.color,
                    fontSize: 13, fontWeight: 600,
                    backdropFilter: "blur(8px)",
                    whiteSpace: "nowrap",
                    letterSpacing: "0.01em",
                    animation: `chipIn 0.4s cubic-bezier(0.34,1.56,0.64,1) ${idx * 0.055}s both`,
                    userSelect: "none",
                  }}
                >
                  <span style={{ fontSize: 15 }}>{chip.icon}</span>
                  <span style={{ textTransform: "capitalize" }}>{chip.label}</span>
                </div>
              ))}
            </div>
          );
        })()}
      </div>

      {/* ── Category pills ── */}
      <div style={{
        position: "absolute",
        bottom: sidebarOpen ? "calc(40% + 16px)" : 18,
        left: showLeftPanel ? leftPanelW + 16 : 16,
        right: 16,
        display: "flex", gap: 8, overflowX: "auto",
        zIndex: 10,
        transition: "all 0.3s ease",
        paddingBottom: 2,
        /* hide scrollbar across browsers */
        msOverflowStyle: "none" as React.CSSProperties["msOverflowStyle"],
        scrollbarWidth: "none" as React.CSSProperties["scrollbarWidth"],
      }}>
        {(Object.entries(CATEGORIES) as [PlaceCategory, CategoryConfig][]).map(([key, cfg]) => (
          <button
            key={key}
            onClick={() => handleCategorySwitch(key)}
            title={cfg.description}
            style={{
              padding: "9px 16px", borderRadius: 100,
              border: `1.5px solid ${activeCategory === key ? "rgba(59,130,246,0.65)" : "rgba(255,255,255,0.11)"}`,
              background: activeCategory === key
                ? "linear-gradient(135deg, #1D4ED8 0%, #6D28D9 100%)"
                : "rgba(7,11,24,0.84)",
              color: activeCategory === key ? "#fff" : "#94A3B8",
              fontWeight: activeCategory === key ? 700 : 500,
              fontSize: 13, cursor: "pointer", whiteSpace: "nowrap",
              flexShrink: 0,
              boxShadow: activeCategory === key
                ? "0 4px 16px rgba(37,99,235,0.45), inset 0 1px 0 rgba(255,255,255,0.12)"
                : "0 2px 8px rgba(0,0,0,0.3)",
              backdropFilter: "blur(14px)",
              WebkitBackdropFilter: "blur(14px)",
              transition: "all 0.18s ease",
              letterSpacing: "0.01em",
            }}
          >
            {cfg.icon} {cfg.label}
          </button>
        ))}
      </div>

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

      {/* ── Loading / error overlay ── */}
      {(!mapsReady || loadError) && (
        <div style={{
          position: "absolute", inset: 0, zIndex: 20,
          background: "linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%)",
          display: "flex", flexDirection: "column",
          alignItems: "center", justifyContent: "center",
          overflow: "hidden",
        }}>
          {/* Animated grid background */}
          <div style={{
            position: "absolute", inset: 0, opacity: 0.08,
            backgroundImage: "linear-gradient(rgba(99,179,237,0.4) 1px, transparent 1px), linear-gradient(90deg, rgba(99,179,237,0.4) 1px, transparent 1px)",
            backgroundSize: "48px 48px",
            animation: "gridScroll 8s linear infinite",
          }} />

          {loadError ? (
            /* ── Error state ── */
            <div style={{
              position: "relative", textAlign: "center", maxWidth: 420, padding: "0 24px",
            }}>
              <div style={{ fontSize: 40, marginBottom: 16 }}>⚠️</div>
              <div style={{ color: "#F1F5F9", fontSize: 18, fontWeight: 600, marginBottom: 12 }}>
                Map failed to load
              </div>
              <div style={{
                color: "#94A3B8", fontSize: 13, lineHeight: 1.6, marginBottom: 24,
                background: "rgba(255,255,255,0.05)", borderRadius: 10,
                padding: "12px 16px", border: "1px solid rgba(255,255,255,0.1)",
              }}>
                {loadError}
              </div>
              <button
                onClick={() => { setLoadError(null); setLoadStep(0); setMapsReady(false); window.googleMapsLoaded = false; }}
                style={{
                  background: "#3B82F6", color: "#fff", border: "none",
                  borderRadius: 8, padding: "10px 24px", fontSize: 14,
                  fontWeight: 600, cursor: "pointer",
                }}
              >
                Retry
              </button>
            </div>
          ) : (
            /* ── Loading state ── */
            (() => {
              const PCT   = [0, 12, 45, 75, 95][loadStep] ?? 0;
              const STEPS = ["Starting up…", "Connecting to Google Maps…", "Loading map tiles…", "Initialising AI layer…", "Almost ready…"];
              const MSG   = STEPS[loadStep] ?? "Loading…";
              return (
                <div style={{ position: "relative", textAlign: "center", padding: "0 24px", minWidth: 260 }}>
                  {/* Pulsing map pin */}
                  <div style={{ display: "inline-block", marginBottom: 28 }}>
                    <div style={{
                      width: 68, height: 68, borderRadius: "50%",
                      background: "linear-gradient(135deg, #3B82F6, #8B5CF6)",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      fontSize: 30, animation: "mapPing 1.5s ease-out infinite",
                    }}>📍</div>
                  </div>

                  <div style={{ color: "#F1F5F9", fontSize: 22, fontWeight: 700, marginBottom: 6, letterSpacing: "-0.3px" }}>
                    The Right Spot
                  </div>
                  <div style={{ color: "#64748B", fontSize: 13, marginBottom: 24, minHeight: 18 }}>
                    {MSG}
                  </div>

                  {/* Real progress bar */}
                  <div style={{
                    width: 220, height: 4, background: "rgba(255,255,255,0.08)",
                    borderRadius: 99, overflow: "hidden", margin: "0 auto 10px",
                  }}>
                    <div style={{
                      height: "100%", borderRadius: 99,
                      background: "linear-gradient(90deg, #3B82F6, #8B5CF6)",
                      width: `${PCT}%`,
                      transition: "width 0.45s cubic-bezier(0.4,0,0.2,1)",
                    }} />
                  </div>
                  <div style={{ fontSize: 11, color: "#475569", letterSpacing: "0.04em" }}>
                    {PCT}%
                  </div>
                </div>
              );
            })()
          )}

          <style>{`
            @keyframes mapPing {
              0%   { box-shadow: 0 0 0 0 rgba(59,130,246,0.6); }
              70%  { box-shadow: 0 0 0 20px rgba(59,130,246,0); }
              100% { box-shadow: 0 0 0 0 rgba(59,130,246,0); }
            }
            @keyframes loadBar {
              0%   { width: 0%; margin-left: 0; }
              50%  { width: 70%; margin-left: 0; }
              100% { width: 0%; margin-left: 100%; }
            }
            @keyframes gridScroll {
              0%   { transform: translate(0, 0); }
              100% { transform: translate(48px, 48px); }
            }
          `}</style>
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
