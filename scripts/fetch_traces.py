#!/usr/bin/env python3
"""Fetch an aircraft's trace from adsb.lol and report its coverage gaps.

The same analysis the web app does, from the command line — useful for scripting
or checking several aircraft at once. Stdlib only.

Usage:
  python3 scripts/fetch_traces.py N703KW 2026-08-27
  python3 scripts/fetch_traces.py a960a9 N700KW 2026-08-27 2026-08-28
  python3 scripts/fetch_traces.py N703KW 2026-08-27 --save

Tail numbers are resolved via hexdb.io; six hex characters are used directly.
Dates default to yesterday (UTC). --save also writes the full trace as JSON.
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
OUT_DIR_FLAG = "--save"
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


def fetch_merged(hexid, day: date, lookback_days=2):
    """Fetch `day` plus a few preceding days and merge into one absolute-time
    series. Traces are per-UTC-day, so a gap spanning midnight is otherwise
    invisible — it looks like two unrelated fragments.
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
                # Stopping at ground level is a parked aircraft; stopping at
                # altitude means it left coverage still flying — the case worth
                # looking at.
                "airborne": prev[3] != "ground",
                "reachable_envelope_km": envelope,
            }
        )
    return gaps


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    # Anything date-shaped is a date; everything else is an aircraft.
    days, aircraft = [], []
    for a in args:
        try:
            days.append(date.fromisoformat(a))
        except ValueError:
            aircraft.append(a)
    if not days:
        days = [date.today() - timedelta(days=1)]
    if not aircraft:
        print("No aircraft given.\n")
        print(__doc__)
        sys.exit(1)

    aircraft = [a for a in aircraft if a != OUT_DIR_FLAG]
    for item in aircraft:
        hexid = item.lower() if len(item) == 6 and all(c in "0123456789abcdef" for c in item.lower()) else reg_to_hex(item)
        if hexid in (None, "n/a", ""):
            print(f"skip {item}: hexdb.io couldn't resolve it to an ICAO address")
            continue
        for day in days:
            doc = fetch_merged(hexid, day)
            if doc is None:
                print(f"{item} ({hexid}) {day}: no trace — not tracked that day or the two before it")
                continue
            day_end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
            gaps = [g for g in gap_report(doc) if g["end_t"] <= day_end]
            airborne = [g for g in gaps if g["airborne"]]
            print(f"{item} ({hexid}) {day}: {len(doc['trace'])} points, "
                  f"{len(gaps)} gaps ({len(airborne)} airborne)")
            for g in airborne:
                mins = round(g["duration_s"] / 60)
                widest = max(e["radius_km"] for e in g["reachable_envelope_km"])
                print(f"    {mins} min from ({g['last_known_lat']:.2f}, {g['last_known_lon']:.2f}) "
                      f"@ {g['last_known_alt']} ft — reachable within {widest} km")

            if OUT_DIR_FLAG in args:
                out_dir = DATA / "traces" / hexid
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{day.isoformat()}.json").write_text(json.dumps(
                    {"reg": item, "hex": hexid, "date": day.isoformat(), "type": doc.get("t"),
                     "trace": doc.get("trace"), "timestamp": doc.get("timestamp"), "gaps": gaps}))
                print(f"    saved {out_dir / (day.isoformat() + '.json')}")


if __name__ == "__main__":
    main()
