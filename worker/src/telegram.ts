// Telegram Bot API plumbing with nothing Booth-specific in it (shared with Good Boy Roman's reply service).

export const LIMIT = 4096; // characters per Telegram message

export interface Incoming {
  updateId: number;
  chat: string;
  name: string;
  text: string;
  replyTo: string; // text of the message being replied to, if any
}

// The parts of a webhook update a chat bot needs: private-chat text messages only.
export function parseUpdate(update: any): Incoming | null {
  const m = update?.message;
  if (typeof update?.update_id !== "number" || !m || m.chat?.type !== "private" || typeof m.text !== "string") {
    return null;
  }
  const name = [m.from?.first_name, m.from?.last_name].filter(Boolean).join(" ");
  const replied = m.reply_to_message;
  return {
    updateId: update.update_id,
    chat: String(m.chat.id),
    name,
    text: m.text,
    replyTo: typeof replied?.text === "string" ? replied.text : "",
  };
}

// Splits text into messages under the limit, preferring paragraph breaks, then line breaks, then spaces.
export function split(text: string, limit = LIMIT): string[] {
  const out: string[] = [];
  let rest = text.trim();
  while (rest.length > limit) {
    const head = rest.slice(0, limit);
    let cut = Math.max(head.lastIndexOf("\n\n"), 0);
    if (cut < limit / 2) cut = Math.max(head.lastIndexOf("\n"), cut);
    if (cut < limit / 2) cut = Math.max(head.lastIndexOf(" "), cut);
    if (cut < limit / 2) cut = limit;
    out.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
  if (rest) out.push(rest);
  return out;
}

export class TelegramError extends Error {}

// Tests point this at a local fake; the deployed worker leaves it alone.
export let API = "https://api.telegram.org";
export function useApi(url: string | undefined): void {
  if (url) API = url;
}

export async function call(token: string, method: string, payload: object): Promise<any> {
  const resp = await fetch(`${API}/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body: any = await resp.json().catch(() => ({}));
  if (!resp.ok || !body.ok) {
    // Never include the URL: it holds the token.
    throw new TelegramError(`Telegram ${method} returned HTTP ${resp.status}: ${body.description ?? "no detail"}`);
  }
  return body.result;
}

export async function sendText(token: string, chat: string, text: string): Promise<void> {
  for (const part of split(text)) {
    await call(token, "sendMessage", { chat_id: chat, text: part, link_preview_options: { is_disabled: true } });
  }
}

export async function typing(token: string, chat: string): Promise<void> {
  await call(token, "sendChatAction", { chat_id: chat, action: "typing" }).catch(() => {});
}
