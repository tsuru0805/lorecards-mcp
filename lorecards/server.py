"""MCP server: five tools over one card vault.

Run it over stdio (the default) or streamable HTTP::

    lorecards-mcp --vault ~/cards
    lorecards-mcp --vault ~/cards --http --port 8000
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server

    MCP_V2 = True
except ImportError:  # pragma: no cover - exercised by whichever SDK is installed
    from mcp.server.fastmcp import FastMCP as _Server

    MCP_V2 = False

from . import engine, schema as sc
from .cli import DEFAULT_VAULT, ENV_VAULT, HOOK_HEADER, resolve_vault

INSTRUCTIONS = """\
This vault is a card book: short Markdown cards about the people, events, places, things
and in-jokes the user has mentioned before. Call recall_cards near the start of a reply
whenever the user names someone or something that might already have a card — it is cheap
and returns nothing when no card matches. Use write_card when the user tells you something
about a subject that is worth keeping: mode="create" for a new card, mode="update_recent"
to refresh only the "recent" section of an existing one."""


def build_server(vault: str | Path, *, budget_chars: int = engine.DEFAULT_BUDGET_CHARS,
                 scan_depth: int = engine.DEFAULT_SCAN_DEPTH):
    """Build the MCP server bound to one vault directory."""
    from . import __version__

    vault_path = Path(vault).expanduser()
    extra = {"version": __version__} if MCP_V2 else {}   # mcp 1.x has no version argument
    mcp = _Server("lorecards", instructions=INSTRUCTIONS, **extra)

    def _schema() -> sc.CardSchema:
        return sc.load_schema(vault_path)

    @mcp.tool()
    def recall_cards(text: str, recent_turns: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """Return the cards that the current message (plus recent turns) triggers.

        text: what the user just said. recent_turns: the last few messages, oldest first,
        as [{"role": "user"|"assistant", "text": "..."}] — pass raw conversation text only.
        Returns a ready-to-read `context` block plus the matched cards.
        """
        result = engine.consult(text, vault_path, turns=recent_turns or [],
                                budget_chars=budget_chars, scan_depth=scan_depth,
                                schema=_schema())
        return {"context": engine.render_context(result, HOOK_HEADER),
                "cards": [c.as_dict() for c in result["hits"]],
                "truncated": result["truncated"], "chars": result["injected"]}

    @mcp.tool()
    def try_text(text: str, recent_turns: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """Dry run: which cards would fire, why, and which words look like they deserve a card."""
        result = engine.consult(text, vault_path, turns=recent_turns or [],
                                budget_chars=budget_chars, scan_depth=scan_depth,
                                dry_run=True, schema=_schema())
        debug = result.get("debug", {})
        return {"matched": debug.get("cards", []), "candidates": debug.get("candidates", {}),
                "jieba": debug.get("jieba", False), "truncated": result["truncated"]}

    @mcp.tool()
    def list_cards(kind: str | None = None) -> dict[str, Any]:
        """List every card in the book, optionally of one kind
        (people, event, place, thing, slang, entry)."""
        return {"vault": str(vault_path), "cards": engine.list_cards(vault_path, kind, _schema())}

    @mcp.tool()
    def read_card(key: str) -> dict[str, Any]:
        """Read one whole card by key (its file name without .md)."""
        try:
            return engine.read_card(vault_path, key, _schema())
        except engine.CardError as e:
            return {"error": str(e)}

    @mcp.tool()
    def write_card(kind: str, key: str, fields: dict[str, str],
                   keywords: list[str] | None = None, aliases: list[str] | None = None,
                   refs: list[str] | None = None, mode: str = "create") -> dict[str, Any]:
        """Write a card.

        kind: people | event | place | thing | slang | entry.
        key: the card's file name, also its default title.
        fields: section name -> text. people: who/stance/recent/impression;
        event: when/who/what/stance/followup; place: where/relation/recent;
        thing: what/usage/recent; slang: meaning/origin/usage; entry: {"_head": "..."}.
        keywords: what the card listens for. aliases: other ways the subject gets named.
        refs: keys of other cards worth one line of context when this one fires.
        mode="create" refuses to overwrite an existing card; mode="update_recent" rewrites
        only the recent section (followup for events) of a card that already exists.
        """
        try:
            return engine.write_card(vault_path, kind=kind, key=key, fields=fields or {},
                                     keywords=keywords, aliases=aliases, refs=refs,
                                     mode=mode, schema=_schema())
        except engine.CardError as e:
            return {"ok": False, "error": str(e)}

    return mcp


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lorecards-mcp", description="MCP server for a card book.")
    p.add_argument("--vault", help=f"card directory (default: ${ENV_VAULT} or {DEFAULT_VAULT})")
    p.add_argument("--budget", type=int, default=engine.DEFAULT_BUDGET_CHARS,
                   help="character budget for one recall (default: %(default)s)")
    p.add_argument("--scan-depth", dest="scan_depth", type=int, default=engine.DEFAULT_SCAN_DEPTH,
                   help="how many past turns a card looks back over (default: %(default)s)")
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    vault = resolve_vault(args.vault)
    vault.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(ENV_VAULT, str(vault))
    mcp = build_server(vault, budget_chars=args.budget, scan_depth=args.scan_depth)
    if args.http:
        if MCP_V2:
            mcp.run(transport="streamable-http", host=args.host, port=args.port)
        else:  # pragma: no cover - mcp 1.x reads host/port off the settings object
            mcp.settings.host = args.host
            mcp.settings.port = args.port
            mcp.run(transport="streamable-http")
    else:
        mcp.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
