# lorecards

A keyword-triggered **card book** for companion agents. One card per person, event, place,
thing or in-joke the user has mentioned — plain Markdown with YAML frontmatter, in a folder
they own. When the conversation mentions a card's keywords (anywhere in the last few turns,
not just the current message), the card surfaces. So the second time a friend or a running
saga comes up, the AI already knows who is who, and nobody has to write a "previously on…"
recap.

It is not RAG and not a memory store. Nothing is embedded, scored by similarity, or written
behind the user's back. A card book is **canon**: what the user (or the agent, through the
`write_card` tool) decided is worth remembering, in a file they can read, edit and delete.

```
~/cards/
  people/Alice.md
  events/the Friday deploy.md
  places/the corner cafe.md
  things/the blue notebook.md
  slang/ship it Friday.md
```

## Install

Two ways to wire it up. They are independent — pick one, or run both.

### 1. As an MCP server (any MCP client)

```bash
# Claude Code
claude mcp add lorecards -- uvx lorecards-mcp --vault ~/cards

# or run it directly
uvx lorecards-mcp --vault ~/cards
pipx run lorecards-mcp --vault ~/cards      # same thing without uv
```

Claude Desktop — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lorecards": {
      "command": "uvx",
      "args": ["lorecards-mcp", "--vault", "/Users/you/cards"]
    }
  }
}
```

For Chinese (or any language without spaces between words), install the segmenter extra:
`uvx --from 'lorecards-mcp[zh]' lorecards-mcp --vault ~/cards`. The server runs on both the
1.x and 2.x `mcp` Python SDK.

In Claude Code, a newly added MCP server waits for approval — start `claude` once in that
project and approve it.

**The limit of this path:** the model has to call `recall_cards` for a card to surface. If it
does not call the tool, nothing happens. Give it a standing instruction — in the system prompt,
or in `CLAUDE.md`:

> Before answering, if the message mentions a person, place, event or phrase that might already
> be in the card book, call `recall_cards` with the user's message and the last few turns. It is
> cheap and returns nothing when there is no match.

### 2. As a Claude Code hook (fires on every turn, no tool call)

This injects matching cards automatically, the way a system-side world-info injection works.
In `.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "lorecards hook --vault ~/cards", "timeout": 10 }
        ]
      }
    ]
  }
}
```

`lorecards hook` reads the hook JSON on stdin (`user_prompt`, plus `transcript_path` for the
recent turns) and answers with `hookSpecificOutput.additionalContext`. It exits 0 and prints
nothing when no card matches, and it never blocks a prompt — a broken vault costs you the
cards, not the turn. (Hook contract per the Claude Code docs, checked 2026-09-13.)

Install `lorecards` on PATH for that command: `uv tool install lorecards-mcp`, or
`pipx install lorecards-mcp`, or point the hook at a virtualenv:
`command: "/path/to/.venv/bin/lorecards hook --vault ~/cards"`.

### From source

```bash
git clone https://github.com/tsuru0805/lorecards-mcp && cd lorecards-mcp
python3 -m venv .venv && .venv/bin/pip install -e '.[zh]'
.venv/bin/lorecards-mcp --vault ~/cards        # MCP server
.venv/bin/lorecards try "hello" --vault ~/cards
```

Point your MCP config or your hook command at the absolute path of `.venv/bin/lorecards-mcp`
or `.venv/bin/lorecards`.

### Start a vault

```bash
lorecards init ~/cards      # makes the directories and one example card per kind
lorecards try "how did the friday deploy go" --vault ~/cards
```

## Card format

`~/cards/people/Alice.md`:

```markdown
---
kind: people
title: Alice
keywords: [Alice, payments team]
aliases: [Al]
secondary:
  not: [Alice Cooper]
scan_depth: 3
refs: [Bob]
priority: 1
---
## who
Alice — works on the payments team, sits two desks away.

## stance
Friendly. She reviews my pull requests and I review hers.

## recent
Took over the refund flow rewrite in September.

