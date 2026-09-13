"""Card format: the single source of truth for frontmatter and body sections.

A card is one Markdown file with YAML frontmatter, stored under a vault directory.
The *directory* decides the kind; a ``kind:`` key in the frontmatter is only a copy
and is never trusted::

    people/     people   a person the user knows
    events/     event    something that happened
    places/     place    a place
    things/     thing    an object, a habit, a regular haunt
    slang/      slang    an in-joke, a private word
    <root>      entry    a free-form entry (no fixed sections)

Frontmatter keys::

    keywords: [Alice, deploy buddy]        # required, the card never fires without them
    aliases: [Al, A.]                      # merged into the match terms, same weight
    secondary: {all: [], any: [], not: []} # extra conditions, see engine.py
    scan_depth: 3                          # how many past turns this card looks back over
    refs: [Bob]                            # pull one line out of another card when this fires
    priority: 1                            # tie-break, higher first
    enabled: true                          # false = never loaded
    inject: true                           # false = readable by tools, never auto-injected
    title: Alice                           # defaults to the file name

Directory names and section names are configurable per vault through ``kinds.yaml``,
so a vault written in another language can keep its own words on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KINDS: tuple[str, ...] = ("people", "event", "place", "thing", "slang", "entry")
#: Kinds that ``write_card`` may create. ``entry`` is included: it is the free-form one.
CARD_KINDS: tuple[str, ...] = KINDS

DEFAULT_KIND_DIRS: dict[str, str] = {
    "people": "people",
    "event": "events",
    "place": "places",
    "thing": "things",
    "slang": "slang",
}

DEFAULT_SECTIONS: dict[str, list[str]] = {
    "people": ["who", "stance", "recent", "impression"],
    "event": ["when", "who", "what", "stance", "followup"],
    "place": ["where", "relation", "recent"],
    "thing": ["what", "usage", "recent"],
    "slang": ["meaning", "origin", "usage"],
    "entry": [],
}

#: The one section an agent is allowed to overwrite with ``mode="update_recent"``.
DEFAULT_RECENT_SECTION: dict[str, str | None] = {
    "people": "recent",
    "event": "followup",
    "place": "recent",
    "thing": "recent",
    "slang": None,
    "entry": None,
}

#: The section a ``refs:`` pull quotes from, per kind (falls back to the first non-empty one).
DEFAULT_WHO_SECTION: dict[str, str | None] = {
    "people": "who",
    "event": "what",
    "place": "where",
    "thing": "what",
    "slang": "meaning",
    "entry": None,
}

#: Sections kept on the card but never injected or matched on: private working notes.
DEFAULT_PRIVATE_SECTIONS: tuple[str, ...] = ("correspondence",)

RECENT_MAX_CHARS = 600
WHO_LINE_MAX_CHARS = 120
DEFAULT_SCAN_DEPTH = 3
MAX_SCAN_DEPTH = 10
CONFIG_FILENAME = "kinds.yaml"

#: Words so common that a card keyed on them fires on almost every turn. ``check`` reports
#: them; the engine does not block them. Extend per vault via ``kinds.yaml: generic_keywords``.
GENERIC_KEYWORDS = frozenset({
    "i", "you", "he", "she", "it", "we", "they", "the", "a", "an", "and", "or",
    "ok", "okay", "yes", "no", "hi", "hey", "thanks", "please", "this", "that",
})

_SECTION_RE = re.compile(r"^## (.+?)\s*$", re.M)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPLIT_RE = re.compile(r"[,，、]")

MANAGED_FM = ("kind", "title", "keywords", "aliases", "secondary", "scan_depth",
              "refs", "priority", "enabled", "inject")


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class CardSchema:
    """Directory and section names for one vault. Immutable; build it with :func:`load_schema`."""

    kind_dirs: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KIND_DIRS))
    sections: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_SECTIONS.items()})
    recent_section: dict[str, str | None] = field(default_factory=lambda: dict(DEFAULT_RECENT_SECTION))
    who_section: dict[str, str | None] = field(default_factory=lambda: dict(DEFAULT_WHO_SECTION))
    generic_keywords: frozenset[str] = GENERIC_KEYWORDS
    private_sections: tuple[str, ...] = DEFAULT_PRIVATE_SECTIONS

    @property
    def dir_to_kind(self) -> dict[str, str]:
        return {v: k for k, v in self.kind_dirs.items()}

    def kind_of_rel(self, rel: str) -> str:
        """Vault-relative path -> kind. The top directory wins; the vault root is ``entry``."""
        rel = (rel or "").replace("\\", "/").strip("/")
        top = rel.split("/", 1)[0] if "/" in rel else ""
        return self.dir_to_kind.get(top, "entry")

    def rel_for(self, kind: str, key: str) -> str:
        """kind + key -> vault-relative path. ``entry`` lands in the vault root."""
        directory = self.kind_dirs.get(kind)
        return f"{directory}/{key}.md" if directory else f"{key}.md"

    def sections_for(self, kind: str) -> list[str]:
        """The sections of this kind that are injected when the card fires."""
        return list(self.sections.get(kind, []))

    def all_sections_for(self, kind: str) -> list[str]:
        """Injected sections plus the private ones, which are edited but never injected."""
        names = self.sections_for(kind)
        return names + [n for n in self.private_sections if n not in names] if names else names

    def recent_for(self, kind: str) -> str | None:
        name = self.recent_section.get(kind)
        return name if name in self.sections_for(kind) else None


DEFAULT_SCHEMA = CardSchema()


def load_schema(vault_root: str | Path) -> CardSchema:
    """Read ``<vault>/kinds.yaml`` if present; fall back to the English defaults.

    ``kinds.yaml`` (every block optional)::

        dirs:            {people: personnages}
        sections:        {people: [qui, relation, recent]}
        recent_section:  {people: recent}
        who_section:     {people: qui}
        generic_keywords: [le, la, tu]
    """
    path = Path(vault_root) / CONFIG_FILENAME
    if not path.is_file():
        return DEFAULT_SCHEMA
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return DEFAULT_SCHEMA
    if not isinstance(raw, dict):
        return DEFAULT_SCHEMA

    dirs = dict(DEFAULT_KIND_DIRS)
    for kind, name in (raw.get("dirs") or {}).items() if isinstance(raw.get("dirs"), dict) else []:
        if kind in dirs and isinstance(name, str) and name.strip():
            dirs[kind] = name.strip().strip("/")

    sections = {k: list(v) for k, v in DEFAULT_SECTIONS.items()}
    for kind, names in (raw.get("sections") or {}).items() if isinstance(raw.get("sections"), dict) else []:
        if kind in sections and isinstance(names, list):
            cleaned = [str(n).strip() for n in names if str(n).strip()]
            if cleaned or kind == "entry":
                sections[kind] = cleaned

    recent = dict(DEFAULT_RECENT_SECTION)
    for kind, name in (raw.get("recent_section") or {}).items() if isinstance(raw.get("recent_section"), dict) else []:
        if kind in recent:
            recent[kind] = str(name).strip() or None if name is not None else None

    who = dict(DEFAULT_WHO_SECTION)
    for kind, name in (raw.get("who_section") or {}).items() if isinstance(raw.get("who_section"), dict) else []:
        if kind in who:
            who[kind] = str(name).strip() or None if name is not None else None

    private = DEFAULT_PRIVATE_SECTIONS
    if isinstance(raw.get("private_sections"), list):
        private = tuple(str(n).strip() for n in raw["private_sections"] if str(n).strip())

    generic = GENERIC_KEYWORDS
    extra = raw.get("generic_keywords")
    if isinstance(extra, list):
        generic = frozenset(GENERIC_KEYWORDS | {str(w).strip().casefold() for w in extra if str(w).strip()})

    return CardSchema(kind_dirs=dirs, sections=sections, recent_section=recent,
                      who_section=who, generic_keywords=generic, private_sections=private)


# --------------------------------------------------------------------------- keys


def safe_key(key: str) -> str | None:
    """Validate a card key (the file name without ``.md``). Returns the clean key or ``None``."""
    k = (key or "").strip()
    if k.endswith(".md"):
        k = k[:-3].strip()
    if (not k or k in (".", "..") or ".." in k or "/" in k or "\\" in k
            or k.startswith(".") or _CTRL_RE.search(k)):
        return None
    return k


# --------------------------------------------------------------------------- frontmatter


def split_frontmatter(text: str) -> tuple[dict | None, str, str | None]:
    """``text`` -> ``(frontmatter, body, problem)``.

    ``problem`` is ``None`` | ``"no_fm"`` | ``"bad_fm"``. On ``no_fm`` the whole text is the
    body and the frontmatter is ``{}`` (such a card has no keywords, so it never fires).
    """
    if not text.startswith("---"):
        return {}, text, "no_fm"
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text, "no_fm"
    try:
        loaded = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None, parts[2], "bad_fm"
    if not isinstance(loaded, dict):
        return None, parts[2], "bad_fm"
    return loaded, parts[2], None


def parse_str_list(raw: Any) -> list[str]:
    """Forgiving list parse for ``keywords`` / ``aliases`` / ``refs``.

    A bare string is split on ``,``/``，``/``、`` after stripping ``[]``; numbers and booleans
    count as one word; nested containers are dropped; quotes are stripped; order-preserving dedupe.
    """
    if isinstance(raw, str):
        raw = _SPLIT_RE.split(raw.strip().strip("[]"))
    elif isinstance(raw, (int, float)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for k in raw:
        if not isinstance(k, (str, int, float)):
            continue
        s = str(k).strip().strip("\"'").strip()
        if s and s not in out:
            out.append(s)
    return out


def parse_secondary(raw: Any) -> dict[str, list[str]]:
    """``secondary`` -> ``{all, any, not}``. Missing or malformed = three empty lists."""
    out: dict[str, list[str]] = {"all": [], "any": [], "not": []}
    if isinstance(raw, dict):
        for k in out:
            out[k] = parse_str_list(raw.get(k))
    return out


def parse_scan_depth(raw: Any) -> int | None:
    """Missing or malformed -> ``None`` (use the engine default); otherwise clamped to 0..MAX."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(v, MAX_SCAN_DEPTH))


