# ADS-B Investigator — session handoff

Cold-start brief. Everything below marked ✅ was **empirically verified** on
2026-09-08, not assumed. Re-verify anything time-sensitive (CORS policies and
GitHub URL shapes do change).

---

## 1. What this is

A browser tool for investigative journalists to query historical ADS-B flight
data — built out of the workflow that identified the aircraft on a US
third-country deportation flight to Burundi (see §7 for the worked example).

**Sister project:** `~/Documents/agreements timeline 3` (the removals timeline).
This tool feeds that investigation but is a separate codebase.

---

## 2. The single most important design truth

**ADS-B has no flight plan. There is no origin, no destination, no country.**
You infer them from where a track starts, descends and stops — and in the
regions this investigation cares about there is often *no receiver coverage at
all*.

Verified examples:
- Bangui (CAR): **nothing within 444 km** across a whole day, and that was an
  overflight at FL400.
- N703KW vanished for **35 hours** across Eswatini, Burundi and Rwanda.
- N700KW vanished for **3 h 41 m** over Cameroon.

So: **the gap is the finding.** A UI offering "destination country" would be
most confident exactly where the evidence is thinnest. Surface *observed
positions, descents and gaps* — never an inferred destination.

Derivable and honest:
- **Registration country** — exact, free, from the ICAO hex range.
  US = `0xA00000`–`0xADF7C7`. This alone cut a whole West-Africa corridor scan
  down to 4 aircraft, 3 of which were Delta airliners.
- **Country overflown** — point-in-polygon in the precompute step.
  (`github.com/adsblol/naturalearthvector` has the boundaries.)

Not derivable: **destination**. Do not ship it.

---

## 3. Verified data-source facts

### CORS — the constraint that shapes the whole architecture ✅
Tested with a real cross-origin `fetch()` from `https://example.org`:

| Endpoint | CORS | Browser-usable |
|---|---|---|
| `adsb.lol/globe_history/...` (traces + heatmaps) | none | ❌ |
| `api.adsb.lol/v2/...` | none | ❌ |
| `opendata.adsb.fi/api/v2/...` | none | ❌ |
| `opensky-network.org/api/...` | own origin only | ❌ |
| `api.airplanes.live` | 403 | ❌ |
| `github.com/.../releases/download/...` | none | ❌ |
| `release-assets.githubusercontent.com` | none | ❌ |
| `api.github.com` | ✅ | ✅ |
| `raw.githubusercontent.com` | `*` | ✅ |
| **`*.github.io` (Pages)** | `*` | ✅ |
| `hexdb.io` | `*` | ✅ |

**Conclusion: a browser cannot fetch ADS-B position data from anywhere.**
This is deliberate load control, not an oversight. Everything in §5 follows.

`DecompressionStream('gzip')` **is** available in-browser ✅ — gzip streaming is
fine once bytes arrive.

### adsb.lol direct paths (server-side only)
- Per-aircraft trace: `https://adsb.lol/globe_history/YYYY/MM/DD/traces/{hex[-2:]}/trace_full_{hex}.json`
  - gzipped JSON, ~25 KB. **This is the cheap, precise primitive.**
  - `globe.adsb.lol` 302-redirects to `adsb.lol` — follow redirects.
  - Shape: `{icao, r: registration, t: type, timestamp: <epoch base>, trace: [[...]]}`
  - Trace element (verified indices): `[0]` seconds after `timestamp`,
    `[1]` lat, `[2]` lon, `[3]` alt (ft, or `"ground"`), `[4]` ground speed kt.
    Later indices exist but were not needed/verified.
  - 404 = aircraft not tracked that day (normal, not an error).
- Hourly heatmap (all aircraft, global): `.../YYYY/MM/DD/heatmap/HH.bin.ttf`
  - **Served as `.ttf` but is gzip.** ~14 MB gz → 15.9 MB raw → ~993k records.
  - **Record = 16 bytes little-endian**, `struct` format `<IiihH`:
    `uint32 hex` (ICAO in low 24 bits, high byte = flags),
    `int32 lat*1e6`, `int32 lon*1e6`, `int16 alt/25`, `uint16 gs*10`.
  - ~84% of records decode to plausible coordinates; the rest are
    timestamp/marker records. **Validate by plausibility**, don't assume.
  - No spatial index — any area query is a full scan.

