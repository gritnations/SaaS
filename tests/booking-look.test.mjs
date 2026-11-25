// Tests for the owner editor's look logic. Run with: node --test tests/booking-look.test.mjs
//
// What these tests establish: every starting theme can be published; the standard look is the customer pages' own
// colours; the readability rules and the button text colour match the server's; the editor reports a look as
// published, saved as a draft or unsaved, and allows saving and publishing accordingly; a draft is sent with exactly
// the fields the API accepts; a logo file is checked before it is sent; and the website button's code is built only
// from fixed texts and checked colours, escaped, in the colours of the look customers see, with a way to tell when
// a copy of it has gone out of date.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { contrast } from "../public/assets/look.js";
import {
  BUTTON_TEXTS,
  BUTTON_TEXT_MAX,
  CORNERS,
  FONTS,
  LOGO_MAX_BYTES,
  STANDARD,
  THEMES,
  buttonSnippet,
  buttonStyle,
  buttonTextProblem,
  copiedCodeIsStale,
  developerEmail,
  draftBody,
  editorState,
  escapeHtml,
  fromServer,
  liveLook,
  logoProblem,
  normaliseButtonText,
  onAccent,
  readSavedButtonText,
  readabilityProblems,
  sameLook,
  snippetFingerprint,
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
  assert.match(logoProblem({ type: "image/png", size: LOGO_MAX_BYTES + 1 }), /The limit is 100 KB/);
  // 60 KB was the old limit: a logo of that size is now fine, and 100 KB is the exact edge.
  assert.equal(logoProblem({ type: "image/png", size: 61441 }), "");
  assert.equal(LOGO_MAX_BYTES, 100 * 1024);
});

const HREF = "https://book.example.com/book/example-barbershop";
const snippetFor = (look, text = "Book now", href = HREF) => buttonSnippet({ href, text, look, contrast });

