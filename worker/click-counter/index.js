/**
 * pod-autopilot click counter -- Cloudflare Worker (free plan: 100k req/day).
 *
 * Why: with $0 ad spend there are no ad-platform impression/click numbers.
 * Every outbound link the pipeline publishes (Pinterest pins, Reddit posts,
 * bio links) goes through  https://<worker>/go/<exp_id>  which 302s to the
 * Square product page and bumps a KV counter. The ops run reads /stats with
 * a bearer token and writes clicks into the experiments table. That gives
 * the test gate a real denominator for conversion rate.
 *
 * Setup (free, ~5 min):
 *   npm i -g wrangler && wrangler login
 *   wrangler kv namespace create CLICKS        -> paste id into wrangler.toml
 *   wrangler secret put STATS_TOKEN            -> any long random string
 *   wrangler deploy                            -> https://pod-go.<you>.workers.dev
 *
 * Register links:  PUT /links  {"exp_abc123":"https://yourstore.square.site/product/..."}
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const auth = request.headers.get("authorization") || "";
    const ok = auth === `Bearer ${env.STATS_TOKEN}`;

    // --- redirect + count -------------------------------------------------
    if (url.pathname.startsWith("/go/")) {
      const id = url.pathname.slice(4).replace(/[^a-zA-Z0-9_-]/g, "");
      const target = await env.CLICKS.get(`link:${id}`);
      if (!target) return new Response("unknown link", { status: 404 });
      // Count humans, not previews: Pinterest/Facebook/Slack fetch links to
      // build cards. Skip obvious bots so CTR isn't inflated by crawlers.
      const ua = (request.headers.get("user-agent") || "").toLowerCase();
      const isBot = /bot|crawl|spider|preview|pinterest|facebookexternalhit|slack|curl|python-requests|headless/.test(ua);
      if (!isBot) {
        const day = new Date().toISOString().slice(0, 10);
        const k = `c:${id}:${day}`;
        const n = parseInt((await env.CLICKS.get(k)) || "0", 10) + 1;
        await env.CLICKS.put(k, String(n), { expirationTtl: 60 * 60 * 24 * 120 });
      }
      const sep = target.includes("?") ? "&" : "?";
      return Response.redirect(`${target}${sep}utm_source=pin&utm_medium=organic&utm_campaign=${id}`, 302);
    }

    if (!ok) return new Response("unauthorized", { status: 401 });

    // --- register links ---------------------------------------------------
    if (url.pathname === "/links" && request.method === "PUT") {
      const body = await request.json();
      for (const [id, target] of Object.entries(body)) {
        if (!/^https:\/\//.test(target)) continue;
        await env.CLICKS.put(`link:${id}`, target);
      }
      return Response.json({ ok: true, n: Object.keys(body).length });
    }

    // --- stats: clicks per experiment since ?days=N -----------------------
    if (url.pathname === "/stats") {
      const days = Math.min(parseInt(url.searchParams.get("days") || "30", 10), 120);
      const out = {};
      let cursor;
      do {
        const page = await env.CLICKS.list({ prefix: "c:", cursor });
        for (const { name } of page.keys) {
          const [, id, day] = name.split(":");
          const age = (Date.now() - Date.parse(day)) / 86400000;
          if (age > days) continue;
          out[id] = (out[id] || 0) + parseInt((await env.CLICKS.get(name)) || "0", 10);
        }
        cursor = page.list_complete ? undefined : page.cursor;
      } while (cursor);
      return Response.json(out);
    }

    return new Response("pod-autopilot click counter", { status: 200 });
  },
};
