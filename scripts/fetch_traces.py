#!/usr/bin/env python3
"""Fetch adsb.lol traces for watchlisted aircraft and write gap reports.

Stdlib only. Run: python3 scripts/fetch_traces.py [YYYY-MM-DD ...]
With no args, fetches yesterday (UTC).
"""
import gzip
import json
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WATCHLIST_FILE = ROOT / "watchlist.txt"
USER_AGENT = "adsb-investigator (+https://github.com/kolenyo2099/adsb-investigator)"

CRUISE_KMH = 850
GAP_THRESHOLD_S = 20 * 60  # gaps shorter than this are normal ADS-B dropout, not a finding


def hexdb(path):
    req = urllib.request.Request(f"https://hexdb.io{path}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode().strip()
    except urllib.error.URLError:
        return "n/a"


def reg_to_hex(reg):
    return hexdb(f"/reg-hex?reg={reg}")


def fetch_trace(hexid, day: date):
    hexid = hexid.lower()
    url = (
        f"https://adsb.lol/globe_history/{day.year:04d}/{day.month:02d}/{day.day:02d}"
        f"/traces/{hexid[-2:]}/trace_full_{hexid}.json"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass  # not gzipped, use as-is
    return json.loads(raw)


def haversine_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, atan2, sqrt

    r = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * atan2(sqrt(a), sqrt(1 - a))


def fetch_merged(hexid, day: date, lookback_days=2):
    """Fetch `day` plus a few preceding days and merge into one absolute-time
    series. Traces are per-UTC-day, so a gap spanning midnight (like the
    35h Eswatini gap in HANDOFF.md §7) is invisible in a single day's file.
    """
    merged = []
    doc_type = None
    for offset in range(lookback_days, -1, -1):
        d = day - timedelta(days=offset)
        doc = fetch_trace(hexid, d)
        if doc is None:
            continue
        doc_type = doc_type or doc.get("t")
        base = doc.get("timestamp", 0)
        for p in doc.get("trace", []):
            merged.append([base + p[0]] + list(p[1:]))
    if not merged:
        return None
    merged.sort(key=lambda p: p[0])
    return {"t": doc_type, "trace": merged, "timestamp": 0}


def gap_report(trace_doc):
    """Find time gaps between consecutive points and the reachable envelope.

    ponytail: assumes a single representative ground-stop guess (30 min) for the
    headline radius, but reports the full min/max envelope across 0-2h stops so
    the UI never implies a single destination. Upgrade path: model actual
    stop-time distribution if this needs to be more rigorous.
    """
    base_ts = trace_doc.get("timestamp", 0)
    points = trace_doc.get("trace", [])
    gaps = []
    for prev, cur in zip(points, points[1:]):
        dt = cur[0] - prev[0]
        if dt < GAP_THRESHOLD_S:
            continue
        lat, lon = prev[1], prev[2]
        hours = dt / 3600
        envelope = []
        for stop_h in (0, 0.5, 1, 2):
            reach_h = max(hours - stop_h, 0) / 2
            envelope.append({"assumed_stop_h": stop_h, "radius_km": round(reach_h * CRUISE_KMH)})
        gaps.append(
            {
                "start_t": base_ts + prev[0],
                "end_t": base_ts + cur[0],
                "duration_s": dt,
                "last_known_lat": lat,
                "last_known_lon": lon,
                "last_known_alt": prev[3],
                "reachable_envelope_km": envelope,
            }
        )
    return gaps


def load_watchlist():
    if not WATCHLIST_FILE.exists():
        return []
    return [l.strip() for l in WATCHLIST_FILE.read_text().splitlines() if l.strip() and not l.startswith("#")]


def main():
    days = [date.fromisoformat(a) for a in sys.argv[1:]] or [date.today() - timedelta(days=1)]
    regs = load_watchlist()
    index = {}
    index_path = DATA / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text())

    for reg in regs:
        hexid = reg_to_hex(reg)
        if hexid in (None, "n/a", ""):
            print(f"skip {reg}: no hex from hexdb.io")
            continue
        for day in days:
            print(f"fetching {reg} ({hexid}) {day}")
            doc = fetch_merged(hexid, day)
            if doc is None:
                print(f"  no trace (not tracked that day or the two before it)")
                continue
            day_end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
            gaps = [g for g in gap_report(doc) if g["end_t"] <= day_end]
            out_dir = DATA / "traces" / hexid.lower()
            out_dir.mkdir(parents=True, exist_ok=True)
            out = {
                "reg": reg,
                "hex": hexid.lower(),
                "date": day.isoformat(),
                "type": doc.get("t"),
                "trace": doc.get("trace"),
                "timestamp": doc.get("timestamp"),
                "gaps": gaps,
            }
            (out_dir / f"{day.isoformat()}.json").write_text(json.dumps(out))
            index.setdefault(hexid.lower(), {"reg": reg, "dates": []})
            if day.isoformat() not in index[hexid.lower()]["dates"]:
                index[hexid.lower()]["dates"].append(day.isoformat())
                index[hexid.lower()]["dates"].sort()

    DATA.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2))
    print(f"wrote index with {len(index)} aircraft")


if __name__ == "__main__":
    main()
