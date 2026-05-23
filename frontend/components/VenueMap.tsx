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
  color: string;   // solid background for the pill
  glow: string;    // box-shadow colour when active
}

const CATEGORIES: Record<PlaceCategory, CategoryConfig> = {
  restaurants: {
    label: "Restaurants", icon: "🍽️", description: "Dining & special occasions",
    placeTypes: ["restaurant"], defaultQuery: "best restaurant near me",
    color: "#C2136B", glow: "rgba(194,19,107,0.55)",
  },
  cafes: {
    label: "Cafés", icon: "☕", description: "Coffee, work, and reading",
    placeTypes: ["cafe", "coffee_shop"], defaultQuery: "quiet cafe with fast wifi",
    color: "#92400E", glow: "rgba(146,64,14,0.55)",
  },
  hiking: {
    label: "Hiking", icon: "🥾", description: "Trails, parks, nature walks",
    placeTypes: ["park", "natural_feature", "hiking_area"], defaultQuery: "hiking trails near the city",
    color: "#166534", glow: "rgba(22,101,52,0.55)",
  },
  parks: {
    label: "Parks", icon: "🌿", description: "Outdoor relaxation spots",
    placeTypes: ["park"], defaultQuery: "peaceful parks to relax",
    color: "#065F46", glow: "rgba(6,95,70,0.55)",
  },
  offices: {
    label: "Offices", icon: "🏢", description: "Business districts, headquarters",
    placeTypes: ["office", "corporate_office"], defaultQuery: "company offices and business district",
    color: "#1E3A8A", glow: "rgba(30,58,138,0.55)",
  },
  bookstores: {
    label: "Bookstores", icon: "📚", description: "Independent & chain bookshops",
    placeTypes: ["book_store"], defaultQuery: "bookstores with reading areas",
    color: "#5B21B6", glow: "rgba(91,33,182,0.55)",
  },
  libraries: {
    label: "Libraries", icon: "🏛️", description: "Public libraries and archives",
    placeTypes: ["library"], defaultQuery: "public libraries near me",
    color: "#312E81", glow: "rgba(49,46,129,0.55)",
  },
  coworking: {
    label: "Coworking", icon: "💻", description: "Shared workspaces and hotdesks",
    placeTypes: ["coworking_space"], defaultQuery: "coworking spaces day pass",
    color: "#0C4A6E", glow: "rgba(12,74,110,0.55)",
  },
  museums: {
    label: "Museums", icon: "🎨", description: "Art, science, history",
    placeTypes: ["museum", "art_gallery"], defaultQuery: "museums and galleries open today",
    color: "#9A3412", glow: "rgba(154,52,18,0.55)",
  },
  all: {
    label: "All", icon: "🔍", description: "Search everything",
    placeTypes: [], defaultQuery: "",
    color: "#334155", glow: "rgba(51,65,85,0.55)",
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
  const userLocationRef = useRef<{ lat: number; lng: number } | null>(null);

  const [mapsReady, setMapsReady] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadStep, setLoadStep] = useState(0);
  const [activeCategory, setActiveCategory] = useState<PlaceCategory>("restaurants");
  const [query, setQuery] = useState(initialQuery);
  const [inputValue, setInputValue] = useState(initialQuery);
  const [selectedPlaceDetails, setSelectedPlaceDetails] = useState<GooglePlaceDetails | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [aiSuggestions, setAiSuggestions] = useState<string[]>([]);
  const [detectedCity, setDetectedCity] = useState<string>(""); // from reverse geocoding
  const [showAllMatches, setShowAllMatches] = useState(false);
  const [modalQuery, setModalQuery] = useState("");

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

  // ── User location dot ───────────────────────────────────────────────────

  useEffect(() => {
    if (!mapsReady || !mapInstanceRef.current || !("geolocation" in navigator)) return;

    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const userPos = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        userLocationRef.current = userPos;

        const wrapper = document.createElement("div");
        wrapper.style.cssText = `
          width: 20px; height: 20px; border-radius: 50%;
          background: #3B82F6; border: 3px solid #fff;
          box-sizing: border-box;
          box-shadow: 0 2px 8px rgba(37,99,235,0.55);
          animation: locationPulse 2s ease-out infinite;
        `;

        new google.maps.marker.AdvancedMarkerElement({
          map: mapInstanceRef.current!,
          position: userPos,
          content: wrapper,
          title: "Your location",
          zIndex: 9999,
        });

        // Center map on user only on initial load (no search results yet)
        if (enrichedMarkers.length === 0) {
          mapInstanceRef.current?.setCenter(userPos);
          mapInstanceRef.current?.setZoom(14);
        }

        // Reverse geocode → city for backend fallback
        const geocoder = new google.maps.Geocoder();
        geocoder.geocode({ location: userPos }, (results, status) => {
          if (status === "OK" && results?.[0]) {
            const locality = results[0].address_components.find((c) => c.types.includes("locality"));
            const area = results[0].address_components.find((c) => c.types.includes("administrative_area_level_1"));
            const city = locality?.long_name || area?.long_name || "";
            if (city) setDetectedCity(city);
          }
        });
      },
      () => { /* permission denied or unavailable */ },
      { enableHighAccuracy: false, timeout: 8000, maximumAge: 120000 },
    );
  }, [mapsReady]); // eslint-disable-line react-hooks/exhaustive-deps

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

  const handleSearch = useCallback(async (rawQ: string) => {
    const q = rawQ.trim();
    if (!q) return;

    const span = traceSearch({ query: q, userId });
    const mapSpan = traceMapInteraction({ action: "ai_query" });
    rumAction("search_submitted", { query: q });
    setQuery(q);

    const detected = classifyQueryCategory(q);
    if (detected !== "all") setActiveCategory(detected);

    try {
      // Pass detectedCity separately — backend applies it only when LLM can't extract a city
      await search(q, detectedCity || undefined);
    } finally {
      mapSpan.finish();
      span.finish();
    }

    setAiSuggestions(generateFollowUps(q, detected));
  }, [search, userId, detectedCity]);

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

  const handleReset = useCallback(() => {
    cancel();
    setInputValue("");
    setQuery("");
    setAiSuggestions([]);
    setSidebarOpen(false);
    selectVenue(null);
    infoWindowRef.current?.close();
    markersRef.current.forEach((m) => { m.map = null; });
    markersRef.current.clear();
    rumAction("search_reset");
  }, [cancel, selectVenue]);

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

  const [leftPanelOpen, setLeftPanelOpen] = useState(true);
  const showLeftPanel = leftPanelOpen;
  const [leftPanelW, setLeftPanelW] = useState(320);

  const startLeftResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = leftPanelW;
    const onMove = (ev: MouseEvent) => {
      setLeftPanelW(Math.max(220, Math.min(560, startW + ev.clientX - startX)));
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [leftPanelW]);

  // Auto-open panel whenever a new search starts
  useEffect(() => {
    if (state.status === "searching") setLeftPanelOpen(true);
  }, [state.status]);

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
        @keyframes locationPulse {
          0%   { box-shadow: 0 0 0 0   rgba(59,130,246,0.55), 0 2px 8px rgba(37,99,235,0.45); }
          70%  { box-shadow: 0 0 0 18px rgba(59,130,246,0),   0 2px 8px rgba(37,99,235,0.45); }
          100% { box-shadow: 0 0 0 0   rgba(59,130,246,0),    0 2px 8px rgba(37,99,235,0.45); }
        }
        @keyframes gridScroll {
          from { background-position: 0 0; }
          to   { background-position: 48px 48px; }
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
          {/* Resize handle — right edge */}
          <div
            onMouseDown={startLeftResize}
            style={{
              position: "absolute", top: 0, right: 0, bottom: 0,
              width: 5, cursor: "col-resize", zIndex: 20,
              background: "transparent",
              transition: "background 0.15s",
            }}
            onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(99,179,237,0.25)"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "transparent"; }}
          />
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
                fontSize: 16, flexShrink: 0,
              }}>✨</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: "#F1F5F9", fontWeight: 700, fontSize: 15 }}>The Right Spot AI</div>
                {state.status === "searching" && (
                  <div style={{
                    fontSize: 12, fontWeight: 700,
                    background: "linear-gradient(90deg, #60A5FA, #A78BFA)",
                    WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
                    letterSpacing: "0.01em",
                  }}>
                    Agents working…
                  </div>
                )}
                {state.status === "done" && (
                  <div style={{
                    fontSize: 13, fontWeight: 800,
                    background: "linear-gradient(90deg, #34D399, #60A5FA)",
                    WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
                    letterSpacing: "0.01em",
                  }}>
                    {state.venues.length} venues found
                  </div>
                )}
                {state.status === "error" && (
                  <div style={{ fontSize: 12, fontWeight: 700, color: "#F87171" }}>Search error</div>
                )}
              </div>
              {/* Close AI Panel button — lives inside the panel */}
              <button
                onClick={() => setLeftPanelOpen(false)}
                title="Close AI panel"
                style={{
                  marginLeft: "auto",
                  display: "inline-flex", alignItems: "center", gap: 5,
                  padding: "5px 10px", borderRadius: 16,
                  background: "rgba(255,255,255,0.06)",
                  border: "1px solid rgba(255,255,255,0.1)",
                  color: "#64748B",
                  fontSize: 11, fontWeight: 600, cursor: "pointer",
                  whiteSpace: "nowrap",
                  flexShrink: 0,
                  transition: "all 0.15s",
                }}
              >
                <span style={{ fontSize: 11 }}>◀</span>
                <span>Close</span>
              </button>
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

          {/* ── Idle state ── */}
          {state.status === "idle" && (
            <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "32px 24px", gap: 16 }}>
              <div style={{ fontSize: 40 }}>✦</div>
              <div style={{ fontSize: 14, fontWeight: 700, color: "#CBD5E1", textAlign: "center" }}>
                Ask AI to discover the perfect venue
              </div>
              <div style={{ fontSize: 12, color: "#475569", textAlign: "center", lineHeight: 1.6 }}>
                Try <em style={{ color: "#93C5FD" }}>"birthday dinner for 8, quiet Italian"</em> or <em style={{ color: "#93C5FD" }}>"cosy café to work from"</em>
              </div>
              {[
                "birthday dinner for 8 in NYC",
                "quiet café with fast wifi",
                "rooftop bar for a group",
              ].map((ex) => (
                <button key={ex} onClick={() => { setInputValue(ex); handleSearch(ex); }}
                  style={{
                    width: "100%", padding: "9px 14px", borderRadius: 10, cursor: "pointer",
                    background: "rgba(59,130,246,0.07)", border: "1px solid rgba(59,130,246,0.2)",
                    color: "#93C5FD", fontSize: 12, textAlign: "left", fontStyle: "italic",
                    transition: "all 0.15s",
                  }}>
                  ↩ {ex}
                </button>
              ))}
            </div>
          )}

          {/* Agent steps — only during / after search */}
          {state.status !== "idle" && (
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
          )} {/* end status !== idle */}

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
            <div style={{ flex: 1, overflowY: "auto", padding: "0 10px 8px", minHeight: 0 }}>
              {/* Header row */}
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "6px 4px 10px" }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  {state.venues.length} Matches
                </span>
                <button
                  onClick={() => { setModalQuery(""); setShowAllMatches(true); }}
                  style={{
                    padding: "4px 11px", borderRadius: 20, border: "1px solid rgba(99,179,237,0.35)",
                    background: "linear-gradient(135deg, rgba(37,99,235,0.18), rgba(124,58,237,0.18))",
                    color: "#93C5FD", fontSize: 11, fontWeight: 700, cursor: "pointer",
                    letterSpacing: "0.02em",
                  }}
                >
                  View All ↗
                </button>
              </div>

              {state.venues.slice(0, 5).map((venue, idx) => {
                const score = Math.round(venue.match_score);
                const isSelected = state.selectedVenueId === venue.venue_id;
                // Rank-based accent gradient
                const accents = [
                  { from: "#F59E0B", to: "#EF4444", glow: "rgba(245,158,11,0.4)" },  // #1 gold-red
                  { from: "#3B82F6", to: "#8B5CF6", glow: "rgba(59,130,246,0.4)" },  // #2 blue-purple
                  { from: "#10B981", to: "#06B6D4", glow: "rgba(16,185,129,0.4)" },  // #3 green-cyan
                  { from: "#EC4899", to: "#F43F5E", glow: "rgba(236,72,153,0.35)" }, // #4 pink-rose
                  { from: "#8B5CF6", to: "#6366F1", glow: "rgba(139,92,246,0.35)" }, // #5 purple
                ];
                const accent = accents[idx] ?? accents[4];
                // Score color
                const scoreColor = score >= 70 ? "#34D399" : score >= 50 ? "#FBBF24" : "#94A3B8";

                return (
                  <div
                    key={venue.venue_id}
                    onClick={() => {
                      selectVenue(venue.venue_id);
                      setSidebarOpen(true);
                      fetchPlaceDetails(venue.venue_id).then(setSelectedPlaceDetails);
                      onVenueSelect?.(venue);
                    }}
                    style={{
                      borderRadius: 14, marginBottom: 8, cursor: "pointer", overflow: "hidden",
                      border: `1.5px solid ${isSelected ? accent.from : "rgba(255,255,255,0.07)"}`,
                      boxShadow: isSelected ? `0 0 18px ${accent.glow}` : "0 2px 8px rgba(0,0,0,0.25)",
                      transition: "all 0.18s ease",
                      background: isSelected
                        ? `linear-gradient(135deg, ${accent.from}22, ${accent.to}18)`
                        : "rgba(255,255,255,0.04)",
                    }}
                  >
                    {/* Colored top stripe */}
                    <div style={{
                      height: 3,
                      background: `linear-gradient(90deg, ${accent.from}, ${accent.to})`,
                    }} />

                    <div style={{ padding: "10px 12px" }}>
                      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
                        {/* Rank badge */}
                        <div style={{
                          flexShrink: 0, width: 28, height: 28, borderRadius: 8,
                          background: `linear-gradient(135deg, ${accent.from}, ${accent.to})`,
                          display: "flex", alignItems: "center", justifyContent: "center",
                          fontSize: 12, fontWeight: 900, color: "#fff",
                          boxShadow: `0 2px 8px ${accent.glow}`,
                        }}>
                          {idx + 1}
                        </div>

                        {/* Name + address */}
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 13, fontWeight: 700, color: "#F1F5F9", lineHeight: 1.3, marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                            {venue.name}
                          </div>
                          {venue.address && (
                            <div style={{ fontSize: 10, color: "#64748B", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                              📍 {venue.address.split(",").slice(0, 2).join(",")}
                            </div>
                          )}
                        </div>

                        {/* Score pill */}
                        <div style={{
                          flexShrink: 0,
                          padding: "3px 8px", borderRadius: 20,
                          background: `${scoreColor}22`,
                          border: `1px solid ${scoreColor}55`,
                          fontSize: 12, fontWeight: 900, color: scoreColor,
                          lineHeight: 1.6,
                        }}>
                          {score}
                        </div>
                      </div>

                      {/* Tags row */}
                      <div style={{ display: "flex", gap: 4, marginTop: 8, flexWrap: "wrap" }}>
                        {venue.has_private_room && (
                          <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(16,185,129,0.15)", color: "#34D399", border: "1px solid rgba(16,185,129,0.25)" }}>🚪 Private</span>
                        )}
                        {venue.price_per_head > 0 && (
                          <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(251,191,36,0.12)", color: "#FCD34D", border: "1px solid rgba(251,191,36,0.2)" }}>${venue.price_per_head}/head</span>
                        )}
                        {venue.intelligence?.why_card && (
                          <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(139,92,246,0.15)", color: "#C4B5FD", border: "1px solid rgba(139,92,246,0.25)" }}>✨ AI</span>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}

              {state.venues.length > 5 && (
                <button
                  onClick={() => { setModalQuery(""); setShowAllMatches(true); }}
                  style={{
                    width: "100%", padding: "10px", borderRadius: 12, marginTop: 2,
                    border: "1.5px dashed rgba(99,179,237,0.3)",
                    background: "rgba(59,130,246,0.06)",
                    color: "#60A5FA", fontSize: 12, fontWeight: 700, cursor: "pointer",
                    letterSpacing: "0.02em",
                  }}
                >
                  + {state.venues.length - 5} more matches — View All
                </button>
              )}
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
          HEADER — opaque bar: logo + search only
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
        boxShadow: "0 4px 24px rgba(0,0,0,0.45)",
        padding: "14px 20px",
      }}>
        {/* Logo + agent status (panel open button only shown when panel is closed) */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          {/* Show AI Panel button — only visible when panel is closed */}
          {!leftPanelOpen && (
            <button
              onClick={() => setLeftPanelOpen(true)}
              title="Show AI panel"
              style={{
                flexShrink: 0,
                display: "inline-flex", alignItems: "center", gap: 5,
                padding: "5px 11px", borderRadius: 20,
                background: "rgba(255,255,255,0.06)",
                border: "1.5px solid rgba(255,255,255,0.12)",
                color: "#64748B",
                fontSize: 12, fontWeight: 700, cursor: "pointer",
                transition: "all 0.2s",
                letterSpacing: "0.02em",
              }}
            >
              <span style={{ fontSize: 13 }}>▶</span>
              <span>AI Panel</span>
            </button>
          )}

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
            background: state.status === "searching" ? "rgba(59,130,246,0.12)" : "rgba(255,255,255,0.04)",
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
            ) : <span>5 AI agents</span>}
          </div>
        </div>

        {/* Search form */}
        <form onSubmit={handleSubmit} style={{ display: "flex", gap: 10 }}>
          <div style={{
            flex: 1, display: "flex", alignItems: "center",
            background: "rgba(255,255,255,0.06)", borderRadius: 14,
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
              <button type="button" onClick={() => setInputValue("")}
                style={{ background: "none", border: "none", color: "#475569", cursor: "pointer", padding: "0 12px", fontSize: 18, lineHeight: 1 }}>
                ×
              </button>
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
              boxShadow: state.status === "searching" ? "none" : "0 4px 18px rgba(37,99,235,0.5), inset 0 1px 0 rgba(255,255,255,0.15)",
              transition: "all 0.2s", letterSpacing: "0.02em",
            }}
          >
            {state.status === "searching" ? "Searching…" : "Ask AI →"}
          </button>
          {(state.status === "done" || state.status === "error" || query) && (
            <button
              type="button"
              onClick={handleReset}
              title="Clear search and start over"
              style={{
                padding: "0 16px", borderRadius: 14, border: "1.5px solid rgba(255,255,255,0.12)",
                background: "rgba(255,255,255,0.06)",
                color: "#94A3B8", fontWeight: 600, fontSize: 13,
                cursor: "pointer", whiteSpace: "nowrap",
                transition: "all 0.18s",
                flexShrink: 0,
              }}
            >
              ↺ Reset
            </button>
          )}
        </form>

        {/* Category pills — row 3, scrollable */}
        <div style={{
          display: "flex", gap: 7, marginTop: 12,
          overflowX: "auto", paddingBottom: 2,
          msOverflowStyle: "none" as React.CSSProperties["msOverflowStyle"],
          scrollbarWidth: "none" as React.CSSProperties["scrollbarWidth"],
        }}>
          {(Object.entries(CATEGORIES) as [PlaceCategory, CategoryConfig][]).map(([key, cfg]) => {
            const isActive = activeCategory === key;
            return (
              <button
                key={key}
                onClick={() => handleCategorySwitch(key)}
                title={cfg.description}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "7px 14px", borderRadius: 100,
                  border: "none", cursor: "pointer", whiteSpace: "nowrap",
                  flexShrink: 0, fontSize: 13,
                  fontWeight: isActive ? 700 : 500,
                  background: isActive ? cfg.color : `${cfg.color}99`,
                  color: "#fff",
                  boxShadow: isActive
                    ? `0 4px 16px ${cfg.glow}, inset 0 1px 0 rgba(255,255,255,0.15)`
                    : "0 2px 6px rgba(0,0,0,0.25)",
                  transform: isActive ? "scale(1.05)" : "scale(1)",
                  transition: "all 0.18s ease",
                  letterSpacing: "0.01em",
                }}
              >
                <span>{cfg.icon}</span>
                <span>{cfg.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* ════════════════════════════════════════════════════════
          INTENT CHIPS — float on the map directly below search bar
          ════════════════════════════════════════════════════════ */}
      {state.intent && (() => {
        const chips = [
          { val: state.intent!.city,               icon: "📍", label: state.intent!.city,                        bg: "#2563EB", shadow: "rgba(37,99,235,0.5)"   },
          { val: state.intent!.occasion,           icon: "🎉", label: state.intent!.occasion?.replace(/_/g, " "), bg: "#7C3AED", shadow: "rgba(124,58,237,0.5)"  },
          { val: state.intent!.cuisine,            icon: "🍽️", label: state.intent!.cuisine,                    bg: "#B45309", shadow: "rgba(180,83,9,0.5)"     },
          { val: state.intent!.group_size > 1,     icon: "👥", label: `${state.intent!.group_size} people`,     bg: "#047857", shadow: "rgba(4,120,87,0.5)"     },
          { val: state.intent!.needs_private_room, icon: "🚪", label: "private room",                           bg: "#0E7490", shadow: "rgba(14,116,144,0.5)"   },
          { val: state.intent!.noise_preference,   icon: "🔊", label: state.intent!.noise_preference,           bg: "#BE185D", shadow: "rgba(190,24,93,0.5)"    },
          { val: state.intent!.price_band,         icon: "💎", label: state.intent!.price_band,                 bg: "#4D7C0F", shadow: "rgba(77,124,15,0.5)"    },
        ].filter(c => c.val);
        return chips.length === 0 ? null : (
          <div style={{
            position: "absolute",
            top: 188, // header: 14px padding + 36px logo + 12px gap + 50px search + 12px gap + 38px pills + 14px padding + 2px border
            left: showLeftPanel ? leftPanelW + 16 : 16,
            right: 16,
            zIndex: 10,
            display: "flex", gap: 8, flexWrap: "wrap",
            transition: "left 0.3s ease",
            pointerEvents: "none", // let map events pass through the gaps
          }}>
            {chips.map((chip, idx) => (
              <div
                key={String(chip.label)}
                style={{
                  pointerEvents: "auto",
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "7px 14px", borderRadius: 100,
                  background: chip.bg,
                  color: "#fff",
                  fontSize: 13, fontWeight: 700,
                  boxShadow: `0 4px 14px ${chip.shadow}, 0 1px 3px rgba(0,0,0,0.3)`,
                  whiteSpace: "nowrap",
                  letterSpacing: "0.01em",
                  animation: `chipIn 0.4s cubic-bezier(0.34,1.56,0.64,1) ${idx * 0.06}s both`,
                  userSelect: "none",
                }}
              >
                <span style={{ fontSize: 14 }}>{chip.icon}</span>
                <span style={{ textTransform: "capitalize" }}>{chip.label}</span>
              </div>
            ))}
          </div>
        );
      })()}

      {/* ── Locate-me button ── */}
      {mapsReady && (
        <button
          title="Jump to your location"
          onClick={() => {
            if (userLocationRef.current) {
              mapInstanceRef.current?.setCenter(userLocationRef.current);
              mapInstanceRef.current?.setZoom(14);
            } else {
              navigator.geolocation?.getCurrentPosition((pos) => {
                const p = { lat: pos.coords.latitude, lng: pos.coords.longitude };
                userLocationRef.current = p;
                mapInstanceRef.current?.setCenter(p);
                mapInstanceRef.current?.setZoom(14);
              });
            }
          }}
          style={{
            position: "absolute",
            bottom: 120, right: 16,
            zIndex: 10,
            width: 44, height: 44, borderRadius: 12,
            background: "rgba(7,11,24,0.9)",
            backdropFilter: "blur(12px)",
            border: "1.5px solid rgba(255,255,255,0.12)",
            boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
            cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 20,
            transition: "all 0.15s",
          }}
        >
          📍
        </button>
      )}

      {/* ════════════════════════════════════════════════════════
          ALL MATCHES MODAL
          ════════════════════════════════════════════════════════ */}
      {showAllMatches && (
        <div
          style={{
            position: "fixed", inset: 0, zIndex: 50,
            background: "rgba(0,0,0,0.75)",
            backdropFilter: "blur(8px)",
            display: "flex", alignItems: "center", justifyContent: "center",
            padding: "20px",
          }}
          onClick={(e) => { if (e.target === e.currentTarget) setShowAllMatches(false); }}
        >
          <div style={{
            width: "100%", maxWidth: 860, maxHeight: "88vh",
            background: "linear-gradient(160deg, #0f172a 0%, #1a2236 100%)",
            borderRadius: 20,
            border: "1.5px solid rgba(255,255,255,0.1)",
            boxShadow: "0 24px 80px rgba(0,0,0,0.7)",
            display: "flex", flexDirection: "column",
            overflow: "hidden",
          }}>
            {/* Modal header */}
            <div style={{
              padding: "20px 24px 16px",
              borderBottom: "1px solid rgba(255,255,255,0.07)",
              flexShrink: 0,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14 }}>
                <div style={{
                  width: 36, height: 36, borderRadius: 10,
                  background: "linear-gradient(135deg, #2563EB, #7C3AED)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 18, flexShrink: 0,
                }}>🏆</div>
                <div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#F1F5F9" }}>All Matches</div>
                  <div style={{ fontSize: 12, color: "#64748B" }}>
                    {state.venues.length} venues found for "{query}"
                  </div>
                </div>
                <button
                  onClick={() => setShowAllMatches(false)}
                  style={{
                    marginLeft: "auto", width: 32, height: 32, borderRadius: 8,
                    background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)",
                    color: "#94A3B8", cursor: "pointer", fontSize: 18, lineHeight: 1,
                    display: "flex", alignItems: "center", justifyContent: "center",
                  }}
                >×</button>
              </div>

              {/* Search bar */}
              <div style={{
                display: "flex", alignItems: "center", gap: 10,
                background: "rgba(255,255,255,0.06)", borderRadius: 12,
                border: "1.5px solid rgba(255,255,255,0.1)",
                padding: "0 14px",
              }}>
                <span style={{ color: "#475569", fontSize: 16 }}>🔍</span>
                <input
                  autoFocus
                  value={modalQuery}
                  onChange={(e) => setModalQuery(e.target.value)}
                  placeholder="Filter venues by name or address…"
                  style={{
                    flex: 1, padding: "11px 0",
                    background: "transparent", border: "none", outline: "none",
                    fontSize: 14, color: "#E2E8F0", caretColor: "#3B82F6",
                  }}
                />
                {modalQuery && (
                  <button onClick={() => setModalQuery("")}
                    style={{ background: "none", border: "none", color: "#475569", cursor: "pointer", fontSize: 18 }}>×</button>
                )}
              </div>
            </div>

            {/* Venue grid */}
            <div style={{
              flex: 1, overflowY: "auto", padding: "16px 20px 20px",
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
              gap: 12,
              alignContent: "start",
            }}>
              {state.venues
                .filter((v) => {
                  if (!modalQuery.trim()) return true;
                  const q = modalQuery.toLowerCase();
                  return v.name.toLowerCase().includes(q) || v.address?.toLowerCase().includes(q);
                })
                .map((venue, idx) => {
                  const score = Math.round(venue.match_score);
                  const isSelected = state.selectedVenueId === venue.venue_id;
                  const cardColors = [
                    { from: "#F59E0B", to: "#EF4444", glow: "rgba(245,158,11,0.35)" },
                    { from: "#3B82F6", to: "#8B5CF6", glow: "rgba(59,130,246,0.35)" },
                    { from: "#10B981", to: "#06B6D4", glow: "rgba(16,185,129,0.35)" },
                    { from: "#EC4899", to: "#F43F5E", glow: "rgba(236,72,153,0.3)" },
                    { from: "#8B5CF6", to: "#6366F1", glow: "rgba(139,92,246,0.3)" },
                    { from: "#F97316", to: "#EAB308", glow: "rgba(249,115,22,0.3)" },
                    { from: "#14B8A6", to: "#3B82F6", glow: "rgba(20,184,166,0.3)" },
                  ];
                  const c = cardColors[idx % cardColors.length];
                  const scoreColor = score >= 70 ? "#34D399" : score >= 50 ? "#FBBF24" : "#94A3B8";

                  return (
                    <div
                      key={venue.venue_id}
                      onClick={() => {
                        selectVenue(venue.venue_id);
                        setSidebarOpen(true);
                        fetchPlaceDetails(venue.venue_id).then(setSelectedPlaceDetails);
                        onVenueSelect?.(venue);
                        setShowAllMatches(false);
                      }}
                      style={{
                        borderRadius: 14, cursor: "pointer", overflow: "hidden",
                        border: `1.5px solid ${isSelected ? c.from : "rgba(255,255,255,0.08)"}`,
                        boxShadow: isSelected ? `0 0 20px ${c.glow}` : "0 2px 10px rgba(0,0,0,0.3)",
                        background: isSelected
                          ? `linear-gradient(160deg, ${c.from}28, ${c.to}20)`
                          : "rgba(255,255,255,0.04)",
                        transition: "all 0.15s",
                      }}
                    >
                      {/* Top gradient bar */}
                      <div style={{
                        height: 4,
                        background: `linear-gradient(90deg, ${c.from}, ${c.to})`,
                      }} />

                      <div style={{ padding: "12px 14px 14px" }}>
                        {/* Rank + score row */}
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
                          <div style={{
                            width: 26, height: 26, borderRadius: 7,
                            background: `linear-gradient(135deg, ${c.from}, ${c.to})`,
                            display: "flex", alignItems: "center", justifyContent: "center",
                            fontSize: 11, fontWeight: 900, color: "#fff",
                            boxShadow: `0 2px 6px ${c.glow}`,
                          }}>#{idx + 1}</div>

                          <div style={{
                            padding: "3px 10px", borderRadius: 20,
                            background: `${scoreColor}22`,
                            border: `1px solid ${scoreColor}55`,
                            fontSize: 13, fontWeight: 900, color: scoreColor,
                          }}>
                            {score} pts
                          </div>
                        </div>

                        {/* Name */}
                        <div style={{ fontSize: 14, fontWeight: 700, color: "#F1F5F9", marginBottom: 4, lineHeight: 1.3 }}>
                          {venue.name}
                        </div>

                        {/* Address */}
                        {venue.address && (
                          <div style={{ fontSize: 11, color: "#64748B", marginBottom: 8, lineHeight: 1.4 }}>
                            {venue.address.split(",").slice(0, 2).join(",")}
                          </div>
                        )}

                        {/* AI snippet */}
                        {venue.intelligence?.why_card && (
                          <div style={{
                            fontSize: 11, color: "#94A3B8", lineHeight: 1.5,
                            padding: "8px 10px", borderRadius: 8,
                            background: "rgba(255,255,255,0.04)",
                            border: "1px solid rgba(255,255,255,0.06)",
                            display: "-webkit-box",
                            WebkitLineClamp: 3,
                            WebkitBoxOrient: "vertical" as React.CSSProperties["WebkitBoxOrient"],
                            overflow: "hidden",
                          }}>
                            {venue.intelligence.why_card}
                          </div>
                        )}

                        {/* Tags */}
                        <div style={{ display: "flex", gap: 4, marginTop: 8, flexWrap: "wrap" }}>
                          {venue.has_private_room && (
                            <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(16,185,129,0.15)", color: "#34D399", border: "1px solid rgba(16,185,129,0.25)" }}>🚪 Private</span>
                          )}
                          {venue.price_per_head > 0 && (
                            <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(251,191,36,0.12)", color: "#FCD34D", border: "1px solid rgba(251,191,36,0.2)" }}>${venue.price_per_head}/head</span>
                          )}
                          {venue.noise_level && (
                            <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(99,179,237,0.1)", color: "#93C5FD", border: "1px solid rgba(99,179,237,0.2)" }}>
                              {venue.noise_level.replace("_", " ")}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}

              {/* Empty state */}
              {state.venues.filter((v) => !modalQuery.trim() || v.name.toLowerCase().includes(modalQuery.toLowerCase()) || v.address?.toLowerCase().includes(modalQuery.toLowerCase())).length === 0 && (
                <div style={{ gridColumn: "1/-1", textAlign: "center", padding: "40px 20px", color: "#475569" }}>
                  No venues match "{modalQuery}"
                </div>
              )}
            </div>
          </div>
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
  const [sidebarW, setSidebarW] = useState(380);
  const [isFullScreen, setIsFullScreen] = useState(false);

  const startRightResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = sidebarW;
    const onMove = (ev: MouseEvent) => {
      setSidebarW(Math.max(280, Math.min(800, startW - (ev.clientX - startX))));
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [sidebarW]);

  if (!venue) return null;

  const intel = venue.intelligence;
  const openLabel = placeDetails?.is_open_now === true
    ? { label: "Open now", color: "#10B981" }
    : placeDetails?.is_open_now === false
      ? { label: "Closed", color: "#EF4444" }
      : null;

  const panelStyle: React.CSSProperties = isFullScreen
    ? {
        position: "fixed",
        inset: 0,
        width: "100%",
        zIndex: 100,
        background: "linear-gradient(180deg, #0f172a 0%, #1a2236 100%)",
        boxShadow: "none",
        display: "flex", flexDirection: "column",
      }
    : {
        position: "absolute",
        top: 188,
        right: 0,
        bottom: 0,
        width: sidebarW,
        background: "linear-gradient(180deg, #0f172a 0%, #1a2236 100%)",
        boxShadow: "-4px 0 32px rgba(0,0,0,0.45)",
        zIndex: 12,
        display: "flex", flexDirection: "column",
        borderLeft: "1px solid rgba(255,255,255,0.08)",
      };

  return (
    <div style={panelStyle}>
      {/* Resize handle — left edge (only in non-fullscreen mode) */}
      {!isFullScreen && (
        <div
          onMouseDown={startRightResize}
          style={{
            position: "absolute", top: 0, left: 0, bottom: 0,
            width: 5, cursor: "col-resize", zIndex: 20,
            background: "transparent",
            transition: "background 0.15s",
          }}
          onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(99,179,237,0.25)"; }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "transparent"; }}
        />
      )}
      {/* Fixed header inside sidebar */}
      <div style={{
        padding: "18px 18px 14px",
        borderBottom: "1px solid rgba(255,255,255,0.07)",
        flexShrink: 0,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div style={{ flex: 1, minWidth: 0, paddingRight: 12 }}>
            <h2 style={{ margin: 0, fontSize: 17, fontWeight: 700, color: "#F1F5F9", lineHeight: 1.3 }}>
              {venue.name}
            </h2>
            {(venue.neighborhood || venue.cuisine) && (
              <div style={{ fontSize: 12, color: "#64748B", marginTop: 3 }}>
                {venue.neighborhood && `${venue.neighborhood} · `}
                {venue.cuisine}
              </div>
            )}
          </div>
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            {/* Fullscreen toggle */}
            <button
              onClick={() => setIsFullScreen((f) => !f)}
              title={isFullScreen ? "Exit full screen" : "Full screen"}
              style={{
                width: 30, height: 30, borderRadius: 8,
                background: "rgba(99,179,237,0.1)", border: "1px solid rgba(99,179,237,0.2)",
                color: "#60A5FA", cursor: "pointer", fontSize: 13, lineHeight: 1,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}
            >{isFullScreen ? "⊠" : "⛶"}</button>
            {/* Close */}
            <button
              onClick={onClose}
              title="Close"
              style={{
                width: 30, height: 30, borderRadius: 8,
                background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)",
                color: "#94A3B8", cursor: "pointer", fontSize: 16, lineHeight: 1,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}
            >✕</button>
          </div>
        </div>

        {/* Pills row */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10 }}>
          <DarkPill color="#6366F1" label={`${Math.round(venue.match_score)}% match`} />
          {openLabel && <DarkPill color={openLabel.color} label={openLabel.label} />}
          {venue.has_private_room && <DarkPill color="#10B981" label="Private room" />}
          {venue.price_per_head > 0 && <DarkPill color="#F59E0B" label={`~$${venue.price_per_head}/head`} />}
          {placeDetails?.rating && (
            <DarkPill color="#F59E0B" label={`⭐ ${placeDetails.rating} (${placeDetails.user_rating_count?.toLocaleString()})`} />
          )}
        </div>
      </div>

      {/* Scrollable body */}
      <div style={{ flex: 1, overflowY: "auto", padding: "16px 18px 28px" }}>
        {/* Why card — always shown first, fully visible */}
        {intel?.why_card && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
              Why this spot
            </div>
            <p style={{ margin: 0, fontSize: 13, color: "#CBD5E1", lineHeight: 1.65 }}>{intel.why_card}</p>
          </div>
        )}

        {/* Scenario */}
        {intel?.scenario && (
          <div style={{
            marginBottom: 16, background: "rgba(255,255,255,0.04)",
            borderRadius: 10, padding: "10px 14px",
            border: "1px solid rgba(255,255,255,0.07)",
          }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 5 }}>
              Your evening
            </div>
            <p style={{ margin: 0, fontSize: 12, color: "#94A3B8", lineHeight: 1.6, fontStyle: "italic" }}>{intel.scenario}</p>
          </div>
        )}

        {/* Sensitivity bars */}
        {intel?.sensitivity_bars && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
              Dimensions
            </div>
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
            marginBottom: 16, padding: "8px 12px",
            background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.2)",
            borderRadius: 8, fontSize: 12, color: "#FCD34D",
          }}>
            ⚡ {intel.live_signal}
          </div>
        )}

        {/* Key quotes */}
        {venue.key_quotes.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
              What people say
            </div>
            {venue.key_quotes.slice(0, 3).map((q, i) => (
              <div key={i} style={{
                fontSize: 12, color: "#94A3B8", marginBottom: 6,
                paddingLeft: 10, borderLeft: "2px solid rgba(99,179,237,0.3)",
                lineHeight: 1.5,
              }}>
                "{q}"
              </div>
            ))}
          </div>
        )}

        {/* Contact links */}
        {(placeDetails?.website_uri || placeDetails?.phone_number) && (
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 4 }}>
            {placeDetails.website_uri && (
              <a href={placeDetails.website_uri} target="_blank" rel="noopener noreferrer"
                style={{
                  fontSize: 12, color: "#60A5FA", textDecoration: "none",
                  display: "flex", alignItems: "center", gap: 4,
                }}>
                🌐 Website
              </a>
            )}
            {placeDetails.phone_number && (
              <a href={`tel:${placeDetails.phone_number}`}
                style={{
                  fontSize: 12, color: "#60A5FA", textDecoration: "none",
                  display: "flex", alignItems: "center", gap: 4,
                }}>
                📞 {placeDetails.phone_number}
              </a>
            )}
          </div>
        )}
      </div>
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

function DarkPill({ label, color }: { label: string; color: string }) {
  return (
    <span style={{
      padding: "3px 9px", borderRadius: 20,
      background: `${color}20`,
      color, border: `1px solid ${color}45`,
      fontSize: 11, fontWeight: 600,
      whiteSpace: "nowrap",
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
        <span style={{ fontSize: 11, fontWeight: 600, color: "#94A3B8" }}>{clamped}%</span>
      </div>
      <div style={{ height: 4, background: "rgba(255,255,255,0.08)", borderRadius: 2, overflow: "hidden" }}>
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
