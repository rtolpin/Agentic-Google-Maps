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
  await Promise.all([
    loader.importLibrary("marker"),
    loader.importLibrary("routes"),
    loader.importLibrary("places"),
  ]);
  onStep?.(4); // 95%
  window.googleMapsLoaded = true;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const FLIGHT_THRESHOLD_KM = 500;

function haversineKm(lat1: number, lng1: number, lat2: number, lng2: number): number {
  const R = 6371;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLng = (lng2 - lng1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLng / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

const TRANSIT_ICON: Record<string, string> = {
  subway: "🚇", train: "🚆", bus: "🚌", airport: "✈️", ferry: "⛴️",
};
const TRANSIT_COLOR: Record<string, string> = {
  subway: "#F59E0B", train: "#3B82F6", bus: "#10B981", airport: "#8B5CF6", ferry: "#06B6D4",
};

interface TransitStop {
  place_id: string;
  name: string;
  address: string;
  latitude: number;
  longitude: number;
  transit_type: "subway" | "train" | "bus" | "airport" | "ferry";
}

type TravelMode = "DRIVING" | "TRANSIT" | "WALKING" | "BICYCLING" | "FLYING";

interface FlightInfo {
  price: number | null;
  stops: number;
  airline: string;
  flightNumber: string;
  departureAirport: string;
  arrivalAirport: string;
  durationStr: string;
}

interface DirectionsLeg {
  distance: string;
  duration: string;
  flight?: FlightInfo;
}

type RouteOption =
  | { type: "directions"; index: number; duration: string; distance: string; summary: string }
  | { type: "flight"; index: number; price: number | null; durationStr: string; stops: number; airline: string; flightNumber: string; departureAirport: string; arrivalAirport: string; departureLat?: number; departureLng?: number; arrivalLat?: number; arrivalLng?: number; outboundDate?: string };

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
    label: "Offices", icon: "🏢", description: "Corporate HQ, company offices",
    placeTypes: ["office", "corporate_office"], defaultQuery: "corporate headquarters and company offices to scout",
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

function _scoreColors(score: number): { bg: string; pointer: string; shadow: string } {
  if (score >= 90) return { bg: "linear-gradient(150deg,#FBBF24,#D97706)", pointer: "#D97706", shadow: "rgba(251,191,36,0.65)" };
  if (score >= 85) return { bg: "linear-gradient(150deg,#34D399,#059669)", pointer: "#059669", shadow: "rgba(52,211,153,0.6)" };
  if (score >= 80) return { bg: "linear-gradient(150deg,#818CF8,#4F46E5)", pointer: "#4F46E5", shadow: "rgba(99,102,241,0.6)" };
  if (score >= 75) return { bg: "linear-gradient(150deg,#60A5FA,#2563EB)", pointer: "#2563EB", shadow: "rgba(96,165,250,0.6)" };
  return { bg: "linear-gradient(150deg,#A78BFA,#7C3AED)", pointer: "#7C3AED", shadow: "rgba(167,139,250,0.6)" };
}

function buildPinElement(m: EnrichedMapMarker): HTMLElement {
  const score  = Math.round(m.match_score);
  const sel    = m.pinColor === "highlighted";
  const dimmed = m.pinColor === "dimmed";
  const { bg, pointer, shadow } = sel ? _scoreColors(score) : _scoreColors(score);

  const wrap = document.createElement("div");
  wrap.style.cssText = [
    "position:relative;cursor:pointer;display:flex;flex-direction:column;align-items:center;",
    "transform-origin:bottom center;transition:transform 0.15s,opacity 0.15s;",
    sel    ? "transform:scale(1.22);" : "",
    dimmed ? "opacity:0.38;transform:scale(0.8);" : "",
  ].join("");

  if (dimmed) {
    const dot = document.createElement("div");
    dot.style.cssText = [
      "width:26px;height:26px;border-radius:50%;",
      "background:linear-gradient(150deg,#475569,#1E293B);",
      "border:1.5px solid rgba(255,255,255,0.18);",
      "box-shadow:0 2px 6px rgba(0,0,0,0.35);",
      "display:flex;align-items:center;justify-content:center;",
    ].join("");
    const lbl = document.createElement("span");
    lbl.style.cssText = "font-size:9px;font-weight:700;color:rgba(255,255,255,0.65);pointer-events:none;";
    lbl.textContent = String(score);
    dot.appendChild(lbl);
    wrap.appendChild(dot);
    return wrap;
  }

  const size = sel ? 52 : 44;

  if (sel) {
    const ring = document.createElement("div");
    ring.style.cssText = [
      `position:absolute;top:${-(size*0.12)}px;left:${-(size*0.12)}px;`,
      `width:${size*1.24}px;height:${size*1.24}px;`,
      "border-radius:50%;border:2.5px solid " + pointer + ";",
      "animation:pinRing 1.8s ease-out infinite;pointer-events:none;",
    ].join("");
    wrap.appendChild(ring);
  }

  const bubble = document.createElement("div");
  bubble.style.cssText = [
    `width:${size}px;height:${size}px;border-radius:50%;`,
    `background:${bg};`,
    `border:${sel ? "2.5px solid #fff" : "2px solid rgba(255,255,255,0.35)"};`,
    `box-shadow:0 ${sel?8:5}px ${sel?22:14}px ${shadow};`,
    "display:flex;flex-direction:column;align-items:center;justify-content:center;",
    "position:relative;z-index:1;",
  ].join("");

  const scoreEl = document.createElement("span");
  scoreEl.style.cssText = [
    `font-size:${sel?17:13}px;font-weight:900;color:#fff;`,
    "letter-spacing:-0.5px;line-height:1;",
    "text-shadow:0 1px 3px rgba(0,0,0,0.35);pointer-events:none;",
  ].join("");
  scoreEl.textContent = String(score);
  bubble.appendChild(scoreEl);

  if (sel) {
    const sub = document.createElement("span");
    sub.style.cssText = "font-size:8px;font-weight:700;color:rgba(255,255,255,0.8);letter-spacing:0.05em;margin-top:2px;pointer-events:none;";
    sub.textContent = "MATCH";
    bubble.appendChild(sub);
  }

  const ptr = document.createElement("div");
  const ph = sel ? 13 : 9, pw = sel ? 10 : 7;
  ptr.style.cssText = [
    "width:0;height:0;",
    `border-left:${pw}px solid transparent;`,
    `border-right:${pw}px solid transparent;`,
    `border-top:${ph}px solid ${pointer};`,
    "margin-top:-1px;filter:drop-shadow(0 2px 3px rgba(0,0,0,0.2));",
  ].join("");

  wrap.appendChild(bubble);
  wrap.appendChild(ptr);
  return wrap;
}

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

const fmt = (s: string | null | undefined) =>
  (s ?? "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

// Reject intersection-style names Google returns as "neighborhood" (e.g. "Greenwood & Hamilton").
// These are useless as search locations — fall through to city/county level instead.
function isUsableNeighborhood(name: string): boolean {
  return !name.includes("&") && !/^\d/.test(name) && name.length > 0;
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
  const userMarkerPlacedRef = useRef(false);
  const hasSearchedRef = useRef(false);
  const searchInputRef = useRef<HTMLInputElement>(null);

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
  const [searchWasGpsAnchored, setSearchWasGpsAnchored] = useState(false);
  const [showAllMatches, setShowAllMatches] = useState(false);
  const [modalQuery, setModalQuery] = useState("");
  const [showSearchArea, setShowSearchArea] = useState(false);
  const [transitStops, setTransitStops] = useState<TransitStop[]>([]);
  const [showTransit, setShowTransit] = useState(false);
  const [transitLoading, setTransitLoading] = useState(false);
  const transitMarkersRef = useRef<google.maps.marker.AdvancedMarkerElement[]>([]);
  const directionsRendererRef = useRef<google.maps.DirectionsRenderer | null>(null);
  const flightArcRef = useRef<google.maps.Polyline | null>(null);
  const directionsResultRef = useRef<google.maps.DirectionsResult | null>(null);
  const [directionsTravelMode, setDirectionsTravelMode] = useState<TravelMode>("TRANSIT");
  const [directionsLeg, setDirectionsLeg] = useState<DirectionsLeg | null>(null);
  const [directionsLoading, setDirectionsLoading] = useState(false);
  const [directionsError, setDirectionsError] = useState<string | null>(null);
  const [routeOptions, setRouteOptions] = useState<RouteOption[] | null>(null);
  const [selectedRouteIndex, setSelectedRouteIndex] = useState<number | null>(null);

  const addressInputRef = useRef<HTMLInputElement>(null);
  const [hasAddressText, setHasAddressText] = useState(false);

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
      if (hasSearchedRef.current) setShowSearchArea(true);
    });

    mapInstanceRef.current.addListener("dragend", () => {
      if (hasSearchedRef.current) setShowSearchArea(true);
    });

    // Auto-refresh transit when map is panned >400 m while transit is showing
    mapInstanceRef.current.addListener("idle", () => {
      const map = mapInstanceRef.current;
      if (!map) return;
      const prev = transitCenterRef.current;
      if (!prev) return; // transit was never fetched
      const c = map.getCenter();
      if (!c) return;
      const distKm = haversineKm(prev.lat, prev.lng, c.lat(), c.lng());
      if (distKm > 0.4) {
        // Map has moved — re-fetch transit silently for the new center
        // showTransit state is read via closure; only re-fetch if transit is visible
        setShowTransit((current) => {
          if (current) fetchTransit(true);
          return current;
        });
      }
    });

    // ── User location dot ─────────────────────────────────────────────────────
    // Persist location to localStorage (30 min TTL) so refreshes don't require
    // a new geolocation request — browsers throttle repeated requests.
    const _LS_KEY = "trs_user_location";
    const _LOC_TTL_MS = 30 * 60 * 1000;

    const placeUserDot = (userPos: { lat: number; lng: number }) => {
      const mapInstance = mapInstanceRef.current;
      if (!mapInstance || userMarkerPlacedRef.current) return;
      userMarkerPlacedRef.current = true;
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
        map: mapInstance,
        position: userPos,
        content: wrapper,
        title: "Your location",
        zIndex: 9999,
      });

      if (markersRef.current.size === 0) {
        mapInstance.setCenter(userPos);
        mapInstance.setZoom(14);
      }

      const geocoder = new google.maps.Geocoder();
      geocoder.geocode({ location: userPos }, (results, status) => {
        if (status === "OK" && results?.[0]) {
          const locality = results[0].address_components.find((c) => c.types.includes("locality"));
          const area = results[0].address_components.find((c) => c.types.includes("administrative_area_level_1"));
          const city = locality?.long_name || area?.long_name || "";
          if (city) setDetectedCity(city);
        }
      });
    };

    // Try cached position first — avoids geolocation request on refresh
    try {
      const cached = localStorage.getItem(_LS_KEY);
      if (cached) {
        const { lat, lng, ts } = JSON.parse(cached);
        if (Date.now() - ts < _LOC_TTL_MS) {
          placeUserDot({ lat, lng });
        }
      }
    } catch (_) {}

    // Always request fresh position in background to keep cache warm
    if ("geolocation" in navigator) {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const userPos = { lat: pos.coords.latitude, lng: pos.coords.longitude };
          try {
            localStorage.setItem(_LS_KEY, JSON.stringify({ ...userPos, ts: Date.now() }));
          } catch (_) {}
          placeUserDot(userPos);
        },
        () => { /* permission denied — cached position already placed if available */ },
        { enableHighAccuracy: false, timeout: 8000, maximumAge: 300000 },
      );
    }

    // ── Address autocomplete — jump to any address or area ─────────────────
    (async () => {
      if (!addressInputRef.current || !mapInstanceRef.current) return;
      try {
        const { Autocomplete } = await google.maps.importLibrary("places") as google.maps.PlacesLibrary;
        const ac = new Autocomplete(addressInputRef.current, {
          fields: ["geometry", "formatted_address", "name"],
        });
        ac.bindTo("bounds", mapInstanceRef.current);
        ac.addListener("place_changed", () => {
          const place = ac.getPlace();
          if (!place.geometry?.location) return;
          mapInstanceRef.current?.panTo(place.geometry.location);
          mapInstanceRef.current?.setZoom(15);
          setHasAddressText(true);
          setShowSearchArea(true);
        });
      } catch (_) { /* Places API not enabled — address bar still works via geocoder on button click */ }
    })();
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

      const existing = markersRef.current.get(m.venue_id);
      if (existing) {
        existing.position = pos;
        existing.content = buildPinElement(m);
        return;
      }

      const pinEl = buildPinElement(m);

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

  // ── Transit markers ─────────────────────────────────────────────────────
  // Separated into two effects:
  //   1. Create markers (hidden) when stops are fetched — runs once per fetch
  //   2. Toggle marker.map to show/hide — no DOM teardown, no flash

  const transitFetchingRef = useRef(false);
  const transitCenterRef  = useRef<{ lat: number; lng: number } | null>(null);

  const fetchTransit = useCallback(async (silent = false) => {
    if (transitFetchingRef.current) return;
    const map = mapInstanceRef.current;
    if (!map) return;
    const center = map.getCenter();
    if (!center) return;
    const loc = { lat: center.lat(), lng: center.lng() };

    // zoom-adaptive radius: wider view → larger search radius
    const zoom = map.getZoom() ?? 14;
    const radiusM = zoom >= 16 ? 600 : zoom >= 14 ? 1200 : zoom >= 12 ? 2000 : 3000;

    transitFetchingRef.current = true;
    if (!silent) setTransitLoading(true);
    try {
      const resp = await fetch(
        `${API_BASE}/api/transit/nearby?lat=${loc.lat}&lng=${loc.lng}&radius_m=${radiusM}`
      );
      if (resp.ok) {
        setTransitStops(await resp.json());
        setShowTransit(true);
        transitCenterRef.current = loc;
      }
    } finally {
      setTransitLoading(false);
      transitFetchingRef.current = false;
    }
  }, []);

  // Build InfoWindow content for a transit stop (no external links — stays in-app)
  const openTransitInfo = useCallback((stop: TransitStop, marker?: google.maps.marker.AdvancedMarkerElement) => {
    const icon  = TRANSIT_ICON[stop.transit_type]  ?? "🚏";
    const color = TRANSIT_COLOR[stop.transit_type] ?? "#94A3B8";
    const typeLabel = stop.transit_type.charAt(0).toUpperCase() + stop.transit_type.slice(1);

    const userLoc = userLocationRef.current;
    const distM = userLoc
      ? Math.round(haversineKm(userLoc.lat, userLoc.lng, stop.latitude, stop.longitude) * 1000)
      : null;
    const distStr = distM != null
      ? distM < 1000
        ? `${distM} m`
        : (() => {
            const km = distM / 1000;
            const mi = km / 1.60934;
            const kmStr = km < 10 ? km.toFixed(1) : Math.round(km).toLocaleString();
            const miStr = mi < 10 ? mi.toFixed(1) : Math.round(mi).toLocaleString();
            return `${miStr} mi (${kmStr} km)`;
          })()
      : null;

    const content = `
      <div style="font-family:system-ui,sans-serif;padding:6px 2px;min-width:180px;max-width:240px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
          <span style="font-size:20px">${icon}</span>
          <div>
            <div style="font-weight:700;font-size:13px;line-height:1.3;color:#0f172a">${stop.name}</div>
            <span style="display:inline-block;font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;background:${color}22;color:${color};border:1px solid ${color}55;margin-top:2px">${typeLabel}</span>
          </div>
        </div>
        <div style="font-size:11px;color:#64748b;margin-bottom:${distStr ? 6 : 0}px;line-height:1.4">
          ${stop.address.split(",").slice(0, 3).join(", ")}
        </div>
        ${distStr ? `<div style="font-size:12px;font-weight:600;color:#2563eb">🚶 ${distStr} from your location</div>` : ""}
      </div>`;

    if (infoWindowRef.current && mapInstanceRef.current) {
      infoWindowRef.current.setContent(content);
      if (marker) {
        infoWindowRef.current.open({ map: mapInstanceRef.current, anchor: marker });
      } else {
        infoWindowRef.current.setPosition({ lat: stop.latitude, lng: stop.longitude });
        infoWindowRef.current.open(mapInstanceRef.current);
      }
    }
    mapInstanceRef.current?.panTo({ lat: stop.latitude, lng: stop.longitude });
  }, []);

  // Effect 1: build markers (initially hidden) whenever the stops list changes
  useEffect(() => {
    if (!mapsReady || !mapInstanceRef.current || transitStops.length === 0) return;
    // Tear down previous set
    transitMarkersRef.current.forEach((m) => { m.map = null; });
    transitMarkersRef.current = [];

    transitStops.forEach((stop) => {
      if (!stop.latitude || !stop.longitude) return;
      const icon  = TRANSIT_ICON[stop.transit_type]  ?? "🚏";
      const color = TRANSIT_COLOR[stop.transit_type] ?? "#94A3B8";
      const pin = document.createElement("div");
      pin.style.cssText = [
        `background:${color}22`, `border:2px solid ${color}`,
        "border-radius:50%", "width:36px", "height:36px",
        "display:flex", "align-items:center", "justify-content:center",
        "font-size:16px", "cursor:pointer", `box-shadow:0 2px 8px ${color}55`,
      ].join(";");
      pin.textContent = icon;

      const marker = new google.maps.marker.AdvancedMarkerElement({
        map: null,
        position: { lat: stop.latitude, lng: stop.longitude },
        content: pin,
        title: stop.name,
      });

      marker.addListener("click", () => openTransitInfo(stop, marker));

      transitMarkersRef.current.push(marker);
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transitStops, mapsReady]);

  // Effect 2: show/hide existing markers — no creation, no flash
  useEffect(() => {
    const map = showTransit ? mapInstanceRef.current : null;
    transitMarkersRef.current.forEach((m) => { m.map = map; });
  }, [showTransit]);

  // ── In-map directions (DirectionsService + DirectionsRenderer) ───────────

  const clearDirections = useCallback(() => {
    directionsRendererRef.current?.setMap(null);
    directionsRendererRef.current = null;
    directionsResultRef.current = null;
    flightArcRef.current?.setMap(null);
    flightArcRef.current = null;
    setDirectionsLeg(null);
    setDirectionsError(null);
    setRouteOptions(null);
    setSelectedRouteIndex(null);
  }, []);

  const getDirections = useCallback(async (
    venue: { place_id?: string | null; latitude?: number | null; longitude?: number | null; name: string; address?: string | null },
    travelMode: TravelMode,
    flightOptions?: { date?: string; depIata?: string; arrIata?: string },
  ) => {
    const origin = userLocationRef.current;
    if (!origin) {
      setDirectionsError("Enable location access in your browser to get directions");
      return;
    }

    // ── Flight search branch ───────────────────────────────────────────────
    if (travelMode === "FLYING") {
      const vLat = venue.latitude;
      const vLng = venue.longitude;
      if (!vLat || !vLng) {
        setDirectionsError("Venue coordinates unavailable for flight search");
        return;
      }
      setDirectionsError(null);
      setDirectionsLoading(true);
      clearDirections();
      try {
        const url = new URL(`${API_BASE}/api/flights`);
        url.searchParams.set("origin_lat", String(origin.lat));
        url.searchParams.set("origin_lng", String(origin.lng));
        url.searchParams.set("dest_lat", String(vLat));
        url.searchParams.set("dest_lng", String(vLng));
        if (flightOptions?.date) url.searchParams.set("outbound_date", flightOptions.date);
        if (flightOptions?.depIata) url.searchParams.set("dep_iata", flightOptions.depIata);
        if (flightOptions?.arrIata) url.searchParams.set("arr_iata", flightOptions.arrIata);
        const resp = await fetch(url.toString());
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }));
          throw new Error((err as { detail?: string }).detail ?? `HTTP ${resp.status}`);
        }
        const data = await resp.json() as {
          options: Array<{
            price?: number; duration_str?: string; stops?: number;
            airline?: string; flight_number?: string;
            departure_airport_display?: string; arrival_airport_display?: string;
            dep_lat?: number; dep_lng?: number; arr_lat?: number; arr_lng?: number;
            outbound_date?: string;
          }>;
        };
        const opts: RouteOption[] = (data.options ?? []).map((o, i) => ({
          type: "flight" as const,
          index: i,
          price: o.price ?? null,
          durationStr: o.duration_str ?? "",
          stops: o.stops ?? 0,
          airline: o.airline ?? "",
          flightNumber: o.flight_number ?? "",
          departureAirport: o.departure_airport_display ?? "",
          arrivalAirport: o.arrival_airport_display ?? "",
          departureLat: o.dep_lat,
          departureLng: o.dep_lng,
          arrivalLat: o.arr_lat,
          arrivalLng: o.arr_lng,
          outboundDate: o.outbound_date,
        }));
        setRouteOptions(opts);

        // Draw great-circle arc between the two airports
        const first = opts[0];
        if (first?.type === "flight" && first.departureLat && first.arrivalLat && mapInstanceRef.current) {
          flightArcRef.current?.setMap(null);
          const arc = new google.maps.Polyline({
            path: [
              { lat: first.departureLat, lng: first.departureLng! },
              { lat: first.arrivalLat,   lng: first.arrivalLng! },
            ],
            geodesic: true,
            strokeColor: "#A78BFA",
            strokeOpacity: 0,
            strokeWeight: 0,
            icons: [{
              icon: { path: "M 0,-1 0,1", strokeOpacity: 1, strokeWeight: 3, strokeColor: "#A78BFA", scale: 4 },
              offset: "0", repeat: "20px",
            }, {
              icon: { path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW, strokeColor: "#A78BFA", fillColor: "#A78BFA", fillOpacity: 1, scale: 4 },
              offset: "50%",
            }],
          });
          arc.setMap(mapInstanceRef.current);
          flightArcRef.current = arc;
          const bounds = new google.maps.LatLngBounds();
          bounds.extend({ lat: first.departureLat, lng: first.departureLng! });
          bounds.extend({ lat: first.arrivalLat,   lng: first.arrivalLng! });
          mapInstanceRef.current.fitBounds(bounds, 80);
        }
        if (opts.length === 1) {
          setSelectedRouteIndex(0);
          const o = opts[0];
          if (o.type === "flight") {
            setDirectionsLeg({ distance: "", duration: "", flight: { price: o.price, stops: o.stops, airline: o.airline, flightNumber: o.flightNumber, departureAirport: o.departureAirport, arrivalAirport: o.arrivalAirport, durationStr: o.durationStr } });
          }
        }
      } catch (e: unknown) {
        setDirectionsError(e instanceof Error ? e.message : String(e));
      } finally {
        setDirectionsLoading(false);
      }
      return;
    }

    if (!mapInstanceRef.current) return;

    // Build the best available destination: Place ID → lat/lng → text query
    // Note: empty string place_id is falsy; also treat 0,0 coords as absent
    const validPlaceId = venue.place_id || null;
    const validLat = venue.latitude && Math.abs(venue.latitude) > 0.001 ? venue.latitude : null;
    const validLng = venue.longitude && Math.abs(venue.longitude) > 0.001 ? venue.longitude : null;
    const textQuery = [venue.name, venue.address?.split(",").slice(0, 2).join(",")].filter(Boolean).join(", ");
    const destination: google.maps.DirectionsRequest["destination"] = validPlaceId
      ? { placeId: validPlaceId }
      : (validLat && validLng)
        ? { lat: validLat, lng: validLng }
        : textQuery;

    setDirectionsError(null);
    setDirectionsLoading(true);
    clearDirections();

    // Import DirectionsService and DirectionsRenderer from the routes library
    const { DirectionsService, DirectionsRenderer } = await google.maps.importLibrary("routes") as google.maps.RoutesLibrary;

    const service = new DirectionsService();
    const renderer = new DirectionsRenderer({
      suppressMarkers: false,
      draggable: true,
      polylineOptions: { strokeColor: "#6366F1", strokeWeight: 5, strokeOpacity: 0.85 },
    });
    renderer.setMap(mapInstanceRef.current);
    directionsRendererRef.current = renderer;

    const request: google.maps.DirectionsRequest = {
      origin,
      destination,
      travelMode: google.maps.TravelMode[travelMode],
      provideRouteAlternatives: true,
      ...(travelMode === "TRANSIT"
        ? { transitOptions: { departureTime: new Date() } }
        : {}),
    };

    try {
      const result = await service.route(request);
      renderer.setDirections(result);
      directionsResultRef.current = result;

      const opts: RouteOption[] = result.routes.map((route, i) => ({
        type: "directions" as const,
        index: i,
        duration: route.legs[0]?.duration?.text ?? "",
        distance: route.legs[0]?.distance?.text ?? "",
        summary: route.summary || `Route ${i + 1}`,
      }));
      setRouteOptions(opts);

      if (opts.length === 1) {
        setSelectedRouteIndex(0);
        const leg = result.routes[0]?.legs[0];
        if (leg) setDirectionsLeg({ distance: leg.distance?.text ?? "", duration: leg.duration?.text ?? "" });
      }
    } catch (e: unknown) {
      clearDirections();
      const errStr = e instanceof Error ? e.message : String(e);
      if (errStr.includes("REQUEST_DENIED")) {
        setDirectionsError("Directions API not enabled — check Google Cloud Console.");
      } else if (errStr.includes("ZERO_RESULTS") || errStr.includes("NOT_FOUND")) {
        setDirectionsError("No route found for this mode — try Drive or Walk.");
      } else {
        setDirectionsError(`Routing failed: ${errStr.slice(0, 80)}`);
      }
    } finally {
      setDirectionsLoading(false);
    }
  }, [clearDirections]);

  const selectRoute = useCallback((option: RouteOption) => {
    setSelectedRouteIndex(option.index);
    if (option.type === "directions") {
      directionsRendererRef.current?.setOptions({ routeIndex: option.index });
      const leg = directionsResultRef.current?.routes[option.index]?.legs[0];
      if (leg) setDirectionsLeg({ distance: leg.distance?.text ?? "", duration: leg.duration?.text ?? "" });
    } else {
      setDirectionsLeg({ distance: "", duration: "", flight: { price: option.price, stops: option.stops, airline: option.airline, flightNumber: option.flightNumber, departureAirport: option.departureAirport, arrivalAirport: option.arrivalAirport, durationStr: option.durationStr } });
    }
  }, []);

  // ── Auto-recalculate directions when travel mode changes ────────────────
  const directionsLegRef = useRef(directionsLeg);
  directionsLegRef.current = directionsLeg;
  const routeOptionsRef = useRef(routeOptions);
  routeOptionsRef.current = routeOptions;
  const prevTravelModeRef = useRef(directionsTravelMode);

  useEffect(() => {
    if (prevTravelModeRef.current === directionsTravelMode) return;
    prevTravelModeRef.current = directionsTravelMode;
    if (directionsTravelMode === "FLYING") {
      // Clear stale routing errors — they don't apply to flight search.
      // User must explicitly click "Search Flights" to choose date/airports.
      setDirectionsError(null);
      return;
    }
    const selectedVenue = state.venues.find((v) => v.venue_id === state.selectedVenueId) ?? null;
    // Re-route immediately when a route is already shown (directionsLeg set for
    // single-route results, routeOptions set for multi-route TRANSIT results).
    if ((directionsLegRef.current || routeOptionsRef.current) && selectedVenue) {
      getDirections(selectedVenue, directionsTravelMode);
    }
  }, [directionsTravelMode, getDirections, state.venues, state.selectedVenueId]);

  // ── Auto-switch to Fly mode for distant venues ──────────────────────────
  useEffect(() => {
    if (!state.selectedVenueId) return;
    const origin = userLocationRef.current;
    if (!origin) return;
    const venue = state.venues.find((v) => v.venue_id === state.selectedVenueId);
    if (!venue?.latitude || !venue?.longitude) return;
    const distKm = haversineKm(origin.lat, origin.lng, venue.latitude, venue.longitude);
    if (distKm > FLIGHT_THRESHOLD_KM) {
      setDirectionsTravelMode("FLYING");
    } else if (directionsTravelMode === "FLYING") {
      setDirectionsTravelMode("TRANSIT");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.selectedVenueId]);

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

  // Matches any query that implies "use my current location" so we resolve GPS
  // instead of falling back to a stale detectedCity.
  // Covers: "near me", "nearby", "near here", "within 5/five miles", "of me",
  //         "around me", "around here", "close to me", "in my area"
  const _NUM_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty";
  const _DIST_PAT  = `within\\s+(\\d+(?:\\.\\d+)?|${_NUM_WORDS})\\s*(mile|km|meter|mi)\\w*`;
  const PROXIMITY_RE = new RegExp(
    `(near me|near here|nearby|of me|${_DIST_PAT}|around me|around here|close to me|in my area)`,
    "i",
  );
  const _WORD_DIST: Record<string, number> = {
    one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8,
    nine: 9, ten: 10, fifteen: 15, twenty: 20, thirty: 30,
  };

  const handleSearch = useCallback(async (rawQ: string) => {
    let q = rawQ.trim();
    if (!q) return;

    const isProximityQuery = PROXIMITY_RE.test(q);

    // Detect an explicitly named target city so we don't GPS-override intentional
    // cross-city searches like "sushi in Tokyo" or "restaurants in London".
    // Exclude "within …" (proximity distance) and articles ("in a/the/my …").
    const qForCityCheck = q.replace(/\bwithin\b\s+\S+\s+(mile|km|mi)\w*/gi, "");
    const hasExplicitCity = !isProximityQuery &&
      /\bin\s+(?!a\b|an\b|the\b|my\b|this\b|that\b|any\b|some\b)\S/i.test(qForCityCheck);

    let searchCoords: { lat: number; lng: number; radiusM?: number } | undefined;

    // ── 1. Load GPS from cache (cheap, no permission prompt) ──────────────────
    if (!userLocationRef.current) {
      try {
        const cached = localStorage.getItem("trs_user_location");
        if (cached) {
          const { lat, lng, ts } = JSON.parse(cached);
          if (Date.now() - ts < 30 * 60 * 1000) userLocationRef.current = { lat, lng };
        }
      } catch (_) {}
    }

    // ── 2. Request fresh GPS only for explicit proximity phrases ──────────────
    if (isProximityQuery && !userLocationRef.current && "geolocation" in navigator) {
      await new Promise<void>((resolve) => {
        navigator.geolocation.getCurrentPosition(
          (pos) => {
            const userPos = { lat: pos.coords.latitude, lng: pos.coords.longitude };
            userLocationRef.current = userPos;
            try { localStorage.setItem("trs_user_location", JSON.stringify({ ...userPos, ts: Date.now() })); } catch (_) {}
            resolve();
          },
          () => resolve(),
          { enableHighAccuracy: false, timeout: 6000, maximumAge: 300000 },
        );
      });
    }

    // ── 3. Build searchCoords whenever GPS is available and applicable ─────────
    // This fires for: explicit proximity phrases AND any query that lacks an explicit
    // city name — so "best sushi" in Trenton never defaults to stale NYC coords.
    if (userLocationRef.current && (isProximityQuery || !hasExplicitCity)) {
      const userLoc = userLocationRef.current;

      // Extract explicit radius from "within N/five miles/km" — convert to metres
      const distMatch = q.match(new RegExp(`within\\s+(\\d+(?:\\.\\d+)?|${_NUM_WORDS})\\s*(mile|km|mi)\\w*`, "i"));
      let radiusM = isProximityQuery ? 2000 : 8000; // wider default for general queries
      if (distMatch) {
        const raw = distMatch[1].toLowerCase();
        const n = _WORD_DIST[raw] ?? parseFloat(raw);
        const unit = distMatch[2].toLowerCase();
        radiusM = unit.startsWith("km") ? n * 1000 : n * 1609;
      }
      searchCoords = { ...userLoc, radiusM };

      // Reverse geocode + query injection only for proximity phrases so we don't
      // silently rewrite "best sushi" into "best sushi in North Caldwell" every time.
      if (isProximityQuery) {
        await new Promise<void>((resolve) => {
          try {
            const geocoder = new google.maps.Geocoder();
            geocoder.geocode({ location: userLoc }, (results, status) => {
              if (status === "OK" && results?.[0]) {
                const comps = results[0].address_components;
                const rawNbhd = comps.find((c) =>
                  c.types.includes("neighborhood") || c.types.includes("sublocality_level_1")
                );
                const locality = comps.find((c) => c.types.includes("locality"));
                const area = comps.find((c) =>
                  c.types.includes("administrative_area_level_2") || c.types.includes("administrative_area_level_1")
                );
                // Skip intersection-style neighborhood names (e.g. "Greenwood & Hamilton")
                const nbhdName = rawNbhd?.long_name ?? "";
                const name = (isUsableNeighborhood(nbhdName) ? nbhdName : null)
                  ?? locality?.long_name ?? area?.long_name ?? "";
                if (name) {
                  if (!q.toLowerCase().includes(" in ")) q = `${q} in ${name}`;
                  setInputValue(q);
                  setDetectedCity(name);
                }
              }
              resolve();
            });
          } catch (_) { resolve(); }
        });
      }
    } else if (!userLocationRef.current && mapInstanceRef.current) {
      // No GPS at all — fall back to map center
      const center = mapInstanceRef.current.getCenter();
      if (center) searchCoords = { lat: center.lat(), lng: center.lng(), radiusM: 3000 };
    }

    hasSearchedRef.current = true;
    setShowSearchArea(false);
    const span = traceSearch({ query: q, userId });
    const mapSpan = traceMapInteraction({ action: "ai_query" });
    rumAction("search_submitted", { query: q });
    setQuery(q);

    const detected = classifyQueryCategory(q);
    if (detected !== "all") setActiveCategory(detected);

    // Suppress stale detectedCity whenever GPS coords are anchoring the search.
    // This prevents a prior "New York" session from hijacking Trenton results.
    const userCityParam = searchCoords ? undefined : (detectedCity || undefined);
    const gpsAnchored = !!(userLocationRef.current && (isProximityQuery || !hasExplicitCity));
    setSearchWasGpsAnchored(gpsAnchored);
    try {
      await search(q, userCityParam, searchCoords);
    } finally {
      mapSpan.finish();
      span.finish();
    }

    setAiSuggestions(generateFollowUps(q, detected));
  }, [search, userId, detectedCity]);

  const handleSearchThisArea = useCallback(async () => {
    const mapInstance = mapInstanceRef.current;
    if (!mapInstance) return;
    // Use the AI search box value; fall back to the active category's default query so
    // Search This Area works even before the user has typed anything.
    const rawQ = inputValue || CATEGORIES[activeCategory].defaultQuery || "places near me";
    const center = mapInstance.getCenter();
    if (!center) return;

    // Use the map center's exact GPS coordinates as the search anchor.
    // Derive the radius from the visible viewport so Search This Area
    // respects the current zoom level rather than using a fixed 5 km.
    const centerLat = center.lat();
    const centerLng = center.lng();
    let radiusM = 5000; // default fallback
    const bounds = mapInstance.getBounds();
    if (bounds) {
      const ne = bounds.getNorthEast();
      const DEG_TO_M = 111320;
      const halfLatM = Math.abs(ne.lat() - centerLat) * DEG_TO_M;
      const halfLngM = Math.abs(ne.lng() - centerLng) * DEG_TO_M * Math.cos(centerLat * Math.PI / 180);
      radiusM = Math.max(500, Math.min(50000, Math.max(halfLatM, halfLngM)));
    }
    const areaCoords = { lat: centerLat, lng: centerLng, radiusM };

    // Strip proximity phrases and any previously injected "in [location]" suffix
    const baseQuery = rawQ
      .replace(/\s*(near me|near here|nearby|of me|within\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty)\s*(mile|km|mi)\w*|around me|around here|close to me|in my area)\s*/gi, " ")
      .replace(/\s+in\s+[^,]+(,\s*[^,]+)*$/i, "")
      .trim();

    // Reverse-geocode in the background for display text only
    let displayCity = "";
    try {
      const geocoder = new google.maps.Geocoder();
      await new Promise<void>((resolve) => {
        geocoder.geocode({ location: areaCoords }, (results, status) => {
          if (status === "OK" && results?.[0]) {
            const sub = results[0].address_components.find((c) =>
              c.types.includes("neighborhood") || c.types.includes("sublocality_level_1")
            );
            const locality = results[0].address_components.find((c) => c.types.includes("locality"));
            const admin = results[0].address_components.find((c) =>
              c.types.includes("administrative_area_level_2") || c.types.includes("administrative_area_level_1")
            );
            const rawNbhd = sub?.long_name || "";
            const neighborhood = isUsableNeighborhood(rawNbhd) ? rawNbhd : "";
            displayCity = locality?.long_name || admin?.long_name || "";
            const fullArea = neighborhood ? `${neighborhood}, ${displayCity}` : displayCity;
            if (fullArea) {
              setInputValue(`${baseQuery} in ${fullArea}`);
              setQuery(`${baseQuery} in ${fullArea}`);
              setDetectedCity(displayCity);
            }
          }
          resolve();
        });
      });
    } catch (_) { /* fall through — still search with coordinates */ }

    setShowSearchArea(false);
    hasSearchedRef.current = true;
    const span = traceSearch({ query: baseQuery, userId });
    try {
      await search(baseQuery, displayCity || undefined, areaCoords);
    } finally {
      span.finish();
    }
    setAiSuggestions(generateFollowUps(baseQuery, activeCategory));
  }, [inputValue, search, userId, activeCategory]);

  // Geocode the typed address, pan the map, then search that area for venues.
  const handleAddressGoAndSearch = useCallback(async () => {
    const addressText = addressInputRef.current?.value?.trim() || "";
    if (!addressText || !mapInstanceRef.current) return;

    addressInputRef.current?.blur();

    const rawQ = inputValue || CATEGORIES[activeCategory].defaultQuery || "places near me";
    const baseQuery = rawQ
      .replace(/\s*(near me|near here|nearby|of me|within\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty)\s*(mile|km|mi)\w*|around me|around here|close to me|in my area)\s*/gi, " ")
      .replace(/\s+in\s+[^,]+(,\s*[^,]+)*$/i, "")
      .trim();

    const geocoder = new google.maps.Geocoder();
    geocoder.geocode({ address: addressText }, async (results, status) => {
      if (status !== "OK" || !results?.[0]?.geometry?.location) {
        // Geocoding failed — pass the typed text as a city string fallback
        setInputValue(`${baseQuery} in ${addressText}`);
        setQuery(`${baseQuery} in ${addressText}`);
        hasSearchedRef.current = true;
        const fallbackQuery = `${baseQuery} in ${addressText}`;
        const span = traceSearch({ query: fallbackQuery, userId });
        try { await search(fallbackQuery, addressText); } finally { span.finish(); }
        setAiSuggestions(generateFollowUps(baseQuery, activeCategory));
        return;
      }

      const loc = results[0].geometry.location;
      const lat = loc.lat();
      const lng = loc.lng();

      mapInstanceRef.current!.panTo({ lat, lng });
      mapInstanceRef.current!.setZoom(14);

      const components = results[0].address_components;
      const rawNbhd = components.find((c: google.maps.GeocoderAddressComponent) =>
        c.types.includes("neighborhood") || c.types.includes("sublocality_level_1")
      )?.long_name || "";
      const neighborhood = isUsableNeighborhood(rawNbhd) ? rawNbhd : "";
      const locality = components.find((c: google.maps.GeocoderAddressComponent) =>
        c.types.includes("locality")
      )?.long_name || "";
      const admin = components.find((c: google.maps.GeocoderAddressComponent) =>
        c.types.includes("administrative_area_level_2") || c.types.includes("administrative_area_level_1")
      )?.long_name || "";
      const displayCity = locality || admin || addressText;
      const displayArea = neighborhood ? `${neighborhood}, ${displayCity}` : displayCity;

      setDetectedCity(displayCity);
      setInputValue(`${baseQuery} in ${displayArea}`);
      setQuery(`${baseQuery} in ${displayArea}`);
      setShowSearchArea(false);
      hasSearchedRef.current = true;

      // Clear the address bar now that the map has panned and the search fired
      if (addressInputRef.current) addressInputRef.current.value = "";

      const areaCoords = { lat, lng, radiusM: 10000 };
      // Send the full "best pancakes in North Caldwell" query (not just "best pancakes")
      // so the intent parser extracts the correct city for scraper query building.
      const fullQuery = `${baseQuery} in ${displayArea}`;
      const span = traceSearch({ query: fullQuery, userId });
      try {
        await search(fullQuery, displayCity, areaCoords);
      } finally {
        span.finish();
      }
      setAiSuggestions(generateFollowUps(baseQuery, activeCategory));
    });
  }, [inputValue, activeCategory, search, userId]);

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
        ? `${fmt(state.intent.occasion)} · ${state.intent.city}${state.intent.group_size > 1 ? ` · ${state.intent.group_size} people` : ""}`
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
        @keyframes spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50%       { opacity: 0.4; transform: scale(0.75); }
        }
        @keyframes pinRing {
          0%   { transform: scale(1);   opacity: 0.7; }
          70%  { transform: scale(1.5); opacity: 0;   }
          100% { transform: scale(1.5); opacity: 0;   }
        }
        /* Google Places Autocomplete dropdown — dark modern theme */
        .pac-container {
          background: #1E293B !important;
          border: 1.5px solid #3B82F6 !important;
          border-radius: 12px !important;
          box-shadow: 0 8px 32px rgba(0,0,0,0.5), 0 2px 8px rgba(37,99,235,0.25) !important;
          margin-top: 4px !important;
          font-family: system-ui, -apple-system, sans-serif !important;
          overflow: hidden !important;
          padding: 4px 0 !important;
          min-width: 480px !important;
          width: max-content !important;
          max-width: 640px !important;
        }
        .pac-item {
          background: transparent !important;
          color: #E2E8F0 !important;
          font-size: 13px !important;
          padding: 10px 14px !important;
          border-top: none !important;
          cursor: pointer !important;
          display: flex !important;
          align-items: center !important;
          gap: 8px !important;
          transition: background 0.12s ease !important;
          white-space: nowrap !important;
        }
        .pac-item:hover, .pac-item-selected {
          background: rgba(59,130,246,0.18) !important;
        }
        .pac-item-query {
          color: #F1F5F9 !important;
          font-weight: 600 !important;
          font-size: 13px !important;
        }
        .pac-matched {
          color: #60A5FA !important;
          font-weight: 700 !important;
        }
        .pac-icon {
          display: none !important;
        }
        .pac-logo::after {
          display: none !important;
        }
        .pac-container:after {
          display: none !important;
        }
      `}</style>

      {/* ── Map canvas (shifts right when panel is open) ── */}
      <div ref={mapRef} style={{
        position: "absolute", top: 0, bottom: 0,
        left: showLeftPanel ? leftPanelW : 0,
        right: 0,
        transition: "left 0.3s ease",
      }} />

      {/* ── Zoom +/- buttons ── */}
      <div style={{
        position: "absolute", bottom: 32, left: showLeftPanel ? leftPanelW + 12 : 12,
        transition: "left 0.3s ease",
        zIndex: 20, display: "flex", flexDirection: "column", gap: 4,
      }}>
        {([{ label: "+", delta: 1 }, { label: "−", delta: -1 }] as const).map(({ label, delta }) => (
          <button
            key={label}
            onClick={() => {
              const z = mapInstanceRef.current?.getZoom();
              if (z !== undefined) mapInstanceRef.current?.setZoom(z + delta);
            }}
            style={{
              width: 36, height: 36, borderRadius: 10,
              background: "rgba(15,23,42,0.85)", backdropFilter: "blur(8px)",
              border: "1px solid rgba(255,255,255,0.15)",
              color: "#E2E8F0", fontSize: 20, fontWeight: 300,
              cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
              boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
              lineHeight: 1,
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {/* ── Loading banner over map ── */}
      {state.status === "searching" && (
        <div style={{
          position: "absolute",
          top: "50%",
          left: showLeftPanel ? leftPanelW : 0,
          right: 0,
          transform: "translateY(-50%)",
          display: "flex",
          justifyContent: "center",
          pointerEvents: "none",
          zIndex: 20,
          transition: "left 0.3s ease",
        }}>
          <div style={{
            display: "inline-flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 6,
            padding: "12px 24px",
            borderRadius: 24,
            background: "rgba(15,23,42,0.88)",
            border: "1px solid rgba(99,179,237,0.35)",
            boxShadow: "0 4px 24px rgba(0,0,0,0.4)",
            backdropFilter: "blur(12px)",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{
                width: 8, height: 8, borderRadius: "50%",
                background: "#60A5FA",
                display: "inline-block",
                animation: "pulse 1.4s ease-in-out infinite",
              }} />
              <span style={{
                fontSize: 13,
                fontWeight: 600,
                color: "#E2E8F0",
                letterSpacing: "0.01em",
              }}>
                Generating Your Place Recommendations…
              </span>
            </div>
            <span style={{
              fontSize: 11,
              color: "#60A5FA",
              fontWeight: 500,
              letterSpacing: "0.02em",
            }}>
              View AI Agent Progress in Side Panel
            </span>
          </div>
        </div>
      )}

      {/* ── Address search + Search This Area bar (always visible once map is ready) ── */}
      {/* Keep this mounted at all times so the Google Places Autocomplete widget
          stays bound to the input element. Hiding with display:none during search
          instead of conditional rendering prevents the DOM node from being recreated
          and losing its Autocomplete binding on the second input. */}
      {mapsReady && (
        <div style={{
          position: "absolute",
          top: 172,
          left: showLeftPanel ? leftPanelW : 0,
          right: 0,
          display: state.status === "searching" ? "none" : "flex",
          justifyContent: "center",
          alignItems: "flex-end",
          gap: 8,
          zIndex: 12,
          pointerEvents: "none",
          transition: "left 0.3s ease",
          padding: "0 16px",
        }}>
          {/* Address / area lookup — Google Places Autocomplete */}
          <div style={{
            display: "flex", flexDirection: "column", gap: 3,
            pointerEvents: "auto",
            flex: "0 1 320px",
          }}>
            <span style={{
              fontSize: 10, fontWeight: 800, letterSpacing: "0.1em",
              color: "#93C5FD", textTransform: "uppercase",
              paddingLeft: 14, userSelect: "none",
              textShadow: "0 0 8px rgba(147,197,253,0.6)",
            }}>
              📍 Go to location
            </span>
            <div style={{
              display: "flex", alignItems: "center", gap: 8,
              background: "rgba(255,255,255,0.97)",
              border: "2px solid #3B82F6",
              borderRadius: 24, padding: "10px 16px",
              boxShadow: "0 0 0 4px rgba(59,130,246,0.18), 0 4px 20px rgba(0,0,0,0.35)",
            }}>
              <span style={{ fontSize: 15, flexShrink: 0 }}>🔍</span>
              <input
                ref={addressInputRef}
                defaultValue=""
                onInput={(e) => setHasAddressText(!!(e.target as HTMLInputElement).value)}
                onKeyDown={(e) => { if (e.key === "Enter") handleAddressGoAndSearch(); }}
                placeholder="Address, neighborhood, or city…"
                style={{
                  flex: 1, minWidth: 0,
                  background: "transparent", border: "none", outline: "none",
                  fontSize: 13, fontWeight: 600, color: "#1E293B",
                  caretColor: "#3B82F6",
                }}
              />
              {hasAddressText && (
                <button
                  type="button"
                  onClick={() => {
                    if (addressInputRef.current) addressInputRef.current.value = "";
                    setHasAddressText(false);
                  }}
                  style={{ background: "none", border: "none", color: "#94A3B8", cursor: "pointer", fontSize: 17, padding: 0, lineHeight: 1, flexShrink: 0 }}
                >×</button>
              )}
            </div>
          </div>

          {/* Go To This Area & Search */}
          <button
            onClick={handleAddressGoAndSearch}
            disabled={!hasAddressText}
            style={{
              pointerEvents: "auto",
              display: "inline-flex", alignItems: "center", gap: 8,
              padding: "11px 22px", borderRadius: 24,
              background: hasAddressText
                ? "linear-gradient(135deg, #2563EB, #1D4ED8)"
                : "rgba(37,99,235,0.35)",
              border: `1.5px solid ${hasAddressText ? "#3B82F6" : "rgba(59,130,246,0.3)"}`,
              color: "#fff", fontSize: 13, fontWeight: 800,
              cursor: hasAddressText ? "pointer" : "default",
              whiteSpace: "nowrap", flexShrink: 0,
              boxShadow: hasAddressText
                ? "0 4px 22px rgba(37,99,235,0.6), 0 2px 8px rgba(0,0,0,0.4)"
                : "none",
              letterSpacing: "0.02em",
              opacity: hasAddressText ? 1 : 0.55,
              transition: "all 0.18s ease",
            }}
          >
            <span style={{ fontSize: 15 }}>📍</span>
            Go To This Area &amp; Search
          </button>
        </div>
      )}

      {/* ── Nearby Transit toggle ── */}
      <div style={{
        position: "absolute", bottom: 175, right: 16, zIndex: 12,
        display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-end",
      }}>
        <button
          onClick={() => {
            if (transitLoading) return;
            if (showTransit) {
              setShowTransit(false);
              transitCenterRef.current = null; // reset so next click re-fetches
            } else {
              fetchTransit(); // always fetch fresh for current map center
            }
          }}
          style={{
            display: "inline-flex", alignItems: "center", gap: 7,
            padding: "9px 16px", borderRadius: 22,
            background: showTransit
              ? "linear-gradient(135deg, #0EA5E9, #6366F1)"
              : "rgba(15,23,42,0.92)",
            border: showTransit
              ? "1.5px solid rgba(99,102,241,0.7)"
              : "1.5px solid rgba(255,255,255,0.15)",
            color: "#fff", fontSize: 13, fontWeight: 700,
            cursor: transitLoading ? "wait" : "pointer", whiteSpace: "nowrap",
            boxShadow: showTransit ? "0 4px 16px rgba(99,102,241,0.45)" : "0 4px 16px rgba(0,0,0,0.45)",
            backdropFilter: "blur(8px)",
            transition: "all 0.2s",
          }}
        >
          {transitLoading ? "⏳" : "🚇"}
          {transitLoading
            ? " Searching this area…"
            : showTransit
              ? ` ${transitStops.length} stops · Hide`
              : " Nearby Transit"}
        </button>

        {/* Loading card — shown while fetching */}
        {transitLoading && (
          <div style={{
            background: "rgba(15,23,42,0.96)", backdropFilter: "blur(12px)",
            border: "1px solid rgba(255,255,255,0.12)", borderRadius: 14,
            padding: "16px 18px", width: 240,
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            display: "flex", flexDirection: "column", gap: 10,
          }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#E2E8F0" }}>
              Finding nearby transit…
            </div>
            {/* Animated skeleton rows */}
            {[90, 75, 82].map((w, i) => (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div style={{
                  width: 28, height: 28, borderRadius: "50%", flexShrink: 0,
                  background: "rgba(255,255,255,0.08)",
                  animation: `pulse 1.4s ease-in-out ${i * 0.15}s infinite`,
                }} />
                <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 5 }}>
                  <div style={{
                    height: 10, borderRadius: 5, width: `${w}%`,
                    background: "rgba(255,255,255,0.08)",
                    animation: `pulse 1.4s ease-in-out ${i * 0.15}s infinite`,
                  }} />
                  <div style={{
                    height: 8, borderRadius: 4, width: "50%",
                    background: "rgba(255,255,255,0.05)",
                    animation: `pulse 1.4s ease-in-out ${i * 0.15 + 0.1}s infinite`,
                  }} />
                </div>
              </div>
            ))}
          </div>
        )}

        {showTransit && transitStops.length > 0 && (
          <div style={{
            background: "rgba(15,23,42,0.95)", backdropFilter: "blur(12px)",
            border: "1px solid rgba(255,255,255,0.1)", borderRadius: 14,
            padding: "10px 14px", maxWidth: 260, maxHeight: 280, overflowY: "auto",
            scrollbarWidth: "thin" as React.CSSProperties["scrollbarWidth"],
            scrollBehavior: "smooth",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
              Nearby Transit
            </div>
            {transitStops.map((stop, idx) => {
              const icon  = TRANSIT_ICON[stop.transit_type]  ?? "🚏";
              const color = TRANSIT_COLOR[stop.transit_type] ?? "#94A3B8";
              const userLoc = userLocationRef.current;
              const distM = userLoc
                ? Math.round(haversineKm(userLoc.lat, userLoc.lng, stop.latitude, stop.longitude) * 1000)
                : null;
              return (
                <button
                  key={stop.place_id || idx}
                  onClick={() => {
                    const marker = transitMarkersRef.current[idx];
                    openTransitInfo(stop, marker);
                  }}
                  style={{
                    display: "flex", alignItems: "flex-start", gap: 8,
                    padding: "7px 4px", width: "100%", textAlign: "left",
                    borderBottom: "1px solid rgba(255,255,255,0.06)",
                    background: "none", border: "none", cursor: "pointer",
                    borderRadius: 6, transition: "background 0.12s",
                  }}
                  onMouseEnter={e => (e.currentTarget.style.background = "rgba(255,255,255,0.05)")}
                  onMouseLeave={e => (e.currentTarget.style.background = "none")}
                >
                  <span style={{ fontSize: 16, flexShrink: 0, marginTop: 1 }}>{icon}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color, lineHeight: 1.3 }}>{stop.name}</div>
                    <div style={{ fontSize: 10, color: "#475569", marginTop: 2 }}>{stop.address.split(",")[0]}</div>
                  </div>
                  {distM != null && (
                    <span style={{ fontSize: 10, color: "#64748B", flexShrink: 0, marginTop: 2 }}>
                      {distM < 1000 ? `${distM}m` : `${(distM/1000).toFixed(1)}km`}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>

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
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "7px 13px", borderRadius: 10,
                  background: "linear-gradient(135deg, rgba(239,68,68,0.2), rgba(220,38,38,0.15))",
                  border: "1.5px solid rgba(239,68,68,0.4)",
                  color: "#FCA5A5",
                  fontSize: 12, fontWeight: 700, cursor: "pointer",
                  whiteSpace: "nowrap",
                  flexShrink: 0,
                  boxShadow: "0 2px 8px rgba(239,68,68,0.2)",
                  transition: "all 0.15s",
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.background = "linear-gradient(135deg, rgba(239,68,68,0.35), rgba(220,38,38,0.3))";
                  (e.currentTarget as HTMLButtonElement).style.color = "#fff";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.background = "linear-gradient(135deg, rgba(239,68,68,0.2), rgba(220,38,38,0.15))";
                  (e.currentTarget as HTMLButtonElement).style.color = "#FCA5A5";
                }}
              >
                <span style={{ fontSize: 12 }}>◀</span>
                <span>Close Panel</span>
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

          {/* Agent steps — only while searching */}
          {state.status === "searching" && (
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
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "2px 4px 10px" }}>
                <span style={{
                  display: "inline-flex", alignItems: "center", gap: 8,
                  fontSize: 15, fontWeight: 800, color: "#F1F5F9",
                  letterSpacing: "-0.01em",
                }}>
                  <span style={{
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    minWidth: 30, height: 30, borderRadius: "50%",
                    background: "rgba(52,211,153,0.22)",
                    border: "1.5px solid rgba(52,211,153,0.55)",
                    boxShadow: "0 0 10px rgba(52,211,153,0.3)",
                    color: "#fff", fontSize: 14, fontWeight: 900,
                    padding: "0 6px",
                  }}>
                    {state.venues.length}
                  </span>
                  <span style={{ color: "#F1F5F9" }}>Matches</span>
                </span>
                <button
                  onClick={() => { setModalQuery(""); setShowAllMatches(true); }}
                  style={{
                    padding: "7px 16px", borderRadius: 20,
                    border: "1.5px solid rgba(167,139,250,0.7)",
                    background: "linear-gradient(135deg, #1D4ED8, #7C3AED)",
                    color: "#fff", fontSize: 12, fontWeight: 800, cursor: "pointer",
                    letterSpacing: "0.04em",
                    boxShadow: "0 0 16px rgba(124,58,237,0.6), 0 2px 8px rgba(0,0,0,0.4)",
                    textShadow: "0 1px 2px rgba(0,0,0,0.3)",
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

                      {/* Tags + directions row */}
                      <div style={{ display: "flex", gap: 4, marginTop: 8, flexWrap: "wrap", alignItems: "center" }}>
                        {venue.has_private_room && (
                          <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(16,185,129,0.15)", color: "#34D399", border: "1px solid rgba(16,185,129,0.25)" }}>🚪 Private</span>
                        )}
                        {venue.price_per_head > 0 && (
                          <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(251,191,36,0.12)", color: "#FCD34D", border: "1px solid rgba(251,191,36,0.2)" }}>${venue.price_per_head}/head</span>
                        )}
                        {venue.intelligence?.why_card && (
                          <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(139,92,246,0.15)", color: "#C4B5FD", border: "1px solid rgba(139,92,246,0.25)" }}>✨ AI</span>
                        )}
                        {venue.name && (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              selectVenue(venue.venue_id);
                              setSidebarOpen(true);
                              fetchPlaceDetails(venue.venue_id).then(setSelectedPlaceDetails);
                              onVenueSelect?.(venue);
                              getDirections(venue, directionsTravelMode);
                            }}
                            style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(52,211,153,0.12)", color: "#34D399", border: "1px solid rgba(52,211,153,0.25)", cursor: "pointer", marginLeft: "auto" }}>
                            🗺️ Directions
                          </button>
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
                    width: "100%", padding: "11px", borderRadius: 12, marginTop: 4,
                    border: "1.5px solid rgba(124,58,237,0.5)",
                    background: "linear-gradient(135deg, rgba(29,78,216,0.22), rgba(124,58,237,0.22))",
                    color: "#C4B5FD", fontSize: 12, fontWeight: 700, cursor: "pointer",
                    letterSpacing: "0.03em",
                    boxShadow: "0 0 14px rgba(124,58,237,0.25), inset 0 0 12px rgba(99,102,241,0.08)",
                  }}
                >
                  + {state.venues.length - 5} more matches — View All ↗
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
                display: "inline-flex", alignItems: "center", gap: 6,
                padding: "7px 14px", borderRadius: 20,
                background: "linear-gradient(135deg, #1D4ED8, #7C3AED)",
                border: "1.5px solid rgba(139,92,246,0.5)",
                color: "#fff",
                fontSize: 12, fontWeight: 700, cursor: "pointer",
                boxShadow: "0 2px 12px rgba(124,58,237,0.45)",
                letterSpacing: "0.02em",
                transition: "all 0.2s",
              }}
            >
              <span style={{ fontSize: 13 }}>✦</span>
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
              ref={searchInputRef}
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
        const intent = state.intent!;
        // When the search was GPS-anchored, use the reverse-geocoded city name instead of the
        // LLM-parsed intent city (which defaults to "New York City" when no city is in the query).
        const city = searchWasGpsAnchored ? detectedCity : intent.city;
        const occasion = intent.occasion?.replace(/_/g, " ") || "dining";
        const cuisine = intent.cuisine || "restaurant";
        const n = intent.group_size;

        const chips = [
          {
            val: city, icon: "📍", label: city, bg: "#2563EB", shadow: "rgba(37,99,235,0.5)",
            hint: "Click to search in a different city",
            refinement: `${occasion} in `,
          },
          {
            val: intent.occasion, icon: "🎉", label: fmt(occasion), bg: "#7C3AED", shadow: "rgba(124,58,237,0.5)",
            hint: "Click to change the occasion or activity",
            refinement: `${occasion} in ${city} for ${n} people`,
          },
          {
            val: intent.cuisine, icon: "🍽️", label: cuisine, bg: "#B45309", shadow: "rgba(180,83,9,0.5)",
            hint: "Click to change cuisine type",
            refinement: `${occasion} ${cuisine} in ${city} for ${n} people`,
          },
          {
            val: intent.group_size > 1, icon: "👥", label: `${n} people`, bg: "#047857", shadow: "rgba(4,120,87,0.5)",
            hint: "Click to change group size",
            refinement: `${occasion} in ${city} for `,
          },
          {
            val: intent.needs_private_room, icon: "🚪", label: "private room", bg: "#0E7490", shadow: "rgba(14,116,144,0.5)",
            hint: "Click to search without private room",
            refinement: `${occasion} in ${city} for ${n} people no private room needed`,
          },
          {
            val: intent.noise_preference, icon: "🔊", label: fmt(intent.noise_preference), bg: "#BE185D", shadow: "rgba(190,24,93,0.5)",
            hint: "Click to change noise preference",
            refinement: `${occasion} in ${city} for ${n} people ${fmt(intent.noise_preference)} atmosphere`,
          },
          {
            val: intent.price_band, icon: "💎", label: fmt(intent.price_band), bg: "#4D7C0F", shadow: "rgba(77,124,15,0.5)",
            hint: "Click to change price range",
            refinement: `${occasion} in ${city} for ${n} people ${fmt(intent.price_band)} budget`,
          },
        ].filter(c => c.val);

        return chips.length === 0 ? null : (
          <div style={{
            position: "absolute",
            top: (mapsReady && state.status !== "searching") ? 252 : 188,
            left: showLeftPanel ? leftPanelW + 16 : 16,
            right: 16,
            zIndex: 10,
            display: "flex", gap: 8, flexWrap: "wrap",
            transition: "top 0.2s ease, left 0.3s ease",
            pointerEvents: "none",
          }}>
            {chips.map((chip, idx) => (
              <button
                key={String(chip.label)}
                title={chip.hint}
                onClick={() => {
                  setInputValue(chip.refinement);
                  searchInputRef.current?.focus();
                  // Place cursor at end
                  setTimeout(() => {
                    const el = searchInputRef.current;
                    if (el) el.setSelectionRange(el.value.length, el.value.length);
                  }, 0);
                }}
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
                  cursor: "pointer",
                  border: "none",
                  outline: "none",
                  transition: "filter 0.15s, transform 0.15s",
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.filter = "brightness(1.2)";
                  (e.currentTarget as HTMLButtonElement).style.transform = "scale(1.05)";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.filter = "brightness(1)";
                  (e.currentTarget as HTMLButtonElement).style.transform = "scale(1)";
                }}
              >
                <span style={{ fontSize: 14 }}>{chip.icon}</span>
                <span style={{ textTransform: "capitalize" }}>{chip.label}</span>
                <span style={{ fontSize: 10, opacity: 0.75, marginLeft: 1 }}>✎</span>
              </button>
            ))}
          </div>
        );
      })()}

      {/* ── Locate-me button ── */}
      {mapsReady && (
        <button
          title="Go to my current location"
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
            height: 40, borderRadius: 20,
            padding: "0 14px 0 10px",
            background: "linear-gradient(135deg, #1D4ED8, #2563EB)",
            backdropFilter: "blur(12px)",
            border: "1.5px solid rgba(96,165,250,0.5)",
            boxShadow: "0 4px 16px rgba(37,99,235,0.5)",
            cursor: "pointer",
            display: "flex", alignItems: "center", gap: 6,
            color: "#fff", fontSize: 12, fontWeight: 700,
            whiteSpace: "nowrap",
            transition: "all 0.15s",
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "linear-gradient(135deg, #1E40AF, #1D4ED8)";
            (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 4px 20px rgba(37,99,235,0.7)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "linear-gradient(135deg, #1D4ED8, #2563EB)";
            (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 4px 16px rgba(37,99,235,0.5)";
          }}
        >
          <span style={{ fontSize: 16 }}>📍</span>
          <span>My Location</span>
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
            width: "100%", maxWidth: 1100, maxHeight: "92vh",
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
            {(() => {
              const q = modalQuery.trim().toLowerCase();
              // Deduplicate by name (ClickHouse may store same venue twice before FINAL merge)
              const seen = new Set<string>();
              const dedupedVenues = state.venues.filter((v) => {
                const key = v.name.toLowerCase().trim();
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
              });
              // Filter only against the displayed address (first 2 comma-parts) to avoid
              // matching hidden geocoder segments (zip codes, country, etc.)
              const displayedVenues = dedupedVenues.filter((v) => {
                if (!q) return true;
                const displayAddr = (v.address ?? "").split(",").slice(0, 2).join(",").toLowerCase();
                return v.name.toLowerCase().includes(q) || displayAddr.includes(q);
              });
              return (
            <div style={{
              flex: 1, minHeight: 0, overflowY: "auto", padding: "16px 20px 20px",
            scrollBehavior: "smooth",
            scrollbarWidth: "thin" as React.CSSProperties["scrollbarWidth"],
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
              gridAutoRows: 110,
              gap: 14,
            }}>
              {displayedVenues.map((venue, idx) => {
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
                        height: "100%", display: "flex", flexDirection: "column",
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
                        borderRadius: "14px 14px 0 0",
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
                        <div style={{
                          fontSize: 14, fontWeight: 700, color: venue.name ? "#F1F5F9" : "#475569",
                          marginBottom: 3, lineHeight: "18px", height: 36,
                          overflow: "hidden", fontStyle: venue.name ? "normal" : "italic",
                        }}>
                          {venue.name || "Unnamed venue"}
                        </div>

                        {/* Address */}
                        {venue.address && (
                          <div style={{
                            fontSize: 11, color: "#64748B", lineHeight: "16px",
                            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                          }}>
                            {venue.address.split(",").slice(0, 2).join(",")}
                          </div>
                        )}

                        {/* Inline tags — price and private room only if known */}
                        {(venue.has_private_room || venue.price_per_head > 0) && (
                          <div style={{ display: "flex", gap: 4, marginTop: 6, flexWrap: "wrap" }}>
                            {venue.has_private_room && (
                              <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(16,185,129,0.15)", color: "#34D399", border: "1px solid rgba(16,185,129,0.25)" }}>🚪 Private</span>
                            )}
                            {venue.price_per_head > 0 && (
                              <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 20, background: "rgba(251,191,36,0.12)", color: "#FCD34D", border: "1px solid rgba(251,191,36,0.2)" }}>${venue.price_per_head}/head</span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}

              {/* Empty state */}
              {displayedVenues.length === 0 && (
                <div style={{ gridColumn: "1/-1", textAlign: "center", padding: "40px 20px", color: "#475569" }}>
                  No venues match "{modalQuery}"
                </div>
              )}
            </div>
              );
            })()}
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
            clearDirections();
          }}
          onGetDirections={getDirections}
          onClearDirections={clearDirections}
          onSelectRoute={selectRoute}
          directionsTravelMode={directionsTravelMode}
          onSetTravelMode={setDirectionsTravelMode}
          directionsLeg={directionsLeg}
          directionsLoading={directionsLoading}
          directionsError={directionsError}
          routeOptions={routeOptions}
          selectedRouteIndex={selectedRouteIndex}
          userLocation={userLocationRef.current}
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
  const score = Math.round(marker.match_score);
  // Score colour: green ≥80, amber 60-79, gray <60
  const scoreColor = score >= 80 ? "#137333" : score >= 60 ? "#B06000" : "#5F6368";
  const scoreBg    = score >= 80 ? "#E6F4EA" : score >= 60 ? "#FEF3DC" : "#F1F3F4";

  // Rating rendered with a filled star matching Google Maps yellow
  const ratingHtml = details?.rating
    ? `<span style="display:inline-flex;align-items:center;gap:2px">
         <span style="color:#F4B400;font-size:13px;line-height:1">★</span>
         <span style="font-weight:600;color:#202124">${details.rating}</span>
         ${details.user_rating_count ? `<span style="color:#70757A">(${details.user_rating_count.toLocaleString()})</span>` : ""}
       </span>`
    : "";

  const openHtml = details?.is_open_now === true
    ? `<span style="color:#137333;font-weight:500">Open</span>`
    : details?.is_open_now === false
    ? `<span style="color:#C5221F;font-weight:500">Closed</span>`
    : "";

  const metaRow = [ratingHtml, openHtml].filter(Boolean).join(`<span style="color:#DADCE0;margin:0 4px">·</span>`);

  const chips: string[] = [];
  if (marker.price_per_head) chips.push(`~$${marker.price_per_head}/head`);
  if (marker.has_private_room) chips.push("Private room");

  const photoHtml = details?.photo_url
    ? `<img src="${details.photo_url}" alt="${escapeHtml(marker.name)}"
         style="width:100%;height:110px;object-fit:cover;border-radius:8px;display:block;margin-bottom:10px" />`
    : "";

  return `
    <div style="font-family:'Google Sans',Roboto,Arial,sans-serif;min-width:190px;max-width:250px;padding:2px 0">
      ${photoHtml}
      <div style="font-size:15px;font-weight:600;color:#202124;line-height:1.35;margin-bottom:${metaRow ? 3 : 8}px;letter-spacing:-0.1px">
        ${escapeHtml(marker.name)}
      </div>
      ${metaRow ? `<div style="font-size:12px;color:#70757A;margin-bottom:9px;display:flex;align-items:center;gap:0;flex-wrap:wrap">${metaRow}</div>` : ""}
      <div style="display:flex;gap:5px;flex-wrap:wrap;align-items:center">
        <span style="background:${scoreBg};color:${scoreColor};font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;letter-spacing:0.1px">
          ${score}% match
        </span>
        ${chips.map(c =>
          `<span style="background:#F1F3F4;color:#3C4043;font-size:11px;padding:3px 9px;border-radius:20px">${escapeHtml(c)}</span>`
        ).join("")}
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

