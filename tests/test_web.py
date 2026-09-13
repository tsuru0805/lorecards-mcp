from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from lorecards import engine, web
from tests.conftest import write_card


def ui_client(vault, **kwargs):
    """A client shaped like the page itself: right Host, and the write guard header."""
    app = web.build_app(vault, allowed_hosts={"testserver"}, **kwargs)
    c = TestClient(app, headers={web.GUARD_HEADER: "1"})
    c.vault = vault
    return c


@pytest.fixture
def client(vault):
    with ui_client(vault) as c:
        yield c


# --------------------------------------------------------------------------- read


def test_kinds_describes_the_actual_schema(client):
    data = client.get("/api/kinds").json()
    people = next(k for k in data["kinds"] if k["kind"] == "people")
    assert people["sections"] == ["who", "stance", "recent", "impression"]
    assert people["dir"] == "people" and people["recent_section"] == "recent"
    assert data["vault"].endswith("cards") and isinstance(data["jieba"], bool)


def test_kinds_follows_a_custom_kinds_yaml(vault):
    (vault / "kinds.yaml").write_text("dirs: {people: personnes}\nsections: {people: [qui]}\n",
                                      encoding="utf-8")
    with ui_client(vault) as c:
        people = next(k for k in c.get("/api/kinds").json()["kinds"] if k["kind"] == "people")
    assert people["dir"] == "personnes" and people["sections"] == ["qui"]


def test_cards_list_and_filter(client):
    rows = client.get("/api/cards").json()["cards"]
    assert {r["key"] for r in rows} == {"Alice", "Bob", "the Friday deploy"}
    assert all("preview" in r and "mtime" in r for r in rows)
    only = client.get("/api/cards?kind=event").json()["cards"]
    assert [r["key"] for r in only] == ["the Friday deploy"]


def test_disabled_cards_stay_visible_and_editable(client, vault):
    write_card(vault, "entry", "off", keywords=["zebra"], body="x", enabled=False)
    rows = client.get("/api/cards").json()["cards"]
    row = next(r for r in rows if r["key"] == "off")
    assert row["enabled"] is False
    assert client.get("/api/cards/entry/off").json()["enabled"] is False


def test_card_detail_splits_sections(client):
    data = client.get("/api/cards/people/Alice").json()
    assert data["fields"]["who"].startswith("Alice works")
    assert set(data["fields"]) == {"who", "stance", "recent", "impression", "correspondence"}
    assert data["keywords"] == ["Alice", "payments team"] and data["aliases"] == ["Al"]
    # a string, because st_mtime_ns does not survive a round trip through a JS number
    assert isinstance(data["mtime"], str) and int(data["mtime"]) > 0
    assert data["archived"] is False


