"""Matching engine and vault access.

What makes a card fire, in one paragraph: the match terms of a card are its
``keywords`` + ``aliases`` + ``title``. They are looked for in a *window* — the current
message plus the last ``scan_depth`` turns (two messages per turn) — so a card still
fires when the name was said two turns ago and the current message only says "him".
Terms of two characters or more may match as a substring; single-character terms must
match a whole token, which is what keeps a one-character keyword from firing on every
word that happens to contain it. A card that hits is then filtered by its ``secondary``
conditions, scored (window hits + an extra point per hit in the current message),
sorted by score, then ``priority``, then title, and finally packed into a character
budget: the first card always gets in, and the first card that would overflow stops the
list (the remainder is reported as ``truncated``).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import schema as sc

log = logging.getLogger("lorecards")

DEFAULT_BUDGET_CHARS = 800
DEFAULT_SCAN_DEPTH = sc.DEFAULT_SCAN_DEPTH
_MIN_SUBSTRING_LEN = 2
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿぀-ヿ]+")

_lock = threading.RLock()
_cache: dict[str, dict] = {}


@dataclass
class Card:
    """One parsed card. ``path`` is vault-relative so a client can open the file."""

    title: str
    path: str
    key: str
    kind: str
    keywords: list[str]
    body: str
    priority: float = 0.0
    aliases: list[str] = field(default_factory=list)
    secondary: dict = field(default_factory=lambda: {"all": [], "any": [], "not": []})
    scan_depth: int | None = None
    refs: list[str] = field(default_factory=list)
    inject: bool = True
    hit_terms: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "title": self.title, "path": self.path,
                "keywords": list(self.keywords), "aliases": list(self.aliases),
                "priority": self.priority, "refs": list(self.refs),
                "hit_terms": list(self.hit_terms), "body": self.body}


# --------------------------------------------------------------------------- tokenizing


def _jieba_module():
    """Return the ``jieba`` module, or ``None`` when it is not installed.

    Kept as a function so the no-jieba path is testable: patch this to return ``None``.
    """
    try:
        import jieba  # noqa: PLC0415
    except ImportError:
        return None
    jieba.setLogLevel(60)
    return jieba


def has_jieba() -> bool:
    return _jieba_module() is not None


def _norm(s: str) -> str:
    return s.strip().casefold()


def tokenize(text: str) -> set[str]:
    """Token set for a message.

    With ``jieba`` installed, Chinese is segmented properly. Without it we fall back to
    splitting on whitespace and punctuation; multi-character terms still match as
    substrings, so the fallback degrades in precision, not in function.
    """
    jieba = _jieba_module()
    if jieba is not None:
        return {_norm(t) for t in jieba.cut(text) if t.strip()}
    return {_norm(t) for t in _TOKEN_RE.findall(text) if t.strip()}


@dataclass
class _Msg:
    role: str          # "query" (this turn) | "user" | "assistant"
    low: str
    tokens: set[str]


def _prep(role: str, text: str) -> _Msg:
    return _Msg(role, _norm(text), tokenize(text))


# --------------------------------------------------------------------------- loading


def _parse_card(md_path: Path, vault_root: Path, schema: sc.CardSchema) -> Card | None:
    """Parse one ``.md`` file into a :class:`Card`, or ``None`` when it can never fire."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("skipping unreadable card %s: %r", md_path, e)
        return None

    fm, body, problem = sc.split_frontmatter(text)
    if problem == "bad_fm" or fm is None:
        log.warning("skipping card with unparseable frontmatter: %s", md_path)
        return None

    try:
        rel = str(md_path.relative_to(vault_root))
    except ValueError:
        rel = md_path.name
    meta = sc.parse_meta(fm, rel, md_path.stem, schema)
    if not meta["keywords"] or not meta["enabled"]:
        return None
    body = body.strip()
    if not body:
        return None

    return Card(title=meta["title"], path=rel, key=meta["key"], kind=meta["kind"],
                keywords=meta["keywords"], body=body, priority=meta["priority"],
                aliases=meta["aliases"], secondary=meta["secondary"],
                scan_depth=meta["scan_depth"], refs=meta["refs"], inject=meta["inject"])


