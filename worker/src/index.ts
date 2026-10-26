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
  // ---- catalog written by scripts/scan.py (tables cat_sites, cat_pages, cat_files)
  catalog: {
    help: "scanned sites: pages, files, file types",
    run: async ({ env }) => {
      const rows = await all(env, `SELECT site_id, name, status, pages, files, last_scan FROM cat_sites ORDER BY name`);
      if (!rows.length) return "Nothing scanned yet. Run the scan workflow first.";
      const types = await all(env, `SELECT site_id, ext, COUNT(*) n FROM cat_files GROUP BY site_id, ext ORDER BY n DESC`);
      const by: Record<string, string[]> = {};
      for (const t of types) (by[t.site_id] ||= []).length < 4 && by[t.site_id].push(`${t.ext} ${t.n}`);
      return `<b>${rows.reduce((a, r) => a + r.pages, 0)} pages · ${rows.reduce((a, r) => a + r.files, 0)} files</b>\n\n` + rows.map((r) =>
        `<b>${esc(r.name)}</b> (<code>${esc(r.site_id)}</code>)${r.status === "partial" ? " ⏳" : r.status === "blocked" ? " 🚫" : ""}\n  ${r.pages} pages · ${r.files} files` +
        (by[r.site_id]?.length ? ` (${esc(by[r.site_id].join(", "))})` : "")).join("\n\n");
    },
  },
  sections: {
    help: "sections of a site, e.g. /sections bbc",
    run: async ({ env, args }) => {
      if (!args[0]) return "Usage: /sections <site id>  (ids: /catalog)";
      const rows = await all(env,
        `SELECT p.section, COUNT(*) pages, (SELECT COUNT(*) FROM cat_files f WHERE f.site_id = p.site_id AND f.section = p.section) files
         FROM cat_pages p WHERE p.site_id=? GROUP BY p.section ORDER BY pages DESC LIMIT 40`, args[0].toLowerCase());
      return rows.length ? rows.map((r) => `• <code>${esc(r.section)}</code> — ${r.pages} pages · ${r.files} files`).join("\n") : "Nothing found.";
    },
  },
  search: {
    help: "search pages, e.g. /search present perfect  or  /search bbc gold",
    run: async ({ env, args }) => {
      if (!args.length) return "Usage: /search [site] <words>";
      const ids = (await all(env, `SELECT site_id FROM cat_sites`)).map((r) => r.site_id);
      const site = ids.includes(args[0].toLowerCase()) ? args[0].toLowerCase() : "";
      const words = (site ? args.slice(1) : args).slice(0, 6);
      if (!words.length) return "Usage: /search [site] <words>";
      const hay = `(COALESCE(title,'') || ' ' || COALESCE(descr,'') || ' ' || COALESCE(body,''))`;
      const rows = await all(env,
        `SELECT site_id, url, title FROM cat_pages WHERE ${words.map(() => `${hay} LIKE ?`).join(" AND ")}${site ? " AND site_id=?" : ""}
         ORDER BY (title LIKE ?) DESC, LENGTH(url) LIMIT 15`,
        ...words.map((w) => `%${w}%`), ...(site ? [site] : []), `%${words[0]}%`);
      return rows.length ? rows.map((r) => `• <code>${esc(r.site_id)}</code> <a href="${esc(r.url)}">${esc(r.title || r.url)}</a>`).join("\n") : "Nothing found.";
    },
  },
  files: {
    help: "files, e.g. /files bbc pdf 10  or  /files mp3 grammar",
    run: async ({ env, args }) => {
      const ids = (await all(env, `SELECT site_id FROM cat_sites`)).map((r) => r.site_id);
      const exts = (await all(env, `SELECT DISTINCT ext FROM cat_files`)).map((r) => r.ext);
      let site = "", ext = "", n = 10;
      const words: string[] = [];
      for (const a of args) {
        const l = a.toLowerCase();
        if (!site && ids.includes(l)) site = l;
        else if (!ext && exts.includes(l.replace(/^\./, ""))) ext = l.replace(/^\./, "");
        else if (!isNaN(Number(a))) n = num(a, 10, 30);
        else words.push(a);
      }
      const cond = ["1=1"], p: unknown[] = [];
      if (site) { cond.push("site_id=?"); p.push(site); }
      if (ext) { cond.push("ext=?"); p.push(ext); }
      for (const w of words.slice(0, 5)) { cond.push("(name LIKE ? OR url LIKE ?)"); p.push(`%${w}%`, `%${w}%`); }
      const rows = await all(env, `SELECT site_id, url, name, bytes, ext FROM cat_files WHERE ${cond.join(" AND ")} ORDER BY found_at DESC, url LIMIT ?`, ...p, n);
      const size = (b: number) => (b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : b > 1024 ? Math.round(b / 1024) + " KB" : "");
      return rows.length ? rows.map((r) => `• <code>${esc(r.site_id)}</code> <a href="${esc(r.url)}">${esc(r.name || r.url)}</a> ${size(r.bytes)}`.trim()).join("\n") : "Nothing found.";
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