### GitHub mirror ✅
`github.com/adsblol/globe_history_2026` (also `_2025`, `_2024`, `_2023`).
- **One release per day**, tag shape `v2026.09.07-planes-readsb-prod-0`.
  Also `-staging-` and `-mlatonly-` variants; **use `prod`**.
- Assets are a tar split into 2 GB parts (`.tar.aa`, `.tar.ab`).
  **A single day = ~3.86 GB.**
- Uncompressed `ustar`. Members: `./LICENSE-cc0.txt`, `./heatmap/HH.bin.ttf`
  (~18 MB each), and the traces.
- **HTTP Range requests work** ✅ (`206`, `accept-ranges: bytes`) — individual
  members are byte-addressable once you know their offset.
- **Licence: CC0 1.0** ✅ (public domain). **You may freely republish derived
  data.** This is what makes §5 legal without asking anyone.

### hexdb.io
- `https://hexdb.io/reg-hex?reg=N703KW` → `A960A9`
- `https://hexdb.io/hex-reg?hex=aa2542` and `/hex-type?hex=...`
- CORS `*` ✅. **Rate-limits aggressively** — send a real User-Agent and
  sleep ~0.8 s between calls or it returns `n/a` for everything.
- `n/a` means "not in their DB", which is itself interesting (unregistered /
  state aircraft), not necessarily an error.

---

## 4. Auth state on this machine ⚠️

- **`gh` is NOT installed.** `brew install gh` if wanted (brew is at
  `/opt/homebrew/bin/brew`).
