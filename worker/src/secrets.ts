// Shared secrets derived from the bot token, so the Mac, the deploy and this worker agree on
// them without another key to manage. Anyone holding the bot token can already act as the bot.
export async function deriveSecret(botToken: string, purpose: "webhook" | "api"): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(botToken),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`booth-${purpose}`));
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Compares in constant time so a wrong secret can't be guessed a character at a time.
export function sameSecret(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
