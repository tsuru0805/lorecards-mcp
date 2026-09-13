from __future__ import annotations

import pytest

from lorecards import engine
from tests.conftest import write_card


def keys(result):
    return [c.key for c in result["hits"]]


def test_current_message_hit(vault):
    assert keys(engine.consult("what did Alice say", vault)) == ["Alice"]
    assert keys(engine.consult("nothing relevant here", vault)) == []
    assert keys(engine.consult("", vault)) == []


def test_alias_and_title_are_match_terms(vault):
    assert keys(engine.consult("ask Al about it", vault)) == ["Alice"]
    assert keys(engine.consult("the corner cafe? no, the payments team", vault)) == ["Alice"]


def test_window_reaches_back_but_respects_scan_depth(vault):
    turns = [{"role": "user", "text": "Alice reviewed my PR"},
             {"role": "assistant", "text": "nice"},
             {"role": "user", "text": "then lunch"},
             {"role": "assistant", "text": "ok"}]
    assert "Alice" in keys(engine.consult("did she approve it", vault, turns=turns))
    # scan_depth=1 keeps only the last turn (two messages), which no longer names her
    assert "Alice" not in keys(engine.consult("did she approve it", vault, turns=turns,
                                              scan_depth=1))
    # a card may pin its own depth
    write_card(vault, "people", "Carol", keywords=["Carol"], scan_depth=0,
               fields={"who": "Carol."})
    deep = [{"role": "user", "text": "Carol dropped by"}, {"role": "assistant", "text": "ok"}]
    assert "Carol" not in keys(engine.consult("and then", vault, turns=deep))
    assert "Carol" in keys(engine.consult("and then Carol left", vault, turns=deep))


def test_current_message_outranks_history(vault):
    write_card(vault, "entry", "back-then", keywords=["kangaroo"], body="a kangaroo")
    write_card(vault, "entry", "right-now", keywords=["platypus"], body="a platypus")
    turns = [{"role": "user", "text": "saw a kangaroo"}, {"role": "assistant", "text": "ok"}]
    order = keys(engine.consult("and now a platypus", vault, turns=turns, budget_chars=9999))
    assert order == ["right-now", "back-then"]


def test_priority_breaks_ties(vault):
    write_card(vault, "entry", "zeta", keywords=["Bob"], body="zeta body", priority=5)
    order = keys(engine.consult("Bob", vault, budget_chars=9999))
    assert order[0] == "zeta"


def test_secondary_all_any_not(vault):
    write_card(vault, "entry", "refund", keywords=["refund"], body="refund flow",
               secondary={"all": ["deploy", "Friday"]})
    assert keys(engine.consult("the refund thing", vault)) == []
    assert "refund" in keys(engine.consult("the refund deploy", vault,
                                           turns=[{"role": "user", "text": "Friday again"}]))

    write_card(vault, "entry", "standup", keywords=["standup"], body="standup notes",
               secondary={"any": ["Monday", "Tuesday"]})
    assert keys(engine.consult("standup?", vault)) == []
    assert "standup" in keys(engine.consult("Tuesday standup?", vault))

    write_card(vault, "entry", "mercury", keywords=["mercury"], body="the planet",
               secondary={"not": ["Freddie"]})
    assert keys(engine.consult("Freddie Mercury sang", vault)) == []
    assert "mercury" in keys(engine.consult("mercury is closest to the sun", vault))


def test_not_only_vetoes_the_turn_that_hit(vault):
    write_card(vault, "entry", "mercury", keywords=["mercury"], body="the planet",
               secondary={"not": ["Freddie"]})
    turns = [{"role": "user", "text": "Freddie was a great singer"},
             {"role": "assistant", "text": "he was"}]
    # the excluded word is two messages back, the hit is in the current message
    assert "mercury" in keys(engine.consult("anyway, mercury is tiny", vault, turns=turns))


def test_refs_add_one_line_only_when_target_did_not_fire(vault):
    hit = engine.consult("how did the Friday deploy go", vault, budget_chars=9999)["hits"]
    body = next(c.body for c in hit if c.key == "the Friday deploy")
    assert "see also: Bob — Bob is on call for releases." in body

    both = engine.consult("Friday deploy with Bob", vault, budget_chars=9999)["hits"]
    deploy = next(c.body for c in both if c.key == "the Friday deploy")
    assert "see also" not in deploy


def test_budget_lets_the_first_card_through_and_then_stops(vault):
    result = engine.consult("Alice and Bob and the Friday deploy", vault, budget_chars=1)
    assert len(result["hits"]) == 1
    assert result["truncated"] == 2
    big = engine.consult("Alice and Bob and the Friday deploy", vault, budget_chars=9999)
    assert len(big["hits"]) == 3 and big["truncated"] == 0


