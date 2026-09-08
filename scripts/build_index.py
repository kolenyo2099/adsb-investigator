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


def build_day(day: date, hours):
    """-> {hex: [min_lat, min_lon, max_lat, max_lon, first_hr, last_hr, max_alt, points]}"""
    seen = {}
    for hour in hours:
        raw = fetch_hour(day, hour)
        if raw is None:
            print(f"  hour {hour:02d}: no data")
            continue
        n = 0
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
        print(f"  hour {hour:02d}: {n:,} positions, {len(seen):,} aircraft so far")
    return seen


def write_day(day: date, seen, hours):
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
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest = [m for m in manifest if m["date"] != day.isoformat()]
    manifest.append({
        "date": day.isoformat(),
        "count": len(aircraft),
        "hours": len(list(hours)),
        "bytes": path.stat().st_size,
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
        seen = build_day(day, hours)
        if not seen:
            print(f"  no data for {day} — skipping (outside the retention window?)")
            continue
        path, n = write_day(day, seen, hours)
        print(f"  {n:,} aircraft -> {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