def _fingerprint(root: Path) -> tuple:
    """``(path, mtime_ns, size)`` per file. Editing a card changes its own mtime but not
    necessarily the directory's, so the fingerprint has to be per file."""
    items = []
    for p in sorted(root.rglob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append((str(p), st.st_mtime_ns, st.st_size))
    return tuple(items)


def load_cards(vault_root: str | Path, schema: sc.CardSchema | None = None,
               include_non_injecting: bool = False) -> list[Card]:
    """Load every usable card in the vault, cached on the directory fingerprint."""
    root = Path(vault_root).expanduser()
    if not root.is_dir():
        return []
    schema = schema or sc.load_schema(root)
    key = str(root)
    fp = _fingerprint(root)
    with _lock:
        cached = _cache.get(key)
        if cached and cached["fp"] == fp:
            cards = cached["cards"]
            return cards if include_non_injecting else [c for c in cards if c.inject]
    cards = [c for p in sorted(root.rglob("*.md"))
             if (c := _parse_card(p, root, schema)) is not None]
    with _lock:
        _cache[key] = {"fp": fp, "cards": cards}
    return cards if include_non_injecting else [c for c in cards if c.inject]


def invalidate_cache() -> None:
    """Force a rescan on the next load (tests, and after a write)."""
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------- matching


def _term_in(term: str, m: _Msg, allow_short: bool) -> bool:
    if len(term) >= _MIN_SUBSTRING_LEN:
        return term in m.low or term in m.tokens
    return term in m.tokens or (allow_short and term in m.low)


def _terms(card: Card) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for kw in [*card.keywords, *card.aliases, card.title]:
        k = _norm(kw)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _window(card: Card, msgs: list[_Msg], default_depth: int) -> list[_Msg]:
    """This card's scan window: the last ``scan_depth`` turns (~2 messages each) plus the
    current message, which is always in."""
    depth = card.scan_depth if card.scan_depth is not None else default_depth
    hist = [m for m in msgs if m.role != "query"]
    take = hist[-(depth * 2):] if depth > 0 else []
    return [*take, *[m for m in msgs if m.role == "query"]]


def _score(card: Card, msgs: list[_Msg], default_depth: int,
           allow_short: bool) -> tuple[int, list[str]] | None:
    """``(score, hit terms)`` or ``None`` when the card does not fire."""
    win = _window(card, msgs, default_depth)
    if not win:
        return None
    hit_terms: list[str] = []
    hit_msgs: list[_Msg] = []
    for k in _terms(card):
        where = [m for m in win if _term_in(k, m, allow_short)]
        if where:
            hit_terms.append(k)
            hit_msgs.extend(m for m in where if m not in hit_msgs)
    if not hit_terms:
        return None

    sec = card.secondary or {}
    all_t = [_norm(x) for x in sec.get("all", []) if _norm(x)]
    any_t = [_norm(x) for x in sec.get("any", []) if _norm(x)]
    not_t = [_norm(x) for x in sec.get("not", []) if _norm(x)]
    if all_t and not all(any(_term_in(k, m, True) for m in win) for k in all_t):
        return None
    if any_t and not any(_term_in(k, m, True) for k in any_t for m in win):
        return None
    # "not" only looks at the messages that actually produced a hit: an excluded word
    # three turns back should not veto a card the user just named.
    if not_t and any(_term_in(k, m, True) for k in not_t for m in hit_msgs):
        return None

    query_msgs = [m for m in win if m.role == "query"]
    current = sum(1 for k in hit_terms if any(_term_in(k, m, allow_short) for m in query_msgs))
    return len(hit_terms) + current, hit_terms


def _with_refs(card: Card, by_key: dict[str, Card], hit_keys: set[str],
               schema: sc.CardSchema) -> Card:
    """One level of ``refs``: append a single "who is this" line per referenced card.
    A referenced card that fired on its own is skipped — it is already in full."""
    if not card.refs:
        return card
    lines: list[str] = []
    for r in card.refs:
        target = by_key.get(r)
        if target is None or target.key in hit_keys or target.path == card.path:
            continue
        line = sc.who_line(target.body, target.kind, schema)
        if line:
            lines.append(f"(see also: {target.key} — {line})")
    if not lines:
        return card
    return replace(card, body=card.body.rstrip() + "\n\n" + "\n".join(lines))


def _candidates(msgs: list[_Msg], texts: list[str], cards: list[Card]) -> dict:
    """Dry-run suggestions only: proper nouns that have no card yet, plus aliases that matched."""
    known = {t for c in cards for t in _terms(c)}

    def is_new(word: str) -> bool:
        low = _norm(word)
        return low not in known and not any(t in low for t in known if len(t) >= _MIN_SUBSTRING_LEN)

    names: list[str] = []
    places: list[str] = []
    jieba = _jieba_module()
    if jieba is not None:
        import jieba.posseg as pseg  # noqa: PLC0415
        for t in texts:
            for w, flag in pseg.cut(t):
                w = w.strip()
                if len(w) < 2:
                    continue
                if not is_new(w):
                    continue
                if flag.startswith("nr") and w not in names:
                    names.append(w)
                elif flag.startswith("ns") and w not in places:
                    places.append(w)
    for t in texts:
        for w in _LATIN_WORD_RE.findall(t):
            if len(w) > 2 and w[0].isupper() and w not in names and is_new(w):
                names.append(w)
    aliases: list[str] = []
    for c in cards:
        for a in [*c.aliases, *c.keywords]:
            k = _norm(a)
            if k and any(_term_in(k, m, False) for m in msgs) and a not in aliases:
                aliases.append(a)
    return {"names": names[:20], "places": places[:20], "aliases": aliases[:20]}


def consult(query: str, vault_root: str | Path, *, turns: list[dict] | None = None,
            budget_chars: int = DEFAULT_BUDGET_CHARS, dry_run: bool = False,
            scan_depth: int = DEFAULT_SCAN_DEPTH, allow_short_keywords: bool = False,
            schema: sc.CardSchema | None = None) -> dict:
    """Look up the cards that the conversation just triggered.

    ``turns`` is the recent history in chronological order, ``[{"role": "user"|"assistant",
    "text": ...}]``. Pass raw conversation text only — feeding a previous injection back in
    makes cards keep themselves alive. Returns ``{"hits": [Card], "injected": int,
    "truncated": int}``, plus ``"debug"`` when ``dry_run`` is set. Never writes anything.
    """
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    if not (query or "").strip():
        return {"hits": [], "injected": 0, "truncated": 0}

    cards = load_cards(root, schema)
    if not cards:
        return {"hits": [], "injected": 0, "truncated": 0}

    msgs: list[_Msg] = []
    texts: list[str] = []
    for t in (turns or []):
        if not isinstance(t, dict):
            continue
        txt, role = t.get("text"), t.get("role")
        if not isinstance(txt, str) or not txt.strip() or role not in ("user", "assistant"):
            continue
        msgs.append(_prep(role, txt))
        texts.append(txt)
    msgs.append(_prep("query", query))
    texts.append(query)

    scored: list[tuple[int, list[str], Card]] = []
    for c in cards:
        r = _score(c, msgs, scan_depth, allow_short_keywords)
        if r is not None:
            scored.append((r[0], r[1], c))
    scored.sort(key=lambda x: (-x[0], -x[2].priority, x[2].title))

    by_key = {c.key: c for c in cards}
    hit_keys = {c.key for _, _, c in scored}
    hits: list[Card] = []
    used, truncated = 0, 0
    debug_entries: list[dict] = []
    stopped = False
    for score, terms, c in scored:
        full = _with_refs(replace(c, hit_terms=list(terms)), by_key, hit_keys, schema)
        cost = len(full.title) + len(full.body)
        if not stopped and (not hits or used + cost <= budget_chars):
            hits.append(full)
            used += cost
            in_budget = True
        else:
            if not stopped:
                stopped = True
                truncated = len(scored) - len(hits)
            in_budget = False
        if dry_run:
            debug_entries.append({"path": c.path, "key": c.key, "kind": c.kind, "title": c.title,
                                  "hits": list(terms), "score": score, "chars": cost,
                                  "in_budget": in_budget})
    out: dict[str, Any] = {"hits": hits, "injected": used, "truncated": truncated}
    if dry_run:
        out["debug"] = {"cards": debug_entries, "candidates": _candidates(msgs, texts, cards),
                        "jieba": has_jieba()}
    return out


def render_context(result: dict, header: str = "Cards in play (from the user's card book):") -> str:
    """Format a :func:`consult` result as the text block to hand a model. Empty when nothing fired."""
    hits = result.get("hits") or []
    if not hits:
        return ""
    parts = [header]
    for c in hits:
        parts.append(f"\n### {c.title} [{c.kind}]\n{c.body.strip()}")
    if result.get("truncated"):
        parts.append(f"\n({result['truncated']} more card(s) matched but did not fit the budget.)")
    return "\n".join(parts).strip()


# --------------------------------------------------------------------------- vault writes


class CardError(Exception):
    """A card operation could not be carried out (bad key, missing card, write conflict)."""


def card_path(vault_root: str | Path, kind: str, key: str,
              schema: sc.CardSchema | None = None) -> Path:
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    safe = sc.safe_key(key)
    if safe is None:
        raise CardError(f"unsafe card key: {key!r}")
    if kind not in sc.KINDS:
        raise CardError(f"unknown kind: {kind!r} (expected one of {', '.join(sc.KINDS)})")
    return root / schema.rel_for(kind, safe)


def find_card(vault_root: str | Path, key: str,
              schema: sc.CardSchema | None = None) -> Path | None:
    """Locate a card file by key, whichever kind directory it lives in."""
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    safe = sc.safe_key(key)
    if safe is None:
        return None
    for kind in sc.KINDS:
        p = root / schema.rel_for(kind, safe)
        if p.is_file():
            return p
    hits = sorted(root.rglob(f"{safe}.md"))
    return hits[0] if hits else None


def write_card(vault_root: str | Path, *, kind: str, key: str, fields: dict[str, str],
               keywords: list[str] | None = None, aliases: list[str] | None = None,
               refs: list[str] | None = None, title: str | None = None,
               priority: float = 0, mode: str = "create",
               schema: sc.CardSchema | None = None) -> dict:
    """Create a card, or refresh only its "recent" section.

    ``mode="create"`` writes a whole card and refuses to clobber an existing one.
    ``mode="update_recent"`` reads the card, replaces its recent section (``recent`` for
    people/places/things, ``followup`` for events) and writes it back, re-checking the
    file's mtime first so a concurrent edit is not silently lost.
    """
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    path = card_path(root, kind, key, schema)
    safe = sc.safe_key(key) or key

    if mode == "create":
        if path.exists():
            raise CardError(f"card already exists: {path.relative_to(root)}")
        text = sc.render_card(kind=kind, key=safe, fields=fields, keywords=keywords or [],
                              title=title, aliases=aliases, refs=refs, priority=priority,
                              schema=schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        invalidate_cache()
        return {"ok": True, "path": str(path.relative_to(root)), "mode": mode, "created": True}

    if mode != "update_recent":
        raise CardError(f"unknown mode: {mode!r} (expected 'create' or 'update_recent')")

    found = find_card(root, safe, schema)
    if found is None:
        raise CardError(f"no such card: {key!r}")
    rel = str(found.relative_to(root))
    real_kind = schema.kind_of_rel(rel)
    section = schema.recent_for(real_kind)
    if section is None:
        raise CardError(f"kind {real_kind!r} has no section that may be updated this way")
    new_text = (fields.get(section) or fields.get("recent") or fields.get("text") or "").strip()
    if not new_text:
        raise CardError(f"nothing to write into the {section!r} section")
    if len(new_text) > sc.RECENT_MAX_CHARS:
        new_text = new_text[:sc.RECENT_MAX_CHARS].rstrip()

    before = found.stat().st_mtime_ns
    raw = found.read_text(encoding="utf-8")
    fm, body, problem = sc.split_frontmatter(raw)
    if problem == "bad_fm" or fm is None:
        raise CardError(f"card {rel} has unparseable frontmatter; refusing to rewrite it")
    secs = sc.split_sections(body, real_kind, schema)
    secs[section] = new_text
    meta = sc.parse_meta(fm, rel, found.stem, schema)
    text = sc.render_card(kind=real_kind, key=found.stem, fields=secs, keywords=meta["keywords"],
                          title=meta["title"], aliases=meta["aliases"], secondary=meta["secondary"],
                          refs=meta["refs"], scan_depth=meta["scan_depth"], priority=meta["priority"],
                          enabled=meta["enabled"], inject=meta["inject"], extra_fm=fm, schema=schema)
    if found.stat().st_mtime_ns != before:
        raise CardError(f"card {rel} changed on disk while it was being updated; nothing written")
    found.write_text(text, encoding="utf-8")
    invalidate_cache()
    return {"ok": True, "path": rel, "mode": mode, "section": section, "chars": len(new_text)}


def read_card(vault_root: str | Path, key: str, schema: sc.CardSchema | None = None) -> dict:
    """Full text of one card by key, frontmatter included."""
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    found = find_card(root, key, schema)
    if found is None:
        raise CardError(f"no such card: {key!r}")
    rel = str(found.relative_to(root))
    raw = found.read_text(encoding="utf-8")
    fm, body, problem = sc.split_frontmatter(raw)
    meta = sc.parse_meta(fm or {}, rel, found.stem, schema)
    return {"key": found.stem, "kind": meta["kind"], "title": meta["title"], "path": rel,
            "keywords": meta["keywords"], "aliases": meta["aliases"], "refs": meta["refs"],
            "problem": problem, "text": raw, "body": body.strip()}


def list_cards(vault_root: str | Path, kind: str | None = None,
               schema: sc.CardSchema | None = None) -> list[dict]:
    """Every loadable card as ``{key, kind, title, path, keywords, aliases, priority}``."""
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    cards = load_cards(root, schema, include_non_injecting=True)
    out = []
    for c in cards:
        if kind and c.kind != kind:
            continue
        out.append({"key": c.key, "kind": c.kind, "title": c.title, "path": c.path,
                    "keywords": list(c.keywords), "aliases": list(c.aliases),
                    "priority": c.priority, "inject": c.inject})
    return out


def check_vault(vault_root: str | Path, schema: sc.CardSchema | None = None) -> list[dict]:
    """Run :func:`lorecards.schema.checkup` over every ``.md`` file in the vault."""
    root = Path(vault_root).expanduser()
    schema = schema or sc.load_schema(root)
    out: list[dict] = []
    for p in sorted(root.rglob("*.md")):
        rel = str(p.relative_to(root))
        try:
            raw = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            out.append({"path": rel, "code": "unreadable", "detail": repr(e)})
            continue
        fm, body, problem = sc.split_frontmatter(raw)
        for issue in sc.checkup(rel, fm, body, problem, schema):
            out.append({"path": rel, **issue})
    return out
