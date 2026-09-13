from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from lorecards import engine, gateway

try:
    import httpx
except ImportError:  # pragma: no cover
    import httpx2 as httpx


# --------------------------------------------------------------------------- fake upstream


@pytest.fixture
def upstream():
    """A stand-in API that records what it was sent and can answer with a stream."""
    seen = {"bodies": [], "headers": [], "paths": []}

    async def record(request: Request):
        seen["paths"].append(request.url.path)
        seen["headers"].append(dict(request.headers))
        raw = await request.body()
        try:
            seen["bodies"].append(json.loads(raw))
        except json.JSONDecodeError:
            seen["bodies"].append(raw.decode("utf-8", "replace"))
        return raw

    def _parse(body):
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {}

    async def chat(request: Request):
        body = await record(request)
        payload = _parse(body)
        if payload.get("stream"):
            async def events():
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b"data: [DONE]\n\n"
            return StreamingResponse(events(), media_type="text/event-stream")
        return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

    async def messages(request: Request):
        body = await record(request)
        payload = _parse(body)
        if payload.get("stream"):
            async def events():
                yield b'event: content_block_delta\ndata: {"delta":{"text":"hi"}}\n\n'
                yield b"event: message_stop\ndata: {}\n\n"
            return StreamingResponse(events(), media_type="text/event-stream")
        return JSONResponse({"content": [{"type": "text", "text": "hi"}]})

    async def models(request: Request):
        await record(request)
        return JSONResponse({"data": [{"id": "gpt-test"}]})

    async def boom(request: Request):
        await record(request)
        code = int(request.url.path.rsplit("/", 1)[-1])
        return Response(json.dumps({"error": {"message": "upstream says no"}}),
                        status_code=code, media_type="application/json")

    app = Starlette(routes=[
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/boom/{code}", boom, methods=["POST", "GET"]),
    ])
    app.state.seen = seen
    return app