def parse_meta(fm: dict, rel: str, stem: str, schema: CardSchema = DEFAULT_SCHEMA) -> dict:
    """Frontmatter -> metadata dict (no body). ``kind`` comes from the path, never from ``fm``."""
    try:
        priority = float(fm.get("priority", 0) or 0)
    except (TypeError, ValueError):
        priority = 0.0
    return {
        "kind": schema.kind_of_rel(rel),
        "key": stem,
        "title": str(fm.get("title") or stem).strip(),
        "keywords": parse_str_list(fm.get("keywords")),
        "aliases": parse_str_list(fm.get("aliases")),
        "secondary": parse_secondary(fm.get("secondary")),
        "scan_depth": parse_scan_depth(fm.get("scan_depth")),
        "refs": parse_str_list(fm.get("refs")),
        "priority": priority,
        "enabled": fm.get("enabled", True) is not False,
        "inject": fm.get("inject", True) is not False,
    }


# --------------------------------------------------------------------------- body


def split_sections(body: str, kind: str, schema: CardSchema = DEFAULT_SCHEMA) -> dict[str, str]:
    """Split ``## section`` headings into ``{name: text}``.

    Every section of the kind is present (empty string when absent). Headings outside the
    table are kept verbatim in ``_extra`` so nothing is ever lost. Text before the first
    heading goes to ``_head``. ``entry`` cards are not split: everything is ``_head``.
    """
    names = schema.all_sections_for(kind)
    out: dict[str, str] = {n: "" for n in names}
    if not names:
        out["_head"] = body.strip()
        out["_extra"] = ""
        return out
    matches = list(_SECTION_RE.finditer(body))
    out["_head"] = (body[: matches[0].start()] if matches else body).strip()
    extra: list[str] = []
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[m.end():end].strip()
        if name in names and not out[name]:
            out[name] = content
        else:
            extra.append(f"## {name}\n{content}".rstrip())
    out["_extra"] = "\n\n".join(extra)
    return out


