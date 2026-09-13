from __future__ import annotations

import json

from lorecards import engine, io as lore_io, schema as sc

BOOK = {
    "entries": {
        "0": {
            "uid": 0, "key": ["Alice", "payments"], "keysecondary": ["deploy"],
            "selectiveLogic": 0, "selective": True, "comment": "Alice",
            "content": "Alice works on payments.", "constant": False, "order": 42,
            "scanDepth": 6, "disable": False, "position": 0, "probability": 100,
        },
        "1": {
            "uid": 1, "key": ["mercury"], "keysecondary": ["Freddie"], "selectiveLogic": 2,
            "comment": "mercury", "content": "The planet.", "disable": True, "order": 0,
        },
        "2": {
            "uid": 2, "key": ["gate"], "keysecondary": ["a", "b"], "selectiveLogic": 1,
            "comment": "gate", "content": "Needs both.",
        },
        "3": {"uid": 3, "key": [], "comment": "", "content": "orphan"},
    }
}


def _write_book(tmp_path, book=BOOK):
    p = tmp_path / "book.json"
    p.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
    return p


def test_import_maps_every_field(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    result = lore_io.import_book(_write_book(tmp_path), vault)
    assert set(result["written"]) == {"Alice", "mercury", "gate"}
    assert result["no_keywords"] == ["3"]

    text = (vault / "Alice.md").read_text(encoding="utf-8")
    fm, body, problem = sc.split_frontmatter(text)
    assert problem is None
    assert fm["keywords"] == ["Alice", "payments"]
    assert fm["secondary"] == {"any": ["deploy"]}
    assert fm["priority"] == 42
    assert fm["scan_depth"] == 3            # 6 SillyTavern messages = 3 turns
    assert fm["st"]["constant"] is False    # unsupported, preserved verbatim
    assert fm["st"]["probability"] == 100
    assert body.strip() == "Alice works on payments."

    mercury, _, _ = sc.split_frontmatter((vault / "mercury.md").read_text(encoding="utf-8"))
    assert mercury["secondary"] == {"not": ["Freddie"]}   # 2 = NOT ANY
    assert mercury["enabled"] is False                    # disable: true

    gate, _, _ = sc.split_frontmatter((vault / "gate.md").read_text(encoding="utf-8"))
    assert gate["secondary"] == {"all": ["a", "b"]}       # 1 = AND ALL


def test_not_all_is_approximated_by_not(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    book = {"entries": {"0": {"uid": 0, "key": ["x"], "keysecondary": ["p", "q"],
                              "selectiveLogic": 3, "comment": "x", "content": "body"}}}
    lore_io.import_book(_write_book(tmp_path, book), vault)
    fm, _, _ = sc.split_frontmatter((vault / "x.md").read_text(encoding="utf-8"))
    assert fm["secondary"] == {"not": ["p", "q"]}


def test_import_does_not_overwrite_without_force(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    (vault / "Alice.md").write_text("---\nkeywords: [mine]\n---\nhands off\n", encoding="utf-8")
    result = lore_io.import_book(_write_book(tmp_path), vault)
    assert result["skipped"] == ["Alice"]
    assert "hands off" in (vault / "Alice.md").read_text(encoding="utf-8")

    result = lore_io.import_book(_write_book(tmp_path), vault, force=True)
    assert "Alice" in result["written"]
    assert "hands off" not in (vault / "Alice.md").read_text(encoding="utf-8")


def test_imported_cards_actually_fire(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    lore_io.import_book(_write_book(tmp_path), vault)
    engine.invalidate_cache()
    hits = engine.consult("what about Alice and the deploy", vault)["hits"]
    assert [c.key for c in hits] == ["Alice"]


def test_round_trip_keeps_the_fields_we_map(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    lore_io.import_book(_write_book(tmp_path), vault)
    out = lore_io.export_book(vault, tmp_path / "out.json")
    by_comment = {e["comment"]: e for e in out["entries"].values()}

    alice = by_comment["Alice"]
    assert alice["key"] == ["Alice", "payments"]
    assert alice["keysecondary"] == ["deploy"] and alice["selectiveLogic"] == 0
    assert alice["order"] == 42 and alice["scanDepth"] == 6
    assert alice["disable"] is False and alice["content"] == "Alice works on payments."
    assert alice["probability"] == 100 and alice["position"] == 0 and alice["uid"] == 0

    assert by_comment["mercury"]["selectiveLogic"] == 2
    assert by_comment["mercury"]["disable"] is True
    assert by_comment["gate"]["selectiveLogic"] == 1

    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert len(written["entries"]) == 3


def test_export_of_a_hand_written_card(tmp_path):
    vault = tmp_path / "cards"
    (vault / "people").mkdir(parents=True)
    (vault / "people" / "Bob.md").write_text(
        "---\nkeywords: [Bob]\naliases: [Bobby]\n---\n## who\nOn call.\n", encoding="utf-8")
    entry = next(iter(lore_io.export_book(vault)["entries"].values()))
    assert entry["key"] == ["Bob", "Bobby"]     # aliases fold into ST primary keys
    assert "On call." in entry["content"] and entry["comment"] == "Bob"


def test_entries_as_a_list_are_accepted(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    book = [{"uid": 7, "key": ["listy"], "comment": "listy", "content": "body"}]
    lore_io.import_book(_write_book(tmp_path, book), vault)
    assert (vault / "listy.md").is_file()


def test_import_into_a_kind_directory(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    lore_io.import_book(_write_book(tmp_path), vault, kind="people")
    assert (vault / "people" / "Alice.md").is_file()
    assert engine.list_cards(vault, "people")


def test_unsafe_comment_does_not_escape_the_vault(tmp_path):
    vault = tmp_path / "cards"
    vault.mkdir()
    book = {"entries": {"0": {"uid": 0, "key": ["k"], "comment": "../../escape",
                              "content": "body"}}}
    lore_io.import_book(_write_book(tmp_path, book), vault)
    assert not (tmp_path.parent / "escape.md").exists()
    assert sorted(p.name for p in vault.rglob("*.md")) == ["k.md"]