@pytest.fixture
def gw(vault, upstream):
    """The gateway, wired to the fake upstream in-process."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream),
                               base_url="http://upstream")

    def make(**kwargs):
        kwargs.setdefault("allowed_hosts", {"testserver"})
        app = gateway.build_gateway(vault, "http://upstream", client=client, **kwargs)
        c = TestClient(app)
        c.seen = upstream.state.seen
        return c

    make.seen = upstream.state.seen
    yield make


def openai_body(*texts, stream=False, system=None):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    for i, t in enumerate(texts):
        messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": t})
    body = {"model": "gpt-test", "messages": messages}
    if stream:
        body["stream"] = True
    return body


def anthropic_body(*texts, stream=False, system=None):
    messages = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": [{"type": "text", "text": t}]} for i, t in enumerate(texts)]
    body = {"model": "claude-test", "messages": messages, "max_tokens": 64}
    if system is not None:
        body["system"] = system
    if stream:
        body["stream"] = True
    return body


def sent(client):
    return client.seen["bodies"][-1]


# --------------------------------------------------------------------------- happy paths


def test_openai_non_streaming_injects_into_the_last_user_message(gw):
    c = gw()
    res = c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"))
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "hi"
    assert res.headers[gateway.INJECTED_HEADER] == "Alice"

    body = sent(c)
    content = body["messages"][-1]["content"]
    assert content.startswith(gateway.MARK_OPEN)
    assert gateway.DEFAULT_FRAMING.strip() in content
    assert "payments team" in content
    assert content.rstrip().endswith("what is Alice up to?")
    assert body["model"] == "gpt-test"          # the rest of the body is untouched


def test_openai_streaming_passes_the_stream_through(gw):
    c = gw()
    with c.stream("POST", "/v1/chat/completions",
                  json=openai_body("what is Alice up to?", stream=True)) as res:
        assert res.status_code == 200
        assert res.headers[gateway.INJECTED_HEADER] == "Alice"
        chunks = b"".join(res.iter_bytes())
    assert b"[DONE]" in chunks and b'"delta"' in chunks
    assert "payments team" in sent(c)["messages"][-1]["content"]


def test_anthropic_non_streaming_injects_a_text_block(gw):
    c = gw()
    res = c.post("/v1/messages", json=anthropic_body("what is Alice up to?"))
    assert res.status_code == 200 and res.headers[gateway.INJECTED_HEADER] == "Alice"
    blocks = sent(c)["messages"][-1]["content"]
    assert blocks[0]["type"] == "text" and gateway.MARK_OPEN in blocks[0]["text"]
    assert blocks[1]["text"] == "what is Alice up to?"


def test_anthropic_streaming(gw):
    c = gw()
    with c.stream("POST", "/v1/messages",
                  json=anthropic_body("what is Alice up to?", stream=True)) as res:
        assert res.status_code == 200
        chunks = b"".join(res.iter_bytes())
    assert b"content_block_delta" in chunks and b"message_stop" in chunks


def test_nothing_matched_means_nothing_changed(gw):
    c = gw()
    body = openai_body("the weather is fine")
    res = c.post("/v1/chat/completions", json=body)
    assert gateway.INJECTED_HEADER not in res.headers
    assert sent(c) == body


def test_history_is_scanned_not_just_the_last_message(gw):
    c = gw()
    res = c.post("/v1/chat/completions",
                 json=openai_body("Alice reviewed my PR", "nice", "did she approve it?"))
    assert res.headers[gateway.INJECTED_HEADER] == "Alice"


def test_window_limits_how_far_back_it_looks(gw):
    c = gw(window=1)
    res = c.post("/v1/chat/completions",
                 json=openai_body("Alice reviewed my PR", "nice", "then lunch", "ok",
                                  "did she approve it?"))
    assert gateway.INJECTED_HEADER not in res.headers


# --------------------------------------------------------------------------- injection site


def test_inject_system_openai_appends_a_system_message(gw):
    c = gw(inject="system")
    res = c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"))
    assert res.headers[gateway.INJECTED_HEADER] == "Alice"
    body = sent(c)
    assert body["messages"][-1]["role"] == "system"
    assert gateway.MARK_OPEN in body["messages"][-1]["content"]
    assert body["messages"][0]["content"] == "what is Alice up to?"   # user left alone


def test_inject_system_anthropic_extends_the_system_field(gw):
    c = gw(inject="system")
    c.post("/v1/messages", json=anthropic_body("what is Alice up to?", system="You are helpful."))
    body = sent(c)
    assert body["system"].startswith("You are helpful.")
    assert gateway.MARK_OPEN in body["system"]

    c.post("/v1/messages", json=anthropic_body("what is Alice up to?"),
           headers={gateway.CONVERSATION_HEADER: "second"})
    assert gateway.MARK_OPEN in sent(c)["system"]                     # absent system is fine

    c.post("/v1/messages", json=anthropic_body("what is Alice up to?",
                                               system=[{"type": "text", "text": "base"}]),
           headers={gateway.CONVERSATION_HEADER: "third"})
    system = sent(c)["system"]
    assert system[0]["text"] == "base" and gateway.MARK_OPEN in system[-1]["text"]


def test_custom_framing(gw):
    c = gw(framing="From your notes:\n\n")
    c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"))
    assert "From your notes:" in sent(c)["messages"][-1]["content"]


# --------------------------------------------------------------------------- dedupe


def test_the_same_card_is_not_injected_twice_in_a_row(gw):
    c = gw()
    head = {gateway.CONVERSATION_HEADER: "conv-1"}
    first = c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"), headers=head)
    assert first.headers[gateway.INJECTED_HEADER] == "Alice"
    second = c.post("/v1/chat/completions",
                    json=openai_body("Alice again", "sure", "and Alice once more"), headers=head)
    assert gateway.INJECTED_HEADER not in second.headers
    assert gateway.MARK_OPEN not in json.dumps(sent(c))


def test_a_card_returns_after_the_reinject_window(gw):
    c = gw(reinject_after=2)
    head = {gateway.CONVERSATION_HEADER: "conv-2"}
    turns = ["what is Alice up to?"]
    c.post("/v1/chat/completions", json=openai_body(*turns), headers=head)
    turns += ["she is fine", "and Alice?"]                     # turn 2
    assert gateway.INJECTED_HEADER not in c.post(
        "/v1/chat/completions", json=openai_body(*turns), headers=head).headers
    turns += ["mm", "Alice once more"]                         # turn 3, two turns later
    assert c.post("/v1/chat/completions", json=openai_body(*turns),
                  headers=head).headers[gateway.INJECTED_HEADER] == "Alice"


def test_reinject_after_zero_injects_every_turn(gw):
    c = gw(reinject_after=0)
    head = {gateway.CONVERSATION_HEADER: "conv-eager"}
    for _ in range(3):
        res = c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"), headers=head)
        assert res.headers[gateway.INJECTED_HEADER] == "Alice"


def test_a_different_conversation_gets_its_own_injection(gw):
    c = gw()
    body = openai_body("what is Alice up to?")
    assert c.post("/v1/chat/completions", json=body,
                  headers={gateway.CONVERSATION_HEADER: "a"}).headers[gateway.INJECTED_HEADER]
    assert c.post("/v1/chat/completions", json=body,
                  headers={gateway.CONVERSATION_HEADER: "b"}).headers[gateway.INJECTED_HEADER]


def test_without_a_header_the_conversation_is_fingerprinted(gw):
    c = gw()
    body = openai_body("what is Alice up to?", system="You are helpful.")
    assert c.post("/v1/chat/completions", json=body).headers[gateway.INJECTED_HEADER] == "Alice"
    # same opening = same conversation = not injected again
    assert gateway.INJECTED_HEADER not in c.post("/v1/chat/completions", json=body).headers
    # a different opening is a different conversation
    other = openai_body("what is Alice up to?", system="You are a pirate.")
    assert c.post("/v1/chat/completions", json=other).headers[gateway.INJECTED_HEADER] == "Alice"


def test_our_own_injection_is_invisible_to_the_next_turn(gw, vault):
    """The injected block quotes the card, keywords and all. If it were scanned, every card
    would keep itself alive forever. The user's own words still count, only ours do not."""
    c = gw(reinject_after=0)
    head = {gateway.CONVERSATION_HEADER: "conv-strip"}
    c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"), headers=head)
    injected = sent(c)["messages"][-1]["content"]
    block = injected.split(gateway.MARK_CLOSE)[0] + gateway.MARK_CLOSE
    assert "payments team" in block                      # the card is quoted in full

    # next turn: the history carries our block, the user has moved on
    res = c.post("/v1/chat/completions",
                 json=openai_body(block, "sure", "what about the weather"), headers=head)
    assert gateway.INJECTED_HEADER not in res.headers
    # the only marked block in the forwarded body is the one the client sent us
    assert json.dumps(sent(c)).count(gateway.MARK_OPEN) == 1
    assert sent(c)["messages"][-1]["content"] == "what about the weather"

    query, turns, turn = gateway.conversation_turns(
        [{"role": "user", "content": block},
         {"role": "assistant", "content": "sure"},
         {"role": "user", "content": "what about the weather"}], 3)
    assert query == "what about the weather"
    assert "payments team" not in json.dumps(turns) and "lorecards" not in json.dumps(turns)
    assert turn == 2


