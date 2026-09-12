# ClipForge — Stage 1: Architecture Session

## Repository

`https://github.com/motionssalt/clipforge.git`

All work in this session happens against this repository. Clone it,
read it, and push your changes back to it before this session ends.

## Your role in this project, and only this role

You are the **first of many separate, independent AI sessions** that will
together rebuild this tool into a new, cleaner version. Every session after
you will be a fresh instance with no memory of this conversation — the only
things that carry forward are what you write down and push to the
repository. This makes your job unusual: **you design, you do not build.**
Do not write the new version's implementation code yourself, even
partially, even for one small piece, even if it seems trivial. If you start
implementing, you will not finish before your session ends, and you will
leave behind a half-designed, half-built repository that the next session
cannot make sense of. Your entire job ends the moment your design is
written down and your handoff prompt is pushed. Stop there.

## What this tool is, functionally, today

This is a Telegram-bot-driven pipeline that turns a source video into an
edited, narrated, published short-form video. Read the current codebase
thoroughly before designing anything — the paragraph below is an outline
to orient you, not a substitute for reading the actual code, and it may be
incomplete or slightly out of date relative to what you find.

Roughly: a video source (a torrent, a Google Drive link, a direct URL, a
public social-media link, a public Telegram channel link, or a video sent
directly to the bot) gets downloaded and processed in a first stage —
transcription, screenshots, scene/event indexing. That stage produces a
prompt that either a human-operated AI agent (in one mode) or an
automated model call (in another mode) turns into a structured plan
describing narration, cuts, and timing. A second stage consumes that plan
and renders the final video — voiceover, captions/banner, reframing,
watermarking, compression — and optionally publishes it. There's also a
mode that chains this into an open-ended multi-part series, each part
ending on a cliffhanger. All of this is driven through a Telegram bot that
also handles per-user settings, credentials (so the tool can be cloned by
other users, each pointing at their own GitHub repo), and task tracking.

## Why this rebuild is happening

The current implementation works, but it accumulated through many rounds
of adaptation on top of adaptation, and it has become genuinely difficult
to use, extend, and debug — colliding features, tangled flows, and a
Telegram UI/UX that real users find confusing even though the underlying
concept (get a prompt, run it through an AI, bring back a result) is
simple. The goal of this rebuild is **not** to preserve the current
implementation's structure, file layout, function boundaries, or
step-by-step flow. The goal is to preserve and improve the *functionality*
— everything the tool currently does for the user must still be
achievable in the new version — while designing a genuinely better,
more coherent, more user-friendly flow from first principles. If you find
a materially better way to structure any part of this — including the
core "get a prompt, run it externally, bring back a result" loop itself —
you are encouraged to design it that way. You are not bound to replicate
the current tool's specific mechanics, only its capabilities.

## Two things that must be preserved exactly as they already work

These two pieces took a very long time to get right, are considered
fragile, and must not be redesigned or reimplemented from scratch. Locate
their current logic, understand it, and carry it forward as-is into the
new version's structure:

1. **The public Telegram channel-link MTProto download path.** This uses
   a user-authorized MTProto session and is deliberately restricted to
   the original ClipForge repository only — it must never become
   available to cloned installations. Find how that restriction is
   currently enforced (grep for how the original repository is
   identified and gated) and preserve that restriction precisely.
2. **The direct-video-to-bot relay path** (Bot A → private internal
   Telegram group → Bot B → GitHub Actions → bot-authorized MTProto
   download). This is what lets any user, including clone owners, send a
   video straight to the bot with no link-pasting.