// ─── Custom flight date picker ────────────────────────────────────────────

const _MONTHS = ["January","February","March","April","May","June",
                 "July","August","September","October","November","December"];
const _DOW = ["Su","Mo","Tu","We","Th","Fr","Sa"];

function FlightDatePicker({ value, min, onChange }: {
  value: string; min: string; onChange: (v: string) => void;
}) {
  const parse = (s: string) => { const [y,m,d] = s.split("-").map(Number); return {y, m: m-1, d}; };
  const sel = parse(value);
  const minP = parse(min);
  const [open, setOpen] = useState(false);
  const [vm, setVm] = useState(sel.m);
  const [vy, setVy] = useState(sel.y);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const isDisabled = (y: number, m: number, d: number) =>
    y < minP.y || (y === minP.y && m < minP.m) || (y === minP.y && m === minP.m && d < minP.d);
  const isSelected = (y: number, m: number, d: number) => y === sel.y && m === sel.m && d === sel.d;
  const isToday = (y: number, m: number, d: number) => {
    const t = new Date(); return y === t.getFullYear() && m === t.getMonth() && d === t.getDate();
  };
  const select = (y: number, m: number, d: number) => {
    if (isDisabled(y, m, d)) return;
    onChange(`${y}-${String(m+1).padStart(2,"0")}-${String(d).padStart(2,"0")}`);
    setOpen(false);
  };
  const prevMonth = () => vm === 0 ? (setVm(11), setVy(vy-1)) : setVm(vm-1);
  const nextMonth = () => vm === 11 ? (setVm(0), setVy(vy+1)) : setVm(vm+1);

  const firstDow = new Date(vy, vm, 1).getDay();
  const daysInMonth = new Date(vy, vm+1, 0).getDate();
  const daysInPrev = new Date(vy, vm, 0).getDate();
  const cells: {y:number;m:number;d:number;cur:boolean}[] = [];
  for (let i = firstDow-1; i >= 0; i--)
    cells.push({y: vm===0?vy-1:vy, m: vm===0?11:vm-1, d: daysInPrev-i, cur: false});
  for (let d = 1; d <= daysInMonth; d++)
    cells.push({y: vy, m: vm, d, cur: true});
  let nx = 1;
  while (cells.length % 7 !== 0)
    cells.push({y: vm===11?vy+1:vy, m: vm===11?0:vm+1, d: nx++, cur: false});

  const displayDate = value
    ? new Date(value + "T12:00:00").toLocaleDateString("en-US", {month:"short", day:"numeric", year:"numeric"})
    : "Select date";

  return (
    <div ref={ref} style={{position:"relative", flex:1}}>
      <button onClick={() => setOpen(o => !o)} style={{
        width:"100%", display:"flex", alignItems:"center", justifyContent:"space-between",
        background:"rgba(0,0,0,0.35)", border:`1px solid ${open ? "rgba(167,139,250,0.7)" : "rgba(167,139,250,0.4)"}`,
        borderRadius:8, padding:"6px 10px", color:"#E2E8F0", fontSize:12, cursor:"pointer", outline:"none",
        transition:"border-color 0.15s",
      }}>
        <span>{displayDate}</span>
        <span style={{color:"#A78BFA", fontSize:11, marginLeft:4}}>▾</span>
      </button>

      {open && (
        <div style={{
          position:"absolute", top:"calc(100% + 5px)", left:0, zIndex:10000, minWidth:260,
          background:"#0F1929", border:"1.5px solid rgba(167,139,250,0.45)",
          borderRadius:14, padding:"14px 12px 12px",
          boxShadow:"0 12px 40px rgba(0,0,0,0.7), 0 0 0 1px rgba(167,139,250,0.1)",
        }}>
          {/* Month / year nav */}
          <div style={{display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:12}}>
            <button onClick={prevMonth} style={{background:"rgba(167,139,250,0.1)", border:"1px solid rgba(167,139,250,0.25)", borderRadius:7, color:"#A78BFA", cursor:"pointer", fontSize:16, width:28, height:28, display:"flex", alignItems:"center", justifyContent:"center"}}>‹</button>
            <span style={{fontSize:13, fontWeight:700, color:"#E2E8F0", letterSpacing:"0.01em"}}>{_MONTHS[vm]} {vy}</span>
            <button onClick={nextMonth} style={{background:"rgba(167,139,250,0.1)", border:"1px solid rgba(167,139,250,0.25)", borderRadius:7, color:"#A78BFA", cursor:"pointer", fontSize:16, width:28, height:28, display:"flex", alignItems:"center", justifyContent:"center"}}>›</button>
          </div>
          {/* Day-of-week headers */}
          <div style={{display:"grid", gridTemplateColumns:"repeat(7,1fr)", marginBottom:4}}>
            {_DOW.map(d => <div key={d} style={{textAlign:"center", fontSize:10, fontWeight:700, color:"#475569", padding:"2px 0"}}>{d}</div>)}
          </div>
          {/* Day cells */}
          <div style={{display:"grid", gridTemplateColumns:"repeat(7,1fr)", gap:2}}>
            {cells.map((c, i) => {
              const dis = isDisabled(c.y, c.m, c.d);
              const sel2 = isSelected(c.y, c.m, c.d);
              const tod = isToday(c.y, c.m, c.d);
              return (
                <button key={i} onClick={() => select(c.y, c.m, c.d)} disabled={dis} style={{
                  height:34, border:"none", borderRadius:8, fontSize:12, cursor: dis ? "default" : "pointer",
                  fontWeight: sel2 ? 700 : 400,
                  background: sel2 ? "linear-gradient(135deg,#4F46E5,#7C3AED)" : tod ? "rgba(167,139,250,0.12)" : "transparent",
                  color: sel2 ? "#fff" : dis ? "#1E293B" : !c.cur ? "#334155" : tod ? "#A78BFA" : "#94A3B8",
                  outline: tod && !sel2 ? "1.5px solid rgba(167,139,250,0.5)" : "none",
                  outlineOffset: "-1px",
                  transition:"background 0.1s, color 0.1s",
                }}>{c.d}</button>
              );
            })}
          </div>
          {/* Footer shortcuts */}
          <div style={{display:"flex", justifyContent:"flex-end", marginTop:10, gap:8}}>
            <button onClick={() => { const d = new Date(); d.setDate(d.getDate()+7); select(d.getFullYear(), d.getMonth(), d.getDate()); }} style={{background:"none", border:"none", color:"#7C3AED", fontSize:11, fontWeight:600, cursor:"pointer", padding:"2px 4px"}}>+7 days</button>
            <button onClick={() => { const d = new Date(); d.setMonth(d.getMonth()+1); select(d.getFullYear(), d.getMonth(), d.getDate()); }} style={{background:"none", border:"none", color:"#7C3AED", fontSize:11, fontWeight:600, cursor:"pointer", padding:"2px 4px"}}>+1 month</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Venue detail sidebar ─────────────────────────────────────────────────

interface VenueDetailSidebarProps {
  venue: VenueSignal | null;
  placeDetails: GooglePlaceDetails | null;
  onClose: () => void;
  onGetDirections: (venue: { place_id?: string | null; latitude?: number | null; longitude?: number | null; name: string; address?: string | null }, mode: TravelMode, flightOptions?: { date?: string; depIata?: string; arrIata?: string }) => void;
  userLocation?: { lat: number; lng: number } | null;
  onClearDirections: () => void;
  onSelectRoute: (option: RouteOption) => void;
  directionsTravelMode: TravelMode;
  onSetTravelMode: (m: TravelMode) => void;
  directionsLeg: DirectionsLeg | null;
  directionsLoading: boolean;
  directionsError?: string | null;
  routeOptions: RouteOption[] | null;
  selectedRouteIndex: number | null;
}

function VenueDetailSidebar({ venue, placeDetails, onClose, onGetDirections, onClearDirections, onSelectRoute, directionsTravelMode, onSetTravelMode, directionsLeg, directionsLoading, directionsError, routeOptions, selectedRouteIndex, userLocation }: VenueDetailSidebarProps) {
  const [sidebarW, setSidebarW] = useState(380);
  const [isFullScreen, setIsFullScreen] = useState(false);

  // ── Flight-specific state ──────────────────────────────────────────────────
  const defaultFlightDate = () => {
    const d = new Date(); d.setDate(d.getDate() + 7);
    return d.toISOString().split("T")[0];
  };
  const [flightDate, setFlightDate] = useState<string>(defaultFlightDate);
  const [depAirports, setDepAirports] = useState<{ iata: string; name: string; latitude: number; longitude: number }[]>([]);
  const [arrAirports, setArrAirports] = useState<{ iata: string; name: string; latitude: number; longitude: number }[]>([]);
  const [selectedDepIata, setSelectedDepIata] = useState<string>("");
  const [selectedArrIata, setSelectedArrIata] = useState<string>("");
  const [airportsLoading, setAirportsLoading] = useState(false);
  const [airlineFilter, setAirlineFilter] = useState<Set<string>>(new Set());
  const [driveToAirport, setDriveToAirport] = useState<{ duration: string; distance: string } | null>(null);

  // Reset airports when venue changes
  useEffect(() => {
    setDepAirports([]); setArrAirports([]);
    setSelectedDepIata(""); setSelectedArrIata("");
    setDriveToAirport(null);
  }, [venue?.venue_id]);

  // Fetch nearby airports when FLYING mode is active
  useEffect(() => {
    if (directionsTravelMode !== "FLYING" || !venue) return;
    if (depAirports.length > 0 || arrAirports.length > 0) return;
    setAirportsLoading(true);
    const base = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
    Promise.all([
      userLocation
        ? fetch(`${base}/api/airports/nearby?lat=${userLocation.lat}&lng=${userLocation.lng}&n=4`).then(r => r.ok ? r.json() : [])
        : Promise.resolve([]),
      (venue.latitude && venue.longitude)
        ? fetch(`${base}/api/airports/nearby?lat=${venue.latitude}&lng=${venue.longitude}&n=4`).then(r => r.ok ? r.json() : [])
        : Promise.resolve([]),
    ]).then(([dep, arr]) => {
      setDepAirports(dep ?? []);
      setArrAirports(arr ?? []);
      if (dep?.[0]) setSelectedDepIata(dep[0].iata);
      if (arr?.[0]) setSelectedArrIata(arr[0].iata);
    }).finally(() => setAirportsLoading(false));
  }, [directionsTravelMode, venue, userLocation, depAirports.length, arrAirports.length]);

  // Reset airline filter when results change
  useEffect(() => { setAirlineFilter(new Set()); }, [routeOptions]);

  // Compute drive-to-departure-airport after flight results load
  useEffect(() => {
    if (!routeOptions || routeOptions[0]?.type !== "flight") { setDriveToAirport(null); return; }
    const first = routeOptions[0];
    if (!first.departureLat || !first.departureLng || !userLocation) return;
    if (typeof window === "undefined" || !window.google?.maps) return;
    const svc = new window.google.maps.DirectionsService();
    svc.route(
      { origin: userLocation, destination: { lat: first.departureLat, lng: first.departureLng! }, travelMode: window.google.maps.TravelMode.DRIVING },
      (result, status) => {
        if (status === window.google.maps.DirectionsStatus.OK && result) {
          const leg = result.routes[0]?.legs[0];
          if (leg) setDriveToAirport({ duration: leg.duration?.text ?? "", distance: leg.distance?.text ?? "" });
        }
      },
    );
  }, [routeOptions, userLocation]);

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
        top: "50%",
        left: "50%",
        transform: "translate(-50%, -50%)",
        width: "min(900px, 92vw)",
        maxHeight: "88vh",
        zIndex: 101,
        background: "linear-gradient(160deg, #0f172a 0%, #1a2236 60%, #0f172a 100%)",
        boxShadow: "0 24px 80px rgba(0,0,0,0.7), 0 0 0 1px rgba(99,179,237,0.15)",
        borderRadius: 20,
        display: "flex", flexDirection: "column",
        border: "1px solid rgba(255,255,255,0.1)",
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
    <>
    <style>{`
      .sidebar-scroll {
        scrollbar-width: thin;
        scrollbar-color: rgba(255,255,255,0.15) transparent;
        -webkit-overflow-scrolling: touch;
        scroll-behavior: smooth;
      }
      .sidebar-scroll::-webkit-scrollbar { width: 4px; }
      .sidebar-scroll::-webkit-scrollbar-track { background: transparent; }
      .sidebar-scroll::-webkit-scrollbar-thumb {
        background: rgba(255,255,255,0.15);
        border-radius: 4px;
      }
      .sidebar-scroll::-webkit-scrollbar-thumb:hover {
        background: rgba(255,255,255,0.3);
      }
    `}</style>
    {/* Backdrop when fullscreen */}
    {isFullScreen && (
      <div
        onClick={() => setIsFullScreen(false)}
        style={{
          position: "fixed", inset: 0, zIndex: 100,
          background: "rgba(0,0,0,0.65)",
          backdropFilter: "blur(4px)",
        }}
      />
    )}
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
        padding: "14px 18px 14px",
        borderBottom: "1px solid rgba(255,255,255,0.07)",
        flexShrink: 0,
      }}>
        {/* Buttons row — Full screen | Get Directions | [Exit Full Screen] | Close */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
          {/* Full screen button — only shown when NOT in full screen */}
          {!isFullScreen && (
            <button
              onClick={() => setIsFullScreen(true)}
              title="Expand to full screen"
              style={{
                display: "inline-flex", alignItems: "center", gap: 5,
                padding: "6px 12px", borderRadius: 10, height: 34,
                background: "linear-gradient(135deg, #1D4ED8, #2563EB)",
                border: "1.5px solid rgba(59,130,246,0.5)",
                color: "#fff", cursor: "pointer",
                fontSize: 12, fontWeight: 700,
                boxShadow: "0 2px 12px rgba(37,99,235,0.45)",
                letterSpacing: "0.01em", whiteSpace: "nowrap", transition: "all 0.18s",
              }}
            >
              <span style={{ fontSize: 14, lineHeight: 1 }}>⤢</span>
              <span>Full screen</span>
            </button>
          )}

          {/* Get Directions / Search Flights / Clear */}
          {(directionsLeg || routeOptions) ? (
            <button
              onClick={onClearDirections}
              style={{
                display: "inline-flex", alignItems: "center", gap: 5,
                padding: "6px 12px", borderRadius: 10, height: 34,
                background: "linear-gradient(135deg, #065F46, #047857)",
                border: "1.5px solid rgba(16,185,129,0.5)",
                color: "#fff", cursor: "pointer",
                fontSize: 12, fontWeight: 700,
                boxShadow: "0 2px 12px rgba(16,185,129,0.4)",
                letterSpacing: "0.01em", whiteSpace: "nowrap", transition: "all 0.18s",
              }}
            >
              <span style={{ fontSize: 13 }}>✕</span>
              <span>{directionsTravelMode === "FLYING" ? "Clear Flight" : "Clear Route"}</span>
            </button>
          ) : (
            <button
              onClick={() => onGetDirections(venue, directionsTravelMode,
                directionsTravelMode === "FLYING"
                  ? { date: flightDate, depIata: selectedDepIata || undefined, arrIata: selectedArrIata || undefined }
                  : undefined
              )}
              disabled={directionsLoading}
              style={{
                display: "inline-flex", alignItems: "center", gap: 5,
                padding: "6px 12px", borderRadius: 10, height: 34,
                background: directionsLoading
                  ? "rgba(99,102,241,0.3)"
                  : "linear-gradient(135deg, #4F46E5, #7C3AED)",
                border: "1.5px solid rgba(99,102,241,0.6)",
                color: "#fff", cursor: directionsLoading ? "not-allowed" : "pointer",
                fontSize: 12, fontWeight: 700,
                boxShadow: "0 2px 12px rgba(99,102,241,0.45)",
                letterSpacing: "0.01em", whiteSpace: "nowrap", transition: "all 0.18s",
                opacity: directionsLoading ? 0.7 : 1,
              }}
            >
              <span style={{ fontSize: 13 }}>{directionsLoading ? "⏳" : directionsTravelMode === "FLYING" ? "✈️" : "🗺️"}</span>
              <span>{directionsLoading ? (directionsTravelMode === "FLYING" ? "Searching…" : "Routing…") : directionsTravelMode === "FLYING" ? "Search Flights" : "Get Directions"}</span>
            </button>
          )}

          {/* Exit Full Screen — shown only when in full screen, pushed to the right */}
          {isFullScreen && (
            <button
              onClick={() => setIsFullScreen(false)}
              title="Exit full screen"
              style={{
                marginLeft: "auto",
                display: "inline-flex", alignItems: "center", gap: 5,
                padding: "6px 12px", borderRadius: 10, height: 34,
                background: "linear-gradient(135deg, #7C3AED, #4F46E5)",
                border: "1.5px solid rgba(139,92,246,0.6)",
                color: "#fff", cursor: "pointer",
                fontSize: 12, fontWeight: 700,
                boxShadow: "0 2px 12px rgba(124,58,237,0.5)",
                letterSpacing: "0.01em", whiteSpace: "nowrap", transition: "all 0.18s",
              }}
            >
              <span style={{ fontSize: 14, lineHeight: 1 }}>⤡</span>
              <span>Exit Full Screen</span>
            </button>
          )}

          {/* Close — pushed to the right when not full screen */}
          <button
            onClick={onClose}
            title="Close panel"
            style={{
              marginLeft: isFullScreen ? 0 : "auto",
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "6px 12px", borderRadius: 10, height: 34,
              background: "linear-gradient(135deg, #991B1B, #DC2626)",
              border: "1.5px solid rgba(239,68,68,0.5)",
              color: "#fff", cursor: "pointer",
              fontSize: 12, fontWeight: 700,
              boxShadow: "0 2px 12px rgba(220,38,38,0.4)",
              letterSpacing: "0.01em", whiteSpace: "nowrap", transition: "all 0.18s",
            }}
          >
            <span style={{ fontSize: 14, lineHeight: 1 }}>✕</span>
            <span>Close</span>
          </button>
        </div>

        {/* Travel mode selector */}
        <div style={{
          display: "flex", gap: 3, marginBottom: 10,
          background: "rgba(0,0,0,0.25)", borderRadius: 14, padding: 4,
          border: "1px solid rgba(255,255,255,0.07)",
        }}>
          {TRAVEL_MODES.map(({ mode, icon, label }) => {
            const sel = directionsTravelMode === mode;
            const colors: Record<TravelMode, { bg: string; shadow: string }> = {
              TRANSIT:   { bg: "linear-gradient(135deg,#B45309,#F59E0B)", shadow: "0 2px 10px rgba(245,158,11,0.45)" },
              DRIVING:   { bg: "linear-gradient(135deg,#1D4ED8,#60A5FA)", shadow: "0 2px 10px rgba(96,165,250,0.45)" },
              WALKING:   { bg: "linear-gradient(135deg,#065F46,#34D399)", shadow: "0 2px 10px rgba(52,211,153,0.45)" },
              BICYCLING: { bg: "linear-gradient(135deg,#0E7490,#22D3EE)", shadow: "0 2px 10px rgba(34,211,238,0.45)" },
              FLYING:    { bg: "linear-gradient(135deg,#6D28D9,#A78BFA)", shadow: "0 2px 10px rgba(167,139,250,0.45)" },
            };
            return (
              <button
                key={mode}
                onClick={() => onSetTravelMode(mode)}
                style={{
                  flex: 1, border: "none", cursor: "pointer", borderRadius: 10,
                  padding: "7px 2px", display: "flex", flexDirection: "column",
                  alignItems: "center", gap: 3, transition: "all 0.18s",
                  background: sel ? colors[mode].bg : "transparent",
                  boxShadow: sel ? colors[mode].shadow : "none",
                  transform: sel ? "scale(1.04)" : "scale(1)",
                }}
              >
                <span style={{ fontSize: 18, lineHeight: 1 }}>{icon}</span>
                <span style={{ fontSize: 10, fontWeight: sel ? 700 : 600, color: sel ? "#fff" : "#94A3B8", letterSpacing: "0.02em" }}>{label}</span>
              </button>
            );
          })}
        </div>
        {/* Flight options — date picker + airport selectors */}
        {directionsTravelMode === "FLYING" && (
          <div style={{ marginBottom: 10 }}>
            {/* Departure date */}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 7 }}>
              <span style={{ fontSize: 11, color: "#94A3B8", fontWeight: 600, minWidth: 44 }}>Depart</span>
              <FlightDatePicker
                value={flightDate}
                min={new Date().toISOString().split("T")[0]}
                onChange={setFlightDate}
              />
            </div>
            {airportsLoading ? (
              <div style={{ fontSize: 11, color: "#64748B" }}>Finding airports…</div>
            ) : (
              <>
                {depAirports.length > 0 && (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                    <span style={{ fontSize: 11, color: "#94A3B8", fontWeight: 600, minWidth: 44 }}>From</span>
                    <select value={selectedDepIata} onChange={e => setSelectedDepIata(e.target.value)} style={{
                      flex: 1, background: "rgba(0,0,0,0.45)", border: "1px solid rgba(167,139,250,0.3)",
                      borderRadius: 8, padding: "5px 8px", color: "#E2E8F0", fontSize: 11, cursor: "pointer",
                    }}>
                      {depAirports.map(a => <option key={a.iata} value={a.iata}>{a.iata} — {a.name}</option>)}
                    </select>
                  </div>
                )}
                {arrAirports.length > 0 && (
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 11, color: "#94A3B8", fontWeight: 600, minWidth: 44 }}>To</span>
                    <select value={selectedArrIata} onChange={e => setSelectedArrIata(e.target.value)} style={{
                      flex: 1, background: "rgba(0,0,0,0.45)", border: "1px solid rgba(167,139,250,0.3)",
                      borderRadius: 8, padding: "5px 8px", color: "#E2E8F0", fontSize: 11, cursor: "pointer",
                    }}>
                      {arrAirports.map(a => <option key={a.iata} value={a.iata}>{a.iata} — {a.name}</option>)}
                    </select>
                  </div>
                )}
              </>
            )}
            {/* Search button inline with the controls */}
            {!airportsLoading && venue && (
              <button
                onClick={() => onGetDirections(venue, "FLYING", { date: flightDate, depIata: selectedDepIata || undefined, arrIata: selectedArrIata || undefined })}
                disabled={directionsLoading}
                style={{
                  display: "flex", width: "100%", alignItems: "center", justifyContent: "center",
                  gap: 6, marginTop: 10, padding: "9px 14px", borderRadius: 10,
                  background: directionsLoading ? "rgba(99,102,241,0.3)" : "linear-gradient(135deg,#4F46E5,#7C3AED)",
                  border: "1.5px solid rgba(99,102,241,0.6)", color: "#fff",
                  cursor: directionsLoading ? "not-allowed" : "pointer",
                  fontSize: 13, fontWeight: 700,
                  boxShadow: directionsLoading ? "none" : "0 2px 12px rgba(99,102,241,0.45)",
                  opacity: directionsLoading ? 0.7 : 1, transition: "all 0.18s",
                }}
              >
                <span style={{ fontSize: 14 }}>{directionsLoading ? "⏳" : "✈️"}</span>
                <span>{directionsLoading ? "Searching…" : "Search Flights"}</span>
              </button>
            )}
          </div>
        )}

        {/* Routing errors — suppressed in FLYING mode (not relevant to flight search) */}
        {directionsError && directionsTravelMode !== "FLYING" && (
          <div style={{ fontSize: 11, color: "#F87171", marginBottom: 6 }}>⚠️ {directionsError}</div>
        )}
        {/* Flight-specific errors */}
        {directionsError && directionsTravelMode === "FLYING" && !directionsError.includes("No route") && (
          <div style={{ fontSize: 11, color: "#F87171", marginBottom: 6 }}>⚠️ {directionsError}</div>
        )}

      </div>

      {/* Scrollable body — includes route/flight options, venue name, and content */}
      <div className="sidebar-scroll" style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: isFullScreen ? "24px 32px 36px" : "16px 18px 28px" }}>

        {/* Route / flight options picker */}
        {routeOptions && routeOptions.length > 0 && (
          <div style={{ marginBottom: 14 }}>
            {routeOptions[0].type === "flight" && (() => {
              const f = routeOptions[0];
              const dateStr = f.outboundDate
                ? new Date(f.outboundDate + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" })
                : "";
              return (
                <div style={{
                  background: "linear-gradient(135deg, rgba(109,40,217,0.35), rgba(167,139,250,0.15))",
                  border: "1px solid rgba(167,139,250,0.4)",
                  borderRadius: 12, padding: "10px 12px", marginBottom: 8,
                  boxShadow: "0 2px 12px rgba(109,40,217,0.2)",
                }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: "#E2E8F0", marginBottom: 2, lineHeight: 1.4 }}>
                    ✈️ {f.departureAirport}
                  </div>
                  <div style={{ fontSize: 11, color: "#94A3B8", marginBottom: (dateStr || driveToAirport) ? 4 : 0 }}>→ {f.arrivalAirport}</div>
                  {dateStr && (
                    <div style={{ fontSize: 11, color: "#A78BFA", fontWeight: 600, marginBottom: driveToAirport ? 4 : 0 }}>🗓 {dateStr}</div>
                  )}
                  {driveToAirport && (
                    <div style={{ fontSize: 11, color: "#34D399", fontWeight: 600 }}>
                      🚗 {driveToAirport.duration} to airport ({driveToAirport.distance})
                    </div>
                  )}
                </div>
              );
            })()}
            {/* Airline filter chips — shown when 2+ airlines present */}
            {(() => {
              const airlines = [...new Set(routeOptions.filter(o => o.type === "flight" && o.airline).map(o => (o as { airline: string }).airline))];
              if (airlines.length < 2) return null;
              const toggleAirline = (airline: string) => setAirlineFilter(prev => {
                const next = new Set(prev);
                if (next.has(airline)) { next.delete(airline); return next.size === 0 ? new Set() : next; }
                next.add(airline);
                return next.size === airlines.length ? new Set() : next;
              });
              return (
                <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginBottom: 8 }}>
                  {airlines.map(airline => {
                    const active = airlineFilter.size === 0 || airlineFilter.has(airline);
                    return (
                      <button key={airline} onClick={() => toggleAirline(airline)} style={{
                        fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 20, cursor: "pointer",
                        border: `1px solid ${active ? "rgba(167,139,250,0.6)" : "rgba(255,255,255,0.1)"}`,
                        background: active ? "rgba(109,40,217,0.3)" : "rgba(255,255,255,0.04)",
                        color: active ? "#C4B5FD" : "#475569", transition: "all 0.15s",
                      }}>{airline}</button>
                    );
                  })}
                </div>
              );
            })()}
            {(airlineFilter.size > 0
              ? routeOptions.filter(o => o.type !== "flight" || airlineFilter.has((o as { airline: string }).airline))
              : routeOptions
            ).map((opt, rankIdx) => {
              const isSelected = selectedRouteIndex === opt.index;
              const isCheapest = opt.type === "flight" && rankIdx === 0;
              const isNonstop  = opt.type === "flight" && opt.stops === 0;
              return (
                <button
                  key={opt.index}
                  onClick={() => onSelectRoute(opt)}
                  style={{
                    display: "flex", width: "100%", alignItems: "center",
                    gap: 8, padding: "10px 12px", borderRadius: 10,
                    marginBottom: 5, cursor: "pointer", border: "none",
                    background: isSelected
                      ? "linear-gradient(135deg, rgba(16,185,129,0.2), rgba(52,211,153,0.1))"
                      : "rgba(255,255,255,0.04)",
                    outline: isSelected
                      ? "1.5px solid rgba(52,211,153,0.6)"
                      : "1px solid rgba(255,255,255,0.08)",
                    transition: "all 0.15s",
                    boxSizing: "border-box",
                  }}
                >
                  {isSelected && <span style={{ fontSize: 12, color: "#34D399", flexShrink: 0 }}>✓</span>}
                  {opt.type === "directions" ? (
                    <>
                      <span style={{ fontSize: 14, fontWeight: 700, color: isSelected ? "#34D399" : "#A5B4FC", minWidth: 55 }}>
                        {opt.duration}
                      </span>
                      <span style={{ fontSize: 12, color: "#64748B" }}>{opt.distance}</span>
                      {opt.summary && (
                        <span style={{ fontSize: 11, color: "#475569", marginLeft: "auto", textAlign: "right" }}>via {opt.summary}</span>
                      )}
                    </>
                  ) : (
                    <>
                      <span style={{ fontSize: 14, fontWeight: 800, color: isSelected ? "#34D399" : "#C4B5FD", minWidth: 58, flexShrink: 0 }}>
                        {opt.durationStr}
                      </span>
                      <span style={{
                        fontSize: 11, fontWeight: 700, flexShrink: 0,
                        color: isNonstop ? "#34D399" : "#94A3B8",
                        background: isNonstop ? "rgba(16,185,129,0.15)" : "rgba(255,255,255,0.06)",
                        padding: "2px 6px", borderRadius: 5,
                        border: isNonstop ? "1px solid rgba(52,211,153,0.4)" : "1px solid rgba(255,255,255,0.08)",
                      }}>
                        {opt.stops === 0 ? "Nonstop" : `${opt.stops} stop${opt.stops > 1 ? "s" : ""}`}
                      </span>
                      {opt.airline && (
                        <span style={{ fontSize: 11, color: "#64748B", flexShrink: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {opt.airline}{opt.flightNumber ? ` ${opt.flightNumber}` : ""}
                        </span>
                      )}
                      <span style={{ marginLeft: "auto", display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2, flexShrink: 0 }}>
                        {opt.price != null && (
                          <span style={{ fontSize: 15, fontWeight: 800, color: isCheapest ? "#FCD34D" : "#E2E8F0" }}>
                            ${opt.price}
                          </span>
                        )}
                        {isCheapest && (
                          <span style={{ fontSize: 9, fontWeight: 700, color: "#FCD34D", textTransform: "uppercase", letterSpacing: "0.05em" }}>Best price</span>
                        )}
                      </span>
                    </>
                  )}
                </button>
              );
            })}
          </div>
        )}

        {/* Name */}
        <h2 style={{
          margin: "0 0 0 0", fontSize: 22, fontWeight: 800, color: "#F1F5F9", lineHeight: 1.25,
          letterSpacing: "-0.3px",
          display: "inline-block",
          background: "rgba(16,185,129,0.15)",
          padding: "2px 10px 4px",
          borderRadius: 8,
          border: "1px solid rgba(16,185,129,0.3)",
          boxShadow: "0 0 12px rgba(16,185,129,0.2)",
        }}>
          {venue.name}
        </h2>
        {(venue.neighborhood || venue.cuisine) && (
          <div style={{ fontSize: 12, color: "#64748B", marginTop: 3 }}>
            {venue.neighborhood && `${venue.neighborhood} · `}
            {fmt(venue.cuisine)}
          </div>
        )}

        {/* Pills row */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 10, marginBottom: 16 }}>
          <DarkPill color="#818CF8" label={`${Math.round(venue.match_score)}% match`} large />
          {openLabel && <DarkPill color={openLabel.color} label={openLabel.label} />}
          {venue.has_private_room && <DarkPill color="#10B981" label="Private room" />}
          {venue.price_per_head > 0 && <DarkPill color="#F59E0B" label={`~$${venue.price_per_head}/head`} />}
          {placeDetails?.rating && (
            <DarkPill color="#F59E0B" label={`⭐ ${placeDetails.rating} (${placeDetails.user_rating_count?.toLocaleString()})`} />
          )}
        </div>

        {isFullScreen ? (
          /* ── Two-column layout in fullscreen ── */
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 28, alignItems: "start" }}>
            {/* Left column: why + scenario + quotes */}
            <div>
              {(intel?.why_card || venue.key_quotes.length > 0) && (
                <div style={{ marginBottom: 22 }}>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
                    Why this spot
                  </div>
                  <p style={{ margin: 0, fontSize: 15, color: "#E2E8F0", lineHeight: 1.75 }}>
                    {intel?.why_card || (
                      `${venue.name} is a ${fmt(venue.cuisine) || "venue"} in ${venue.neighborhood || venue.city}. ` +
                      venue.key_quotes.slice(0, 2).join(" ")
                    )}
                  </p>
                </div>
              )}
              {intel?.scenario && (
                <div style={{
                  marginBottom: 22, background: "rgba(255,255,255,0.04)",
                  borderRadius: 12, padding: "14px 18px",
                  border: "1px solid rgba(255,255,255,0.08)",
                }}>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
                    Your evening
                  </div>
                  <p style={{ margin: 0, fontSize: 14, color: "#94A3B8", lineHeight: 1.75, fontStyle: "italic" }}>{intel.scenario}</p>
                </div>
              )}
              {venue.key_quotes.length > 0 && (
                <div>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 10 }}>
                    What people say
                  </div>
                  {venue.key_quotes.slice(0, 4).map((q, i) => (
                    <div key={i} style={{
                      fontSize: 13, color: "#94A3B8", marginBottom: 10,
                      paddingLeft: 14, borderLeft: "2px solid rgba(99,179,237,0.35)",
                      lineHeight: 1.6,
                    }}>
                      "{q}"
                    </div>
                  ))}
                </div>
              )}
            </div>
            {/* Right column: sensitivity bars + live signal + contact */}
            <div>
              {intel?.sensitivity_bars && (
                <div style={{ marginBottom: 22 }}>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 12 }}>
                    Match dimensions
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {Object.entries(intel.sensitivity_bars).map(([dim, score]) => (
                      <SensitivityBar key={dim} label={fmt(dim)} value={score} large />
                    ))}
                  </div>
                </div>
              )}
              {intel?.live_signal && (
                <div style={{
                  marginBottom: 22, padding: "12px 16px",
                  background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.25)",
                  borderRadius: 10, fontSize: 14, color: "#FCD34D", lineHeight: 1.5,
                }}>
                  ⚡ {intel.live_signal}
                </div>
              )}
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8 }}>
                {placeDetails?.website_uri && (
                  <a href={placeDetails.website_uri} target="_blank" rel="noopener noreferrer"
                    style={{ fontSize: 14, color: "#60A5FA", textDecoration: "none", display: "flex", alignItems: "center", gap: 5 }}>
                    🌐 Website
                  </a>
                )}
                {placeDetails?.phone_number && (
                  <a href={`tel:${placeDetails.phone_number}`}
                    style={{ fontSize: 14, color: "#60A5FA", textDecoration: "none", display: "flex", alignItems: "center", gap: 5 }}>
                    📞 {placeDetails.phone_number}
                  </a>
                )}
              </div>
            </div>
          </div>
        ) : (
          /* ── Single-column layout in side panel ── */
          <>
            {(intel?.why_card || venue.key_quotes.length > 0) && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
                  Why this spot
                </div>
                <p style={{ margin: 0, fontSize: 13, color: "#CBD5E1", lineHeight: 1.65 }}>
                  {intel?.why_card || (
                    `${venue.name} is a ${fmt(venue.cuisine) || "venue"} in ${venue.neighborhood || venue.city}. ` +
                    venue.key_quotes.slice(0, 2).join(" ")
                  )}
                </p>
              </div>
            )}
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
            {intel?.sensitivity_bars && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>
                  Dimensions
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 16px" }}>
                  {Object.entries(intel.sensitivity_bars).map(([dim, score]) => (
                    <SensitivityBar key={dim} label={fmt(dim)} value={score} />
                  ))}
                </div>
              </div>
            )}
            {intel?.live_signal && (
              <div style={{
                marginBottom: 16, padding: "8px 12px",
                background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.2)",
                borderRadius: 8, fontSize: 12, color: "#FCD34D",
              }}>
                ⚡ {intel.live_signal}
              </div>
            )}
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
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 8 }}>
              {placeDetails?.website_uri && (
                <a href={placeDetails.website_uri} target="_blank" rel="noopener noreferrer"
                  style={{ fontSize: 12, color: "#60A5FA", textDecoration: "none", display: "flex", alignItems: "center", gap: 4 }}>
                  🌐 Website
                </a>
              )}
              {placeDetails?.phone_number && (
                <a href={`tel:${placeDetails.phone_number}`}
                  style={{ fontSize: 12, color: "#60A5FA", textDecoration: "none", display: "flex", alignItems: "center", gap: 4 }}>
                  📞 {placeDetails.phone_number}
                </a>
              )}
            </div>
          </>
        )}
      </div>
    </div>
    </>
  );
}

