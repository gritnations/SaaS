// ESLint covers new JavaScript at the repository root only.
// The inherited engine under schedule-booking/ has never been linted and is deliberately excluded.
import js from "@eslint/js";

export default [{ ignores: ["schedule-booking/**", "node_modules/**", ".venv/**"] }, js.configs.recommended];
