// ESLint covers new JavaScript at the repository root only.
// The inherited engine under schedule-booking/ has never been linted and is deliberately excluded.
import js from "@eslint/js";

const browser = Object.fromEntries(
  [
    "window",
    "document",
    "location",
    "history",
    "fetch",
    "URL",
    "URLSearchParams",
    "Blob",
    "Intl",
    "setTimeout",
    "sessionStorage",
    "localStorage",
    "navigator",
    "atob",
    "TextDecoder",
  ].map((name) => [name, "readonly"]),
);

export default [
  { ignores: ["schedule-booking/**", "node_modules/**", ".venv/**"] },
  js.configs.recommended,
  { files: ["public/**/*.js", "site/**/*.js"], languageOptions: { sourceType: "module", globals: browser } },
];
