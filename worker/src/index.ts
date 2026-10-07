// Booth's reply service: Telegram sends each of Dan's messages here, and Booth answers with Claude.
//
// POST /telegram       Telegram's webhook (checked against the secret set with setWebhook)
// POST /context        the Mac uploads Booth's current data after reports and data changes
// GET  /changes        the Mac fetches notes, and roster moves and reruns to carry out (?chat=<id>)
// POST /changes/ack    the Mac reports how each change went
// GET  /chats          everyone who has messaged the bot (for connecting a new chat)
import Anthropic from "@anthropic-ai/sdk";
import { DurableObject } from "cloudflare:workers";

import {
  Change, Note, Prices, RUNS, RUN_LABELS, TOOLS, Turn, Upload, checkMove, context, conversation, cost, finalText,
  instructions, situation, userText,
} from "./prompt";
import { deriveSecret, sameSecret } from "./secrets";
import { Incoming, parseUpdate, sendText, typing, useApi } from "./telegram";

export interface Env {
  ANTHROPIC_API_KEY: string;
  TELEGRAM_BOT_TOKEN: string;
  BOOTH: DurableObjectNamespace<Booth>;
  MODEL?: string;
  EFFORT?: "low" | "medium" | "high" | "xhigh" | "max";
  MONTHLY_CAP_USD?: string;
  PRICE_INPUT?: string;
  PRICE_OUTPUT?: string;
  PRICE_CACHE_WRITE?: string;
  PRICE_CACHE_READ?: string;
  PRICE_SEARCH?: string;
  // Local testing only: fake Telegram and Anthropic servers.
  TELEGRAM_API?: string;
  ANTHROPIC_BASE_URL?: string;
}