def test_disabled_and_non_injecting_cards(vault):
    write_card(vault, "entry", "off", keywords=["zebra"], body="x", enabled=False)
    write_card(vault, "entry", "quiet", keywords=["quokka"], body="x", inject=False)
    assert keys(engine.consult("zebra quokka", vault)) == []
    listed = {c["key"] for c in engine.list_cards(vault)}
    assert "quiet" in listed and "off" not in listed


def test_cards_with_no_keywords_or_no_body_are_skipped(vault):
    (vault / "empty.md").write_text("---\nkeywords: []\n---\nbody\n", encoding="utf-8")
    (vault / "nobody.md").write_text("---\nkeywords: [ghost]\n---\n\n", encoding="utf-8")
    (vault / "broken.md").write_text("---\nkeywords: [oops\n---\nbody\n", encoding="utf-8")
    engine.invalidate_cache()
    assert keys(engine.consult("ghost oops", vault)) == []


def test_single_character_keyword_needs_a_whole_token(vault):
    write_card(vault, "entry", "letter", keywords=["x"], body="the letter x")
    assert keys(engine.consult("this is an example", vault)) == []
    assert keys(engine.consult("the variable x again", vault)) == ["letter"]
    assert keys(engine.consult("this is an example", vault, allow_short_keywords=True)) == ["letter"]


def test_two_character_terms_match_inside_words(vault):
    """Documented trade-off: 2+ character terms match as substrings, so the alias "Al"
    fires on "wallaby". `lorecards check` is what warns about keywords this short."""
    assert "Alice" in keys(engine.consult("a wallaby", vault))


def test_cache_notices_an_edited_card(vault):
    assert keys(engine.consult("kangaroo", vault)) == []
    path = write_card(vault, "entry", "marsupial", keywords=["kangaroo"], body="it hops")
    assert keys(engine.consult("kangaroo", vault)) == ["marsupial"]
    path.write_text(path.read_text(encoding="utf-8").replace("kangaroo", "platypus"),
                    encoding="utf-8")
    # the fixture's invalidate_cache is not involved here: only the fingerprint is
    assert keys(engine.consult("kangaroo", vault)) == []
    assert keys(engine.consult("platypus", vault)) == ["marsupial"]


def test_missing_vault_is_not_an_error(tmp_path):
    assert engine.consult("anything", tmp_path / "nope")["hits"] == []


def test_render_context(vault):
    result = engine.consult("Alice", vault)
    text = engine.render_context(result)
    assert "### Alice [people]" in text and "payments team" in text
    assert engine.render_context({"hits": []}) == ""


def test_dry_run_reports_candidates_and_out_of_budget_cards(vault):
    result = engine.consult("Alice and Bob", vault, budget_chars=1, dry_run=True)
    cards = result["debug"]["cards"]
    assert [c["in_budget"] for c in cards] == [True, False]
    assert isinstance(result["debug"]["candidates"]["names"], list)


# --------------------------------------------------------------------------- tokenizer


def test_chinese_matches_with_and_without_jieba(vault, no_jieba):
    write_card(vault, "people", "linlin", keywords=["林林"], aliases=["小林"],
               fields={"who": "林林是同事。"})
    assert engine.has_jieba() is False
    assert keys(engine.consult("今天和林林吃饭了", vault)) == ["linlin"]
    assert keys(engine.consult("小林呢", vault)) == ["linlin"]


def test_fallback_tokenizer_still_handles_latin_tokens(vault, no_jieba):
    assert keys(engine.consult("did Alice reply?", vault)) == ["Alice"]
    assert engine.tokenize("Hello, world!") == {"hello", "world"}


@pytest.mark.skipif(not engine.has_jieba(), reason="jieba not installed")
def test_jieba_path_is_used_when_available(vault):
    write_card(vault, "people", "linlin", keywords=["林林"], fields={"who": "林林是同事。"})
    assert keys(engine.consult("今天和林林吃饭了", vault)) == ["linlin"]
    assert "林林" in engine.tokenize("今天和林林吃饭了")


# --------------------------------------------------------------------------- writes


def test_write_card_creates_and_refuses_to_clobber(vault):
    out = engine.write_card(vault, kind="place", key="the corner cafe",
                            fields={"where": "two blocks away"}, keywords=["corner cafe"])
    assert out["path"] == "places/the corner cafe.md"
    assert keys(engine.consult("meet at the corner cafe", vault)) == ["the corner cafe"]
    with pytest.raises(engine.CardError):
        engine.write_card(vault, kind="place", key="the corner cafe",
                          fields={"where": "x"}, keywords=["corner cafe"])


