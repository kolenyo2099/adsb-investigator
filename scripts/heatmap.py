#!/usr/bin/env python3
"""Reading adsb.lol's hourly global heatmap files. Stdlib only.

Each hour is one file of fixed-size binary records covering every aircraft
seen worldwide. There's no spatial index, so any area question is a full scan —
which is why this runs server-side and its output gets published as a per-day
index rather than queried live.
"""
import gzip
import struct
import urllib.error
import urllib.request
from datetime import date

USER_AGENT = "adsb-investigator (+https://github.com/kolenyo2099/adsb-investigator)"

RECORD_FMT = "<IiihH"
RECORD_SIZE = struct.calcsize(RECORD_FMT)  # 16 bytes

# ponytail: only the range that mattered for the investigation this grew out of.
# Extend with the full ICAO allocation table if other registration countries
# start carrying weight.
US_HEX_RANGE = (0xA00000, 0xADF7C7)


def reg_country(hex_int):
    """Registration country from the ICAO address — exact, free, no lookup."""
    if US_HEX_RANGE[0] <= hex_int <= US_HEX_RANGE[1]:
        return "US"
    return "unknown"


def fetch_hour(day: date, hour: int):
    """Raw decompressed bytes for one hour, or None if that hour isn't published.

    Served with a .ttf extension but it's gzip — trust the bytes, not the name.
    """
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
    """Yield (icao, lat, lon, alt_ft, gs_kt) for every plausible position record."""
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
        # receivers they otherwise aggregate into one impossible
        # globe-spanning "aircraft".
        if icao < 0x000100:
            continue
        yield icao, lat, lon, alt_i * 25, gs_i / 10
