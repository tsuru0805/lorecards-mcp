# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-13

First release.

### Added

- **Card format** — Markdown + YAML frontmatter, one card per subject. Kinds `people`,
  `event`, `place`, `thing`, `slang` and free-form `entry`, decided by the directory.
  Forgiving frontmatter parsing; unknown headings are preserved. Directory and section
  names are configurable per vault through `kinds.yaml`.
- **Matching engine** — keyword/alias/title matching over a window of the current message
  plus the last `scan_depth` turns; `secondary.all` / `any` / `not` conditions; one level
  of `refs`; scoring with extra weight on the current message; a character budget that
  always lets the top card through; per-file fingerprint caching.
- **Gateway** (`lorecards gateway`) — a proxy in front of an OpenAI-compatible or Anthropic
  Messages API that injects the matching cards into the prompt, streaming included. Works with
  any client that lets you set a base URL. Credentials pass through untouched and unlogged;
  a failure on our side forwards the request unmodified rather than breaking the conversation.
- **Dedupe** — a per-conversation ledger (`<vault>/.lorecards/injections.json`) keeps the same
  card from being injected, and paid for, on every consecutive turn. Tunable with
  `--reinject-after`; conversations identified by `X-Lorecards-Conversation` or a fingerprint
  of how the conversation opened, and by `session_id` in the Claude Code hook.
- **Web UI + HTTP API** (`lorecards ui`) — one dependency-free page for writing cards
  (kind-aware forms built from the schema, keyword chips, try-a-sentence, recently surfaced,
  health check, import/export, archive and restore), plus the JSON API it is built on, so any
  other client can do the same. Works on a phone, installable as a PWA, no external requests.
  Beyond localhost a `--token` is required.
- **Guards** — the UI API checks `Host` and `Origin` and requires `X-Lorecards: 1` on writes,
  so another browser tab cannot reach a localhost vault; the gateway takes a `--token`
  (its own header, never the upstream's `Authorization`) and requires one beyond localhost;
  the dedupe ledger is written under a file lock with a unique temp name.
- **Private sections** — sections named in `private_sections` (default `correspondence`) stay
  on the card and stay editable, but are never matched on and never injected.
- **MCP server** (`lorecards-mcp`, stdio or streamable HTTP) with `write_card`, `read_card`
  and `list_cards` — the writing end. There is deliberately no recall tool: surfacing is the
  gateway's and the hook's job. Works with the `mcp` SDK 1.x and 2.x.
- **Claude Code hook** (`lorecards hook`) for `UserPromptSubmit`, so cards surface on every
  turn without the model having to call a tool. Reads recent turns from the transcript when
  the hook payload points at one, and never blocks a prompt.
- **CLI** — `try`, `list`, `read`, `check`, `init`, `import`, `export`, `hook`, `ui`, `gateway`.
- **SillyTavern lorebook import/export**, including `selectiveLogic` mapping and verbatim
  preservation of fields this project does not interpret.
- Optional `[zh]` extra for jieba segmentation; without it, matching falls back to substring
  search plus whitespace/punctuation tokens.