def test_card_detail_404_and_bad_paths(client):
    assert client.get("/api/cards/people/nobody").status_code == 404
    assert client.get("/api/cards/people/nobody").json()["error"] == "not_found"
    assert client.get("/api/cards/nonsense/Alice").status_code == 400
    # a traversal attempt never resolves to a path outside the vault
    assert client.get("/api/cards/people/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)


def test_checkup(client, vault):
    assert client.get("/api/checkup").json()["issues"] == []
    (vault / "bare.md").write_text("no frontmatter\n", encoding="utf-8")
    issues = client.get("/api/checkup").json()["issues"]
    assert any(i["code"] == "no_fm" for i in issues)


# --------------------------------------------------------------------------- write


def _payload(**over):
    data = {"kind": "place", "key": "the corner cafe", "title": "the corner cafe",
            "keywords": ["corner cafe"], "aliases": [], "refs": [],
            "secondary": {"all": [], "any": [], "not": []},
            "fields": {"where": "Two blocks away.", "relation": "", "recent": ""},
            "priority": 0, "enabled": True, "inject": True, "scan_depth": None}
    data.update(over)
    return data


def test_create_then_recall(client, vault):
    res = client.post("/api/cards", json=_payload())
    assert res.status_code == 201
    assert res.json()["path"] == "places/the corner cafe.md"
    engine.invalidate_cache()
    assert [c.key for c in engine.consult("meet at the corner cafe", vault)["hits"]] \
        == ["the corner cafe"]


def test_create_conflicts_with_an_existing_card(client):
    client.post("/api/cards", json=_payload())
    res = client.post("/api/cards", json=_payload())
    assert res.status_code == 409
    body = res.json()
    assert body["error"] == "exists" and body["current"]["key"] == "the corner cafe"


def test_create_rejects_bad_key_and_kind(client):
    assert client.post("/api/cards", json=_payload(key="../escape")).status_code == 400
    assert client.post("/api/cards", json=_payload(key="")).status_code == 400
    assert client.post("/api/cards", json=_payload(kind="nonsense")).status_code == 400
    assert client.post("/api/cards", content="not json").status_code == 400


def test_update_with_optimistic_lock(client):
    card = client.get("/api/cards/people/Alice").json()
    card["fields"]["recent"] = "Now leading the refund rewrite."
    res = client.put("/api/cards/people/Alice", json=card)
    assert res.status_code == 200
    updated = res.json()
    assert updated["fields"]["recent"] == "Now leading the refund rewrite."
    assert updated["fields"]["who"].startswith("Alice works")   # other sections untouched
    assert updated["mtime"] != card["mtime"]

    stale = dict(card)                                          # second editor, old mtime
    stale["fields"] = dict(card["fields"], recent="Something else entirely.")
    conflict = client.put("/api/cards/people/Alice", json=stale)
    assert conflict.status_code == 409
    assert conflict.json()["current"]["fields"]["recent"] == "Now leading the refund rewrite."


def test_update_without_mtime_just_writes(client):
    card = client.get("/api/cards/people/Alice").json()
    card.pop("mtime")
    card["keywords"] = ["Alice", "payments team", "refunds"]
    assert client.put("/api/cards/people/Alice", json=card).status_code == 200
    assert "refunds" in client.get("/api/cards/people/Alice").json()["keywords"]


def test_update_missing_card_is_404(client):
    assert client.put("/api/cards/people/nobody", json=_payload(kind="people")).status_code == 404


def test_delete_archives_and_restores(client, vault):
    assert client.delete("/api/cards/people/Bob").json()["ok"] is True
    assert not (vault / "people" / "Bob.md").exists()
    assert (vault / "_archive" / "people" / "Bob.md").is_file()

    # an archived card no longer fires, and is not in the normal listing
    engine.invalidate_cache()
    assert engine.consult("what about Bob", vault)["hits"] == []
    assert "Bob" not in {r["key"] for r in client.get("/api/cards").json()["cards"]}
    archived = client.get("/api/cards?archived=1").json()["cards"]
    assert [r["key"] for r in archived] == ["Bob"]
    assert client.get("/api/cards/people/Bob?archived=1").json()["archived"] is True

    assert client.post("/api/cards/people/Bob/restore").json()["ok"] is True
    assert (vault / "people" / "Bob.md").is_file()
    engine.invalidate_cache()
    assert [c.key for c in engine.consult("what about Bob", vault)["hits"]] == ["Bob"]


def test_restore_conflicts_with_a_new_card_of_the_same_name(client):
    client.delete("/api/cards/people/Bob")
    client.post("/api/cards", json=_payload(kind="people", key="Bob",
                                            fields={"who": "a different Bob"}))
    res = client.post("/api/cards/people/Bob/restore")
    assert res.status_code == 409
    assert client.delete("/api/cards/people/nobody").status_code == 404
    assert client.post("/api/cards/people/nobody/restore").status_code == 404


# --------------------------------------------------------------------------- try / ledger


def test_try_is_a_dry_run_and_does_not_log(client, vault):
    data = client.post("/api/try", json={"text": "Alice and Bob"}).json()
    assert {m["key"] for m in data["matched"]} == {"Alice", "Bob"}
    assert all("in_budget" in m and "hits" in m for m in data["matched"])
    assert "candidates" in data and "### Alice" in data["context"]
    assert client.get("/api/recent").json()["recent"] == []


def test_try_with_turns_and_opt_in_logging(client):
    data = client.post("/api/try", json={"text": "did she approve it",
                                         "turns": [{"role": "user", "text": "Alice reviewed my PR"}]}).json()
    assert [m["key"] for m in data["matched"]] == ["Alice"]
    client.post("/api/try?log=1", json={"text": "Alice again"})
    recent = client.get("/api/recent").json()["recent"]
    assert recent[0]["keys"] == ["Alice"] and recent[0]["source"] == "try"


def test_recent_reports_what_the_mcp_server_surfaced(client, vault):
    result = engine.consult("Alice", vault)
    engine.log_hits(vault, "mcp", result["hits"], text="Alice")
    recent = client.get("/api/recent?limit=5").json()["recent"]
    assert recent[0]["source"] == "mcp" and recent[0]["keys"] == ["Alice"]
    # the alias "Al" is a substring of "Alice", so both terms are recorded
    assert recent[0]["hits"] == ["al", "alice"]


def test_hit_ledger_is_bounded(vault, monkeypatch):
    monkeypatch.setattr(engine, "HITS_MAX_LINES", 20)
    card = engine.consult("Alice", vault)["hits"]
    for _ in range(60):
        engine.log_hits(vault, "mcp", card, text="x" * 300)
    lines = engine.hits_path(vault).read_text(encoding="utf-8").strip().splitlines()
    assert 0 < len(lines) <= 20
    assert engine.read_hits(vault, 5)[0]["keys"] == ["Alice"]


def test_log_hits_ignores_empty_results(vault):
    engine.log_hits(vault, "mcp", [])
    assert engine.read_hits(vault) == []


# --------------------------------------------------------------------------- import / export


def test_import_and_export(client, vault):
    book = {"entries": {"0": {"uid": 0, "key": ["dragon"], "comment": "dragon",
                              "content": "big lizard"}}}
    res = client.post("/api/import", json={"book": book}).json()
    assert res["written"] == ["dragon"]
    assert client.post("/api/import", json={"book": book}).json()["skipped"] == ["dragon"]
    assert client.post("/api/import", json={"book": book, "force": True}).json()["written"] \
        == ["dragon"]
    assert client.post("/api/import", json={}).status_code == 400
    assert client.post("/api/import", json={"book": book, "kind": "bogus"}).status_code == 400

    out = client.get("/api/export")
    assert out.headers["content-disposition"].endswith('filename="lorecards.json"')
    entries = json.loads(out.text)["entries"]
    assert any(e["comment"] == "dragon" for e in entries.values())


def test_export_leaves_out_archived_cards(client):
    client.delete("/api/cards/people/Bob")
    entries = client.get("/api/export").json()["entries"]
    assert not any(e["comment"] == "Bob" for e in entries.values())


# --------------------------------------------------------------------------- static / auth


def test_the_page_and_its_pwa_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "<title>lorecards</title>" in page.text and "/api/kinds" in page.text
    assert "cdn" not in page.text.lower() and "http://" not in page.text

    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    data = json.loads(manifest.text)
    assert data["name"] == "lorecards" and data["display"] == "standalone"
    assert {i["sizes"] for i in data["icons"]} == {"192x192", "512x512"}

    for icon in ("/icon-192.png", "/icon-512.png"):
        res = client.get(icon)
        assert res.status_code == 200 and res.content[:8] == b"\x89PNG\r\n\x1a\n"

    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "/api/" in sw.text


def test_token_guards_the_api_but_not_the_page(vault):
    with ui_client(vault, token="s3cret") as c:
        assert c.get("/api/cards").status_code == 401
        assert c.get("/api/cards").json()["error"] == "unauthorized"
        assert c.get("/api/cards", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/cards", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/cards?token=s3cret").status_code == 200
        assert c.get("/").status_code == 200          # the page itself holds no card data


def test_serving_beyond_localhost_without_a_token_is_refused(vault):
    with pytest.raises(SystemExit) as e:
        web.run_ui(vault, host="0.0.0.0", port=8766)
    assert "--token" in str(e.value)


def test_localhost_without_a_token_is_allowed(vault, monkeypatch):
    started = {}
    monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                        type("m", (), {"run": staticmethod(lambda app, **kw: started.update(kw))}))
    web.run_ui(vault, host="127.0.0.1", port=8799)
    assert started["port"] == 8799 and started["host"] == "127.0.0.1"