def test_write_card_rejects_bad_key_and_kind(vault):
    with pytest.raises(engine.CardError):
        engine.write_card(vault, kind="place", key="../escape", fields={}, keywords=["x"])
    with pytest.raises(engine.CardError):
        engine.write_card(vault, kind="nonsense", key="x", fields={}, keywords=["x"])


def test_update_recent_touches_only_that_section(vault):
    engine.write_card(vault, kind="people", key="Alice", mode="update_recent",
                      fields={"recent": "Now leading the refund rewrite."})
    card = engine.read_card(vault, "Alice")
    assert "Now leading the refund rewrite." in card["body"]
    assert "Alice works on the payments team." in card["body"]   # `who` untouched
    assert "Friendly." in card["body"]
    assert card["keywords"] == ["Alice", "payments team"] and card["aliases"] == ["Al"]


def test_update_recent_maps_events_to_followup_and_caps_length(vault):
    engine.write_card(vault, kind="event", key="the Friday deploy", mode="update_recent",
                      fields={"followup": "y" * 900})
    body = engine.read_card(vault, "the Friday deploy")["body"]
    assert "y" * 600 in body and "y" * 601 not in body
    assert "The weekly release goes out." in body


def test_update_recent_error_cases(vault):
    with pytest.raises(engine.CardError):
        engine.write_card(vault, kind="people", key="Nobody", mode="update_recent",
                          fields={"recent": "x"})
    with pytest.raises(engine.CardError):
        engine.write_card(vault, kind="people", key="Alice", mode="update_recent", fields={})
    write_card(vault, "slang", "ship it Friday", keywords=["ship it Friday"],
               fields={"meaning": "a joke"})
    with pytest.raises(engine.CardError):   # slang has no updatable section
        engine.write_card(vault, kind="slang", key="ship it Friday", mode="update_recent",
                          fields={"recent": "x"})
    with pytest.raises(engine.CardError):
        engine.write_card(vault, kind="people", key="Alice", mode="sideways", fields={})


def test_read_and_list_and_check(vault):
    assert engine.read_card(vault, "Alice")["kind"] == "people"
    with pytest.raises(engine.CardError):
        engine.read_card(vault, "nobody at all")
    assert {c["key"] for c in engine.list_cards(vault, "people")} == {"Alice", "Bob"}
    assert engine.check_vault(vault) == []
    (vault / "bare.md").write_text("no frontmatter\n", encoding="utf-8")
    assert any(i["code"] == "no_fm" for i in engine.check_vault(vault))


def test_candidates_skip_subjects_that_already_have_a_card(vault):
    debug = engine.consult("Alice met Dolores at the office", vault, dry_run=True)["debug"]
    names = debug["candidates"]["names"]
    assert "Dolores" in names and not any(n.lower() == "alice" for n in names)


# --------------------------------------------------------------------------- private sections


def test_private_sections_never_reach_a_model_but_stay_on_the_card(vault):
    path = write_card(vault, "people", "Carol", keywords=["Carol"],
                      fields={"who": "Carol runs the reading group.",
                              "correspondence": "Draft: ask about the Tuesday book. "
                                                "Her address is on the envelope."})
    raw = path.read_text(encoding="utf-8")
    assert "## correspondence" in raw and "Tuesday book" in raw     # written to disk in full

    card = engine.consult("what did Carol say", vault)["hits"][0]
    assert "reading group" in card.body
    assert "correspondence" not in card.body and "Tuesday book" not in card.body

    # ...and the words in there do not make the card fire either
    assert keys(engine.consult("anything about the Tuesday book?", vault)) == []

    detail = engine.card_detail(vault, "people", "Carol")
    assert detail["fields"]["correspondence"].startswith("Draft: ask")
    assert "Tuesday book" in engine.read_card(vault, "Carol")["text"]


def test_private_sections_survive_an_edit(vault):
    write_card(vault, "people", "Carol", keywords=["Carol"],
               fields={"who": "Carol runs the reading group.", "correspondence": "Draft: hello."})
    engine.upsert_card(vault, "people", "Carol",
                       {"keywords": ["Carol"], "fields": {"who": "Carol runs it.",
                                                          "correspondence": "Draft: hello."}})
    assert "Draft: hello." in engine.read_card(vault, "Carol")["text"]


def test_private_sections_are_configurable(vault):
    (vault / "kinds.yaml").write_text("private_sections: [scratch]\n", encoding="utf-8")
    write_card(vault, "entry", "note", keywords=["kraken"],
               body="the kraken is real\n\n## scratch\nmy private doubts\n\n## after\nvisible")
    engine.invalidate_cache()
    card = engine.consult("kraken", vault)["hits"][0]
    assert "private doubts" not in card.body and "visible" in card.body
    # correspondence is no longer private once the config names something else
    assert "scratch" not in card.body
