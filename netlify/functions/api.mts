// Online relay for Job Bot.
//
// The real bot (resume parsing, job search, Chrome automation for LinkedIn/Dice/Workday, Gmail) runs on your PC.
// This function lets you use the same UI from anywhere:
//   - GET pages are answered from a cache the PC worker keeps up to date, so they load even when the PC is off.
//   - Changes (apply, answers, uploads, searches...) are queued; the PC worker runs them and sends the reply back.
//     If the PC answers within a few seconds you get its reply, otherwise "queued" and it runs when the PC is on.
// Passwords / API keys can only be changed on the PC; they never pass through here.
import type { Config } from "@netlify/functions";
import { getStore } from "@netlify/blobs";
import { createHash, randomUUID, timingSafeEqual } from "node:crypto";

const store = () => getStore({ name: "jobbot", consistency: "strong" });
const WAIT_MS = 7000;           // how long a change waits for the PC's reply (function limit is 10s)
const STALE_MS = 15000;         // cached GETs older than this ask the PC for a refresh
const SECRET_KEYS = ["anthropic_api_key", "imap_password", "ats_account_password", "brave_api_key", "serper_api_key",
  "online_password", "online_url", "chrome_path"];

type Cached = { at: number; status: number; ctype: string; body: string };          // body is base64
type Pending = { id: string; method: string; path: string; ctype: string; body: string; at: number };

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

function authed(req: Request) {
  const pw = process.env.APP_PASSWORD;
  if (!pw) return false;                         // refuse to run open: your resume and history live here
  const given = req.headers.get("x-app-password") || "";
  const h = (s: string) => createHash("sha256").update(s).digest();
  return timingSafeEqual(h(given), h(pw));
}

const key = (s: string) => createHash("sha1").update(s).digest("hex");
const b64 = (s: string) => Buffer.from(s).toString("base64");
const fromCached = (c: Cached, extra: Record<string, string> = {}) =>
  new Response(Buffer.from(c.body, "base64"), {
    status: c.status, headers: { "Content-Type": c.ctype || "application/json", "Cache-Control": "no-store",
      "x-cached-at": String(c.at), ...extra },
  });

async function workerSeen(): Promise<number> {
  const w = (await store().get("worker", { type: "json" })) as { last_seen?: number } | null;
  return w?.last_seen || 0;
}

async function markUiActive() {
  await store().setJSON("ui", { active_until: Date.now() + 120000 });
}

async function enqueue(p: Omit<Pending, "id" | "at">, id: string = randomUUID()): Promise<string> {
  const item: Pending = { ...p, id, at: Date.now() };
  // page refreshes use a fixed key, so repeated ones replace each other instead of piling up while the PC is off
  const k = id.startsWith("refresh-") ? `req/0-${id}` : `req/${item.at.toString().padStart(15, "0")}-${id}`;
  await store().setJSON(k, item);
  return id;
}

async function waitFor(id: string): Promise<Cached | null> {
  const end = Date.now() + WAIT_MS;
  while (Date.now() < end) {
    const r = (await store().get(`res/${id}`, { type: "json" })) as Cached | null;
    if (r) { await store().delete(`res/${id}`); return r; }
    await new Promise((s) => setTimeout(s, 400));
  }
  return null;
}

// ------------------------------------------------------------------ PC worker endpoints
async function worker(req: Request, path: string) {
  const s = store();
  await s.setJSON("worker", { last_seen: Date.now() });
  if (path === "_worker/poll") {
    const { blobs } = await s.list({ prefix: "req/" });
    const keys = blobs.map((b) => b.key).sort().slice(0, 25);
    const requests = (await Promise.all(keys.map((k) => s.get(k, { type: "json" })))).filter(Boolean);
    await Promise.all(keys.map((k) => s.delete(k)));
    const ui = (await s.get("ui", { type: "json" })) as { active_until?: number } | null;
    return json({ requests, ui_active: (ui?.active_until || 0) > Date.now() });
  }
  if (path === "_worker/respond" && req.method === "POST") {
    const body = (await req.json()) as { responses?: (Cached & { id: string })[]; cache?: Record<string, Cached> };
    const now = Date.now();
    await Promise.all([
      ...(body.responses || []).map((r) => s.setJSON(`res/${r.id}`, { ...r, at: now })),
      ...Object.entries(body.cache || {}).map(([k, c]) => s.setJSON(`cache/${key(k)}`, { ...c, at: now })),
    ]);
    return json({ ok: true });
  }
  return json({ error: "Not found" }, 404);
}

// ------------------------------------------------------------------ UI requests
export default async (req: Request) => {
  const url = new URL(req.url);
  const path = url.pathname.replace(/^\/api\/?/, "");
  if (path === "_auth") return json({ locked: !!process.env.APP_PASSWORD, authed: authed(req) });
  if (!process.env.APP_PASSWORD) return json({ error: "Set APP_PASSWORD in Netlify (Site configuration > Environment variables), then redeploy." }, 503);
  if (!authed(req)) return json({ error: "Wrong or missing password" }, 401);
  try {
    if (path.startsWith("_worker/")) return await worker(req, path);
    await markUiActive();
    if (path === "_meta") return json({ online: true, worker_last_seen: await workerSeen(), now: Date.now() });

    const full = url.pathname + url.search;        // same string the UI and the PC use as the cache key
    if (req.method === "GET") {
      const c = (await store().get(`cache/${key(full)}`, { type: "json" })) as Cached | null;
      if (c) {
        if (Date.now() - c.at > STALE_MS) await enqueue({ method: "GET", path: full, ctype: "", body: "" }, `refresh-${key(full)}`);
        return fromCached(c);
      }
      const id = await enqueue({ method: "GET", path: full, ctype: "", body: "" });
      const r = await waitFor(id);
      return r ? fromCached(r) : json({ error: "Your PC worker is offline and this page has not been loaded before." }, 503);
    }

    // a change: strip settings that may only be changed on the PC
    let ctype = req.headers.get("content-type") || "";
    let raw = Buffer.from(await req.arrayBuffer());
    if (/^\/api\/settings\//.test(url.pathname) && ctype.includes("json")) {
      const body = JSON.parse(raw.toString() || "{}");
      for (const k of SECRET_KEYS) delete body[k];
      raw = Buffer.from(JSON.stringify(body));
    }
    const id = await enqueue({ method: req.method, path: full, ctype, body: raw.toString("base64") });
    const r = await waitFor(id);
    if (r) return fromCached(r);
    return json({ queued: true, message: "Sent to your PC. It will run as soon as the PC worker is online." }, 202);
  } catch (e) {
    console.error(path, e);
    return json({ error: (e as Error).message || "Server error" }, 500);
  }
};

export const config: Config = { path: "/api/*" };
