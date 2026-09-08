#!/usr/bin/env python3
"""Split a day of positions into flight legs, and say where each began and ended.

The honest problem this solves: aircraft are almost never observed on the
ground. Receivers rarely hear a parked aircraft, so a track typically starts
already climbing and ends still descending. Departure and arrival therefore
have to be inferred from altitude, vertical trend and distance to a runway —
and the strength of that inference varies enormously, so every endpoint carries
a confidence level rather than being stated flatly.

Stdlib only.
"""
import gzip
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AIRPORTS_FILE = ROOT / "data" / "airports.json.gz"

# A pause this long means the aircraft almost certainly landed and sat, provided
# it was low enough beforehand to have been landing at all.
SPLIT_GAP_S = 25 * 60
LOW_ALT_FT = 12000          # below this, a long pause reads as a turnaround
GROUND_GS_KT = 45           # slower than this is taxiing or parked, not flying

# Endpoint confidence bands: (max_altitude_ft, max_distance_km, label)
BANDS = [
    (4000, 18, "high"),
    (12000, 45, "likely"),
]

MIN_LEG_S = 8 * 60          # shorter runs are noise, not flights
MIN_LEG_KM = 15


def haversine_km(lat1, lon1, lat2, lon2):
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


_airports = None


def airports():
    """[(icao, iata, name, lat, lon, country, city), ...] — loaded once."""
    global _airports
    if _airports is None:
        if AIRPORTS_FILE.exists():
            with gzip.open(AIRPORTS_FILE, "rt", encoding="utf-8") as fh:
                _airports = json.load(fh)["airports"]
        else:
            _airports = []
    return _airports


_grid = None
GRID = 2.0  # degrees


def _index_airports():
    """Bucket airports by whole-degree cell so lookup isn't a 10k scan per point."""
    global _grid
    if _grid is None:
        _grid = {}
        for a in airports():
            key = (int(a[3] // GRID), int(a[4] // GRID))
            _grid.setdefault(key, []).append(a)
    return _grid


def nearest_airport(lat, lon):
    g = _index_airports()
    if not g:
        return None, None
    best, best_km = None, 1e9
    cy, cx = int(lat // GRID), int(lon // GRID)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for a in g.get((cy + dy, cx + dx), ()):
                km = haversine_km(lat, lon, a[3], a[4])
                if km < best_km:
                    best, best_km = a, km
    return (best, best_km) if best else (None, None)


def classify(lat, lon, alt, gs, trend, which):
    """Describe one end of a leg: where it was, and how sure we can be.

    which: 'dep' or 'arr'. Returns a dict the UI renders directly.
    """
    ap, km = nearest_airport(lat, lon)
    on_ground = gs is not None and gs <= GROUND_GS_KT

    level = "unknown"
    if on_ground and ap and km <= 18:
        level = "observed"
    elif ap is not None:
        for max_alt, max_km, label in BANDS:
            if alt <= max_alt and km <= max_km:
                # A departure should be climbing and an arrival descending; the
                # wrong trend means it was probably just passing overhead low.
                if trend is None or which == "dep" and trend >= 0 or which == "arr" and trend <= 0:
                    level = label
                break

    return {
        "lat": round(lat, 4), "lon": round(lon, 4),
        "alt": int(alt), "level": level,
        "icao": ap[0] if ap and level != "unknown" else None,
        "name": ap[2] if ap and level != "unknown" else None,
        "km": round(km, 1) if ap and level != "unknown" else None,
        # Kept even when unknown, so the UI can still say what was nearby.
        "near_icao": ap[0] if ap else None,
        "near_name": ap[2] if ap else None,
        "near_km": round(km, 1) if ap else None,
    }


def trend_of(points, at_start):
    """Feet per minute across the few points nearest one end of a leg."""
    seq = points[:6] if at_start else points[-6:]
    if len(seq) < 2:
        return None
    dt = seq[-1][0] - seq[0][0]
    if dt <= 0:
        return None
    return (seq[-1][3] - seq[0][3]) / (dt / 60)


def split_runs(points):
    """Break a day's positions into candidate flights.

    Split on a long pause that follows a low-altitude position (a landing), and
    on any stretch spent taxiing. A long pause at cruise altitude is loss of
    coverage, not a landing, so it does not split the leg.
    """
    runs, cur = [], []
    for i, p in enumerate(points):
        if cur:
            prev = cur[-1]
            gap = p[0] - prev[0]
            landed = gap >= SPLIT_GAP_S and (prev[3] <= LOW_ALT_FT or p[3] <= LOW_ALT_FT)
            if landed:
                runs.append(cur)
                cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    return runs


def legs_for(points):
    """points: [(t, lat, lon, alt, gs), ...] sorted by t -> list of leg dicts."""
    out = []
    for run in split_runs(points):
        if len(run) < 2:
            continue
        dur = run[-1][0] - run[0][0]
        dist = haversine_km(run[0][1], run[0][2], run[-1][1], run[-1][2])
        # Keep short hops that clearly moved, and orbits that clearly took time.
        if dur < MIN_LEG_S and dist < MIN_LEG_KM:
            continue
        dep = classify(run[0][1], run[0][2], run[0][3], run[0][4], trend_of(run, True), "dep")
        arr = classify(run[-1][1], run[-1][2], run[-1][3], run[-1][4], trend_of(run, False), "arr")
        out.append({
            "t0": int(run[0][0]), "t1": int(run[-1][0]),
            "n": len(run),
            "max_alt": int(max(p[3] for p in run)),
            "dep": dep, "arr": arr,
        })
    return out


def demo():
    """Self-check with synthetic tracks: python3 scripts/flights.py"""
    # Two legs separated by a four-hour pause at low altitude near Dakar.
    dakar = (14.67, -17.07)
    pts = []
    for i in range(20):  # descending into Dakar
        pts.append((3600 + i * 60, 14.40 + i * 0.013, -17.60 + i * 0.026, 9000 - i * 430, 200))
    for i in range(20):  # climbing out four hours later
        pts.append((22000 + i * 60, 14.68 + i * 0.02, -17.07 + i * 0.03, 700 + i * 900, 220))
    legs = legs_for(pts)
    assert len(legs) == 2, f"a long low-altitude pause must split the day: got {len(legs)}"
    assert legs[0]["arr"]["level"] in ("high", "likely"), legs[0]["arr"]
    assert legs[1]["dep"]["level"] in ("high", "likely"), legs[1]["dep"]
    assert legs[0]["arr"]["icao"] == "GOBD", legs[0]["arr"]
    assert legs[1]["dep"]["icao"] == "GOBD", legs[1]["dep"]

    # A pause at cruise is lost coverage, not a landing, and must not split.
    cruise = [(i * 60, 20 + i * 0.05, 0 + i * 0.05, 37000, 480) for i in range(10)]
    cruise += [(i * 60 + 9000, 25 + i * 0.05, 5 + i * 0.05, 37000, 480) for i in range(10)]
    assert len(legs_for(cruise)) == 1, "a gap at altitude is lost coverage, not a landing"

    # An endpoint far from any runway at altitude must stay unknown.
    high = [(i * 60, 30 + i * 0.05, -40 + i * 0.05, 36000, 470) for i in range(10)]
    assert legs_for(high)[0]["arr"]["level"] == "unknown"

    print("all flight-segmentation checks passed")


if __name__ == "__main__":
    demo()
