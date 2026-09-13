from __future__ import annotations

import asyncio

import pytest

from lorecards import server


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


def test_tools_are_registered(mcp):
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"recall_cards", "try_text", "list_cards", "read_card", "write_card"}


def test_recall_cards_returns_context_and_cards(mcp):
    out = call(mcp, "recall_cards", {"text": "what is Alice up to"})
    assert [c["key"] for c in out["cards"]] == ["Alice"]
    assert "### Alice [people]" in out["context"]
    assert out["truncated"] == 0 and out["chars"] > 0


def test_recall_cards_uses_recent_turns(mcp):
    args = {"text": "did she approve it",
            "recent_turns": [{"role": "user", "text": "Alice reviewed my PR"},
                             {"role": "assistant", "text": "nice"}]}
    assert [c["key"] for c in call(mcp, "recall_cards", args)["cards"]] == ["Alice"]
    assert call(mcp, "recall_cards", {"text": "did she approve it"})["cards"] == []


def test_try_text_is_a_dry_run(mcp):
    out = call(mcp, "try_text", {"text": "Alice and Bob"})
    assert {c["key"] for c in out["matched"]} == {"Alice", "Bob"}
    assert "candidates" in out


def test_list_and_read(mcp):
    listed = call(mcp, "list_cards", {"kind": "people"})
    assert {c["key"] for c in listed["cards"]} == {"Alice", "Bob"}
    card = call(mcp, "read_card", {"key": "Alice"})
    assert card["kind"] == "people" and "payments team" in card["text"]
    assert "error" in call(mcp, "read_card", {"key": "nobody"})


def test_write_card_creates_then_recalls(mcp, vault):
    out = call(mcp, "write_card", {
        "kind": "place", "key": "the corner cafe",
        "fields": {"where": "Two blocks away, green awning.", "relation": "Where I go to think."},
        "keywords": ["corner cafe", "the cafe"], "aliases": ["cafe with the awning"]})
    assert out["ok"] and out["path"] == "places/the corner cafe.md"
    recalled = call(mcp, "recall_cards", {"text": "meet at the corner cafe?"})
    assert [c["key"] for c in recalled["cards"]] == ["the corner cafe"]


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


def test_server_instructions_tell_the_model_when_to_call(mcp):
    assert "recall_cards" in server.INSTRUCTIONS


def test_server_reports_its_name(mcp):
    assert mcp.name == "lorecards"
