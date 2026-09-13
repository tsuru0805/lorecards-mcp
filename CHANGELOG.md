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
- **MCP server** (`lorecards-mcp`, stdio or streamable HTTP) with the tools `recall_cards`,
  `write_card`, `list_cards`, `read_card` and `try_text`. Works with the `mcp` SDK 1.x and 2.x.
- **Claude Code hook** (`lorecards hook`) for `UserPromptSubmit`, so cards surface on every
  turn without the model having to call a tool. Reads recent turns from the transcript when
  the hook payload points at one, and never blocks a prompt.
- **CLI** — `try`, `list`, `read`, `check`, `init`, `import`, `export`, `hook`.
- **SillyTavern lorebook import/export**, including `selectiveLogic` mapping and verbatim
  preservation of fields this project does not interpret.
- Optional `[zh]` extra for jieba segmentation; without it, matching falls back to substring
  search plus whitespace/punctuation tokens.
