// worker/src/index.ts - Telegram webhook for langfeed: read-only commands over the D1 tables (topics, episodes).
// Add a command = add one entry to COMMANDS below.

type D1 = any; // keep the file free of @cloudflare/workers-types (wrangler bundles TS without type-checking)

export interface Env {
  DB: D1;
  TG_BOT_TOKEN: string;
  WEBHOOK_SECRET: string;
  ALLOWED_IDS: string; // comma separated Telegram user ids
  CHAT_ID?: string;    // -100xxxxxxxxxx (only used to build topic links)
}

interface Ctx { env: Env; chat: number; thread?: number; args: string[] }

const esc = (s: unknown) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const date = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }) : "?");
const num = (s: string | undefined, d: number, max: number) => Math.min(Math.max(parseInt(s ?? "", 10) || d, 1), max);

async function send(env: Env, c: { chat: number; thread?: number }, html: string) {
  const text = html.length > 3900 ? html.slice(0, 3890) + "\n…" : html;
  await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: c.chat, message_thread_id: c.thread, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
}

async function all(env: Env, sql: string, ...p: unknown[]): Promise<any[]> {
  return (await env.DB.prepare(sql).bind(...p).all()).results ?? [];
}

const line = (r: any) => `• ${esc(date(r.pub_ts))} · ` + (r.link ? `<a href="${esc(r.link)}">${esc(r.title)}</a>` : esc(r.title));

const COMMANDS: Record<string, { help: string; run: (c: Ctx) => Promise<string> }> = {
  stats: {
    help: "how many episodes per site",
    run: async ({ env }) => {
      const rows = await all(env,
        `SELECT e.site_id, COALESCE(t.title, e.site_id) AS name,
                SUM(e.status='sent') AS sent, SUM(e.status='skipped') AS skipped, MAX(e.pub_ts) AS last_pub, MAX(e.sent_at) AS last_sent
         FROM episodes e LEFT JOIN topics t ON t.site_id = e.site_id GROUP BY e.site_id ORDER BY name`);
      if (!rows.length) return "No data yet.";
      const total = rows.reduce((a, r) => a + (r.sent || 0), 0);
      return `<b>Sent to Telegram: ${total}</b>\n\n` + rows.map((r) =>
        `<b>${esc(r.name)}</b>\n  sent: ${r.sent || 0} · older, not sent: ${r.skipped || 0}\n  latest episode: ${esc(date(r.last_pub))}`).join("\n\n");
    },
  },
  list: {
    help: "latest episodes, e.g. /list bbc 5 (default 10, max 30)",
    run: async ({ env, args }) => {
      const site = args.find((a) => isNaN(Number(a)));
      const n = num(args.find((a) => !isNaN(Number(a))), 10, 30);
      const rows = site
        ? await all(env, `SELECT title, link, pub_ts FROM episodes WHERE status='sent' AND site_id=? ORDER BY pub_ts DESC LIMIT ?`, site.toLowerCase(), n)
        : await all(env, `SELECT title, link, pub_ts FROM episodes WHERE status='sent' ORDER BY pub_ts DESC LIMIT ?`, n);
      return rows.length ? rows.map(line).join("\n") : "Nothing found.";
    },
  },
  find: {
    help: "search titles, e.g. /find gold",
    run: async ({ env, args }) => {
      if (!args.length) return "Usage: /find <words>";
      const rows = await all(env, `SELECT title, link, pub_ts FROM episodes WHERE status='sent' AND title LIKE ? ORDER BY pub_ts DESC LIMIT 20`, `%${args.join(" ")}%`);
      return rows.length ? rows.map(line).join("\n") : "Nothing found.";
    },
  },
  sites: {
    help: "sites and their topics",
    run: async ({ env }) => {
      const rows = await all(env, `SELECT site_id, topic_id, title FROM topics ORDER BY site_id`);
      const cid = (env.CHAT_ID || "").replace(/^-100/, "");
      return rows.length
        ? rows.map((r) => `• <b>${esc(r.title)}</b> (<code>${esc(r.site_id)}</code>)` + (cid ? ` — <a href="https://t.me/c/${cid}/${r.topic_id}">topic</a>` : ` — topic ${r.topic_id}`)).join("\n")
        : "No topics yet.";
    },
  },
};

const BASE_HELP: [string, string][] = [["start", "show this list"], ["help", "show this list"]];
const helpText = () => "<b>langfeed commands</b>\n" +
  [...BASE_HELP, ...Object.entries(COMMANDS).map(([k, v]) => [k, v.help] as [string, string])].map(([k, h]) => `/${k} — ${esc(h)}`).join("\n");

export default {
  async fetch(req: Request, env: Env, ctx: { waitUntil(p: Promise<unknown>): void }): Promise<Response> {
    if (req.method !== "POST") return new Response("ok");
    if (req.headers.get("x-telegram-bot-api-secret-token") !== env.WEBHOOK_SECRET) return new Response("forbidden", { status: 401 });
    const u: any = await req.json().catch(() => null);
    const m = u?.message;
    if (!m?.text || !m.text.startsWith("/")) return new Response("ok");
    const where = { chat: m.chat.id as number, thread: m.is_topic_message ? (m.message_thread_id as number) : undefined };
    const work = (async () => {
      const allowed = (env.ALLOWED_IDS || "").split(",").map((s) => s.trim()).filter(Boolean);
      if (!allowed.includes(String(m.from?.id))) {
        return send(env, where, `Not allowed. Your Telegram id is <code>${m.from?.id}</code>; add it to the ALLOWED_IDS secret.`);
      }
      const [first, ...args] = m.text.trim().split(/\s+/);
      const name = first.slice(1).split("@")[0].toLowerCase();
      if (name === "start" || name === "help") return send(env, where, helpText());
      const cmd = COMMANDS[name];
      if (!cmd) return send(env, where, "Unknown command.\n\n" + helpText());
      try {
        await send(env, where, await cmd.run({ env, ...where, args }));
      } catch (e) {
        await send(env, where, "Error: " + esc((e as Error).message));
      }
    })();
    ctx.waitUntil(work);
    return new Response("ok");
  },
};