const HISTORY = 16; // earlier messages, both sides, sent with each new one
const KEEP_HISTORY = 60;
const MAX_ROUNDS = 10; // API calls per answer (tool use and paused research turns)
const MAX_UPLOAD = 2_000_000; // bytes

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const booth = env.BOOTH.get(env.BOOTH.idFromName("booth"));

    if (request.method === "POST" && url.pathname === "/telegram") {
      const given = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
      if (!sameSecret(given, await deriveSecret(env.TELEGRAM_BOT_TOKEN, "webhook"))) {
        return new Response("forbidden", { status: 403 });
      }
      const incoming = parseUpdate(await request.json().catch(() => null));
      // Answered later from the queue, so Telegram gets its 200 at once and never resends.
      if (incoming) await booth.enqueue(incoming);
      return new Response("ok");
    }

    const auth = request.headers.get("Authorization") ?? "";
    if (!sameSecret(auth, `Bearer ${await deriveSecret(env.TELEGRAM_BOT_TOKEN, "api")}`)) {
      return new Response("forbidden", { status: 403 });
    }
    if (request.method === "POST" && url.pathname === "/context") {
      const body = await request.text();
      if (body.length > MAX_UPLOAD) return Response.json({ error: "too big" }, { status: 413 });
      let u: Upload;
      try {
        u = JSON.parse(body) as Upload;
      } catch {
        return Response.json({ error: "not JSON" }, { status: 400 });
      }
      if (!/^-?\d+$/.test(String(u?.chat ?? "")) || typeof u.context !== "object" || !u.context) {
        return Response.json({ error: "chat and context are required" }, { status: 400 });
      }
      await booth.storeContext({ chat: String(u.chat), built_at: String(u.built_at ?? ""), context: u.context });
      return Response.json({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/changes") {
      return Response.json(await booth.changesFor(url.searchParams.get("chat") ?? ""));
    }
    if (request.method === "POST" && url.pathname === "/changes/ack") {
      const b = (await request.json().catch(() => null)) as { chat?: string; results?: unknown } | null;
      if (!b || !Array.isArray(b.results)) return Response.json({ error: "results are required" }, { status: 400 });
      await booth.ack(String(b.chat ?? ""), b.results as { id: number; ok: boolean; detail: string }[]);
      return Response.json({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/chats") {
      return Response.json({ chats: await booth.chats() });
    }
    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

function month(now = new Date()): string {
  return now.toLocaleDateString("en-CA", { timeZone: "America/New_York" }).slice(0, 7);
}

// One instance holds all of Booth's chat state in SQLite and works through messages in order.
export class Booth extends DurableObject<Env> {
  sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    useApi(env.TELEGRAM_API);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS context (id INTEGER PRIMARY KEY CHECK (id = 1), chat TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, text TEXT, at TEXT);
      CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, kind TEXT, data TEXT,
                                          status TEXT DEFAULT 'pending', detail TEXT DEFAULT '', at TEXT, done_at TEXT);
      CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, role TEXT, text TEXT, at TEXT);
      CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, chat TEXT, data TEXT, done INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS chats (chat TEXT PRIMARY KEY, name TEXT, text TEXT, at TEXT, greeted INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS spend (month TEXT PRIMARY KEY, usd REAL NOT NULL DEFAULT 0, answers INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    `);
  }

  async enqueue(m: Incoming): Promise<void> {
    // The update id makes a resent update a no-op.
    this.sql.exec("INSERT OR IGNORE INTO updates (id, chat, data) VALUES (?, ?, ?)", m.updateId, m.chat, JSON.stringify(m));
    this.sql.exec(
      `INSERT INTO chats (chat, name, text, at) VALUES (?, ?, ?, ?)
       ON CONFLICT (chat) DO UPDATE SET name = excluded.name, text = excluded.text, at = excluded.at`,
      m.chat, m.name, m.text.slice(0, 200), new Date().toISOString(),
    );
    await this.ctx.storage.setAlarm(Date.now());
  }

  // Only one chat is Booth's: whichever the Mac last uploaded data for.
  async storeContext(u: Upload): Promise<void> {
    this.sql.exec(
      "INSERT OR REPLACE INTO context (id, chat, data, at) VALUES (1, ?, ?, ?)",
      u.chat, JSON.stringify(u), new Date().toISOString(),
    );
    this.setMeta("mac_seen_at", new Date().toISOString());
  }

  upload(): Upload | null {
    const row = this.sql.exec("SELECT data FROM context WHERE id = 1").toArray()[0];
    return row ? (JSON.parse(row.data as string) as Upload) : null;
  }

  setMeta(key: string, value: string): void {
    this.sql.exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", key, value);
  }

  meta(key: string): string | null {
    return (this.sql.exec("SELECT value FROM meta WHERE key = ?", key).toArray()[0]?.value as string) ?? null;
  }

  notes(chat: string): Note[] {
    return this.sql.exec("SELECT id, text, at FROM notes WHERE chat = ? ORDER BY id", chat).toArray() as unknown as Note[];
  }

  changes(chat: string, where: string): Change[] {
    return this.sql
      .exec(`SELECT id, kind, data, status, detail, at FROM changes WHERE chat = ? AND ${where} ORDER BY id`, chat)
      .toArray()
      .map((r) => ({ ...r, data: JSON.parse(r.data as string) })) as unknown as Change[];
  }

  async changesFor(chat: string): Promise<{ notes: Note[]; changes: Change[] }> {
    this.setMeta("mac_seen_at", new Date().toISOString());
    return { notes: this.notes(chat), changes: this.changes(chat, "status = 'pending'") };
  }

  async ack(chat: string, results: { id: number; ok: boolean; detail: string }[]): Promise<void> {
    const now = new Date().toISOString();
    for (const r of results) {
      if (typeof r?.id !== "number") continue;
      this.sql.exec(
        "UPDATE changes SET status = ?, detail = ?, done_at = ? WHERE id = ? AND chat = ? AND status = 'pending'",
        r.ok ? "applied" : "failed", String(r.detail ?? "").slice(0, 300), now, r.id, chat,
      );
    }
    this.sql.exec("DELETE FROM changes WHERE status != 'pending' AND done_at < ?", new Date(Date.now() - 14 * 86400_000).toISOString());
  }

  async chats(): Promise<object[]> {
    return this.sql.exec("SELECT chat AS id, name, text, at FROM chats ORDER BY at DESC").toArray();
  }

  // Answers queued messages one at a time, oldest first. An alarm can run for minutes,
  // which leaves room for research.
  async alarm(): Promise<void> {
    for (;;) {
      const next = this.sql.exec("SELECT id, data FROM updates WHERE done = 0 ORDER BY id LIMIT 1").toArray()[0];
      if (!next) break;
      // Marked first, so a message that crashes the alarm is never answered twice.
      this.sql.exec("UPDATE updates SET done = 1 WHERE id = ?", next.id);
      const m = JSON.parse(next.data as string) as Incoming;
      try {
        await this.answer(m);
      } catch (e) {
        console.error(`Couldn't answer update ${m.updateId}:`, e instanceof Error ? e.message : e);
        await sendText(this.env.TELEGRAM_BOT_TOKEN, m.chat, "Booth hit a snag answering that. Try again in a minute?").catch(() => {});
      }
    }
    this.sql.exec("DELETE FROM updates WHERE done = 1 AND id < (SELECT MAX(id) - 500 FROM updates)");
  }

  prices(): Prices {
    const n = (v: string | undefined, d: number) => (v && !Number.isNaN(Number(v)) ? Number(v) : d);
    return {
      input: n(this.env.PRICE_INPUT, 5),
      output: n(this.env.PRICE_OUTPUT, 25),
      cacheWrite: n(this.env.PRICE_CACHE_WRITE, 6.25),
      cacheRead: n(this.env.PRICE_CACHE_READ, 0.5),
      search: n(this.env.PRICE_SEARCH, 0.01),
    };
  }

  spent(): number {
    return (this.sql.exec("SELECT usd FROM spend WHERE month = ?", month()).toArray()[0]?.usd as number) ?? 0;
  }

  addSpend(usd: number): void {
    this.sql.exec(
      `INSERT INTO spend (month, usd, answers) VALUES (?, ?, 1)
       ON CONFLICT (month) DO UPDATE SET usd = usd + excluded.usd, answers = answers + 1`,
      month(), usd,
    );
  }

  // Runs one of Booth's own tools and returns what to tell Claude.
  runTool(chat: string, name: string, input: Record<string, unknown>): { text: string; error?: boolean } {
    const now = new Date().toISOString();
    if (name === "save_note") {
      const text = String(input.text ?? "").trim().slice(0, 500);
      if (!text) return { text: "text is required", error: true };
      if (this.notes(chat).length >= 40) return { text: "There are 40 notes already; remove an old one first.", error: true };
      this.sql.exec("INSERT INTO notes (chat, text, at) VALUES (?, ?, ?)", chat, text, now);
      return { text: "Saved. Future reports and answers will follow it." };
    }
    if (name === "remove_note") {
      const gone = this.sql.exec("DELETE FROM notes WHERE chat = ? AND id = ?", chat, Number(input.id)).rowsWritten;
      return gone ? { text: "Removed." } : { text: `No note with id ${input.id}.`, error: true };
    }
    if (name === "record_roster_move") {
      const problem = checkMove(input);
      if (problem) return { text: problem, error: true };
      this.sql.exec("INSERT INTO changes (chat, kind, data, at) VALUES (?, 'roster', ?, ?)", chat, JSON.stringify(input), now);
      return { text: "Queued. Dan's Mac updates the roster copy at its next check, before any report it builds." };
    }
    if (name === "request_report_rerun") {
      const run = String(input.run ?? "");
      if (!RUNS.includes(run as never)) return { text: `run must be one of ${RUNS.join(", ")}`, error: true };
      const waiting = this.changes(chat, "status = 'pending' AND kind = 'rerun'").some((c) => c.data.run === run);
      if (!waiting) this.sql.exec("INSERT INTO changes (chat, kind, data, at) VALUES (?, 'rerun', ?, ?)", chat, JSON.stringify({ run }), now);
      return { text: `${waiting ? "Already queued" : "Queued"}: the ${RUN_LABELS[run]} runs at the Mac's next check if it's awake, and arrives as a new message in a few minutes after that.` };
    }
    return { text: `Unknown tool ${name}`, error: true };
  }

  async answer(m: Incoming): Promise<void> {
    const token = this.env.TELEGRAM_BOT_TOKEN;
    const u = this.upload();
    if (!u || u.chat !== m.chat) {
      // Not Dan's chat: tell them once how to get connected, then stay quiet.
      const greeted = this.sql.exec("SELECT greeted FROM chats WHERE chat = ?", m.chat).toArray()[0]?.greeted;
      if (!greeted) {
        await sendText(token, m.chat, `Hi, I'm Booth, a private fantasy football assistant. This chat isn't connected to me. Your chat id is ${m.chat}.`);
        this.sql.exec("UPDATE chats SET greeted = 1 WHERE chat = ?", m.chat);
      }
      return;
    }
    const cap = Number(this.env.MONTHLY_CAP_USD || "20");
    if (this.spent() >= cap) {
      await sendText(token, m.chat, `Booth has used its $${cap} chat budget for this month, so it can't answer until the 1st. Scheduled reports still come as usual.`);
      return;
    }

    await typing(token, m.chat);
    let usd = 0; // counted even when an answer fails partway, since those calls cost too
    const keepTyping = setInterval(() => typing(token, m.chat), 4500); // Telegram shows "typing" for 5 seconds
    try {
      const history = (
        this.sql.exec("SELECT role, text FROM history WHERE chat = ? ORDER BY id DESC LIMIT ?", m.chat, HISTORY).toArray() as unknown as Turn[]
      ).reverse();
      const said = userText(m.text, m.replyTo);
      const client = new Anthropic({ apiKey: this.env.ANTHROPIC_API_KEY, baseURL: this.env.ANTHROPIC_BASE_URL || undefined });
      const messages: Anthropic.Messages.MessageParam[] = conversation(history, said);
      const recent = this.changes(m.chat, `at > '${new Date(Date.now() - 3 * 86400_000).toISOString()}'`);
      const system: Anthropic.Messages.TextBlockParam[] = [
        { type: "text", text: instructions() },
        // Booth's data is the bulk of the prompt and changes a few times a day: cache it.
        { type: "text", text: context(u), cache_control: { type: "ephemeral" } },
        { type: "text", text: situation({ now: new Date(), macSeen: this.meta("mac_seen_at"), notes: this.notes(m.chat), changes: recent }) },
      ];

      let reply = "";
      for (let round = 0; round < MAX_ROUNDS; round++) {
        const response = await client.messages.create({
          model: this.env.MODEL || "claude-opus-5-5",
          max_tokens: 8000,
          output_config: { effort: this.env.EFFORT || "medium" },
          system,
          tools: TOOLS,
          messages,
        });
        usd += cost(response.usage, this.prices());
        console.log(
          `Round ${round + 1}: ${response.usage.input_tokens} in, ${response.usage.cache_read_input_tokens ?? 0} cached, ` +
            `${response.usage.output_tokens} out, ${response.usage.server_tool_use?.web_search_requests ?? 0} searches, ${response.stop_reason}`,
        );
        // A paused turn is continued by sending it back; what comes next continues the same turn.
        const last = messages[messages.length - 1];
        if (last.role === "assistant" && Array.isArray(last.content)) last.content.push(...(response.content as Anthropic.Messages.ContentBlockParam[]));
        else messages.push({ role: "assistant", content: response.content });
        if (response.stop_reason === "pause_turn") continue; // long research: let it carry on
        if (response.stop_reason === "tool_use") {
          const results: Anthropic.Messages.ToolResultBlockParam[] = [];
          for (const b of response.content) {
            if (b.type !== "tool_use") continue;
            const r = this.runTool(m.chat, b.name, (b.input ?? {}) as Record<string, unknown>);
            results.push({ type: "tool_result", tool_use_id: b.id, content: r.text, is_error: r.error });
          }
          messages.push({ role: "user", content: results });
          continue;
        }
        reply = finalText(messages[messages.length - 1].content as Anthropic.Messages.ContentBlock[]);
        break;
      }
      if (!reply) throw new Error("Claude finished without an answer");

      await sendText(token, m.chat, reply);
      const now = new Date().toISOString();
      this.sql.exec("INSERT INTO history (chat, role, text, at) VALUES (?, 'user', ?, ?)", m.chat, said, now);
      this.sql.exec("INSERT INTO history (chat, role, text, at) VALUES (?, 'assistant', ?, ?)", m.chat, reply, now);
      this.sql.exec(
        "DELETE FROM history WHERE chat = ? AND id <= (SELECT id FROM history WHERE chat = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
        m.chat, m.chat, KEEP_HISTORY,
      );
    } finally {
      clearInterval(keepTyping);
      this.addSpend(usd);
    }
  }
}