def test_mtime_survives_a_javascript_number_round_trip(client):
    """A browser parses JSON numbers as doubles, and st_mtime_ns is far past 2**53.
    Carrying it as a string is what makes the optimistic lock usable from a web page."""
    card = client.get("/api/cards/people/Alice").json()
    assert isinstance(card["mtime"], str)
    mangled = float(card["mtime"])                 # what a JS client would have sent back
    assert int(mangled) != int(card["mtime"])      # ...and it really is lossy
    card["fields"]["recent"] = "Saved from a browser."
    assert client.put("/api/cards/people/Alice", json=card).status_code == 200


# --------------------------------------------------------------------------- CSRF / rebinding


def test_cross_origin_requests_are_refused(vault):
    """The attack this closes: a page on another site POSTs text/plain to localhost and
    writes a card. The Origin no longer matches the Host, so it never reaches a handler."""
    with ui_client(vault) as c:
        evil = {"Origin": "http://attacker.example", web.GUARD_HEADER: "1"}
        res = c.post("/api/cards", json=_payload(), headers=evil)
        assert res.status_code == 403 and res.json()["error"] == "forbidden"
        assert c.get("/api/cards", headers={"Origin": "http://attacker.example"}).status_code == 403
        # our own origin is fine
        assert c.get("/api/cards", headers={"Origin": "http://testserver"}).status_code == 200
    assert not (vault / "places").exists()


