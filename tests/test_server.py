from __future__ import annotations

import asyncio

import pytest

from lorecards import engine, server


def call(mcp, name, args):
    """Invoke a tool the way a client would.

    mcp 2.x returns a CallToolResult, mcp 1.x a ``(content, structured)`` tuple.
    """
    result = asyncio.run(mcp.call_tool(name, args))
    if isinstance(result, tuple):
        return result[1]
    for attr in ("structured_content", "structuredContent"):
        structured = getattr(result, attr, None)
        if structured is not None:
            return structured
    return result


@pytest.fixture
def mcp(vault):
    return server.build_server(vault)


def test_only_the_writing_tools_are_exposed(mcp):
    """Recall is the gateway's and the hook's job: a model that has to ask for a card
    mostly does not ask. The MCP surface is how an agent writes cards, nothing more."""
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"list_cards", "read_card", "write_card"}


def test_list_and_read(mcp):
    listed = call(mcp, "list_cards", {"kind": "people"})
    assert {c["key"] for c in listed["cards"]} == {"Alice", "Bob"}
    card = call(mcp, "read_card", {"key": "Alice"})
    assert card["kind"] == "people" and "payments team" in card["text"]
    assert "error" in call(mcp, "read_card", {"key": "nobody"})


def test_write_card_creates_a_card_that_then_fires(mcp, vault):
    out = call(mcp, "write_card", {
        "kind": "place", "key": "the corner cafe",
        "fields": {"where": "Two blocks away, green awning.", "relation": "Where I go to think."},
        "keywords": ["corner cafe", "the cafe"], "aliases": ["cafe with the awning"]})
    assert out["ok"] and out["path"] == "places/the corner cafe.md"
    engine.invalidate_cache()
    assert [c.key for c in engine.consult("meet at the corner cafe?", vault)["hits"]] \
        == ["the corner cafe"]


def test_write_card_reports_errors_instead_of_raising(mcp):
    assert call(mcp, "write_card", {"kind": "people", "key": "Alice", "fields": {"who": "x"},
                                    "keywords": ["Alice"]})["ok"] is False
    assert call(mcp, "write_card", {"kind": "people", "key": "../esc", "fields": {},
                                    "keywords": ["x"]})["ok"] is False


def test_write_card_update_recent(mcp):
    out = call(mcp, "write_card", {"kind": "people", "key": "Alice", "mode": "update_recent",
                                   "fields": {"recent": "Now leading the refund rewrite."}})
    assert out["ok"] and out["section"] == "recent"
    text = call(mcp, "read_card", {"key": "Alice"})["text"]
    assert "Now leading the refund rewrite." in text
    assert "Alice works on the payments team." in text


def test_server_instructions_are_about_writing(mcp):
    assert "write_card" in server.INSTRUCTIONS and "recall_cards" not in server.INSTRUCTIONS


def test_server_reports_its_name(mcp):
    assert mcp.name == "lorecards"
