// Tests for the owner editor's look logic. Run with: node --test tests/booking-look.test.mjs
//
// What these tests establish: every starting theme can be published; the standard look is the customer pages' own
// colours; the readability rules and the button text colour match the server's; the editor reports a look as
// published, saved as a draft or unsaved, and allows saving and publishing accordingly; a draft is sent with exactly
// the fields the API accepts; and a logo file is checked before it is sent.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { contrast } from "../public/assets/look.js";
import {
  CORNERS,
  FONTS,
  LOGO_MAX_BYTES,
  STANDARD,
  THEMES,
  draftBody,
  editorState,
  fromServer,
  logoProblem,
  onAccent,
  readabilityProblems,
  sameLook,
} from "../site/assets/look-model.js";

const MIDNIGHT = THEMES.find((t) => t.id === "midnight");

test("every starting theme can be published, and uses only choices the server knows", () => {
  const fonts = FONTS.map(([value]) => value);
  const corners = CORNERS.map(([value]) => value);
  for (const theme of THEMES) {
    assert.deepEqual(readabilityProblems(theme, contrast), [], theme.name);
    assert.match(theme.accent, /^#[0-9a-f]{6}$/, theme.name);
    assert.ok(fonts.includes(theme.font), theme.name);
    assert.ok(corners.includes(theme.corners), theme.name);
  }
  assert.equal(new Set(THEMES.map((t) => t.id)).size, THEMES.length);
});

test("the standard look is the customer pages' own colours", () => {
  const css = readFileSync(join(import.meta.dirname, "../public/assets/gn.css"), "utf8");
  const colour = (name) => css.match(new RegExp(`^\\s*--${name}:\\s*(#[0-9a-f]{6});`, "m"))[1];
  assert.equal(STANDARD.accent, colour("red"));
  assert.equal(STANDARD.background, colour("ink"));
  assert.equal(STANDARD.text, colour("text"));
  assert.equal(STANDARD.font, "modern");
  assert.equal(STANDARD.corners, "soft");
  assert.deepEqual(THEMES[0], { id: "standard", name: "Standard", ...STANDARD });
});

test("button text is white on dark accents and near-black on light ones", () => {
  assert.equal(onAccent("#13355a", contrast), "#ffffff");
  assert.equal(onAccent("#d62c2d", contrast), "#ffffff");
  assert.equal(onAccent("#e0aa55", contrast), "#111111");
});

test("each readability problem is named with its numbers", () => {
  const base = { accent: "#13355a", background: "#ffffff", text: "#1c2733" };
  assert.deepEqual(readabilityProblems(base, contrast), []);
  assert.match(
    readabilityProblems({ ...base, text: "#f0f0f0" }, contrast)[0],
    /^text on the background is hard to read/,
  );
  assert.match(readabilityProblems({ ...base, accent: "#ffffff" }, contrast)[0], /^highlights barely stand out/);
  assert.ok(readabilityProblems({ ...base, accent: "#777777" }, contrast).some((p) => p.startsWith("button text")));
  assert.match(readabilityProblems({ ...base, text: "#ffffff" }, contrast)[0], /1\.0:1, needs 4\.5:1/);
});

test("looks compare field by field, and a missing look matches nothing", () => {
  assert.ok(sameLook(MIDNIGHT, { ...MIDNIGHT }));
  assert.ok(sameLook({ ...MIDNIGHT, logo_id: undefined }, { ...MIDNIGHT, logo_id: null }));
  assert.ok(!sameLook(MIDNIGHT, { ...MIDNIGHT, accent: "#e0aa56" }));
  assert.ok(!sameLook(MIDNIGHT, { ...MIDNIGHT, logo_id: "0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10" }));
  assert.ok(!sameLook(null, null));
  assert.ok(!sameLook(MIDNIGHT, null));
});

test("a server look keeps only the look's fields, and a draft is sent with only those", () => {
  const served = { ...MIDNIGHT, on_accent: "#111111", extra: "x" };
  assert.deepEqual(Object.keys(fromServer(served)).sort(), [
    "accent",
    "background",
    "corners",
    "font",
    "logo_id",
    "text",
  ]);
  assert.equal(fromServer(null), null);
  const body = draftBody({ ...served, css: "body{}" });
  assert.deepEqual(Object.keys(body).sort(), ["accent", "background", "corners", "font", "logo_id", "text"]);
  assert.equal(body.logo_id, null);
});

test("the editor reports published, saved and unsaved, and allows saving and publishing accordingly", () => {
  const ok = [];
  const bad = ["text on the background is hard to read"];
  // Nothing published: the standard look is live.
  let s = editorState({ working: { ...STANDARD }, draft: null, published: null, allowed: true, problems: ok });
  assert.deepEqual(s, { status: "published", canSave: false, canPublish: false, needsPlan: false });
  // A change that is not saved yet.
  s = editorState({ working: MIDNIGHT, draft: null, published: null, allowed: true, problems: ok });
  assert.deepEqual(s, { status: "unsaved", canSave: true, canPublish: true, needsPlan: false });
  // The same change after saving a draft.
  s = editorState({ working: MIDNIGHT, draft: { ...MIDNIGHT }, published: null, allowed: true, problems: ok });
  assert.deepEqual(s, { status: "draft-saved", canSave: false, canPublish: true, needsPlan: false });
  // After publishing it, nothing is pending.
  s = editorState({
    working: MIDNIGHT,
    draft: { ...MIDNIGHT },
    published: { ...MIDNIGHT },
    allowed: true,
    problems: ok,
  });
  assert.deepEqual(s, { status: "published", canSave: false, canPublish: false, needsPlan: false });
  // A look that is hard to read can be saved but not published.
  s = editorState({ working: MIDNIGHT, draft: null, published: null, allowed: true, problems: bad });
  assert.deepEqual([s.canSave, s.canPublish], [true, false]);
  // No plan: publishing becomes a prompt to choose one, while saving still works.
  s = editorState({ working: MIDNIGHT, draft: null, published: null, allowed: false, problems: ok });
  assert.deepEqual([s.canSave, s.needsPlan], [true, true]);
});

test("a logo file is checked before it is sent", () => {
  assert.equal(logoProblem({ type: "image/png", size: 1000 }), "");
  assert.equal(logoProblem({ type: "image/jpeg", size: LOGO_MAX_BYTES }), "");
  assert.equal(logoProblem({ type: "image/webp", size: 1 }), "");
  assert.match(logoProblem({ type: "image/svg+xml", size: 1000 }), /isn't allowed/);
  assert.match(logoProblem({ type: "application/pdf", size: 1000 }), /isn't allowed/);
  assert.match(logoProblem({ type: "image/gif", size: 1000 }), /isn't allowed/);
  assert.match(logoProblem({ type: "image/png", size: 0 }), /empty/);
  assert.match(logoProblem({ type: "image/png", size: LOGO_MAX_BYTES + 1 }), /The limit is 60 KB/);
});
