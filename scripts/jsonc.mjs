/**
 * Minimal JSONC parser for wrangler .jsonc files (bug-62).
 *
 * The clipforge repo has no jsonc-parser npm dependency and the only place
 * that reads these files from Node is scripts/reclaim-stale-task-labels.mjs,
 * so we hand-roll a small helper instead of pulling in a dep just for this.
 *
 * JSONC legitimately allows:
 *   - `//` line comments and `/* ... *\/` block comments,
 *   - a trailing comma before a closing `}` or `]`.
 *
 * The previous inline implementation in reclaim-stale-task-labels.mjs only
 * stripped comments and then called strict JSON.parse, which does NOT tolerate
 * trailing commas. bot/wrangler.bot-a.jsonc used to have the shape:
 *
 *   {
 *     ...
 *     "triggers": { "crons": ["* * * * *"] },
 *     // some comment lines
 *     // more comment lines
 *   }
 *
 * — after `// ...` lines were blanked to whitespace, the parser saw
 * `..., }` which is invalid strict JSON and threw:
 *   Expected double-quoted property name in JSON at position 534
 *   (line 31 column 1)
 * — the literal hourly cleanup.yml failure.
 *
 * Fix: strip trailing commas before `}` and `]` after comment removal, so
 * both existing-clean files and future JSONC-authored files parse robustly.
 * Kept as a standalone module (not exported from the CLI script) so tests
 * can import it without triggering the CLI script's top-level env checks.
 */

export function stripJsoncCommentsAndTrailingCommas(source) {
  return source
    // Block comments /* ... */
    .replace(/\/\*[\s\S]*?\*\//g, '')
    // Whole-line // comments (indented allowed). Keep the newline so line
    // numbers in downstream error messages stay meaningful.
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    // Trailing commas before } or ], allowing whitespace/newlines between
    // the comma and the closing token. The comment strippers above turn
    // comment-only lines into pure whitespace, so this catches the "comma,
    // then a block of comments, then }" pattern too. String contents are
    // untouched because a real string literal starts with a `"` and this
    // regex is anchored on `,` followed by whitespace and a bracket.
    .replace(/,(\s*[}\]])/g, '$1');
}

export function parseWranglerJsonc(source) {
  return JSON.parse(stripJsoncCommentsAndTrailingCommas(source));
}
