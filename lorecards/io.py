"""Import and export SillyTavern lorebooks (World Info JSON).

Field mapping, both directions::

    key            <-> keywords
    keysecondary   <-> secondary.{any,all,not}   (paired with selectiveLogic)
    selectiveLogic <-> which of the three lists is used
                       0 AND ANY -> any     1 AND ALL -> all
                       2 NOT ANY -> not     3 NOT ALL -> not  (approximated, see README)
    comment        <-> title
    content        <-> card body
    order          <-> priority
    scanDepth      <-> scan_depth   (SillyTavern counts messages, a card counts turns)
    disable        <-> enabled      (inverted)
    constant           not supported; kept under `st:` so export returns it unchanged
    everything else    kept verbatim under `st:` in the frontmatter

Import never overwrites an existing card unless ``force`` is set.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import engine
from . import schema as sc

LOGIC_AND_ANY = 0
LOGIC_AND_ALL = 1
LOGIC_NOT_ANY = 2
LOGIC_NOT_ALL = 3

#: Keys we translate; anything else in a SillyTavern entry is preserved but not interpreted.
_MAPPED = {"key", "keys", "keysecondary", "secondary_keys", "selectiveLogic", "comment",
           "content", "order", "scanDepth", "disable", "enabled"}


def _as_entries(book: Any) -> list[tuple[str, dict]]:
    """SillyTavern writes ``entries`` as an object keyed by uid; some tools emit a list."""
    if isinstance(book, dict):
        raw = book.get("entries", book)
    else:
        raw = book
    if isinstance(raw, dict):
        return [(str(k), v) for k, v in raw.items() if isinstance(v, dict)]
    if isinstance(raw, list):
        return [(str(v.get("uid", i)), v) for i, v in enumerate(raw) if isinstance(v, dict)]
    return []


def _str_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return sc.parse_str_list(raw)
    if isinstance(raw, list):
        return sc.parse_str_list(raw)
    return []


def _key_from(entry: dict, uid: str, keywords: list[str]) -> str:
    for candidate in (entry.get("comment"), keywords[0] if keywords else None, f"entry-{uid}"):
        if not candidate:
            continue
        safe = sc.safe_key(str(candidate).strip().replace("/", "-").replace("\\", "-"))
        if safe:
            return safe
    return f"entry-{uid}"


def _messages_to_turns(value: Any) -> int | None:
    """SillyTavern scans N *messages*; a card scans N *turns* of roughly two messages."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return 0
    return min(math.ceil(n / 2), sc.MAX_SCAN_DEPTH)


def entry_to_card(entry: dict, uid: str, kind: str = "entry",
                  schema: sc.CardSchema = sc.DEFAULT_SCHEMA) -> tuple[str, str]:
    """One SillyTavern entry -> ``(card key, card file text)``."""
    keywords = _str_list(entry.get("key") or entry.get("keys"))
    secondary_keys = _str_list(entry.get("keysecondary") or entry.get("secondary_keys"))
    logic = entry.get("selectiveLogic", LOGIC_AND_ANY)
    logic = logic if isinstance(logic, int) and not isinstance(logic, bool) else LOGIC_AND_ANY
    secondary = {"all": [], "any": [], "not": []}
    if secondary_keys:
        bucket = {LOGIC_AND_ANY: "any", LOGIC_AND_ALL: "all",
                  LOGIC_NOT_ANY: "not", LOGIC_NOT_ALL: "not"}.get(logic, "any")
        secondary[bucket] = secondary_keys

    title = str(entry.get("comment") or "").strip()
    key = _key_from(entry, uid, keywords)
    content = str(entry.get("content") or "").strip()
    try:
        priority = float(entry.get("order") or 0)
    except (TypeError, ValueError):
        priority = 0.0
    enabled = not bool(entry.get("disable", False))
    scan_depth = _messages_to_turns(entry.get("scanDepth"))
    if scan_depth is None:
        scan_depth = _messages_to_turns(entry.get("depth"))

    preserved = {k: v for k, v in entry.items() if k not in _MAPPED}
    if not keywords:
        keywords = [title] if title else []
    fields = {"_head": content}
    text = sc.render_card(kind=kind, key=key, fields=fields, keywords=keywords,
                          title=title or key, secondary=secondary, scan_depth=scan_depth,
                          priority=priority, enabled=enabled,
                          extra_fm={"st": preserved} if preserved else None, schema=schema)
    return key, text