You may refactor *where* this logic lives or *how it's organized* within
the new architecture, but the underlying mechanics, credentials flow, and
security boundaries (especially: the shared relay bot's credentials must
never become reachable from a cloned repository's workflow) must not
change unless you find and fix an actual bug in them — and if you do,
the fix must be minimal and targeted, not a rewrite of that subsystem.

## What "done" looks like for this session

By the end of this session, you must have:

1. **Moved every existing file into a clearly-named legacy folder**
   (e.g. `_legacy/`) at the repository root, preserving its internal
   structure, so it remains available as a working reference for exactly
   the two preserved subsystems above, and for anyone who wants to
   compare old and new behavior. Do not delete anything.
2. **Read and understood the current tool in full** — every workflow,
   every script, the entire Telegram bot — well enough to know precisely
   what it does end to end, not just what a few files suggest.
3. **Designed the new architecture.** This includes, at minimum:
   - The new stage/step boundaries and what flows between them.
   - The exact shape of every handoff — most importantly, the contract
     for "produce a prompt, take it externally, bring back a result" (or
     whatever you redesign that loop to be) — specified precisely enough
     that two different future sessions building against it will produce
     compatible code without ever talking to each other.
   - The new Telegram bot's command/settings/flow structure, designed
     for actual usability — assume the person using it is capable but has
     found the current bot's flow confusing, and design accordingly.
   - How Series Mode and Manual Mode fit into the new
     structure (these capabilities must still exist; how they're
     organized is yours to redesign).
   - Where and how the two preserved subsystems above plug into the new
     structure without being altered themselves.
4. **Written the design down** in the new repository root (e.g.
   `ARCHITECTURE.md` or similar — your call on naming), in enough detail
   that it is the single, unambiguous source of truth every future
   session will build against. This document is what prevents future
   sessions from each inventing their own incompatible version of the
   same decision — treat it as the most important artifact you produce.
5. **Written the build-progress checkpoint mechanism.** Design a JSON
   file (decide its exact shape, name, and location — e.g.
   `BUILD_PROGRESS.json`) that records, after every future session's
   work, precisely where that session left off, what was completed, and
   what remains — detailed enough that a fresh session with no memory of
   any prior one can read it and know exactly what to do next without
   guessing. Initialize this file to reflect that only the architecture
   phase (this session) is complete.
6. **Written the handoff prompt for every future building session.**
   This is a new file (e.g. `NEXT_SESSION_PROMPT.md`) — a complete,
   self-contained prompt that you write, addressed to the next AI
   session, that will be reused unchanged, session after session, for
   the rest of this build. It must instruct that session to:
   - Read `ARCHITECTURE.md` (or whatever you named it) and
     `BUILD_PROGRESS.json` first, in full, before doing anything else.
   - Build the next unbuilt piece of the design — using its own
     engineering judgment for implementation details, but never deviating
     from the architecture document's decisions.
   - If it believes it has found a genuine flaw in the architecture, it
     must not silently redesign around it — it should record the
     concern clearly in `BUILD_PROGRESS.json` for human review, continue
     building against the existing design as-is, and flag the concern
     clearly in its own final summary to the person running it.
   - Commit and push its work — every meaningful increment, not just at
     the end of the session — and update `BUILD_PROGRESS.json` on every
     push, so that a session that fails or runs out of room mid-task
     still leaves a resumable trail.
   - Never touch the two preserved subsystems' underlying logic (list
     them explicitly again in this prompt, by name/location as they
     exist in the new structure) except for narrowly-scoped, clearly
     justified bug fixes.
   - Include placeholders for the credentials the build may need to test
     against real infrastructure: Cloudflare account ID and API token,
     GitHub token, both Telegram bot tokens (main bot and relay bot), and
     the internal relay group's chat ID. State plainly in the prompt that
     these are placeholders to be filled in by the human operator before
     each session runs, and that they must never be committed to the
     repository, logged, or exposed in any output.
   - You (the architecture session) decide the exact wording and
     structure of this prompt — you understand the new design, so you
     are better positioned than anyone to write instructions for
     building it correctly.
   - **Credentials must never appear as plaintext in this prompt, in any
     file in the repository, or in anything the operator pastes into a
     chat — with exactly one narrow exception.** Every future session
     authenticates to the repository via a placeholder in the prompt
     itself, filled in by the operator before each session:

     ```
     GITHUB_REPO_PAT = <PLACEHOLDER>
     ```

     This must be a fine-grained GitHub PAT scoped to this repository
     only, with the bare minimum permissions to operate (Contents,
     Actions, Workflows — read/write), nothing account-wide. This is the
     one credential allowed to appear as a fillable placeholder, because
     it is narrowly scoped to this repository alone and carries no access
     to any other system. It is used only for git/gh operations on this
     repository, never printed, logged, or written into any file, commit,
     or output.

     Everything the *application* itself needs at runtime is different
     and must NOT follow this placeholder pattern — this repository
     already has the correct pattern in place for that: `stage-a.yml`
     reads `CLIPFORGE_TELEGRAM_API_ID`, `CLIPFORGE_TELEGRAM_API_HASH`,
     `CLIPFORGE_TELEGRAM_SESSION`, and `GITHUB_TOKEN` via
     `${{ secrets.NAME }}`, never as literal values anywhere in the
     codebase. Every session's prompt must instruct the builder to use
     this same mechanism for any new credential the new architecture
     needs (e.g. a Cloudflare API token, additional Telegram bot tokens,
     the relay group's chat ID): reference it by name through GitHub
     Actions Secrets (or the equivalent Cloudflare Worker secret binding
     for anything living in a Worker), and instruct the operator to set
     the actual value directly in GitHub's or Cloudflare's secret store —
     never in a chat message, never in a text file, never in the prompt
     itself. If a new secret name is needed that doesn't exist yet, the
     prompt should list the name and where it needs to be set (GitHub
     repo secrets vs. Cloudflare Worker secrets), and nothing more.

## How this session authenticates to the repository

Fill in the placeholder below before running this prompt. This must be a
**fine-grained GitHub Personal Access Token, scoped to this repository
only** (`motionssalt/clipforge`), with the bare minimum permissions
needed to operate — Contents (read/write), Actions (read/write), and
Workflows (read/write) — nothing account-wide, no access to other
repositories, no billing/account/user management scope:

```
GITHUB_REPO_PAT = <PLACEHOLDER>
```

Use it via `git`/`gh` CLI commands (`git push`, `gh pr create`, etc.) to
operate on this repository. This token is scoped narrowly on purpose —
treat it accordingly: use it to read, commit, and push, and never print
its value, log it, write it into a file, or include it in any commit,
comment, or output, even though it's a limited-scope credential rather
than a broad one. This token is entirely separate from the runtime
secrets described below — it exists only so you can operate on the
repository itself, not so the deployed application can call Telegram/
Cloudflare/GitHub APIs at runtime.

## How future application code authenticates at runtime

This repository already uses GitHub Actions Secrets correctly for its
existing credentials (see `CLIPFORGE_TELEGRAM_API_ID`,
`CLIPFORGE_TELEGRAM_API_HASH`, `CLIPFORGE_TELEGRAM_SESSION`,
`GITHUB_TOKEN` in `stage-a.yml`, referenced as `${{ secrets.NAME }}`, never
as literal values). If you need to validate real behavior while
understanding the current tool (e.g. confirming how the relay path
behaves), do so by running the existing workflows, which already read
their secrets this way — do not ask the operator to paste credential
values into this conversation or into any file, and do not accept them if
offered that way. If understanding the design requires a credential that
isn't already wired up as a named secret, note that in your handoff
(`ARCHITECTURE.md` / `BUILD_PROGRESS.json`) as something the operator
needs to add to the repository's or Worker's secret store under a specific
name — never as a value you request directly.

## Constraints on how you work this session

- Do not ask the operator questions expecting a reply — you will not get
  one, and this session will end while waiting. Make the best reasoned
  decision yourself, document your reasoning where it matters, and move
  forward.
- Do not begin implementing the new version. If you find yourself writing
  application code rather than documentation/design/scaffolding, stop —
  that is the next session's job, not yours.
- Push your work — the legacy-file move, `ARCHITECTURE.md`,
  `BUILD_PROGRESS.json`, and `NEXT_SESSION_PROMPT.md` — before your
  session ends. An unpushed design is a lost design.
- You have full autonomy over the *how* of the new design. Nothing above
  should be read as dictating specific architecture choices — it dictates
  what you must decide and hand off, not what the decisions must be.