def render_sections(kind: str, fields: dict[str, str], schema: CardSchema = DEFAULT_SCHEMA) -> str:
    """``{name: text}`` -> body text. Section order is the table's; empty sections keep their
    heading so there is an obvious place to write. ``_head`` leads, ``_extra`` trails."""
    names = schema.all_sections_for(kind)
    parts: list[str] = []
    head = (fields.get("_head") or "").strip()
    if head:
        parts.append(head)
    for n in names:
        text = (fields.get(n) or "").strip()
        if not text and n in schema.private_sections and n not in schema.sections_for(kind):
            continue   # do not stamp an empty private heading onto every card
        parts.append(f"## {n}\n{text}".rstrip())
    extra = (fields.get("_extra") or "").strip()
    if extra:
        parts.append(extra)
    return "\n\n".join(parts).strip() + "\n"


def strip_private(body: str, schema: CardSchema = DEFAULT_SCHEMA) -> str:
    """Remove every private section from a body. What is left is what may be injected.

    Works on any card, sectioned or free-form: a ``## correspondence`` heading in a
    free-form entry is cut just the same, down to the next heading or the end.
    """
    if not schema.private_sections:
        return body
    private = {n.strip().casefold() for n in schema.private_sections}
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return body
    keep: list[tuple[int, int]] = []
    cut_from = None
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        if m.group(1).strip().casefold() in private:
            if cut_from is None:
                cut_from = m.start()
            continue
        if cut_from is not None:
            keep.append((cut_from, m.start()))
            cut_from = None
    if cut_from is not None:
        keep.append((cut_from, len(body)))
    for start, end in reversed(keep):
        body = body[:start] + body[end:]
    return body


