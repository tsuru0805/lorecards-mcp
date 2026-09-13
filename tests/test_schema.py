from __future__ import annotations

import textwrap

from lorecards import schema as sc


def test_parse_str_list_is_forgiving():
    assert sc.parse_str_list("Alice, Bob、Carol") == ["Alice", "Bob", "Carol"]
    assert sc.parse_str_list("[Alice, Alice]") == ["Alice"]
    assert sc.parse_str_list(["'Alice'", 3, None, ["nested"]]) == ["Alice", "3"]
    assert sc.parse_str_list(None) == []
    assert sc.parse_str_list({"a": 1}) == []


def test_parse_secondary_and_scan_depth_fall_back():
    assert sc.parse_secondary(None) == {"all": [], "any": [], "not": []}
    assert sc.parse_secondary({"any": "a, b"}) == {"all": [], "any": ["a", "b"], "not": []}
    assert sc.parse_scan_depth(None) is None
    assert sc.parse_scan_depth("nope") is None
    assert sc.parse_scan_depth(True) is None
    assert sc.parse_scan_depth(99) == sc.MAX_SCAN_DEPTH
    assert sc.parse_scan_depth(-4) == 0


def test_safe_key_rejects_traversal():
    assert sc.safe_key(" Alice.md ") == "Alice"
    for bad in ("", ".", "..", "../x", "a/b", "a\\b", ".hidden", "a\x00b"):
        assert sc.safe_key(bad) is None


def test_split_frontmatter_problems():
    fm, body, problem = sc.split_frontmatter("no frontmatter here")
    assert (fm, problem) == ({}, "no_fm")
    fm, _, problem = sc.split_frontmatter("---\nkeywords: [a\n---\nbody\n")
    assert fm is None and problem == "bad_fm"
    fm, body, problem = sc.split_frontmatter("---\nkeywords: [a]\n---\nbody\n")
    assert problem is None and fm["keywords"] == ["a"] and body.strip() == "body"


def test_kind_comes_from_directory_not_frontmatter():
    fm = {"kind": "people", "keywords": ["x"]}
    assert sc.parse_meta(fm, "events/x.md", "x")["kind"] == "event"
    assert sc.parse_meta(fm, "x.md", "x")["kind"] == "entry"
    assert sc.DEFAULT_SCHEMA.rel_for("people", "Alice") == "people/Alice.md"
    assert sc.DEFAULT_SCHEMA.rel_for("entry", "Alice") == "Alice.md"


def test_sections_round_trip_and_keep_unknown_headings():
    body = textwrap.dedent("""\
        lead-in text

        ## who
        Alice.

        ## private notes
        kept verbatim

        ## recent
        the refund rewrite
        """)
    secs = sc.split_sections(body, "people")
    assert secs["who"] == "Alice."
    assert secs["recent"] == "the refund rewrite"
    assert secs["stance"] == ""
    assert secs["_head"] == "lead-in text"
    assert "## private notes" in secs["_extra"]
    rendered = sc.render_sections("people", secs)
    assert "## private notes" in rendered and rendered.index("## who") < rendered.index("## recent")


def test_render_card_omits_empty_optional_keys():
    text = sc.render_card(kind="people", key="Alice", fields={"who": "hi"}, keywords=["Alice"])
    fm, _, problem = sc.split_frontmatter(text)
    assert problem is None
    assert fm["keywords"] == ["Alice"] and fm["title"] == "Alice"
    assert "aliases" not in fm and "secondary" not in fm and "refs" not in fm


def test_who_line_truncates():
    body = "## who\n" + "x" * 300
    assert len(sc.who_line(body, "people")) == sc.WHO_LINE_MAX_CHARS


def test_checkup_codes():
    codes = lambda *a: {i["code"] for i in sc.checkup(*a)}
    assert codes("people/x.md", {}, "body", "no_fm") == {"no_fm"}
    assert codes("people/x.md", None, "body", "bad_fm") == {"bad_fm"}
    assert "no_kw" in codes("people/x.md", {"title": "x"}, "body", None)
    assert "generic_kw" in codes("people/x.md", {"keywords": ["the", "A"]}, "body", None)
    assert "title_mismatch" in codes("people/x.md", {"keywords": ["q"], "title": "y"}, "b", None)
    assert "no_body" in codes("people/x.md", {"keywords": ["q"]}, "   ", None)
    assert codes("people/Alice.md", {"keywords": ["Alice"], "title": "Alice"}, "body", None) == set()


def test_kinds_yaml_overrides_directories_and_sections(tmp_path):
    (tmp_path / "kinds.yaml").write_text(
        "dirs: {people: personnes}\nsections: {people: [qui, recent]}\n"
        "recent_section: {people: recent}\ngeneric_keywords: [le]\n", encoding="utf-8")
    schema = sc.load_schema(tmp_path)
    assert schema.rel_for("people", "Alice") == "personnes/Alice.md"
    assert schema.kind_of_rel("personnes/Alice.md") == "people"
    assert schema.kind_of_rel("people/Alice.md") == "entry"
    assert schema.sections_for("people") == ["qui", "recent"]
    assert schema.recent_for("people") == "recent"
    assert "le" in schema.generic_keywords


def test_kinds_yaml_broken_falls_back(tmp_path):
    (tmp_path / "kinds.yaml").write_text("dirs: [not, a, mapping\n", encoding="utf-8")
    assert sc.load_schema(tmp_path) is sc.DEFAULT_SCHEMA