def test_ledger_is_bounded(vault, monkeypatch):
    monkeypatch.setattr(gateway, "MAX_CONVERSATIONS", 5)
    monkeypatch.setattr(gateway, "MAX_CARDS_PER_CONVERSATION", 3)
    for i in range(20):
        gateway.record(vault, f"conv-{i}", [f"card-{j}" for j in range(10)], turn=i)
    data = gateway.load_ledger(vault)
    assert len(data["conversations"]) == 5
    assert all(len(c["cards"]) == 3 for c in data["conversations"].values())


def test_a_corrupt_ledger_does_not_stop_injection(gw, vault):
    gateway.ledger_path(vault).parent.mkdir(parents=True, exist_ok=True)
    gateway.ledger_path(vault).write_text("{not json", encoding="utf-8")
    c = gw()
    assert c.post("/v1/chat/completions",
                  json=openai_body("what is Alice up to?")).headers[gateway.INJECTED_HEADER]


def test_recent_records_the_gateway_as_a_source(gw, vault):
    c = gw()
    c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"))
    hits = engine.read_hits(vault)
    assert hits[0]["source"] == "gateway" and hits[0]["keys"] == ["Alice"]


# --------------------------------------------------------------------------- passthrough


def test_other_paths_are_proxied_untouched(gw):
    c = gw()
    res = c.get("/v1/models")
    assert res.status_code == 200 and res.json()["data"][0]["id"] == "gpt-test"


def test_upstream_errors_are_passed_through(gw):
    c = gw()
    for code in (400, 401, 429, 500):
        res = c.post(f"/v1/boom/{code}", json={"anything": True})
        assert res.status_code == code
        assert res.json()["error"]["message"] == "upstream says no"


def test_credentials_reach_the_upstream_unchanged(gw):
    c = gw()
    c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"),
           headers={"Authorization": "Bearer sk-test", "x-api-key": "anth-test",
                    "anthropic-version": "2023-06-01"})
    headers = c.seen["headers"][-1]
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["x-api-key"] == "anth-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "host" not in {k for k in headers if k == "host" and headers[k] == "testserver"}


def test_a_body_that_is_not_json_is_forwarded_as_is(gw):
    c = gw()
    res = c.post("/v1/chat/completions", content=b"not json at all")
    assert res.status_code == 200 and c.seen["bodies"][-1] == "not json at all"


def test_an_engine_failure_still_forwards_the_request(gw, monkeypatch):
    monkeypatch.setattr(engine, "consult", _boom)
    c = gw()
    res = c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"))
    assert res.status_code == 200 and gateway.INJECTED_HEADER not in res.headers
    assert sent(c)["messages"][-1]["content"] == "what is Alice up to?"


