#!/usr/bin/env python3
"""Self-check for the gap-report logic. Offline, stdlib only: python3 scripts/test_gaps.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_traces import gap_report, GAP_THRESHOLD_S, CRUISE_KMH

H = 3600


def trace(points):
    return {"timestamp": 0, "trace": points}


# [seconds, lat, lon, alt, groundspeed]
short = trace([[0, 10, 20, 30000, 450], [10 * 60, 11, 21, 30000, 450]])
assert gap_report(short) == [], "gaps under the threshold are routine dropout, not findings"

edge = trace([[0, 10, 20, 30000, 450], [GAP_THRESHOLD_S, 11, 21, 30000, 450]])
assert len(gap_report(edge)) == 1, "a gap exactly at the threshold counts"

# Airborne vs parked drives whether we draw a reachable envelope at all.
parked = trace([[0, 10, 20, "ground", 0], [5 * H, 10, 20, "ground", 0]])
assert gap_report(parked)[0]["airborne"] is False, "ground-level stop is a parked aircraft"

flying = trace([[0, 10, 20, 28700, 450], [5 * H, 11, 21, 30000, 450]])
assert gap_report(flying)[0]["airborne"] is True, "stopping at altitude means it left coverage flying"

# The envelope must shrink as the assumed ground stop grows, and never go negative.
g = gap_report(trace([[0, 10, 20, 35000, 450], [4 * H, 11, 21, 35000, 450]]))[0]
radii = [e["radius_km"] for e in g["reachable_envelope_km"]]
assert radii == sorted(radii, reverse=True), "a longer assumed stop must mean a smaller radius"
assert all(r >= 0 for r in radii), "radius can never be negative"
assert radii[0] == round(4 / 2 * CRUISE_KMH), "0h stop: half the gap out, half back"

tiny = gap_report(trace([[0, 10, 20, 35000, 450], [int(0.5 * H), 11, 21, 35000, 450]]))[0]
assert tiny["reachable_envelope_km"][-1]["radius_km"] == 0, "stop longer than the gap clamps to 0"

# Regression: the real 35h gap that motivated this tool — aircraft left coverage
# descending at 28,700 ft, so it must read as airborne with a real envelope.
real = gap_report(trace([[0, -25.64, 30.38, 28700, 400], [35 * H, 14.74, -17.49, 30000, 450]]))[0]
assert real["airborne"] is True
assert round(real["duration_s"] / H) == 35
assert real["reachable_envelope_km"][0]["radius_km"] > 10000

print("all gap-report checks passed")
