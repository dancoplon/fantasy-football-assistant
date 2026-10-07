import assert from "node:assert/strict";
import { test } from "node:test";

import { ASLEEP_AFTER_MS, REMIND_AFTER_MS, shouldSayAsleep } from "../src/rules.ts";
import { deriveSecret, sameSecret } from "../src/secrets.ts";

const now = Date.parse("2026-10-11T17:00:00Z");
const min = 60_000;

test("no asleep note while the Mac is checking in", () => {
  assert.equal(shouldSayAsleep(now, now - 10_000, null), false);
  assert.equal(shouldSayAsleep(now, now - ASLEEP_AFTER_MS + 1000, null), false);
});

test("an asleep note once the Mac has gone quiet, or has never checked in", () => {
  assert.equal(shouldSayAsleep(now, now - ASLEEP_AFTER_MS, null), true);
  assert.equal(shouldSayAsleep(now, null, null), true);
});

test("one note per sleep, then again only after a long while", () => {
  const slept = now - 60 * min;
  assert.equal(shouldSayAsleep(now, slept, now - 5 * min), false); // already said since the Mac went quiet
  assert.equal(shouldSayAsleep(now, slept, slept - 10 * min), true); // said during an earlier sleep
  assert.equal(shouldSayAsleep(now, now - 10 * 3600_000, now - REMIND_AFTER_MS), true); // long sleep: remind
  assert.equal(shouldSayAsleep(now, null, now - 5 * min), false);
});

test("derived secrets match the Mac's and are safe for Telegram's secret_token", async () => {
  const a = await deriveSecret("123:abc", "webhook");
  assert.equal(a, await deriveSecret("123:abc", "webhook"));
  assert.notEqual(a, await deriveSecret("123:abc", "api"));
  assert.match(a, /^[0-9a-f]{64}$/);
  // python3 -c 'import hmac,hashlib;print(hmac.new(b"123:abc",b"booth-api",hashlib.sha256).hexdigest())'
  assert.equal(await deriveSecret("123:abc", "api"), "737cd05f33237c58f337c1b85f2fcb06ebdbd89da88cbd5b6a8b0f1303260f07");
  assert.ok(sameSecret(a, a));
  assert.ok(!sameSecret(a, ""));
});
