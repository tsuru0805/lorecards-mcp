from __future__ import annotations

import json

import pytest

from lorecards import cli, engine


def run(args, capsys):
    code = cli.main(args)
    return code, capsys.readouterr().out


def test_init_then_try(tmp_path, capsys):
    vault = tmp_path / "cards"
    code, out = run(["init", str(vault)], capsys)
    assert code == 0 and "created" in out
    assert (vault / "people" / "Alice.md").is_file()
    assert (vault / "events" / "the Friday deploy.md").is_file()

    code, out = run(["--vault", str(vault), "try", "how did the friday deploy go"], capsys)
    assert code == 0 and "the Friday deploy" in out

    # init is idempotent
    code, out = run(["init", str(vault)], capsys)
    assert code == 0 and "kept" in out


def test_init_examples_pass_check(tmp_path, capsys):
    vault = tmp_path / "cards"
    run(["init", str(vault)], capsys)
    code, out = run(["--vault", str(vault), "check"], capsys)
    assert code == 0 and "all cards look fine" in out


def test_try_json_and_show_context(vault, capsys):
    code, out = run(["--vault", str(vault), "try", "Alice", "--json"], capsys)
    payload = json.loads(out)
    assert payload["hits"][0]["key"] == "Alice"

    code, out = run(["--vault", str(vault), "try", "Alice", "--show-context"], capsys)
    assert "context that would be injected" in out and "### Alice" in out

    code, out = run(["--vault", str(vault), "try", "nothing here"], capsys)
    assert "no cards matched" in out


def test_try_with_turns(vault, capsys):
    turns = json.dumps([{"role": "user", "text": "Alice reviewed my PR"}])
    code, out = run(["--vault", str(vault), "try", "did she approve", "--turns", turns], capsys)
    assert "Alice" in out
    code, out = run(["--vault", str(vault), "try", "did she approve"], capsys)
    assert "no cards matched" in out


def test_list_and_read(vault, capsys):
    code, out = run(["--vault", str(vault), "list"], capsys)
    assert "Alice" in out and "[people]" in out
    code, out = run(["--vault", str(vault), "list", "--kind", "event", "--json"], capsys)
    assert [r["key"] for r in json.loads(out)] == ["the Friday deploy"]
    code, out = run(["--vault", str(vault), "read", "Alice"], capsys)
    assert out.startswith("---") and "payments team" in out
    assert run(["--vault", str(vault), "read", "nobody"], capsys)[0] == 1


def test_check_reports_a_broken_card(vault, capsys):
    (vault / "bare.md").write_text("just text\n", encoding="utf-8")
    code, out = run(["--vault", str(vault), "check"], capsys)
    assert code == 1 and "no_fm" in out


def test_import_export_round_trip(vault, tmp_path, capsys):
    book = tmp_path / "book.json"
    book.write_text(json.dumps({"entries": {"0": {"uid": 0, "key": ["dragon"],
                                                  "comment": "dragon", "content": "big lizard"}}}),
                    encoding="utf-8")
    code, out = run(["--vault", str(vault), "import", str(book)], capsys)
    assert code == 0 and "imported 1 card" in out
    assert (vault / "dragon.md").is_file()

    code, out = run(["--vault", str(vault), "import", str(book)], capsys)
    assert "kept 1 existing card" in out

    dest = tmp_path / "out.json"
    code, out = run(["--vault", str(vault), "export", str(dest)], capsys)
    assert code == 0 and "exported 4" in out
    assert any(e["comment"] == "dragon"
               for e in json.loads(dest.read_text(encoding="utf-8"))["entries"].values())


def test_vault_falls_back_to_the_environment(vault, monkeypatch, capsys):
    monkeypatch.setenv(cli.ENV_VAULT, str(vault))
    code, out = run(["list"], capsys)
    assert "Alice" in out
    assert cli.resolve_vault(None) == vault


# --------------------------------------------------------------------------- hook