def _boom(*a, **k):
    raise RuntimeError("the vault is on fire")


def test_an_unreachable_upstream_answers_502(vault):
    app = gateway.build_gateway(vault, "http://127.0.0.1:9",     # discard port
                                allowed_hosts={"testserver"})
    with TestClient(app) as c:
        res = c.post("/v1/chat/completions", json=openai_body("hello"))
    assert res.status_code == 502 and "lorecards" in res.json()["error"]["message"]


# --------------------------------------------------------------------------- pieces


def test_text_of_ignores_tool_and_image_blocks():
    content = [{"type": "text", "text": "hello"},
               {"type": "image", "source": {"data": "…"}},
               {"type": "tool_use", "name": "search", "input": {}},
               {"type": "text", "text": gateway.wrap("injected earlier")}]
    assert gateway.text_of(content) == "hello"
    assert gateway.text_of("plain") == "plain"
    assert gateway.text_of(None) == ""


def test_strip_marks_removes_a_whole_block():
    text = gateway.wrap("### Alice\nsome card") + "\n\nwhat is up?"
    assert gateway.strip_marks(text).strip() == "what is up?"


# --------------------------------------------------------------------------- guards


def test_a_token_is_required_when_one_is_configured(gw):
    c = gw(token="s3cret")
    body = openai_body("what is Alice up to?")
    assert c.post("/v1/chat/completions", json=body).status_code == 401
    assert c.post("/v1/chat/completions", json=body,
                  headers={gateway.TOKEN_HEADER: "wrong"}).status_code == 401
    ok = c.post("/v1/chat/completions", json=body, headers={gateway.TOKEN_HEADER: "s3cret"})
    assert ok.status_code == 200 and ok.headers[gateway.INJECTED_HEADER] == "Alice"
    # passthrough paths are guarded too
    assert c.get("/v1/models").status_code == 401


def test_our_token_never_reaches_the_upstream(gw):
    c = gw(token="s3cret")
    c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"),
           headers={gateway.TOKEN_HEADER: "s3cret", "Authorization": "Bearer sk-real"})
    headers = c.seen["headers"][-1]
    assert headers["authorization"] == "Bearer sk-real"     # theirs goes through
    assert gateway.TOKEN_HEADER not in headers              # ours does not


def test_serving_beyond_localhost_without_a_token_is_refused(vault):
    with pytest.raises(SystemExit) as e:
        gateway.run_gateway(vault, "https://api.openai.com", host="0.0.0.0", port=8765)
    assert "--token" in str(e.value)


def test_an_unexpected_host_header_is_refused(gw):
    c = gw(allowed_hosts={"127.0.0.1:8765"})
    assert c.post("/v1/chat/completions", json=openai_body("hi"),
                  headers={"Host": "attacker.example"}).status_code == 403
    assert c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"),
                  headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_upstream_must_be_http(vault):
    for bad in ("file:///etc/passwd", "ftp://host", "api.openai.com", ""):
        with pytest.raises(ValueError):
            gateway.build_gateway(vault, bad)


def test_paths_that_climb_out_of_the_upstream_are_refused(gw):
    """An encoded `..` is not normalised by the client, so it arrives intact and would
    otherwise be pasted onto the upstream base and walk out of its path prefix."""
    c = gw()
    for path in ("/v1/..%2F..%2Fadmin", "/v1/%2e%2e/admin", "/%2e%2e/%2e%2e/admin"):
        assert c.get(path).status_code == 400, path
    assert c.get("/v1/models").status_code == 200


def test_percent_encoding_in_a_path_survives(vault):
    """`raw_path` keeps the client's encoding, so `%2F` inside a segment is forwarded as
    written instead of turning into a path separator."""
    class _Req:
        def __init__(self, raw, path):
            self.scope = {"raw_path": raw}
            self.url = type("u", (), {"path": path, "query": ""})()

    assert gateway.upstream_path(_Req(b"/v1/models%2Fx", "/v1/models/x")) == "/v1/models%2Fx"
    assert gateway.upstream_path(_Req(b"/v1/chat/completions", "/v1/chat/completions")) \
        == "/v1/chat/completions"
    assert gateway.upstream_path(_Req(b"/v1/%2e%2e/admin", "/v1/../admin")) is None
    # no raw_path (some servers omit it): fall back to the parsed path
    assert gateway.upstream_path(_Req(None, "/v1/models")) == "/v1/models"


