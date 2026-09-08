/**
 * CORS proxy for adsb.lol per-aircraft traces.
 *
 * adsb.lol serves no CORS headers, so a browser can't read flight data
 * directly. This forwards exactly one kind of request — a single aircraft's
 * trace for a single day (~25 KB) — and nothing else.
 *
 * Deliberately NOT proxied: the hourly heatmap files (~14 MB each, 24/day).
 * Those stay in the nightly job. Proxying them would move real bandwidth onto
 * a volunteer-funded network via an anonymous endpoint, which is exactly the
 * thing worth not building.
 *
 * API:  GET /trace/YYYY-MM-DD/{icao24 hex}
 */

const UPSTREAM = "https://adsb.lol";
const UA = "adsb-investigator (+https://github.com/kolenyo2099/adsb-investigator)";

// Strict: date + 6 hex chars, nothing else. Keeps this from becoming an open proxy.
const ROUTE = /^\/trace\/(\d{4})-(\d{2})-(\d{2})\/([0-9a-fA-F]{6})$/;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Max-Age": "86400",
};

const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extra },
  });

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405);

    const url = new URL(request.url);
    const match = ROUTE.exec(url.pathname);
    if (!match) {
      return json(
        { error: "not_found", usage: "GET /trace/YYYY-MM-DD/{hex}, e.g. /trace/2026-08-27/a960a9" },
        404
      );
    }

    const [, year, month, day, rawHex] = match;
    const hex = rawHex.toLowerCase();

    // Reject impossible dates before spending a subrequest on them.
    const asDate = new Date(`${year}-${month}-${day}T00:00:00Z`);
    if (Number.isNaN(asDate.getTime()) || asDate.getUTCDate() !== Number(day)) {
      return json({ error: "bad_date" }, 400);
    }

    // adsb.lol sharding: traces live under the last two chars of the hex.
    const upstreamUrl = `${UPSTREAM}/globe_history/${year}/${month}/${day}/traces/${hex.slice(-2)}/trace_full_${hex}.json`;

    // Today's trace is still being appended to; past days are immutable.
    const today = new Date().toISOString().slice(0, 10);
    const isToday = `${year}-${month}-${day}` >= today;
    const ttl = isToday ? 300 : 2592000; // 5 min vs 30 days

    const cache = caches.default;
    const cacheKey = new Request(upstreamUrl, { method: "GET" });

    let response = await cache.match(cacheKey);
    let cacheStatus = "HIT";

    if (!response) {
      cacheStatus = "MISS";
      const upstream = await fetch(upstreamUrl, {
        headers: { "User-Agent": UA },
        cf: { cacheTtl: ttl, cacheEverything: true },
      });

      if (upstream.status === 404) {
        // Normal and meaningful: this aircraft wasn't tracked that day.
        return json({ error: "no_trace", hex, date: `${year}-${month}-${day}` }, 404, {
          "Cache-Control": `public, max-age=${isToday ? 300 : 86400}`,
        });
      }
      if (!upstream.ok) {
        return json({ error: "upstream_error", status: upstream.status }, 502);
      }

      // Pass the body straight through — the runtime handles gzip on both ends,
      // so there's no decode step and effectively no CPU cost here.
      response = new Response(upstream.body, {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": `public, max-age=${ttl}`,
          ...CORS,
        },
      });
      await cache.put(cacheKey, response.clone());
    }

    const out = new Response(response.body, response);
    Object.entries(CORS).forEach(([k, v]) => out.headers.set(k, v));
    out.headers.set("X-Proxy-Cache", cacheStatus);
    return out;
  },
};