test("the button code is a plain link in the look's colours, with no script or frame", () => {
  const standard = snippetFor(STANDARD);
  assert.match(standard, /^<a href="https:\/\/book\.example\.com\/book\/example-barbershop"/);
  assert.match(standard, /target="_blank" rel="noopener"/);
  assert.match(standard, /border-radius:10px;background:#d62c2d;color:#ffffff;/);
  assert.ok(standard.endsWith(">\n  Book now\n</a>"));
  const midnight = snippetFor(MIDNIGHT, "Book online");
  assert.match(midnight, /background:#e0aa55;color:#111111;/);
  assert.doesNotMatch(standard + midnight, /<script|<iframe|<style|onclick|javascript:/i);
  assert.equal(buttonStyle({ ...MIDNIGHT, corners: "round" }, contrast).borderRadius, "24px");
  assert.equal(buttonStyle({ ...MIDNIGHT, corners: "sharp" }, contrast).borderRadius, "2px");
});

test("every theme and every text makes a code that fits in a mailto link", () => {
  for (const theme of THEMES) {
    for (const text of BUTTON_TEXTS) {
      const snippet = snippetFor(theme, text);
      assert.ok(snippet.includes(escapeHtml(text)), `${theme.name} / ${text}`);
      assert.ok(developerEmail(snippet).length < 2000, `${theme.name} / ${text}`);
    }
  }
});

test("an owner's own button text is accepted in any language, tidied, and escaped", () => {
  for (const text of [
    "Get my free quote",
    "Book now!",
    "احجز الآن",
    "Réservez ici ✂️",
    "Book & save",
    "A".repeat(40),
  ]) {
    assert.equal(buttonTextProblem(text), "", text);
    assert.ok(snippetFor(STANDARD, text).endsWith(`>\n  ${escapeHtml(text)}\n</a>`), text);
  }
  assert.ok(snippetFor(STANDARD, "   Book    now  ").endsWith(">\n  Book now\n</a>"));
  assert.equal(normaliseButtonText("  a   b  "), "a b");
  assert.equal(normaliseButtonText(null), "");
  // HTML in the text is shown as text on the website, never run: it is escaped, not removed.
  const sneaky = snippetFor(STANDARD, '<script>alert(1)</script> & "x"');
  assert.doesNotMatch(sneaky, /<script/);
  assert.ok(sneaky.includes("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;"));
  // A quote in the text cannot close the style attribute or the tag around it.
  const quoted = snippetFor(STANDARD, 'x" onclick="y');
  assert.ok(quoted.includes("x&quot; onclick=&quot;y"));
  assert.doesNotMatch(quoted, /\sonclick="/);
});

test("a button text that is empty, too long or has forbidden characters is refused with a plain reason", () => {
  assert.equal(BUTTON_TEXT_MAX, 40);
  const tooLong = "A".repeat(41);
  assert.match(buttonTextProblem(tooLong), /at most 40 characters. This is 41/);
  assert.equal(buttonTextProblem("A".repeat(40)), "");
  // Characters are counted as people see them: 40 emoji fit even though each is two units of text.
  assert.equal(buttonTextProblem("😀".repeat(40)), "");
  assert.match(buttonTextProblem("😀".repeat(41)), /at most 40/);
  for (const text of ["", "   ", null, undefined, 42, ["Book now"], { toString: () => "Book now" }]) {
    assert.match(buttonTextProblem(text), /Type the words/, String(text));
  }
  for (const text of [
    "Book\nnow",
    "Book\tnow",
    "Book\u0000now",
    "Book\u007fnow",
    "Book\u2028now",
    "Book\u202enow",
    "Book\u2066now",
  ]) {
    assert.match(buttonTextProblem(text), /line break or a character we can't use/, JSON.stringify(text));
  }
  for (const text of [tooLong, "", "Book\nnow", null, 42]) {
    assert.throws(() => snippetFor(STANDARD, text), /button text cannot be used/, String(text));
  }
  assert.throws(() => buttonSnippet({ href: HREF, look: STANDARD, contrast }), /button text cannot be used/);
});

test("the worst-case button text still gives an email link that is short enough to open", () => {
  for (const text of [
    "&".repeat(40),
    "<".repeat(40),
    "احجز".repeat(10),
    "😀".repeat(40),
    '"'.repeat(40),
    "ß".repeat(40),
  ]) {
    const length = developerEmail(snippetFor(STANDARD, text)).length;
    assert.ok(length < 2000, `${text.slice(0, 6)}… gives ${length} characters`);
  }
});

test("a saved button text is checked again before it is used", () => {
  assert.equal(readSavedButtonText("Get my free quote"), "Get my free quote");
  assert.equal(readSavedButtonText("  Book   now "), "Book now");
  for (const saved of [null, undefined, "", "   ", "A".repeat(41), "Book\nnow", 42, ["x"]]) {
    assert.equal(readSavedButtonText(saved), null, String(saved));
  }
});

test("only checked colours and corner styles are accepted", () => {
  for (const accent of ["red", "#D62C2D", "#d62c2d;}body{display:none", "url(x)", "#fff"]) {
    assert.throws(() => snippetFor({ ...STANDARD, accent }), /cannot be used/, accent);
  }
  for (const corners of ["pill", "constructor", "__proto__"]) {
    assert.throws(() => snippetFor({ ...STANDARD, corners }), /cannot be used/, corners);
  }
});

test("only an absolute http or https address is accepted, so no script address can reach the code", () => {
  for (const href of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "\u0009javascript:alert(1)",
    "\njavascript:alert(1)",
    "data:text/html,<b>x</b>",
    "vbscript:x",
    "//evil.example/book/x",
    "/book/x",
    "book.example.com/book/x",
    "https://",
    "https://book.example.com/a b",
    "",
    null,
    42,
    ["https://book.example.com/book/x"],
    { toString: () => "https://book.example.com/book/x" },
  ]) {
    assert.throws(() => snippetFor(STANDARD, "Book now", href), /not a web address/, String(href));
  }
  assert.throws(() => buttonSnippet({ text: "Book now", look: STANDARD, contrast }), /not a web address/);
  for (const href of [
    "https://book.example.com/book/x",
    "http://localhost:8000/book/x",
    "HTTPS://BOOK.EXAMPLE.COM/book/x",
  ]) {
    assert.ok(snippetFor(STANDARD, "Book now", href).startsWith(`<a href="${href}"`), href);
  }
});

test("the address is escaped, so it cannot break out of its attribute", () => {
  const snippet = snippetFor(STANDARD, "Book now", 'https://book.example.com/book/x?a=1&b="2"><script>');
  assert.ok(snippet.includes('href="https://book.example.com/book/x?a=1&amp;b=&quot;2&quot;&gt;&lt;script&gt;"'));
  assert.doesNotMatch(snippet, /<script/);
  assert.equal(escapeHtml(`<>&"'`), "&lt;&gt;&amp;&quot;&#39;");
});

test("the button uses the look customers see: the published look while branding is included", () => {
  assert.deepEqual(liveLook({ allowed: true, published: MIDNIGHT }), MIDNIGHT);
  assert.deepEqual(liveLook({ allowed: true, published: null }), { ...STANDARD });
  assert.deepEqual(liveLook({ allowed: false, published: MIDNIGHT }), { ...STANDARD });
});

test("a copied code is out of date only after the code changes", () => {
  const first = snippetFingerprint(snippetFor(STANDARD));
  assert.match(first, /^[0-9a-f]{8}$/);
  assert.equal(first, snippetFingerprint(snippetFor(STANDARD)));
  assert.notEqual(first, snippetFingerprint(snippetFor(MIDNIGHT)));
  assert.notEqual(first, snippetFingerprint(snippetFor(STANDARD, "Book online")));
  assert.notEqual(first, snippetFingerprint(snippetFor(STANDARD, "Book now", `${HREF}-2`)));
  assert.equal(copiedCodeIsStale(null, first), false);
  assert.equal(copiedCodeIsStale("", first), false);
  assert.equal(copiedCodeIsStale(first, first), false);
  assert.equal(copiedCodeIsStale(first, snippetFingerprint(snippetFor(MIDNIGHT))), true);
});

test("the email to a web developer carries the code and the steps, and nothing is sent by us", () => {
  const snippet = snippetFor(MIDNIGHT);
  const link = developerEmail(snippet);
  assert.match(link, /^mailto:\?subject=/);
  const body = decodeURIComponent(link.split("&body=")[1]);
  assert.ok(body.includes(snippet));
  assert.match(body, /Paste it into the page's HTML/);
  assert.match(decodeURIComponent(link.split("?subject=")[1].split("&")[0]), /booking button/);
});
