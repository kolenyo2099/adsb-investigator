#!/usr/bin/env python3
"""Scan a day's global heatmap for aircraft inside a bounding box.

Expensive by design (24 x ~14 MB gz fetches for a full day) — this is why
it's a manual, on-demand script rather than something the nightly Action runs.
Stdlib only.

Usage:
  python3 scripts/scan_area.py YYYY-MM-DD min_lat min_lon max_lat max_lon [--hours 0,1,2]
"""
import gzip
import json
import struct
import sys
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "area_scans"
USER_AGENT = "adsb-investigator (+https://github.com/kolenyo2099/adsb-investigator)"

RECORD_FMT = "<IiihH"
RECORD_SIZE = struct.calcsize(RECORD_FMT)  # 16 bytes, per HANDOFF.md §3

# ponytail: only the range that mattered for this investigation (cut a whole
# West-Africa corridor scan to 4 aircraft, HANDOFF.md §2). Extend with the
# full ICAO allocation table if other registration countries become relevant.
US_HEX_RANGE = (0xA00000, 0xADF7C7)


def reg_country(hex_int):
    if US_HEX_RANGE[0] <= hex_int <= US_HEX_RANGE[1]:
        return "US"
    return "unknown"


def fetch_hour(day: date, hour: int):
    url = (
        f"https://adsb.lol/globe_history/{day.year:04d}/{day.month:02d}/{day.day:02d}"
        f"/heatmap/{hour:02d}.bin.ttf"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return gzip.decompress(raw)


def parse_records(raw):
    for off in range(0, len(raw) - RECORD_SIZE + 1, RECORD_SIZE):
        hex_flagged, lat_i, lon_i, alt_i, gs_i = struct.unpack_from(RECORD_FMT, raw, off)
        lat, lon = lat_i / 1e6, lon_i / 1e6
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue  # timestamp/marker record, not a position
        # Null island: a failed fix, not a real position over the Atlantic.
        if lat == 0 and lon == 0:
            continue
        icao = hex_flagged & 0xFFFFFF
        # ICAO addresses below 0x000100 aren't validly assigned. In practice
        # they're placeholders, and because they repeat across unrelated
        # receivers they otherwise aggregate into one impossible globe-spanning
        # "aircraft" in the daily index.
        if icao < 0x000100:
            continue
        yield icao, lat, lon, alt_i * 25, gs_i / 10


def scan(day: date, bbox, hours):
    min_lat, min_lon, max_lat, max_lon = bbox
    seen = {}
    for hour in hours:
        print(f"hour {hour:02d}…", end=" ", flush=True)
        raw = fetch_hour(day, hour)
        if raw is None:
            print("no data")
            continue
        hits = 0
        for hexid, lat, lon, alt, gs in parse_records(raw):
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                hits += 1
                key = f"{hexid:06x}"
                if key not in seen or hour < seen[key]["first_hour"]:
                    seen.setdefault(key, {"hex": key, "reg_country": reg_country(hexid)})
                    seen[key].update({"first_hour": hour, "lat": lat, "lon": lon, "alt_ft": alt, "gs_kt": gs})
        print(f"{hits} hits")
    return list(seen.values())


def main():
    if len(sys.argv) < 6:
        print(__doc__)
        sys.exit(1)
    day = date.fromisoformat(sys.argv[1])
    min_lat, min_lon, max_lat, max_lon = (float(x) for x in sys.argv[2:6])
    hours = range(24)
    for arg in sys.argv[6:]:
        if arg.startswith("--hours"):
            hours = [int(h) for h in arg.split("=", 1)[1].split(",")]

    results = scan(day, (min_lat, min_lon, max_lat, max_lon), hours)
    results.sort(key=lambda r: r["hex"])

    DATA.mkdir(parents=True, exist_ok=True)
    slug = f"{day.isoformat()}_{min_lat}_{min_lon}_{max_lat}_{max_lon}"
    out_path = DATA / f"{slug}.json"
    out_path.write_text(json.dumps({
        "date": day.isoformat(),
        "bbox": {"min_lat": min_lat, "min_lon": min_lon, "max_lat": max_lat, "max_lon": max_lon},
        "hours_scanned": list(hours),
        "aircraft": results,
    }, indent=2))

    manifest_path = DATA / "index.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest = [m for m in manifest if m["file"] != out_path.name]
    manifest.append({"file": out_path.name, "date": day.isoformat(),
                      "bbox": [min_lat, min_lon, max_lat, max_lon], "count": len(results)})
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print(f"{len(results)} distinct aircraft in bbox -> {out_path}")


if __name__ == "__main__":
    main()
