import assert from "node:assert/strict";
import { test } from "node:test";

import { parseUpdate, split } from "../src/telegram.ts";

test("parseUpdate keeps private text messages and the message they reply to", () => {
  const m = parseUpdate({
    update_id: 7,
    message: {
      chat: { id: 8874735915, type: "private" },
      from: { first_name: "Dan", last_name: "C" },
      text: "tell me more about Headway",
      reply_to_message: { text: "Top picks ..." },
    },
  });
  assert.deepEqual(m, { updateId: 7, chat: "8874735915", name: "Dan C", text: "tell me more about Headway", replyTo: "Top picks ..." });
});

test("parseUpdate ignores groups, stickers and edits", () => {
  assert.equal(parseUpdate({ update_id: 1, message: { chat: { id: -5, type: "group" }, text: "hi" } }), null);
  assert.equal(parseUpdate({ update_id: 2, message: { chat: { id: 5, type: "private" }, sticker: {} } }), null);
  assert.equal(parseUpdate({ update_id: 3, edited_message: { chat: { id: 5, type: "private" }, text: "hi" } }), null);
  assert.equal(parseUpdate(null), null);
});

test("split leaves short text alone", () => {
  assert.deepEqual(split("  hello  "), ["hello"]);
  assert.deepEqual(split(""), []);
});

test("split breaks long text at paragraphs and stays under the limit", () => {
  const para = "word ".repeat(30).trim();
  const text = Array(10).fill(para).join("\n\n");
  const parts = split(text, 400);
  assert.ok(parts.length > 1);
  for (const p of parts) assert.ok(p.length <= 400, `${p.length} > 400`);
  assert.equal(parts.join(" ").replace(/\s+/g, " "), text.replace(/\s+/g, " "));
});

test("split cuts text with no spaces at the limit", () => {
  const parts = split("x".repeat(1000), 300);
  assert.deepEqual(parts.map((p) => p.length), [300, 300, 300, 100]);
});