def render_card(*, kind: str, key: str, fields: dict[str, str], keywords: list[str],
                title: str | None = None, aliases: list[str] | None = None,
                secondary: dict | None = None, refs: list[str] | None = None,
                scan_depth: int | None = None, priority: float = 0, enabled: bool = True,
                inject: bool = True, extra_fm: dict | None = None,
                schema: CardSchema = DEFAULT_SCHEMA) -> str:
    """Assemble a whole card file: frontmatter + body. Empty optional keys are left out."""
    fm: dict[str, Any] = {"kind": kind}
    t = (title or key).strip()
    if t:
        fm["title"] = t
    fm["keywords"] = parse_str_list(keywords)
    al = parse_str_list(aliases)
    if al:
        fm["aliases"] = al
    sec = parse_secondary(secondary)
    if any(sec.values()):
        fm["secondary"] = {k: v for k, v in sec.items() if v}
    if scan_depth is not None:
        fm["scan_depth"] = int(scan_depth)
    rf = parse_str_list(refs)
    if rf:
        fm["refs"] = rf
    if priority:
        fm["priority"] = int(priority) if float(priority).is_integer() else priority
    if not enabled:
        fm["enabled"] = False
    if not inject:
        fm["inject"] = False
    for k, v in (extra_fm or {}).items():
        if k not in MANAGED_FM:
            fm[k] = v
    head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=None, width=1000)
    return f"---\n{head}---\n{render_sections(kind, fields, schema)}"


def who_line(body: str, kind: str, schema: CardSchema = DEFAULT_SCHEMA) -> str:
    """The one line a ``refs:`` pull quotes: first line of the "who is this" section."""
    secs = split_sections(body, kind, schema)
    preferred = schema.who_section.get(kind)
    text = (secs.get(preferred) if preferred else "") or secs.get("_head") or ""
    if not text:
        for n in schema.sections_for(kind):
            if secs.get(n):
                text = secs[n]
                break
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return first[:WHO_LINE_MAX_CHARS]


def checkup(rel: str, fm: dict | None, body: str, problem: str | None,
            schema: CardSchema = DEFAULT_SCHEMA) -> list[dict]:
    """Read-only health check of one card. Returns ``[{code, detail}, ...]``."""
    out: list[dict] = []
    stem = rel.rsplit("/", 1)[-1][:-3] if rel.endswith(".md") else rel
    if problem == "no_fm":
        return [{"code": "no_fm", "detail": "no frontmatter: this card can never fire"}]
    if problem == "bad_fm" or fm is None:
        return [{"code": "bad_fm", "detail": "frontmatter failed to parse"}]
    meta = parse_meta(fm, rel, stem, schema)
    if not meta["keywords"]:
        out.append({"code": "no_kw", "detail": "no keywords"})
    generic = [k for k in meta["keywords"] + meta["aliases"]
               if k.casefold() in schema.generic_keywords or len(k) == 1]
    if generic:
        out.append({"code": "generic_kw",
                    "detail": "generic or single-character keywords fire on almost every turn: "
                              + ", ".join(generic)})
    if fm.get("title") and str(fm["title"]).strip() != stem:
        out.append({"code": "title_mismatch",
                    "detail": f"title {fm['title']!r} differs from file name {stem!r}"})
    if not body.strip():
        out.append({"code": "no_body", "detail": "empty body"})
    return out
