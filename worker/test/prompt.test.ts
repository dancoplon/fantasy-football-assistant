import assert from "node:assert/strict";
import { test } from "node:test";

import { changeText, checkMove, context, conversation, cost, finalText, situation, userText } from "../src/prompt.ts";
import type { Change, Upload } from "../src/prompt.ts";
import { deriveSecret, sameSecret } from "../src/secrets.ts";

const upload: Upload = {
  chat: "42",
  built_at: "Wed Oct 7 4:00 PM ET",
  context: {
    week: 6,
    strategy: "  Contend in 2026.\n",
    roster: { source: "manual", players: [{ name: "Tyson Bagent", position: "QB" }] },
    reports: [{ week: 5, run: "tue", text: "Booth: Tuesday waiver report\n\nClaim Rodgers" }],
  },
};

test("context tags each piece of Booth's data and lists the reports separately", () => {
  const text = context(upload);
  assert.match(text, /^<booth_data built="Wed Oct 7 4:00 PM ET">\n<week>\n6\n<\/week>/);
  assert.match(text, /<strategy>\nContend in 2026.\n<\/strategy>/);
  assert.match(text, /<roster>\n\{"source":"manual","players":\[\{"name":"Tyson Bagent"/);
  assert.match(text, /<\/booth_data>\n<reports_sent>\n<report week="5" run="tue">\nBooth: Tuesday waiver report\n\nClaim Rodgers\n<\/report>/);
  assert.ok(!text.includes("<reports>"));
});

test("situation says when the Mac is probably asleep", () => {
  const now = new Date("2026-10-07T20:00:00Z");
  const base = { now, notes: [], changes: [] };
  assert.match(situation({ ...base, macSeen: "2026-10-07T19:45:00Z" }), /last checked in 15 minutes ago; it checks every 30/);
  assert.match(situation({ ...base, macSeen: "2026-10-07T15:00:00Z" }), /5 hours ago, so it's probably asleep/);
  assert.match(situation({ ...base, macSeen: null }), /hasn't checked in yet/);
  assert.match(situation({ ...base, macSeen: null }), /Now: Wed, Oct 7, 4:00 PM ET/);
  const notes = [{ id: 3, text: "Keep $40 FAAB until week 10", at: "2026-10-07T19:00:00Z" }];
  assert.match(situation({ ...base, macSeen: null, notes }), /<notes>\n- \[id 3, 2026-10-07\] Keep \$40 FAAB until week 10\n<\/notes>/);
});

test("changeText describes each change and where it stands", () => {
  const c = (data: Record<string, unknown>, status: Change["status"], kind: Change["kind"] = "roster", detail = ""): Change =>
    ({ id: 1, kind, data, status, detail, at: "2026-10-07T19:00:00.000Z" });
  assert.equal(changeText(c({ action: "add", player: "Tyson Bagent" }, "pending")), "- 2026-10-07 19:00 UTC: add Tyson Bagent: waiting for the Mac");
  assert.equal(changeText(c({ action: "slot", player: "Tank Bigsby", slot: "IR" }, "applied", "roster", "moved Tank Bigsby to IR")),
    "- 2026-10-07 19:00 UTC: slot Tank Bigsby to IR: done (moved Tank Bigsby to IR)");
  assert.equal(changeText(c({ action: "faab", faab_remaining: 83 }, "failed", "roster", "no file")), "- 2026-10-07 19:00 UTC: FAAB left: $83: failed: no file");
  assert.equal(changeText(c({ run: "sun" }, "pending", "rerun")), "- 2026-10-07 19:00 UTC: rerun the Sunday final lineup pass: waiting for the Mac");
});

test("checkMove accepts complete moves and names what's missing", () => {
  assert.equal(checkMove({ action: "add", player: "Tyson Bagent", position: "QB", nfl_team: "CHI" }), null);
  assert.equal(checkMove({ action: "drop", player: "Tre Tucker" }), null);
  assert.equal(checkMove({ action: "slot", player: "Bigsby", slot: "IR" }), null);
  assert.equal(checkMove({ action: "faab", faab_remaining: 0 }), null);
  assert.match(checkMove({ action: "trade", player: "x" })!, /action must be/);
  assert.match(checkMove({ action: "add", player: "x" })!, /position is required/);
  assert.match(checkMove({ action: "drop", player: " " })!, /player is required/);
  assert.match(checkMove({ action: "slot", player: "x", slot: "FLEX" })!, /slot must be/);
  assert.match(checkMove({ action: "faab" })!, /faab_remaining is required/);
});

test("finalText keeps only the answer after the last tool use", () => {
  const blocks = [
    { type: "text", text: "Let me check the injury news.", citations: null },
    { type: "server_tool_use", id: "s1", name: "web_search", input: {} },
    { type: "web_search_tool_result", tool_use_id: "s1", content: [] },
    { type: "text", text: "Swift is out", citations: [{}] },
    { type: "text", text: " (per ESPN).\n\nStart Kraft.", citations: null },
  ] as any;
  assert.equal(finalText(blocks), "Swift is out (per ESPN).\n\nStart Kraft.");
  assert.equal(finalText([{ type: "text", text: " hi ", citations: null }] as any), "hi");
  assert.equal(finalText([{ type: "server_tool_use", id: "s1", name: "web_search", input: {} }] as any), "");
});

test("cost adds tokens, cache use and searches", () => {
  const prices = { input: 5, output: 25, cacheWrite: 6.25, cacheRead: 0.5, search: 0.01 };
  const usage = {
    input_tokens: 2000, output_tokens: 1000, cache_creation_input_tokens: 40000, cache_read_input_tokens: 80000,
    server_tool_use: { web_search_requests: 3, web_fetch_requests: 1 },
  } as any;
  assert.ok(Math.abs(cost(usage, prices) - (0.01 + 0.025 + 0.25 + 0.04 + 0.03)) < 1e-9);
  assert.equal(cost({ input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: null, cache_read_input_tokens: null, server_tool_use: null } as any, prices), 0);
});

test("userText quotes the message being replied to", () => {
  assert.equal(userText("hi", ""), "hi");
  assert.equal(userText("why him?", "Claim Rodgers"), "[Replying to this earlier message:\nClaim Rodgers]\n\nwhy him?");
});

test("conversation starts with the user and alternates", () => {
  const msgs = conversation(
    [
      { role: "assistant", text: "orphan" },
      { role: "user", text: "a" },
      { role: "user", text: "b" },
      { role: "assistant", text: "c" },
    ],
    "d",
  );
  assert.deepEqual(msgs, [
    { role: "user", content: "a\n\nb" },
    { role: "assistant", content: "c" },
    { role: "user", content: "d" },
  ]);
});

test("derived secrets are stable, distinct per purpose, match the Mac's, and safe for Telegram's secret_token", async () => {
  const a = await deriveSecret("123:abc", "webhook");
  assert.equal(a, await deriveSecret("123:abc", "webhook"));
  assert.notEqual(a, await deriveSecret("123:abc", "api"));
  assert.match(a, /^[0-9a-f]{64}$/);
  // python3 -c 'import hmac,hashlib;print(hmac.new(b"123:abc",b"booth-api",hashlib.sha256).hexdigest())'
  assert.equal(await deriveSecret("123:abc", "api"), "737cd05f33237c58f337c1b85f2fcb06ebdbd89da88cbd5b6a8b0f1303260f07");
  assert.ok(sameSecret(a, a));
  assert.ok(!sameSecret(a, ""));
});
