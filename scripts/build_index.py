#!/usr/bin/env python3
"""Build the per-day data the browser reads: what flew, where it went, and
where each flight began and ended.

This is the half of the pipeline a browser cannot do for itself. There is no
index upstream, so answering "what was over here that day" would otherwise mean
scanning around a gigabyte of heatmap files per query.

It is also an archive. adsb.lol keeps only a rolling ~31-day window of the
current year, so a day not captured now becomes expensive to recover later.

Files per day:
  YYYY-MM-DD.json.gz            every aircraft seen, with where its day began
                                and ended — one fetch answers most questions
  YYYY-MM-DD.flights.N.json.gz  full leg detail, sharded by first hex digit
  YYYY-MM-DD.tracks.json.gz     simplified paths, fetched only when drawing

Usage: python3 scripts/build_index.py [YYYY-MM-DD ...] [--blocks 0,1,2]
"""
import gzip
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from heatmap import BLOCKS_PER_DAY, fetch_block, parse_with_time, reg_country
from flights import legs_for

ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / "data" / "daily"

# Positions arrive every 10 s. Segmentation needs far less than that, and
# holding a whole day at full rate would cost many gigabytes — but detail near
# the ground is what departure and arrival are judged on, so keep more of it.
KEEP_LOW_S = 30
KEEP_HIGH_S = 120
LOW_ALT_FT = 12000

# Paths are thinned in proportion to how far the aircraft ranged: a fixed
# tolerance either flattens a traffic circuit or bloats a long crossing.
BLOCK_TOL_KM = 0.25
FINAL_TOL_FRAC = 0.010
FINAL_TOL_MIN_KM = 0.25
FINAL_TOL_MAX_KM = 6.0

LEVELS = {"unknown": 0, "likely": 1, "high": 2, "observed": 3}
# Sharded on two hex digits: US addresses all begin "a", so one digit puts a
# third of the world in a single file.
SHARD_CHARS = 2


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


