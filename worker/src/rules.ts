// The mailbox's decisions, kept apart from storage so they can be tested on their own.

export const ASLEEP_AFTER_MS = 2 * 60_000; // the Mac checks in every few seconds while awake
export const REMIND_AFTER_MS = 3 * 3600_000; // during a long sleep, say it again after this long

export const ASLEEP_TEXT =
  "Booth is asleep right now (your laptop is closed or sleeping). It will answer this as soon as your Mac wakes up.";

export function notConnectedText(chat: string): string {
  return `Hi, I'm Booth, a private fantasy football assistant. This chat isn't connected to me. Your chat id is ${chat}.`;
}

// Whether a message arriving now gets the "Booth is asleep" reply: the Mac hasn't checked in
// lately, and Booth hasn't already said so since the Mac last checked in (or not for a while).
export function shouldSayAsleep(now: number, macSeen: number | null, lastAsleepNote: number | null): boolean {
  if (macSeen !== null && now - macSeen < ASLEEP_AFTER_MS) return false;
  if (lastAsleepNote === null) return true;
  if (macSeen !== null && lastAsleepNote < macSeen) return true;
  return now - lastAsleepNote >= REMIND_AFTER_MS;
}