## impression
Direct, allergic to meetings that could have been a message.
```

The **directory** decides the kind; `kind:` in the frontmatter is only a copy. Sections per kind:

| kind | directory | sections |
| --- | --- | --- |
| people | `people/` | who · stance · recent · impression |
| event | `events/` | when · who · what · stance · followup |
| place | `places/` | where · relation · recent |
| thing | `things/` | what · usage · recent |
| slang | `slang/` | meaning · origin · usage |
| entry | vault root | free-form |

Headings you add that are not in the table are kept verbatim, never dropped. Frontmatter is
parsed forgivingly: `keywords: Alice, Bob` works as well as a proper list, and a malformed
value falls back to the default instead of breaking the card.

Directory and section names are configurable, so a vault can be written in another language —
put a `kinds.yaml` in the vault root:

```yaml
dirs:     { people: personnes, event: evenements }
sections: { people: [qui, relation, recent, impression] }
recent_section: { people: recent }
```

## How matching works

| | |
| --- | --- |
| **Match terms** | `keywords` + `aliases` + `title`, case-insensitive |
| **Window** | the current message + the last `scan_depth` turns (default 3, ~2 messages each) |
| **Substring vs token** | terms of 2+ characters may match inside a word; 1-character terms must match a whole token, so a single-character keyword does not fire on every word containing it |
| **`secondary.all`** | every listed word must appear somewhere in the window |
| **`secondary.any`** | at least one must appear in the window |
| **`secondary.not`** | checked only in the messages that produced the hit, so an excluded word three turns back does not veto a card the user just named |
| **`refs`** | one level deep: a referenced card contributes one line ("who is this"), not its whole body — and nothing if it already fired on its own |
| **Score** | number of distinct terms hit, plus one per term hit in the *current* message |
| **Order** | score ↓, then `priority` ↓, then title |
| **Budget** | 800 characters by default. The top card always gets in even if it is over budget; the first card that would overflow stops the list, and the rest are reported as `truncated` |
| **`enabled: false`** | never loaded. **`inject: true/false`** — `false` keeps a card readable by `read_card`/`list_cards` but out of automatic recall |

Chinese, Japanese and other unspaced text are segmented with [jieba](https://github.com/fxsy/jieba)
when the `[zh]` extra is installed. Without it, matching falls back to substring search plus
whitespace/punctuation tokens: multi-character terms still work, single-character ones mostly
will not.

## Import / export (SillyTavern lorebooks)

```bash
lorecards import book.json --vault ~/cards          # existing cards are kept
lorecards import book.json --vault ~/cards --force  # overwrite them
lorecards export out.json --vault ~/cards
```

| SillyTavern | card |
| --- | --- |
| `key` | `keywords` |
| `keysecondary` + `selectiveLogic` | `secondary` — `0 AND ANY → any`, `1 AND ALL → all`, `2 NOT ANY → not`, `3 NOT ALL → not` |
| `comment` | `title` |
| `content` | card body |
| `order` | `priority` |
| `scanDepth` (falling back to `depth`) | `scan_depth` |
| `disable` | `enabled`, inverted |
| everything else (`constant`, `position`, `probability`, `uid`, …) | kept verbatim under `st:` in the frontmatter, so an export round-trips |

**Known mapping limits**

- `constant` (always-on entries) is **not supported** — a card fires on keywords or not at all.
  The flag is preserved on export; it does nothing while the card is in this book.
- `3 NOT ALL` is approximated by `not`, which vetoes when **any** of the words appears.
  SillyTavern only vetoes when **all** of them do, so an imported NOT ALL entry is stricter here.
- `scanDepth` counts **messages** in SillyTavern and **turns** here, so it is halved on import
  and doubled on export; odd values round up.
- `position`, `probability`, `depth` (insertion depth), `group`, `role` and recursion settings
  have no equivalent — they are carried through, not honoured.
- Import puts everything in one kind (`--kind`, default `entry`); sorting a book into
  people/events/places is a manual pass afterwards.

## CLI

```
lorecards try "sentence"        which cards fire, why, and what looks like it deserves a card
  --turns '[{"role":"user","text":"..."}]'   --show-context   --json
lorecards list [--kind people]  every card with its keywords
lorecards read Alice            print one card
lorecards check                 cards with no keywords, keywords so generic they fire
                                every turn, or a title that does not match the file name
lorecards init ~/cards          create the vault with one example card per kind
lorecards import book.json      SillyTavern lorebook in
lorecards export out.json       SillyTavern lorebook out
lorecards hook                  the Claude Code UserPromptSubmit hook
```

## Known limits

- **The MCP path depends on the model choosing to call `recall_cards`.** Only the hook path
  fires on every turn.
- Matching is keywords, not meaning. "my sister" does not find the card titled `Rin` unless
  `my sister` is one of its keywords or aliases. That is the trade: it is predictable, auditable,
  and costs no embedding pass.
- One vault per server process. Multiple characters or profiles means multiple entries in your
  MCP config, one `--vault` each.
- Cards are cached on file mtime and size, so editing a card in an editor takes effect on the
  next lookup — but a change that keeps both identical will not be noticed.
- `write_card(mode="update_recent")` re-checks the file's mtime before writing and refuses if
  the file changed underneath it. It is not a lock: two agents writing the same card in the same
  millisecond is out of scope.
- No pagination: a very large book still loads every card into memory at lookup time.

## Author

- **晚晚** ([@tsuru0805](https://github.com/tsuru0805)) — design, decisions, real-world acceptance.
- **弥野** (Claude, 晚晚's engineering hand) — implementation and docs.

Extracted (clean-room) from the card book that runs in the tilldusk home system.

MIT licensed. Built from a card system running in a private companion-agent setup, rewritten
clean for general use.
