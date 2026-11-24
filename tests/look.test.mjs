// Tests for the customer pages' look module. Run with: node --test tests/look.test.mjs
//
// What these tests establish: a look is only applied if every value is one the server could have stored (a #rrggbb
// colour, a known font and corner style); anything else, including CSS fragments, is ignored and the page stays as
// it was; values reach the page only as CSS custom properties; the default look ("modern", "soft") reproduces the
// page's own font and shape; links fall back to the text colour when the accent is too faint to read; and the page
// is released from its hold in every outcome, including when the look cannot be loaded.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { applyLook, contrast, lookProperties } from "../public/assets/look.js";

const GOOD = {
  accent: "#e0aa55",
  background: "#0c1726",
  text: "#f3f5f8",
  on_accent: "#111111",
  font: "classic",
  corners: "round",
  logo_id: "0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10",
};

test("contrast matches the WCAG reference values", () => {
  assert.ok(Math.abs(contrast("#000000", "#ffffff") - 21) < 1e-9);
  assert.ok(Math.abs(contrast("#13355a", "#13355a") - 1) < 1e-9);
  assert.equal(contrast("#767676", "#ffffff"), contrast("#ffffff", "#767676"));
});

test("a good look becomes CSS custom properties only", () => {
  const properties = lookProperties(GOOD);
  assert.ok(properties);
  assert.equal(properties["--page-bg"], "#0c1726");
  assert.equal(properties["--text"], "#f3f5f8");
  assert.equal(properties["--action"], "#e0aa55");
  assert.equal(properties["--on-action"], "#111111");
  assert.equal(properties["--glow"], "none");
  for (const [name, value] of Object.entries(properties)) {
    assert.match(name, /^--[a-z-]+$/);
    assert.doesNotMatch(value, /[;{}<>]|url\(|expression|@import/i, `${name} carries something that is not a value`);
  }
});

test("anything the server could not have stored is ignored", () => {
  const bad = [
    null,
    undefined,
    "midnight",
    [],
    { ...GOOD, accent: "#E0AA55" },
    { ...GOOD, accent: "red" },
    { ...GOOD, accent: "#e0aa55;}body{display:none" },
    { ...GOOD, background: "url(https://example.com/x)" },
    { ...GOOD, text: "#fff" },
    { ...GOOD, on_accent: "#222222" },
    { ...GOOD, on_accent: undefined },
    { ...GOOD, font: "Comic Sans" },
    { ...GOOD, font: "constructor" },
    { ...GOOD, font: "__proto__" },
    { ...GOOD, corners: "pill" },
    { ...GOOD, corners: "toString" },
  ];
  for (const look of bad) assert.equal(lookProperties(look), null, JSON.stringify(look));
});

test("the default look reproduces the page's own font and shape", () => {
  const properties = lookProperties({ ...GOOD, font: "modern", corners: "soft" });
  assert.equal(properties["--font"], '"Montserrat", Helvetica, Arial, sans-serif');
  assert.equal(properties["--radius"], "8px");
  assert.equal(properties["--radius-lg"], "16px");
  assert.equal(properties["--radius-btn"], "10px");
});

test("links use the accent only when it is readable on the background", () => {
  assert.equal(lookProperties(GOOD)["--link"], "#e0aa55");
  const faint = lookProperties({ ...GOOD, accent: "#f4d58d", background: "#ffffff", text: "#222222" });
  assert.equal(faint["--link"], "#222222");
  assert.equal(faint["--link-hover"], "#222222");
});

test("button hover moves away from the button text so contrast does not drop", () => {
  assert.match(lookProperties({ ...GOOD, accent: "#13355a", on_accent: "#ffffff" })["--action-hover"], /#000000\)$/);
  assert.match(lookProperties({ ...GOOD, accent: "#e0aa55", on_accent: "#111111" })["--action-hover"], /#ffffff\)$/);
});

// applyLook, with a stand-in page and a stand-in network.

const realFetch = globalThis.fetch;
const realDocument = globalThis.document;

function standIn(response) {
  const page = { classes: new Set(["look-pending"]), properties: {}, fetched: [] };
  globalThis.document = {
    documentElement: {
      classList: { remove: (name) => page.classes.delete(name) },
      style: { setProperty: (name, value) => (page.properties[name] = value) },
    },
  };
  globalThis.fetch = async (path) => {
    page.fetched.push(path);
    if (response instanceof Error) throw response;
    return { ok: response.ok, status: response.status, json: async () => response.body };
  };
  return page;
}

afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.document = realDocument;
});

test("applyLook applies a published look, returns the logo address and releases the page", async () => {
  const page = standIn({ ok: true, status: 200, body: { look: GOOD } });
  const logo = await applyLook("barber-shop");
  assert.deepEqual(page.fetched, ["/businesses/barber-shop/look"]);
  assert.equal(logo, "/businesses/barber-shop/logos/0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10");
  assert.equal(page.properties["--page-bg"], "#0c1726");
  assert.equal(page.classes.has("look-pending"), false);
});

test("applyLook leaves the page alone for the default look, but still releases it", async () => {
  const page = standIn({ ok: true, status: 200, body: { look: null } });
  assert.equal(await applyLook("barber-shop"), null);
  assert.deepEqual(page.properties, {});
  assert.equal(page.classes.has("look-pending"), false);
});

test("applyLook leaves the page alone when the look cannot be loaded or is malformed", async () => {
  for (const response of [
    { ok: false, status: 404, body: { detail: "Business not found." } },
    { ok: false, status: 503, body: null },
    { ok: true, status: 200, body: { look: { ...GOOD, accent: "red;}" } } },
    { ok: true, status: 200, body: null },
    new Error("offline"),
  ]) {
    const page = standIn(response);
    assert.equal(await applyLook("barber-shop"), null);
    assert.deepEqual(page.properties, {});
    assert.equal(page.classes.has("look-pending"), false, "the page must never stay hidden");
  }
});

test("applyLook applies the colours but shows no logo when the logo identifier is not a UUID", async () => {
  for (const logo_id of ["../../etc/passwd", "0B6C3F0E-6A8E-4D3E-9A51-0F3C1F2D9A10", 7, null]) {
    const page = standIn({ ok: true, status: 200, body: { look: { ...GOOD, logo_id } } });
    assert.equal(await applyLook("barber-shop"), null);
    assert.equal(page.properties["--page-bg"], "#0c1726");
  }
});