def test_the_injected_header_is_only_claimed_on_success(gw):
    c = gw()
    res = c.post("/v1/boom/429", json=openai_body("what is Alice up to?"))
    assert res.status_code == 429 and gateway.INJECTED_HEADER not in res.headers


def test_date_and_server_headers_are_not_duplicated(gw):
    c = gw()
    res = c.post("/v1/chat/completions", json=openai_body("hi"))
    assert len(res.headers.get_list("date")) <= 1
    assert len(res.headers.get_list("server")) <= 1


# --------------------------------------------------------------------------- ledger


def test_a_turn_index_that_went_backwards_is_treated_as_a_reset(gw, vault):
    """A client that trims its history sends fewer user messages, so the turn count drops.
    Without this, a card injected at turn 40 would never be injected again."""
    conv = "conv-rewind"
    gateway.record(vault, conv, ["Alice"], turn=40)
    assert gateway.pick_new(vault, conv, ["Alice"], turn=41, reinject_after=6) == []
    assert gateway.pick_new(vault, conv, ["Alice"], turn=2, reinject_after=6) == ["Alice"]

    c = gw()
    head = {gateway.CONVERSATION_HEADER: conv}
    res = c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"), headers=head)
    assert res.headers[gateway.INJECTED_HEADER] == "Alice"
    # the clock is rewritten, not merely ignored
    assert gateway.load_ledger(vault)["conversations"][conv]["cards"]["Alice"] == 1


def test_concurrent_writers_do_not_lose_each_other(vault):
    """Two gateways sharing a vault, each doing read-modify-write on the same ledger."""
    import subprocess
    import sys
    script = (
        "import sys;from lorecards import gateway;"
        "v=sys.argv[1];i=int(sys.argv[2]);"
        "[gateway.record(v, f'conv-{i}-{n}', ['Alice'], turn=n) for n in range(25)]"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(vault), str(i)])
             for i in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    convs = gateway.load_ledger(vault)["conversations"]
    assert len(convs) == 100, f"a writer's updates were lost: {len(convs)} of 100"
    assert not list(gateway.ledger_path(vault).parent.glob("*.tmp"))


def test_the_ledger_temp_file_name_is_unique(vault, monkeypatch):
    written = []
    real = pathlib_write = None

    def fake_replace(src, dst):
        written.append(str(src))

    monkeypatch.setattr(gateway.os, "replace", fake_replace)
    gateway.save_ledger(vault, {"conversations": {}})
    gateway.save_ledger(vault, {"conversations": {}})
    assert len(set(written)) == 2 and all(".tmp" in w for w in written)
    for leftover in gateway.ledger_path(vault).parent.glob("*.tmp"):
        leftover.unlink()


# --------------------------------------------------------------------------- hit ledger


def test_the_hit_ledger_records_the_triggering_message(gw, vault):
    c = gw()
    c.post("/v1/chat/completions", json=openai_body("what is Alice up to these days?"))
    entry = engine.read_hits(vault)[0]
    assert entry["source"] == "gateway" and entry["keys"] == ["Alice"]
    assert entry["text"] == "what is Alice up to these days?"     # documented, and capped
    assert set(entry) == {"ts", "source", "keys", "hits", "text"}


def test_no_log_keeps_the_hit_ledger_empty_but_still_dedupes(gw, vault):
    c = gw(log_hits=False)
    res = c.post("/v1/chat/completions", json=openai_body("what is Alice up to?"),
                 headers={gateway.CONVERSATION_HEADER: "quiet"})
    assert res.headers[gateway.INJECTED_HEADER] == "Alice"
    assert engine.read_hits(vault) == []                          # nothing written
    assert gateway.load_ledger(vault)["conversations"]["quiet"]["cards"] == {"Alice": 1}
    assert gateway.INJECTED_HEADER not in c.post(
        "/v1/chat/completions", json=openai_body("what is Alice up to?"),
        headers={gateway.CONVERSATION_HEADER: "quiet"}).headers


def test_a_forged_marker_does_not_swallow_the_users_words(gw):
    """The markers carry a per-process nonce, so text a user pasted that merely looks like
    ours is left alone — and so is a block written by a different lorecards process."""
    forged = "<!-- lorecards:deadbe -->\nnot ours\n<!-- /lorecards:deadbe -->"
    assert gateway.strip_marks(forged + "\n\nwhat is Alice up to?") == \
        forged + "\n\nwhat is Alice up to?"
    c = gw()
    res = c.post("/v1/chat/completions",
                 json=openai_body(f"{forged}\n\nwhat is Alice up to?"))
    assert res.headers[gateway.INJECTED_HEADER] == "Alice"
    assert "not ours" in sent(c)["messages"][-1]["content"]