- **No SSH keys exist**; `ssh -T git@github.com` → permission denied.
- **A working OAuth token IS in the macOS keychain** (VS Code's, most likely).
  Retrieve it with:
  ```bash
  printf "protocol=https\nhost=github.com\n\n" | git credential fill
  ```
  - GitHub login: **kolenyo2099**
  - Scopes: `read:user, repo, user:email, workflow` ✅ — enough to create repos,
    push, and write Actions workflows.
  - **Use HTTPS remotes**, not SSH.
  - ⚠️ Do not echo the token into logs or transcripts.

---

## 5. The architecture (decided)

The browser cannot fetch the archive, and the archive's granularity is a
3.86 GB day. So **invert it**: a GitHub Action fetches and derives; the browser
reads *your own* CORS-open output. CC0 makes this permitted.

```
  adsb.lol / adsblol GitHub releases   (3.86 GB/day, no CORS)
                 │
                 ▼   nightly GitHub Action (free on public repos)
        derive small artifacts
                 │
                 ▼   commit / deploy to GitHub Pages  (CORS: *)
        static browser app  ──►  dropdowns read a few MB, not terabytes
```

**Layer 1 — nightly Action** emits:
- `aircraft/YYYY-MM-DD.json` — every hex seen, bbox, first/last seen, registration country
- `by-country/YYYY-MM-DD/XX.json` — hexes observed over each country (point-in-polygon)
- `traces/{hex}/YYYY-MM-DD.json` — full trace, watchlisted aircraft only (~25 KB)

**Layer 2 — static page on GitHub Pages**: dropdowns (date / country /
registration country / altitude band) query the index; selecting an aircraft
fetches its trace and renders track + **gap report**.

**Host on GitHub Pages, not releases** — Pages has CORS, releases do not.
Deploy from a force-pushed `gh-pages` branch so daily commits don't bloat history.

### Rejected, with reasons
- **Pure browser-only**: impossible. No ADS-B source allows cross-origin reads.
- **CORS proxy (Worker/Deno)**: works, but *you* own the bandwidth for 14 MB
  files and the reputational risk of hammering a volunteer-fed network.
- **Browser extension**: genuinely serverless and viable — good fallback if
  Pages/Actions proves awkward. Install friction for a small team is minor.
- **Ask adsb.lol to add `Access-Control-Allow-Origin`**: costs an email, and
  would make much of this unnecessary. Worth doing in parallel.

### Pyodide
Do **not** use it for parsing — a JS `DataView` beats it on 16-byte record
scans. **Do** offer it as an optional analysis layer with the day's records
preloaded as a DataFrame, so a journalist can write pandas without leaving the
page. That's worth the ~10 MB; parsing isn't.

---

## 6. Build order

1. **Nightly Action + watchlist traces.** Smallest useful thing; no CORS tricks;
   this is the half that did the real work. Seed the watchlist from §7.
2. **Gap report.** The differentiator — see §7 for the exact algorithm.
3. **Static Pages UI** — date/country dropdowns over the index.
4. **Area discovery** (heatmap scan) — expensive, do last, and time-box it in
   the UI by design rather than by hope.
5. Optional: Pyodide analysis cell.

**Politeness matters.** adsb.lol is volunteer-fed. Cache aggressively, never
re-fetch a day you already have, and identify yourself in the User-Agent.

---

## 7. Regression targets — the answers are already known

Use these as tests, not demos.

**N703KW** — Eastern Airlines B767-336ER, hex **`A960A9`**. 27 Aug 2026.
Alexandria LA (26 Aug 17:28Z) → Atlantic → Dakar/Diass (27 Aug 04:41Z, 725 ft)
→ over Botswana FL370 (11:51Z) → **descending into Eswatini 12:21Z at 28,700 ft,
155 km out — coverage ends** → *[35 h gap: Eswatini, Burundi, Rwanda]* →
Dakar (28 Aug 23:59Z) → Alexandria LA → El Paso (29 Aug 15:53Z).

**N700KW** — same operator/type, hex **`A95584`**. A *separate* run, 29–30 Aug.
Fort Worth Alliance (28 Aug 23:48Z) → Dakar (**29 Aug 09:27Z, 425 ft**) →
*[11.8 h gap]* → over Cameroon FL370 eastbound (21:12–21:20Z) →
***[3 h 41 m gap]*** → westbound from the same point (30 Aug 01:01Z) → Dakar
(05:14Z) → Fort Worth (31 Aug 13:16Z).

**Gap-report algorithm** (validated against the above): for a gap of duration
`T` at turn point `P`, with cruise ~850 km/h and assumed ground stop `g`,
reachable radius = `(T − g)/2 × speed`. For N700KW's 221-minute gap that gives
1,353 km at a 30-min stop down to 715 km at 2 h — which **excludes** Burundi
(2,138 km), Rwanda (2,158 km) and Eswatini (4,114 km), and **includes** Yaoundé
(201), Douala (314), Malabo (426), Libreville (631), Bangui (712).
*Report the envelope, never a single guessed destination.*

**Watchlist seed** (ICE Air contractors seen in this investigation):
`N703KW N705KW N700KW` (Eastern) · `N207AX N225AX N351AX N378AX N423AX N486AX N819AX N846AX` (Omni) ·
`N588AT N588TN N996GA N352BH` (Journey) · `N276GX N278GX N281GX N630VA N837VA` (GlobalX) ·
`N59JE N50JE` (Talon) · `N668CP N624XA` (Eastern Air Express)

---

## 8. Gotchas that cost time

- The heatmap files are **`.ttf` but gzip**. Check magic bytes, not extension.
- Uncompressed heatmap size is exactly divisible by 16 — use that to confirm
  the record layout before trusting any decode.
- `curl -I` (HEAD) is **not** a reliable CORS test. Only a real browser
  `fetch()` from a real origin is. `fetch` throws the same
  `TypeError: Failed to fetch` for a CORS block and a dead host — disambiguate
  server-side.
- OpenSky's historical endpoint refuses anonymous access
  ("You cannot access historical flights"). Don't build on it.
- Central/East Africa has effectively **no ADS-B coverage**. Absence of a track
  is not absence of a flight. Never let the UI imply otherwise.
- ET↔local timezone conversion matters for arrival dates (it decided a whole
  date-convention question in the sister project). Convert explicitly, and mind
  US DST boundaries.
