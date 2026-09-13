"""Command line: try, list, read, check, init, import, export, hook, ui, gateway."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import engine, gateway, io as lore_io, schema as sc, web

DEFAULT_VAULT = "~/.lorecards"
ENV_VAULT = "LORECARDS_VAULT"
HOOK_HEADER = "Cards in play (from the user's card book):"


def resolve_vault(explicit: str | None = None) -> Path:
    """``--vault`` beats ``$LORECARDS_VAULT`` beats ``~/.lorecards``."""
    return Path(explicit or os.environ.get(ENV_VAULT) or DEFAULT_VAULT).expanduser()


# --------------------------------------------------------------------------- init


EXAMPLES: dict[str, tuple[str, dict[str, str], list[str]]] = {
    "people": ("Alice", {
        "who": "Alice — works on the payments team, sits two desks away.",
        "stance": "Friendly. She reviews my pull requests and I review hers.",
        "recent": "Took over the refund flow rewrite in September.",
        "impression": "Direct, allergic to meetings that could have been a message.",
    }, ["Alice", "payments team"]),
    "event": ("the Friday deploy", {
        "when": "Every Friday afternoon, since the team moved to weekly releases.",
        "who": "Alice runs it, Bob is on call.",
        "what": "The weekly release goes out behind a feature flag, then the flag is flipped Monday.",
        "stance": "I think Friday is a bad day for this and keep saying so.",
        "followup": "Nothing decided yet.",
    }, ["Friday deploy", "weekly release"]),
    "place": ("the corner cafe", {
        "where": "Two blocks from the office, the one with the green awning.",
        "relation": "Where I go when I need to think instead of type.",
        "recent": "They changed the beans in August and I have opinions.",
    }, ["corner cafe", "the cafe"]),
    "thing": ("the blue notebook", {
        "what": "A small blue notebook I carry for design sketches.",
        "usage": "Anything that is not yet worth a ticket goes in there first.",
        "recent": "Almost full; the next one is already bought.",
    }, ["blue notebook", "the notebook"]),
    "slang": ("ship it Friday", {
        "meaning": "Said sarcastically when someone suggests a risky change late in the week.",
        "origin": "From the Friday deploy argument that never quite ends.",
        "usage": "Only ever used as a joke. Nobody actually wants to ship it Friday.",
    }, ["ship it Friday"]),
}


def cmd_init(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    schema = sc.load_schema(vault)
    vault.mkdir(parents=True, exist_ok=True)
    made, kept = [], []
    for kind, (key, fields, keywords) in EXAMPLES.items():
        safe = sc.safe_key(key) or kind
        dest = vault / schema.rel_for(kind, safe)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            kept.append(str(dest.relative_to(vault)))
            continue
        refs = ["Alice"] if kind == "event" else None
        dest.write_text(sc.render_card(kind=kind, key=safe, fields=fields, keywords=keywords,
                                       title=key, refs=refs, schema=schema), encoding="utf-8")
        made.append(str(dest.relative_to(vault)))
    engine.invalidate_cache()
    print(f"vault: {vault}")
    for m in made:
        print(f"  created  {m}")
    for k in kept:
        print(f"  kept     {k}")
    print("\nTry it:  lorecards try \"how did the friday deploy go\" --vault " + str(vault))
    return 0


# --------------------------------------------------------------------------- try / list / check


def _turns_from_arg(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def cmd_try(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    result = engine.consult(args.text, vault, turns=_turns_from_arg(args.turns),
                            budget_chars=args.budget, dry_run=True, scan_depth=args.scan_depth)
    if args.json:
        payload = {"hits": [c.as_dict() for c in result["hits"]],
                   "injected": result["injected"], "truncated": result["truncated"],
                   "debug": result.get("debug", {})}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    debug = result.get("debug", {})
    cards = debug.get("cards", [])
    if not cards:
        print("no cards matched.")
    for c in cards:
        mark = "+" if c["in_budget"] else "-"
        print(f"{mark} {c['title']}  [{c['kind']}]  score={c['score']}  chars={c['chars']}  "
              f"hits={', '.join(c['hits'])}")
    cand = debug.get("candidates", {})
    extra = [w for w in (cand.get("names", []) + cand.get("places", []))]
    if extra:
        print("\nno card yet for: " + ", ".join(extra[:10]))
    if not debug.get("jieba", True):
        print("\n(jieba is not installed: Chinese text falls back to substring matching)")
    if args.show_context and result["hits"]:
        print("\n--- context that would be injected ---")
        print(engine.render_context(result, HOOK_HEADER))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    rows = engine.list_cards(vault, args.kind)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"no cards in {vault}")
        return 0
    width = max(len(r["key"]) for r in rows)
    for r in rows:
        flag = "" if r["inject"] else "  (inject: false)"
        print(f"{r['key']:<{width}}  [{r['kind']}]  {', '.join(r['keywords'])}{flag}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    issues = engine.check_vault(vault)
    if args.json:
        print(json.dumps(issues, ensure_ascii=False, indent=2))
        return 1 if issues else 0
    if not issues:
        print(f"{vault}: all cards look fine.")
        return 0
    for i in issues:
        print(f"{i['path']}: {i['code']}: {i['detail']}")
    print(f"\n{len(issues)} issue(s).")
    return 1


def cmd_read(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    try:
        card = engine.read_card(vault, args.key)
    except engine.CardError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(card["text"])
    return 0


# --------------------------------------------------------------------------- import / export


def cmd_import(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    vault.mkdir(parents=True, exist_ok=True)
    result = lore_io.import_book(args.path, vault, kind=args.kind, force=args.force)
    print(f"imported {len(result['written'])} card(s) into {vault}")
    if result["skipped"]:
        print(f"kept {len(result['skipped'])} existing card(s) (use --force to overwrite): "
              + ", ".join(result["skipped"][:10]))
    if result["no_keywords"]:
        print(f"skipped {len(result['no_keywords'])} entry/entries with no keywords and no title")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    book = lore_io.export_book(vault, args.path, kind=args.kind)
    print(f"exported {len(book['entries'])} entry/entries to {args.path}")
    return 0


# --------------------------------------------------------------------------- hook


def _recent_turns_from_transcript(path: str | None, depth: int) -> list[dict]:
    """Best-effort read of a Claude Code transcript (JSONL) for the last few turns."""
    if not path:
        return []
    p = Path(path).expanduser()
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        return []
    turns: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
        role = (msg or {}).get("role") or rec.get("type")
        if role not in ("user", "assistant"):
            continue
        content = (msg or {}).get("content", rec.get("content"))
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            turns.append({"role": role, "text": text})
    return turns[-(max(depth, 1) * 2):]


def cmd_ui(args: argparse.Namespace) -> int:
    """Serve the web editor. Blocks until interrupted."""
    web.run_ui(resolve_vault(args.vault), host=args.host, port=args.port, token=args.token)
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    """Run the injecting proxy. Blocks until interrupted."""
    gateway.run_gateway(resolve_vault(args.vault), args.upstream, host=args.host, port=args.port,
                        inject=args.inject, window=args.window,
                        reinject_after=args.reinject_after, budget_chars=args.budget,
                        framing=args.framing, token=args.token, log_hits=not args.no_log)
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    """Claude Code ``UserPromptSubmit`` hook (contract as documented on 2026-09-13).

    Reads the hook JSON on stdin, matches the prompt (plus the last few turns of the
    transcript) against the vault, and answers with ``hookSpecificOutput.additionalContext``.
    Any failure exits 0 with no output: a card book must never block a prompt.
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        payload = {}
    prompt = payload.get("user_prompt") or payload.get("prompt") or ""
    if not str(prompt).strip():
        return 0
    vault = resolve_vault(args.vault)
    try:
        turns = _recent_turns_from_transcript(payload.get("transcript_path"), args.scan_depth)
        result = engine.consult(prompt, vault, turns=turns, budget_chars=args.budget,
                                scan_depth=args.scan_depth)
        hits = result["hits"]
        if hits and args.reinject_after > 0:
            # one Claude Code session is one conversation: a card that surfaced two turns
            # ago is still in the context window, so do not pay for it again
            conversation = str(payload.get("session_id") or "hook")
            turn = gateway.next_turn(vault, conversation)
            allowed = gateway.pick_new(vault, conversation, [c.key for c in hits], turn,
                                       args.reinject_after)
            hits = [c for c in hits if c.key in allowed]
            gateway.record(vault, conversation, [c.key for c in hits], turn)
            result = dict(result, hits=hits)
        context = engine.render_context(result, HOOK_HEADER)
        if hits and not args.no_log:
            engine.log_hits(vault, "hook", hits, text=str(prompt))
    except Exception as e:  # never break the user's prompt over a card
        print(f"lorecards hook: {e}", file=sys.stderr)
        return 0
    if not context:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": context}},
                     ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    vault_help = f"card directory (default: ${ENV_VAULT} or {DEFAULT_VAULT})"
    # --vault is accepted on either side of the subcommand; SUPPRESS keeps an absent
    # subcommand-level flag from overwriting the one given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS, help=vault_help)

    p = argparse.ArgumentParser(prog="lorecards", description="A keyword-triggered card book.")
    p.add_argument("--vault", help=vault_help)
    sub = p.add_subparsers(dest="command", required=True, parser_class=lambda **kw:
                           argparse.ArgumentParser(parents=[common], **kw))

    t = sub.add_parser("try", help="see which cards a sentence would fire")
    t.add_argument("text")
    t.add_argument("--turns", help="recent history as JSON: [{\"role\":\"user\",\"text\":\"...\"}]")
    t.add_argument("--budget", type=int, default=engine.DEFAULT_BUDGET_CHARS)
    t.add_argument("--scan-depth", dest="scan_depth", type=int, default=engine.DEFAULT_SCAN_DEPTH)
    t.add_argument("--show-context", action="store_true", help="print the text that would be injected")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_try)

    ls = sub.add_parser("list", help="list every card")
    ls.add_argument("--kind", choices=sc.KINDS)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    rd = sub.add_parser("read", help="print one card")
    rd.add_argument("key")
    rd.set_defaults(func=cmd_read)

    ck = sub.add_parser("check", help="report cards that can never fire, or fire too often")
    ck.add_argument("--json", action="store_true")
    ck.set_defaults(func=cmd_check)

    it = sub.add_parser("init", help="create the vault directories and one example card per kind")
    it.add_argument("vault_positional", nargs="?", metavar="VAULT")
    it.set_defaults(func=cmd_init)

    im = sub.add_parser("import", help="import a SillyTavern lorebook JSON")
    im.add_argument("path")
    im.add_argument("--kind", choices=sc.KINDS, default="entry")
    im.add_argument("--force", action="store_true", help="overwrite cards of the same name")
    im.set_defaults(func=cmd_import)

    ex = sub.add_parser("export", help="export the vault as a SillyTavern lorebook JSON")
    ex.add_argument("path")
    ex.add_argument("--kind", choices=sc.KINDS)
    ex.set_defaults(func=cmd_export)

    hk = sub.add_parser("hook", help="Claude Code UserPromptSubmit hook (reads hook JSON on stdin)")
    hk.add_argument("--budget", type=int, default=engine.DEFAULT_BUDGET_CHARS)
    hk.add_argument("--scan-depth", dest="scan_depth", type=int, default=engine.DEFAULT_SCAN_DEPTH)
    hk.add_argument("--reinject-after", dest="reinject_after", type=int,
                    default=gateway.DEFAULT_REINJECT_AFTER,
                    help="turns before a card may be injected again in one session "
                         "(0 = inject every time; default: %(default)s)")
    hk.add_argument("--no-log", action="store_true",
                    help="do not record what surfaced in the vault's hit ledger")
    hk.set_defaults(func=cmd_hook)

    ui = sub.add_parser("ui", help="serve the local web editor (JSON API + one page)")
    ui.add_argument("--host", default="127.0.0.1",
                    help="interface to bind (default: %(default)s; anything else needs --token)")
    ui.add_argument("--port", type=int, default=web.DEFAULT_PORT)
    ui.add_argument("--token", help="require Authorization: Bearer <token> on /api/*")
    ui.set_defaults(func=cmd_ui)

    gw = sub.add_parser("gateway", help="proxy an LLM API and inject cards into the prompt")
    gw.add_argument("--upstream", required=True,
                    help="the API this sits in front of, e.g. https://api.openai.com")
    gw.add_argument("--host", default="127.0.0.1")
    gw.add_argument("--port", type=int, default=gateway.DEFAULT_PORT)
    gw.add_argument("--inject", choices=["user", "system"], default="user",
                    help="where the cards go (default: %(default)s)")
    gw.add_argument("--window", type=int, default=gateway.DEFAULT_WINDOW,
                    help="turns of history to scan (default: %(default)s)")
    gw.add_argument("--reinject-after", dest="reinject_after", type=int,
                    default=gateway.DEFAULT_REINJECT_AFTER,
                    help="turns before the same card may be injected again "
                         "(0 = every time; default: %(default)s)")
    gw.add_argument("--budget", type=int, default=engine.DEFAULT_BUDGET_CHARS)
    gw.add_argument("--framing", default=gateway.DEFAULT_FRAMING,
                    help="the line that introduces the cards (default: %(default)r)")
    gw.add_argument("--token", help=f"require {gateway.TOKEN_HEADER}: <token> on every "
                                    "request (mandatory unless --host is localhost)")
    gw.add_argument("--no-log", action="store_true",
                    help="do not record what surfaced in the vault's hit ledger")
    gw.set_defaults(func=cmd_gateway)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "vault"):
        args.vault = None
    # `lorecards init ~/cards` puts the vault where a positional argument is natural.
    if getattr(args, "vault_positional", None):
        args.vault = args.vault_positional
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