// ─── Directions panel ────────────────────────────────────────────────────

const TRAVEL_MODES: { mode: TravelMode; icon: string; label: string }[] = [
  { mode: "TRANSIT",   icon: "🚇", label: "Transit"  },
  { mode: "DRIVING",   icon: "🚗", label: "Drive"    },
  { mode: "WALKING",   icon: "🚶", label: "Walk"     },
  { mode: "BICYCLING", icon: "🚲", label: "Bike"     },
  { mode: "FLYING",    icon: "✈️",  label: "Fly"      },
];

function DirectionsPanel({
  venue, travelMode, onSetMode, onGet, onClear, leg, loading, error, small,
}: {
  venue: { place_id?: string | null; latitude?: number | null; longitude?: number | null; name: string; address?: string | null };
  travelMode: TravelMode;
  onSetMode: (m: TravelMode) => void;
  onGet: (v: { place_id?: string | null; latitude?: number | null; longitude?: number | null; name: string; address?: string | null }, m: TravelMode) => void;
  onClear: () => void;
  leg: DirectionsLeg | null;
  loading: boolean;
  error?: string | null;
  small?: boolean;
}) {
  const fs = small ? 11 : 13;
  return (
    <div style={{ marginTop: 12, marginBottom: 4 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 7 }}>
        Directions
      </div>
      {/* Travel mode tabs */}
      <div style={{ display: "flex", gap: 5, marginBottom: 8 }}>
        {TRAVEL_MODES.map(({ mode, icon, label }) => (
          <button
            key={mode}
            onClick={() => onSetMode(mode)}
            style={{
              padding: "4px 9px", borderRadius: 8, cursor: "pointer",
              fontSize: fs, fontWeight: travelMode === mode ? 700 : 400,
              background: travelMode === mode ? "rgba(99,102,241,0.25)" : "rgba(255,255,255,0.05)",
              border: travelMode === mode ? "1.5px solid rgba(99,102,241,0.6)" : "1px solid rgba(255,255,255,0.1)",
              color: travelMode === mode ? "#A5B4FC" : "#64748B",
              transition: "all 0.12s",
            }}
          >
            {icon} {label}
          </button>
        ))}
      </div>
      {/* Show / clear row */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <button
          onClick={() => onGet(venue, travelMode)}
          disabled={loading}
          style={{
            padding: "5px 14px", borderRadius: 8, cursor: loading ? "not-allowed" : "pointer",
            fontSize: fs, fontWeight: 700,
            background: "linear-gradient(135deg, #4F46E5, #7C3AED)",
            border: "1.5px solid rgba(99,102,241,0.6)",
            color: "#fff", opacity: loading ? 0.6 : 1,
            display: "flex", alignItems: "center", gap: 5,
          }}
        >
          {loading ? "⏳ Routing…" : "🗺️ Show on map"}
        </button>
        {leg && (
          <>
            <span style={{ fontSize: fs, color: "#34D399", fontWeight: 600 }}>{leg.duration}</span>
            <span style={{ fontSize: fs - 1, color: "#475569" }}>({leg.distance})</span>
            <button
              onClick={onClear}
              style={{ padding: "3px 8px", borderRadius: 6, fontSize: fs - 1, cursor: "pointer", background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)", color: "#475569" }}
            >✕ Clear</button>
          </>
        )}
      </div>
      {error && (
        <div style={{ marginTop: 6, fontSize: 11, color: "#F87171", display: "flex", alignItems: "center", gap: 5 }}>
          ⚠️ {error}
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

function DarkPill({ label, color, large }: { label: string; color: string; large?: boolean }) {
  return (
    <span style={{
      padding: large ? "5px 12px" : "3px 9px",
      borderRadius: 20,
      background: `${color}20`,
      color, border: `1px solid ${color}45`,
      fontSize: large ? 14 : 11,
      fontWeight: large ? 700 : 600,
      whiteSpace: "nowrap",
    }}>
      {label}
    </span>
  );
}

function SensitivityBar({ label, value, large }: { label: string; value: number; large?: boolean }) {
  const clamped = Math.max(0, Math.min(100, value));
  const hue = Math.round((clamped / 100) * 120);
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: large ? 5 : 2 }}>
        <span style={{ fontSize: large ? 13 : 11, color: large ? "#94A3B8" : "#6B7280", textTransform: "capitalize", fontWeight: large ? 500 : 400 }}>{label}</span>
        <span style={{ fontSize: large ? 13 : 11, fontWeight: 600, color: large ? "#E2E8F0" : "#94A3B8" }}>{clamped}%</span>
      </div>
      <div style={{ height: large ? 7 : 4, background: "rgba(255,255,255,0.08)", borderRadius: 4, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${clamped}%`,
          background: `hsl(${hue},70%,45%)`,
          borderRadius: 4,
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
