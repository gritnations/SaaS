// Tests for the set-up box rules. Run with: node --test tests/setup-steps.test.mjs
//
// What these tests establish: what sign-up creates does not tick a step by itself; changing it, or opening the screen,
// does; a step is never ticked without a bookable service or an open day; and a failed load ticks nothing.

import assert from "node:assert/strict";
import { test } from "node:test";

import { DEFAULT_SERVICE, hoursDone, readFlag, servicesDone, writeFlag } from "../site/assets/setup-model.js";

const fresh = { ...DEFAULT_SERVICE, active: true };
const week = (days = [1, 2, 3, 4, 5], start = "09:00", end = "17:00") => ({
  blocks: days.map((weekday) => ({ weekday, start, end })),
});

test("a new business has neither step done", () => {
  assert.equal(servicesDone([fresh], false), false);
  assert.equal(hoursDone(week(), false), false);
  assert.equal(hoursDone(week([1, 2, 3, 4, 5], "09:00:00", "17:00:00"), false), false);
});

test("opening the screen ticks the step even with the defaults kept", () => {
  assert.equal(servicesDone([fresh], true), true);
  assert.equal(hoursDone(week(), true), true);
});

test("changing the services ticks the step without a visit", () => {
  for (const change of [{ name: "Haircut" }, { duration_minutes: 45 }, { price_minor: 1200 }, { slug: "cut" }]) {
    assert.equal(servicesDone([{ ...fresh, ...change }], false), true, JSON.stringify(change));
  }
  assert.equal(servicesDone([fresh, { ...fresh, slug: "cut" }], false), true);
  assert.equal(
    servicesDone(
      [
        { ...fresh, active: false },
        { ...fresh, slug: "cut" },
      ],
      false,
    ),
    true,
  );
});

test("changing the hours ticks the step without a visit", () => {
  assert.equal(hoursDone(week([1, 2, 3, 4]), false), true);
  assert.equal(hoursDone(week([1, 2, 3, 4, 6]), false), true);
  assert.equal(hoursDone(week([1, 2, 3, 4, 5], "10:00"), false), true);
  assert.equal(hoursDone(week([1, 2, 3, 4, 5], "09:00", "18:00"), false), true);
});

test("nothing is ticked without a bookable service, an open day or a loaded answer", () => {
  for (const services of [null, [], [{ ...fresh, active: false }]]) assert.equal(servicesDone(services, true), false);
  for (const hours of [null, {}, { blocks: [] }]) assert.equal(hoursDone(hours, true), false);
});

test("flags are kept for each business, and broken storage reads as not set", () => {
  const kept = new Map();
  globalThis.localStorage = {
    getItem: (key) => kept.get(key) ?? null,
    setItem: (key, value) => kept.set(key, value),
    removeItem: (key) => kept.delete(key),
  };
  writeFlag("setup_hidden", "one");
  assert.equal(readFlag("setup_hidden", "one"), true);
  assert.equal(readFlag("setup_hidden", "two"), false);
  writeFlag("setup_hidden", "one", false);
  assert.equal(readFlag("setup_hidden", "one"), false);
  const blocked = () => {
    throw new Error("blocked");
  };
  globalThis.localStorage = { getItem: blocked, setItem: blocked, removeItem: blocked };
  assert.doesNotThrow(() => writeFlag("seen_hours", "one"));
  assert.equal(readFlag("seen_hours", "one"), false);
  delete globalThis.localStorage;
});