def import_book(path: str | Path, vault_root: str | Path, *, kind: str = "entry",
                force: bool = False, schema: sc.CardSchema | None = None) -> dict:
    """Import a lorebook JSON file into the vault. Existing cards are kept unless ``force``."""
    book = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return import_book_data(book, vault_root, kind=kind, force=force, schema=schema)


def import_book_data(book: Any, vault_root: str | Path, *, kind: str = "entry",
                     force: bool = False, schema: sc.CardSchema | None = None) -> dict:
    """Import an already-parsed lorebook (what an upload hands you) into the vault."""
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    written, skipped, empty = [], [], []
    for uid, entry in _as_entries(book):
        # A card with neither keywords nor a title could never fire; report it instead.
        if not _str_list(entry.get("key") or entry.get("keys")) \
                and not str(entry.get("comment") or "").strip():
            empty.append(uid)
            continue
        key, text = entry_to_card(entry, uid, kind, schema)
        dest = root / schema.rel_for(kind, key)
        if dest.exists() and not force:
            skipped.append(key)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        written.append(key)
    engine.invalidate_cache()
    return {"written": written, "skipped": skipped, "no_keywords": empty,
            "vault": str(root), "kind": kind}


def card_to_entry(card_text: str, rel: str, stem: str,
                  schema: sc.CardSchema = sc.DEFAULT_SCHEMA) -> dict:
    """One card file -> a SillyTavern entry dict."""
    fm, body, problem = sc.split_frontmatter(card_text)
    fm = fm or {}
    meta = sc.parse_meta(fm, rel, stem, schema)
    preserved = fm.get("st") if isinstance(fm.get("st"), dict) else {}

    sec = meta["secondary"]
    if sec["not"]:
        logic, keysecondary = LOGIC_NOT_ANY, sec["not"]
    elif sec["all"]:
        logic, keysecondary = LOGIC_AND_ALL, sec["all"]
    else:
        logic, keysecondary = LOGIC_AND_ANY, sec["any"]

    entry: dict[str, Any] = {
        "key": list(meta["keywords"]) + list(meta["aliases"]),
        "keysecondary": keysecondary,
        "comment": meta["title"],
        "content": body.strip(),
        "constant": False,
        "selective": bool(keysecondary),
        "selectiveLogic": logic,
        "order": int(meta["priority"]) if float(meta["priority"]).is_integer() else meta["priority"],
        "disable": not meta["enabled"],
    }
    if meta["scan_depth"] is not None:
        entry["scanDepth"] = meta["scan_depth"] * 2
    entry.update(preserved)
    return entry


def export_book(vault_root: str | Path, path: str | Path | None = None, *,
                kind: str | None = None, schema: sc.CardSchema | None = None) -> dict:
    """Export the vault as a SillyTavern lorebook. Writes the JSON when ``path`` is given."""
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    entries: dict[str, dict] = {}
    for i, p in enumerate(engine.iter_card_files(root)):
        rel = str(p.relative_to(root))
        if kind and schema.kind_of_rel(rel) != kind:
            continue
        try:
            raw = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        entry = card_to_entry(raw, rel, p.stem, schema)
        if not entry["key"]:
            continue
        entry.setdefault("uid", i)
        entries[str(i)] = entry
    book = {"entries": entries}
    if path is not None:
        Path(path).expanduser().write_text(
            json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return book