def build_day(day: date, blocks):
    """-> (summary, tracks, samples)"""
    seen, tracks, samples, last_t = {}, {}, {}, {}
    tol = BLOCK_TOL_KM / 111.0
    for block in blocks:
        raw = fetch_block(day, block)
        if raw is None:
            print(f"  block {block:02d}: no data")
            continue
        n = 0
        buf = {}
        for t, hexid, lat, lon, alt, gs in parse_with_time(raw, block):
            n += 1
            hour = int(t // 3600)
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
            buf.setdefault(hexid, []).append((lat, lon))

            # Thinned copy for flight segmentation.
            gap = KEEP_LOW_S if alt <= LOW_ALT_FT else KEEP_HIGH_S
            if t - last_t.get(hexid, -1e9) >= gap:
                last_t[hexid] = t
                samples.setdefault(hexid, []).append((t, lat, lon, alt, gs))

        # Simplify as each block lands, so memory stays bounded.
        for hexid, pts in buf.items():
            tracks.setdefault(hexid, []).extend(simplify(pts, tol))
        print(f"  block {block:02d}: {n:,} positions, {len(seen):,} aircraft so far")
    return seen, tracks, samples


def write_day(day: date, seen, tracks, samples, blocks):
    DAILY.mkdir(parents=True, exist_ok=True)
    iso = day.isoformat()
    sizes = {}

    # Flights, sharded by first hex digit so the trace view fetches one small file.
    shards, n_legs, summary = {}, 0, {}
    for hexid, pts in samples.items():
        pts.sort()
        legs = legs_for(pts)
        if not legs:
            continue
        key = f"{hexid:06x}"
        rows = []
        for lg in legs:
            row = [lg["t0"], lg["t1"], lg["max_alt"]]
            for end in ("dep", "arr"):
                e = lg[end]
                row += [e["lat"], e["lon"], e["alt"], LEVELS[e["level"]],
                        e["icao"] or e["near_icao"] or "",
                        -1 if e["near_km"] is None else int(round(e["near_km"]))]
            rows.append(row)
        shards.setdefault(key[:SHARD_CHARS], {})[key] = rows
        n_legs += len(rows)
        # Where the day began and ended, carried in the light index so a region
        # can be filtered by airport without fetching any leg detail.
        # Every field the day touched, not just the first and last, so "what
        # came through here" catches an aircraft that only stopped mid-day.
        touched = []
        for lg in legs:
            for end in ("dep", "arr"):
                code = lg[end]["icao"]
                if code and code not in touched:
                    touched.append(code)
        summary[hexid] = (len(rows), legs[0]["dep"]["icao"] or "",
                          legs[-1]["arr"]["icao"] or "", " ".join(touched))

    total_flight_bytes = 0
    for prefix, sh in shards.items():
        spath = DAILY / f"{iso}.flights.{prefix}.json.gz"
        with gzip.open(spath, "wt", encoding="utf-8") as fh:
            json.dump({"date": iso, "flights": sh}, fh, separators=(",", ":"))
        total_flight_bytes += spath.stat().st_size
    sizes["flights"] = total_flight_bytes
    print(f"  {n_legs:,} flights across {len(summary):,} aircraft -> {len(shards)} shards "
          f"({total_flight_bytes / 1e6:.1f} MB total, largest "
          f"{max((DAILY / f'{iso}.flights.{p}.json.gz').stat().st_size for p in shards) / 1e3:.0f} KB)")

    # Paths, delta-encoded in thousandths of a degree (~110 m)
    enc = {}
    for h, pts in tracks.items():
        if len(pts) < 2:
            continue
        r = seen[h]
        span_km = max(abs(r[2] - r[0]), abs(r[3] - r[1])) * 111.0
        ftol = min(max(span_km * FINAL_TOL_FRAC, FINAL_TOL_MIN_KM), FINAL_TOL_MAX_KM)
        out_pts, pla, plo = [], 0, 0
        for la, lo in simplify(pts, ftol / 111.0):
            ila, ilo = int(round(la * 1000)), int(round(lo * 1000))
            out_pts.append(ila - pla)
            out_pts.append(ilo - plo)
            pla, plo = ila, ilo
        enc[f"{h:06x}"] = out_pts
    tpath = DAILY / f"{iso}.tracks.json.gz"
    with gzip.open(tpath, "wt", encoding="utf-8") as fh:
        json.dump({"date": iso, "scale": 1000, "tracks": enc}, fh, separators=(",", ":"))
    sizes["tracks"] = tpath.stat().st_size

    aircraft = [
        [f"{h:06x}", round(r[0], 4), round(r[1], 4), round(r[2], 4), round(r[3], 4),
         r[4], r[5], r[6], r[7], reg_country(h),
         *(summary.get(h) or (0, "", "", ""))]
        for h, r in sorted(seen.items())
    ]
    path = DAILY / f"{iso}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({
            "date": iso,
            "blocks_scanned": len(list(blocks)),
            "fields": ["hex", "min_lat", "min_lon", "max_lat", "max_lon",
                       "first_hour", "last_hour", "max_alt_ft", "positions", "reg_country",
                       "legs", "first_dep", "last_arr", "airports"],
            "aircraft": aircraft,
        }, fh, separators=(",", ":"))
    sizes["index"] = path.stat().st_size

    manifest_path = DAILY / "index.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest = [m for m in manifest if m["date"] != iso]
    manifest.append({
        "date": iso, "count": len(aircraft), "flights": n_legs,
        "blocks": len(list(blocks)), **{f"{k}_bytes": v for k, v in sizes.items()},
    })
    manifest.sort(key=lambda m: m["date"], reverse=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return path, len(aircraft)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    blocks = range(BLOCKS_PER_DAY)
    for a in sys.argv[1:]:
        if a.startswith("--blocks"):
            blocks = [int(b) for b in a.split("=", 1)[1].split(",")]

    days = [date.fromisoformat(a) for a in args] or [date.today() - timedelta(days=1)]
    for day in days:
        print(f"building {day}")
        seen, tracks, samples = build_day(day, blocks)
        if not seen:
            print(f"  no data for {day} — skipping (outside the retention window?)")
            continue
        path, n = write_day(day, seen, tracks, samples, blocks)
        print(f"  {n:,} aircraft -> {path.name} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
