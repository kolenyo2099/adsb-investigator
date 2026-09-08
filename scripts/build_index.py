#!/usr/bin/env python3
"""Build a per-day index of every aircraft seen, from the hourly heatmaps.

This is the half of the pipeline a browser can't do for itself. Looking up a
known aircraft needs no index at all (the trace URL is derivable from hex and
date), but "what was flying over here that day" has no index upstream and would
otherwise mean scanning ~340 MB of heatmaps per query.

It's also an archive: adsb.lol keeps only a rolling ~31-day window of the
current year, so a day not captured now becomes expensive to recover later.

One compact file per day, so the browser makes one request and then answers
every question — by hex, by area, by altitude — locally.

Usage: python3 scripts/build_index.py [YYYY-MM-DD ...] [--hours 0,1,2]
"""
import gzip
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from heatmap import fetch_hour, parse_records, reg_country

ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / "data" / "daily"


# Records arrive in time order within each hourly file, so consecutive positions
# for one aircraft form its path. Straight cruise emits a position every ~2 km
# and compresses away almost entirely; turns and orbits are what survive.
#
# Tolerance is proportional to how far the aircraft ranged that day. A holding
# pattern a few km across and a transatlantic crossing need very different
# thresholds, and a fixed one either flattens the orbit or bloats the crossing.
HOUR_TOL_KM = 0.25          # first pass, purely to bound memory
FINAL_TOL_FRAC = 0.006      # then this fraction of the aircraft's own range
FINAL_TOL_MIN_KM = 0.12
FINAL_TOL_MAX_KM = 4.0


def simplify(pts, tol):
    """Douglas-Peucker, iterative so a long track can't blow the stack."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        den = dx * dx + dy * dy
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if den == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / den
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, k
        if best > tol * tol:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(pts, keep) if k]


def build_day(day: date, hours, want_tracks=True):
    """-> (index, tracks)

    index: {hex: [min_lat, min_lon, max_lat, max_lon, first_hr, last_hr, max_alt, points]}
    tracks: {hex: [(lat, lon), ...]} simplified
    """
    seen, tracks = {}, {}
    tol = HOUR_TOL_KM / 111.0
    for hour in hours:
        raw = fetch_hour(day, hour)
        if raw is None:
            print(f"  hour {hour:02d}: no data")
            continue
        n = 0
        buf = {}
        for hexid, lat, lon, alt, _gs in parse_records(raw):
            n += 1
            rec = seen.get(hexid)
            if rec is None:
                seen[hexid] = [lat, lon, lat, lon, hour, hour, alt, 1]
            else:
                if lat < rec[0]: rec[0] = lat
                if lon < rec[1]: rec[1] = lon
                if lat > rec[2]: rec[2] = lat
                if lon > rec[3]: rec[3] = lon
                rec[5] = hour
                if alt > rec[6]: rec[6] = alt
                rec[7] += 1
            if want_tracks:
                buf.setdefault(hexid, []).append((lat, lon))
        if want_tracks:
            # Simplify each hour as it lands: holding a whole day of raw
            # positions in memory would cost gigabytes.
            for hexid, pts in buf.items():
                tracks.setdefault(hexid, []).extend(simplify(pts, tol))
        print(f"  hour {hour:02d}: {n:,} positions, {len(seen):,} aircraft so far")
    return seen, tracks


def write_day(day: date, seen, tracks, hours):
    DAILY.mkdir(parents=True, exist_ok=True)
    # Array-of-arrays, coords rounded to ~11 m. Keys and full float precision
    # would roughly triple the file for no analytical gain.
    aircraft = [
        [f"{h:06x}", round(r[0], 4), round(r[1], 4), round(r[2], 4), round(r[3], 4),
         r[4], r[5], r[6], r[7], reg_country(h)]
        for h, r in sorted(seen.items())
    ]
    out = {
        "date": day.isoformat(),
        "hours_scanned": list(hours),
        "fields": ["hex", "min_lat", "min_lon", "max_lat", "max_lon",
                   "first_hour", "last_hour", "max_alt_ft", "positions", "reg_country"],
        "aircraft": aircraft,
    }
    # Gzipped on disk: ~3x smaller, which matters when this lands in git every
    # night forever. Browsers decompress it with DecompressionStream.
    path = DAILY / f"{day.isoformat()}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))

    manifest_path = DAILY / "index.json"
    tracks_bytes = 0
    if tracks:
        # Separate file: filtering only needs the index, so the heavier
        # geometry is fetched only when someone actually draws paths.
        # Coordinates are delta-encoded thousandths of a degree (~110 m).
        enc = {}
        for h, pts in tracks.items():
            if len(pts) < 2:
                continue
            # Second pass, now that the aircraft's full extent is known.
            r = seen[h]
            span_km = max(abs(r[2] - r[0]), abs(r[3] - r[1])) * 111.0
            ftol = min(max(span_km * FINAL_TOL_FRAC, FINAL_TOL_MIN_KM), FINAL_TOL_MAX_KM)
            pts = simplify(pts, ftol / 111.0)
            out_pts, pla, plo = [], 0, 0
            for la, lo in pts:
                ila, ilo = int(round(la * 1000)), int(round(lo * 1000))
                out_pts.append(ila - pla)
                out_pts.append(ilo - plo)
                pla, plo = ila, ilo
            enc[f"{h:06x}"] = out_pts
        tpath = DAILY / f"{day.isoformat()}.tracks.json.gz"
        with gzip.open(tpath, "wt", encoding="utf-8") as fh:
            json.dump({"date": day.isoformat(), "scale": 1000,
                       "simplified": "adaptive", "tracks": enc}, fh, separators=(",", ":"))
        tracks_bytes = tpath.stat().st_size
        print(f"  {len(enc):,} tracks -> {tpath.name} ({tracks_bytes / 1e6:.1f} MB)")

    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest = [m for m in manifest if m["date"] != day.isoformat()]
    manifest.append({
        "date": day.isoformat(),
        "count": len(aircraft),
        "hours": len(list(hours)),
        "bytes": path.stat().st_size,
        "tracks_bytes": tracks_bytes,
    })
    manifest.sort(key=lambda m: m["date"], reverse=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return path, len(aircraft)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    hours = range(24)
    for a in sys.argv[1:]:
        if a.startswith("--hours"):
            hours = [int(h) for h in a.split("=", 1)[1].split(",")]

    days = [date.fromisoformat(a) for a in args] or [date.today() - timedelta(days=1)]
    for day in days:
        print(f"building {day}")
        seen, tracks = build_day(day, hours)
        if not seen:
            print(f"  no data for {day} — skipping (outside the retention window?)")
            continue
        path, n = write_day(day, seen, tracks, hours)
        print(f"  {n:,} aircraft -> {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