def _hook(payload, vault, capsys, monkeypatch, extra=()):
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(payload)))
    code = cli.main(["--vault", str(vault), "hook", *extra])
    return code, capsys.readouterr().out


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_hook_emits_additional_context(vault, capsys, monkeypatch):
    code, out = _hook({"hook_event_name": "UserPromptSubmit",
                       "user_prompt": "what is Alice up to"}, vault, capsys, monkeypatch)
    assert code == 0
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "### Alice [people]" in payload["hookSpecificOutput"]["additionalContext"]


def test_hook_is_silent_when_nothing_matches(vault, capsys, monkeypatch):
    code, out = _hook({"user_prompt": "unrelated chatter"}, vault, capsys, monkeypatch)
    assert code == 0 and out == ""
    code, out = _hook({"user_prompt": "   "}, vault, capsys, monkeypatch)
    assert code == 0 and out == ""


def test_hook_accepts_the_older_prompt_field(vault, capsys, monkeypatch):
    code, out = _hook({"prompt": "what is Alice up to"}, vault, capsys, monkeypatch)
    assert "Alice" in out


def test_hook_reads_recent_turns_from_the_transcript(vault, tmp_path, capsys, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user",
                                                "content": "Alice reviewed my PR"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                     "content": [{"type": "text", "text": "nice"}]}}),
        "not json at all",
    ]), encoding="utf-8")
    code, out = _hook({"user_prompt": "did she approve it",
                       "transcript_path": str(transcript)}, vault, capsys, monkeypatch)
    assert "Alice" in out
    code, out = _hook({"user_prompt": "did she approve it"}, vault, capsys, monkeypatch)
    assert out == ""


def test_hook_never_blocks_on_a_broken_vault(tmp_path, capsys, monkeypatch):
    code, out = _hook({"user_prompt": "anything"}, tmp_path / "missing", capsys, monkeypatch)
    assert code == 0 and out == ""
    monkeypatch.setattr("sys.stdin", _Stdin("this is not json"))
    assert cli.main(["--vault", str(tmp_path), "hook"]) == 0
    monkeypatch.setattr(engine, "consult", _boom)
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"user_prompt": "Alice"})))
    assert cli.main(["--vault", str(tmp_path), "hook"]) == 0


def _boom(*a, **k):
    raise RuntimeError("vault on fire")


def test_unknown_command_exits(capsys):
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])


def test_vault_is_accepted_on_either_side_of_the_subcommand(vault, capsys):
    code, before = run(["--vault", str(vault), "list"], capsys)
    code2, after = run(["list", "--vault", str(vault)], capsys)
    assert code == code2 == 0 and before == after and "Alice" in after


def test_hook_dedupes_within_one_claude_code_session(vault, capsys, monkeypatch):
    payload = {"user_prompt": "what is Alice up to", "session_id": "session-a"}
    assert "Alice" in _hook(payload, vault, capsys, monkeypatch)[1]
    # same session, next turn: the card is still in the context window upstream
    assert _hook(payload, vault, capsys, monkeypatch)[1] == ""
    # a different session starts fresh
    other = dict(payload, session_id="session-b")
    assert "Alice" in _hook(other, vault, capsys, monkeypatch)[1]
    # and --reinject-after 0 turns dedupe off
    assert "Alice" in _hook(payload, vault, capsys, monkeypatch,
                            extra=["--reinject-after", "0"])[1]


def test_hook_dedupe_expires(vault, capsys, monkeypatch):
    payload = {"user_prompt": "what is Alice up to", "session_id": "session-c"}
    assert "Alice" in _hook(payload, vault, capsys, monkeypatch, extra=["--reinject-after", "2"])[1]
    assert _hook(payload, vault, capsys, monkeypatch, extra=["--reinject-after", "2"])[1] == ""
    assert "Alice" in _hook(payload, vault, capsys, monkeypatch, extra=["--reinject-after", "2"])[1]


def test_a_bad_token_or_upstream_is_a_clean_error_not_a_traceback(vault, capsys):
    assert cli.main(["--vault", str(vault), "ui", "--token", "密码"]) == 2
    assert "ASCII" in capsys.readouterr().err
    assert cli.main(["--vault", str(vault), "gateway", "--upstream", "ftp://nope"]) == 2
    assert "http(s)" in capsys.readouterr().err