def test_writes_need_the_guard_header(vault):
    """A cross-site form can post JSON-ish bodies but cannot add a custom header without a
    preflight, and we answer no preflight."""
    app = web.build_app(vault, allowed_hosts={"testserver"})
    with TestClient(app) as c:                           # no guard header at all
        assert c.post("/api/cards", json=_payload()).status_code == 403
        assert c.put("/api/cards/people/Alice", json={}).status_code == 403
        assert c.delete("/api/cards/people/Alice").status_code == 403
        assert c.post("/api/try", json={"text": "Alice"}).status_code == 403
        assert c.get("/api/cards").status_code == 200                     # reads are fine
    assert (vault / "people" / "Alice.md").is_file()


def test_an_unexpected_host_header_is_refused(vault):
    """DNS rebinding: the attacker's name resolves to 127.0.0.1, so the browser sends
    their name as Host. Only the names we actually serve are accepted."""
    app = web.build_app(vault, host="127.0.0.1", port=8766)
    with TestClient(app, headers={web.GUARD_HEADER: "1"}) as c:
        assert c.get("/api/cards", headers={"Host": "attacker.example"}).status_code == 403
        assert c.get("/api/cards", headers={"Host": "127.0.0.1:8766"}).status_code == 200
        assert c.get("/api/cards", headers={"Host": "localhost:8766"}).status_code == 200
        assert c.get("/", headers={"Host": "attacker.example"}).status_code == 200   # page only


def test_allowed_hosts_include_the_bind_address(vault):
    hosts = web.allowed_hosts_for("192.168.1.5", 8766)
    assert "192.168.1.5:8766" in hosts and "localhost:8766" in hosts
    assert "attacker.example" not in hosts
    # binding every interface is only allowed with a token, and then any Host is accepted
    assert web.build_app(vault, token="t", host="0.0.0.0", port=8766) is not None


def test_the_page_sends_the_guard_header_and_clears_the_token_from_the_url(client):
    page = client.get("/").text
    assert '"X-Lorecards"] = "1"' in page
    assert "history.replaceState" in page


def test_a_non_ascii_token_is_refused_at_startup(vault):
    with pytest.raises(ValueError, match="ASCII"):
        web.check_token("密码")
    with pytest.raises(ValueError, match="ASCII"):
        web.build_app(vault, token="密码")


def test_a_non_ascii_token_in_a_request_is_401_not_500(vault):
    with ui_client(vault, token="s3cret") as c:
        # bytes: an httpx str header must be ASCII, but a raw client can send this
        res = c.get("/api/cards", headers={b"Authorization": "Bearer café".encode("utf-8")})
        assert res.status_code == 401


def test_ui_no_log_overrides_an_opt_in_try(vault):
    app = web.build_app(vault, allowed_hosts={"testserver"}, log_hits=False)
    with TestClient(app, headers={web.GUARD_HEADER: "1"}) as c:
        assert c.post("/api/try?log=1", json={"text": "Alice"}).status_code == 200
        assert c.get("/api/recent").json()["recent"] == []


def test_export_is_fetched_with_a_header_not_a_token_in_the_url(client):
    page = client.get("/").text
    assert "createObjectURL" in page and 'token=" + encodeURIComponent' not in page
