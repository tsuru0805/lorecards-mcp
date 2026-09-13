from __future__ import annotations

from pathlib import Path

import pytest

from lorecards import engine, schema as sc


@pytest.fixture(autouse=True)
def _clean_cache():
    engine.invalidate_cache()
    yield
    engine.invalidate_cache()


def write_card(vault: Path, kind: str, key: str, *, keywords, body="", fields=None,
               aliases=None, secondary=None, refs=None, scan_depth=None, priority=0,
               enabled=True, inject=True) -> Path:
    """Write a card straight to disk, bypassing the engine's guards."""
    schema = sc.load_schema(vault)
    path = vault / schema.rel_for(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = dict(fields or {})
    if body:
        fields.setdefault("_head" if kind == "entry" else schema.sections_for(kind)[0], body)
    path.write_text(sc.render_card(kind=kind, key=key, fields=fields, keywords=keywords,
                                   aliases=aliases, secondary=secondary, refs=refs,
                                   scan_depth=scan_depth, priority=priority, enabled=enabled,
                                   inject=inject, schema=schema), encoding="utf-8")
    engine.invalidate_cache()
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "cards"
    v.mkdir()
    write_card(v, "people", "Alice", keywords=["Alice", "payments team"], aliases=["Al"],
               fields={"who": "Alice works on the payments team.",
                       "stance": "Friendly.", "recent": "Took over the refund rewrite."})
    write_card(v, "people", "Bob", keywords=["Bob"],
               fields={"who": "Bob is on call for releases."})
    write_card(v, "event", "the Friday deploy", keywords=["Friday deploy", "weekly release"],
               refs=["Bob"],
               fields={"when": "Every Friday.", "what": "The weekly release goes out.",
                       "followup": "Nothing decided yet."})
    return v


@pytest.fixture
def no_jieba(monkeypatch):
    """Force the fallback tokenizer, whether or not jieba is installed."""
    monkeypatch.setattr(engine, "_jieba_module", lambda: None)
