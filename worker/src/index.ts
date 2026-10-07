// Booth's mailbox: Telegram sends Dan's messages here, and Booth on his Mac picks them up.
//
// Booth itself (and Claude) runs on Dan's Mac, which sleeps. The mailbox is always on: it keeps
// every message until the Mac collects it, and while the Mac is asleep it tells Dan so. There is
// no AI here and nothing that costs money.
//
// POST /telegram   Telegram's webhook (checked against the secret set with setWebhook)
// GET  /poll       the Mac checks in and collects waiting messages (?chat=<Dan's chat id>)
// POST /ack        the Mac has answered these messages ({"ids": [...]})
// GET  /chats      everyone who has messaged the bot (for connecting a new chat)
import { DurableObject } from "cloudflare:workers";

import { ASLEEP_TEXT, notConnectedText, shouldSayAsleep } from "./rules";
import { deriveSecret, sameSecret } from "./secrets";
import { Incoming, parseUpdate, sendText, useApi } from "./telegram";

export interface Env {
  TELEGRAM_BOT_TOKEN: string;
  MAILBOX: DurableObjectNamespace<Mailbox>;
  TELEGRAM_API?: string; // local testing only: a fake Telegram
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const mailbox = env.MAILBOX.get(env.MAILBOX.idFromName("booth"));

    if (request.method === "POST" && url.pathname === "/telegram") {
      const given = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
      if (!sameSecret(given, await deriveSecret(env.TELEGRAM_BOT_TOKEN, "webhook"))) {
        return new Response("forbidden", { status: 403 });
      }
      const incoming = parseUpdate(await request.json().catch(() => null));
      if (incoming) await mailbox.receive(incoming);
      return new Response("ok"); // always 200, so Telegram never resends
    }

    const auth = request.headers.get("Authorization") ?? "";
    if (!sameSecret(auth, `Bearer ${await deriveSecret(env.TELEGRAM_BOT_TOKEN, "api")}`)) {
      return new Response("forbidden", { status: 403 });
    }
    if (request.method === "GET" && url.pathname === "/poll") {
      const chat = url.searchParams.get("chat") ?? "";
      if (!/^-?\d+$/.test(chat)) return Response.json({ error: "chat is required" }, { status: 400 });
      return Response.json({ messages: await mailbox.poll(chat) });
    }
    if (request.method === "POST" && url.pathname === "/ack") {
      const body = (await request.json().catch(() => null)) as { ids?: unknown } | null;
      const ids = Array.isArray(body?.ids) ? body.ids.filter((i): i is number => Number.isInteger(i)) : null;
      if (!ids) return Response.json({ error: "ids are required" }, { status: 400 });
      await mailbox.ack(ids);
      return Response.json({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/chats") {
      return Response.json({ chats: await mailbox.chats() });
    }
    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

export class Mailbox extends DurableObject<Env> {
  sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    useApi(env.TELEGRAM_API);
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat TEXT, data TEXT, at TEXT, done INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS chats (chat TEXT PRIMARY KEY, name TEXT, text TEXT, at TEXT, greeted INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    `);
  }

  meta(key: string): string | null {
    return (this.sql.exec("SELECT value FROM meta WHERE key = ?", key).toArray()[0]?.value as string) ?? null;
  }

  setMeta(key: string, value: string): void {
    this.sql.exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", key, value);
  }

  async receive(m: Incoming): Promise<void> {
    const now = new Date();
    this.sql.exec(
      `INSERT INTO chats (chat, name, text, at) VALUES (?, ?, ?, ?)
       ON CONFLICT (chat) DO UPDATE SET name = excluded.name, text = excluded.text, at = excluded.at`,
      m.chat, m.name, m.text.slice(0, 200), now.toISOString(),
    );
    const dans = this.meta("chat"); // set by the Mac when it checks in
    if (dans && m.chat !== dans) {
      // Not Dan's chat: tell them once, then stay quiet.
      const greeted = this.sql.exec("SELECT greeted FROM chats WHERE chat = ?", m.chat).toArray()[0]?.greeted;
      if (!greeted) {
        this.sql.exec("UPDATE chats SET greeted = 1 WHERE chat = ?", m.chat);
        await sendText(this.env.TELEGRAM_BOT_TOKEN, m.chat, notConnectedText(m.chat)).catch(() => {});
      }
      return;
    }
    // The update id makes a resent update a no-op.
    const added = this.sql.exec(
      "INSERT OR IGNORE INTO messages (id, chat, data, at) VALUES (?, ?, ?, ?)",
      m.updateId, m.chat, JSON.stringify(m), now.toISOString(),
    ).rowsWritten;
    if (!added || !dans) return;
    const seen = this.meta("mac_seen_at");
    const said = this.meta("asleep_note_at");
    if (shouldSayAsleep(now.getTime(), seen ? Date.parse(seen) : null, said ? Date.parse(said) : null)) {
      this.setMeta("asleep_note_at", now.toISOString());
      await sendText(this.env.TELEGRAM_BOT_TOKEN, m.chat, ASLEEP_TEXT).catch((e) => console.error("Couldn't send the asleep note:", e?.message));
    }
  }

  // The Mac checks in: remember it's awake and which chat is Dan's, and hand over what's waiting.
  async poll(chat: string): Promise<object[]> {
    this.setMeta("mac_seen_at", new Date().toISOString());
    this.setMeta("chat", chat);
    return this.sql
      .exec("SELECT id, data, at FROM messages WHERE chat = ? AND done = 0 ORDER BY id", chat)
      .toArray()
      .map((r) => ({ ...JSON.parse(r.data as string), receivedAt: r.at }));
  }

  async ack(ids: number[]): Promise<void> {
    for (const id of ids) this.sql.exec("UPDATE messages SET done = 1 WHERE id = ?", id);
    this.sql.exec("DELETE FROM messages WHERE done = 1 AND at < ?", new Date(Date.now() - 30 * 86400_000).toISOString());
  }

  async chats(): Promise<object[]> {
    return this.sql.exec("SELECT chat AS id, name, text, at FROM chats ORDER BY at DESC").toArray();
  }
}
