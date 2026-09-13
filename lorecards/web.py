"""Local web UI: a JSON API plus one self-contained page for editing cards.

``lorecards ui --vault ~/cards`` serves both on 127.0.0.1. The API is the point: the page
is only one client, and a PWA, a SwiftUI app or a shell script can speak the same endpoints.

Bound to localhost there is no authentication — anyone who can reach the port can read and
write the vault. Serving on any other interface therefore requires ``--token``, which the
API checks as ``Authorization: Bearer <token>`` (or a ``?token=`` query parameter, so a
phone can open the page from a link). Static files are public either way: they carry no
card data.

Against the other browser tab, ``/api/*`` also checks the ``Host`` header (so a rebound
name cannot reach it), rejects a request whose ``Origin`` is not this server, and requires
``X-Lorecards: 1`` on every write — a header a cross-site form cannot send without a
preflight, and we answer no preflight.

Runs on starlette + uvicorn, both of which the ``mcp`` SDK already brings in — no extra
dependency for people who only want the server.
"""

from __future__ import annotations

import hmac
import json
import struct
import zlib
from importlib import resources
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import engine, io as lore_io, schema as sc

DEFAULT_PORT = 8766
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
#: Every write must carry this header. A browser cannot send it cross-origin without a
#: preflight, and a preflight we never answer, so a page on another site cannot write here.
GUARD_HEADER = "x-lorecards"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                "/sw.js": ("sw.js", "text/javascript; charset=utf-8")}


class ApiError(HTTPException):
    def __init__(self, status: int, error: str, detail: str, extra: dict | None = None):
        super().__init__(status_code=status, detail=detail)
        self.error = error
        self.extra = extra or {}


def _fail(status: int, error: str, detail: str, **extra) -> ApiError:
    return ApiError(status, error, detail, extra)


def _read_static(name: str) -> bytes:
    return (resources.files("lorecards.ui") / name).read_bytes()


# --------------------------------------------------------------------------- icon


def _png(size: int) -> bytes:
    """A tiny card-stack icon, rasterised here so the app never reaches for the network."""
    bg, card, ink = (0x1B, 0x1E, 0x24), (0xF7, 0xF5, 0xEF), (0x6E, 0x8B, 0xA8)
    u = size / 16
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            px = bg
            # two offset "cards", the back one tinted
            if 3.4 * u <= x < 12.2 * u and 2.2 * u <= y < 11.4 * u:
                px = ink
            if 2.0 * u <= x < 10.8 * u and 4.0 * u <= y < 13.2 * u:
                px = card
                # three "keyword" lines on the front card
                for i in range(3):
                    top = (5.6 + i * 2.0) * u
                    if top <= y < top + 0.9 * u and 3.2 * u <= x < (9.4 - i * 1.6) * u:
                        px = ink
            row.extend(px)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">2I5B", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- helpers


def _rows(vault: Path, schema: sc.CardSchema, kind: str | None) -> list[dict]:
    out = []
    for card in engine.load_cards(vault, schema, include_non_injecting=True):
        if kind and card.kind != kind:
            continue
        try:
            mtime = str((vault / card.path).stat().st_mtime_ns)   # a string: see card_detail
        except OSError:
            mtime = "0"
        out.append({"key": card.key, "kind": card.kind, "title": card.title, "path": card.path,
                    "keywords": card.keywords, "aliases": card.aliases, "enabled": True,
                    "inject": card.inject, "preview": " ".join(card.body.split())[:120],
                    "mtime": mtime, "archived": False})
    # cards switched off never load, so read them straight from disk to keep them editable
    for path in engine.iter_card_files(vault):
        rel = str(path.relative_to(vault))
        if any(r["path"] == rel for r in out):
            continue
        fm, body, problem = sc.split_frontmatter(path.read_text(encoding="utf-8"))
        meta = sc.parse_meta(fm or {}, rel, path.stem, schema)
        if kind and meta["kind"] != kind:
            continue
        out.append({"key": path.stem, "kind": meta["kind"], "title": meta["title"], "path": rel,
                    "keywords": meta["keywords"], "aliases": meta["aliases"],
                    "enabled": meta["enabled"], "inject": meta["inject"],
                    "preview": " ".join(body.split())[:120], "problem": problem,
                    "mtime": str(path.stat().st_mtime_ns), "archived": False})
    out.sort(key=lambda r: (r["kind"], r["title"].casefold()))
    return out


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise _fail(400, "bad_json", "the request body is not valid JSON")
    if not isinstance(data, dict):
        raise _fail(400, "bad_json", "the request body must be a JSON object")
    return data


def _kind_and_key(request: Request) -> tuple[str, str]:
    kind = request.path_params["kind"]
    key = sc.safe_key(request.path_params["key"])
    if kind not in sc.KINDS:
        raise _fail(400, "bad_kind", f"unknown kind: {kind!r}")
    if key is None:
        raise _fail(400, "bad_key", "a card key may not contain a path")
    return kind, key


# --------------------------------------------------------------------------- security


def allowed_hosts_for(host: str, port: int) -> set[str]:
    """Host headers this server will answer to, so a page on another site cannot reach it
    by rebinding a name to 127.0.0.1 (the browser would send that name as Host)."""
    hosts = {f"{h}:{port}" for h in LOOPBACK}
    hosts |= {h for h in LOOPBACK}          # a default port is omitted from Host
    if host and host not in LOOPBACK and host not in ("0.0.0.0", "::"):
        hosts.add(f"{host}:{port}")
        hosts.add(host)
    return hosts


def security_guard(allowed_hosts: set[str] | None, token: str | None):
    """Guard /api/*: Host, Origin, a header a cross-site form cannot send, and the token.

    Static files stay open: they hold no card data, and the page must load before it can
    send anything. ``allowed_hosts=None`` means any Host is accepted, which only happens
    when the server was told to bind every interface — and that requires a token.
    """
    def deny(detail: str, code: int = 403):
        return JSONResponse({"error": "forbidden" if code == 403 else "unauthorized",
                             "detail": detail}, status_code=code)

    async def guard(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        host = (request.headers.get("host") or "").lower()
        if allowed_hosts is not None and host not in allowed_hosts:
            return deny(f"unexpected Host header: {host!r}")

        origin = request.headers.get("origin")
        if origin:
            netloc = origin.split("://", 1)[-1].lower()
            if netloc != host:
                return deny("cross-origin requests are not accepted")

        if request.method in WRITE_METHODS and request.headers.get(GUARD_HEADER) != "1":
            return deny(f"writes must carry the {GUARD_HEADER}: 1 header")

        if token:
            header = request.headers.get("authorization", "")
            given = (header[7:] if header.lower().startswith("bearer ")
                     else request.query_params.get("token", ""))
            if not hmac.compare_digest(given, token):
                return deny("send Authorization: Bearer <token>", 401)
        return await call_next(request)

    return guard


# --------------------------------------------------------------------------- app


def build_app(vault: str | Path, token: str | None = None, *, host: str = "127.0.0.1",
              port: int = DEFAULT_PORT, allowed_hosts: set[str] | None = ...) -> Starlette:
    """Build the Starlette app for one vault.

    ``allowed_hosts`` defaults to the loopback names plus the bind address; pass a set to
    override it, or ``None`` to accept any Host (only sensible behind a token).
    """
    vault_path = Path(vault).expanduser()
    if allowed_hosts is ...:
        allowed_hosts = (None if host in ("0.0.0.0", "::") and token
                         else allowed_hosts_for(host, port))

    def schema() -> sc.CardSchema:
        return sc.load_schema(vault_path)

    # ---------------- static

    async def static(request: Request) -> Response:
        name, media = STATIC_FILES[request.url.path]
        return Response(_read_static(name), media_type=media)

    async def icon(request: Request) -> Response:
        size = 512 if "512" in request.url.path else 192
        return Response(_png(size), media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})

    # ---------------- read

    async def api_kinds(request: Request) -> JSONResponse:
        s = schema()
        return JSONResponse({
            "kinds": [{"kind": k, "dir": s.kind_dirs.get(k, ""), "sections": s.sections_for(k),
                       "private_sections": [n for n in s.private_sections
                                            if n not in s.sections_for(k) and s.sections_for(k)],
                       "recent_section": s.recent_for(k)} for k in sc.KINDS],
            "private_sections": list(s.private_sections),
            "vault": str(vault_path), "scan_depth": engine.DEFAULT_SCAN_DEPTH,
            "budget_chars": engine.DEFAULT_BUDGET_CHARS, "jieba": engine.has_jieba(),
            "max_scan_depth": sc.MAX_SCAN_DEPTH})

    async def api_cards(request: Request) -> JSONResponse:
        kind = request.query_params.get("kind") or None
        if request.query_params.get("archived") in ("1", "true"):
            rows = [r for r in engine.list_archived(vault_path, schema())
                    if not kind or r["kind"] == kind]
        else:
            rows = _rows(vault_path, schema(), kind)
        return JSONResponse({"cards": rows, "vault": str(vault_path)})

    async def api_card(request: Request) -> JSONResponse:
        kind, key = _kind_and_key(request)
        archived = request.query_params.get("archived") in ("1", "true")
        try:
            return JSONResponse(engine.card_detail(vault_path, kind, key, schema(), archived))
        except engine.CardError as e:
            raise _fail(404, "not_found", str(e))

    async def api_checkup(request: Request) -> JSONResponse:
        return JSONResponse({"issues": engine.check_vault(vault_path, schema())})

    async def api_recent(request: Request) -> JSONResponse:
        try:
            limit = int(request.query_params.get("limit", 30))
        except ValueError:
            limit = 30
        return JSONResponse({"recent": engine.read_hits(vault_path, limit)})

    # ---------------- write

    async def api_create(request: Request) -> JSONResponse:
        data = await _body(request)
        kind = data.get("kind")
        key = sc.safe_key(str(data.get("key") or ""))
        if kind not in sc.KINDS:
            raise _fail(400, "bad_kind", f"unknown kind: {kind!r}")
        if key is None:
            raise _fail(400, "bad_key", "a card needs a name, and it may not contain a path")
        try:
            detail = engine.upsert_card(vault_path, kind, key, data, create=True, schema=schema())
        except engine.ConflictError as e:
            raise _fail(409, "exists", str(e), current=e.current)
        except engine.CardError as e:
            raise _fail(400, "bad_card", str(e))
        return JSONResponse(detail, status_code=201)

    async def api_update(request: Request) -> JSONResponse:
        kind, key = _kind_and_key(request)
        data = await _body(request)
        expected = data.get("mtime")
        expected = str(expected) if isinstance(expected, (int, float, str)) and expected else None
        try:
            detail = engine.upsert_card(vault_path, kind, key, data, expected_mtime=expected,
                                        schema=schema())
        except engine.ConflictError as e:
            raise _fail(409, "conflict", str(e), current=e.current)
        except engine.CardError as e:
            raise _fail(404, "not_found", str(e))
        return JSONResponse(detail)

    async def api_delete(request: Request) -> JSONResponse:
        kind, key = _kind_and_key(request)
        try:
            return JSONResponse(engine.archive_card(vault_path, kind, key, schema()))
        except engine.CardError as e:
            raise _fail(404, "not_found", str(e))

    async def api_restore(request: Request) -> JSONResponse:
        kind, key = _kind_and_key(request)
        try:
            return JSONResponse(engine.restore_card(vault_path, kind, key, schema()))
        except engine.ConflictError as e:
            raise _fail(409, "exists", str(e), current=e.current)
        except engine.CardError as e:
            raise _fail(404, "not_found", str(e))

    # ---------------- try / import / export

    async def api_try(request: Request) -> JSONResponse:
        data = await _body(request)
        text = str(data.get("text") or "")
        turns = data.get("turns") if isinstance(data.get("turns"), list) else []
        result = engine.consult(text, vault_path, turns=turns, dry_run=True, schema=schema())
        debug = result.get("debug", {})
        matched = [{"key": c["key"], "kind": c["kind"], "title": c["title"], "hits": c["hits"],
                    "score": c["score"], "chars": c["chars"], "in_budget": c["in_budget"]}
                   for c in debug.get("cards", [])]
        if request.query_params.get("log") in ("1", "true") and result["hits"]:
            engine.log_hits(vault_path, "try", result["hits"], text=text)
        return JSONResponse({"matched": matched, "candidates": debug.get("candidates", {}),
                             "truncated": result["truncated"],
                             "context": engine.render_context(result)})

    async def api_import(request: Request) -> JSONResponse:
        data = await _body(request)
        book = data.get("book")
        if book is None:
            raise _fail(400, "no_book", "send the lorebook JSON as the `book` field")
        kind = data.get("kind") or "entry"
        if kind not in sc.KINDS:
            raise _fail(400, "bad_kind", f"unknown kind: {kind!r}")
        result = lore_io.import_book_data(book, vault_path, kind=kind,
                                          force=bool(data.get("force")), schema=schema())
        return JSONResponse(result)

    async def api_export(request: Request) -> Response:
        kind = request.query_params.get("kind") or None
        book = lore_io.export_book(vault_path, kind=kind, schema=schema())
        return Response(json.dumps(book, ensure_ascii=False, indent=2),
                        media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="lorecards.json"'})

    # ---------------- wiring

    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, ApiError):
            return JSONResponse({"error": exc.error, "detail": exc.detail, **exc.extra},
                                status_code=exc.status_code)
        status = getattr(exc, "status_code", 500)
        return JSONResponse({"error": "http_error", "detail": str(getattr(exc, "detail", exc))},
                            status_code=status)

    routes = [Route(path, static, methods=["GET"]) for path in STATIC_FILES]
    routes += [
        Route("/icon-192.png", icon, methods=["GET"]),
        Route("/icon-512.png", icon, methods=["GET"]),
        Route("/api/kinds", api_kinds, methods=["GET"]),
        Route("/api/cards", api_cards, methods=["GET"]),
        Route("/api/cards", api_create, methods=["POST"]),
        Route("/api/cards/{kind}/{key}", api_card, methods=["GET"]),
        Route("/api/cards/{kind}/{key}", api_update, methods=["PUT"]),
        Route("/api/cards/{kind}/{key}", api_delete, methods=["DELETE"]),
        Route("/api/cards/{kind}/{key}/restore", api_restore, methods=["POST"]),
        Route("/api/try", api_try, methods=["POST"]),
        Route("/api/recent", api_recent, methods=["GET"]),
        Route("/api/checkup", api_checkup, methods=["GET"]),
        Route("/api/import", api_import, methods=["POST"]),
        Route("/api/export", api_export, methods=["GET"]),
    ]
    middleware = [Middleware(BaseHTTPMiddleware,
                             dispatch=security_guard(allowed_hosts, token))]
    app = Starlette(routes=routes, middleware=middleware,
                    exception_handlers={HTTPException: on_error, ApiError: on_error})
    app.state.vault = vault_path
    return app


def run_ui(vault: str | Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
           token: str | None = None) -> None:
    """Serve the UI. Refuses to listen beyond localhost without a token."""
    import uvicorn  # noqa: PLC0415 - only needed when the UI is actually run

    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit(
            f"refusing to serve on {host} without --token: anyone who can reach that address "
            "could read and rewrite the vault. Pass --token <secret>, or keep the default host.")
    vault_path = Path(vault).expanduser()
    vault_path.mkdir(parents=True, exist_ok=True)
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    suffix = f"?token={token}" if token else ""
    print(f"lorecards ui  vault: {vault_path}")
    print(f"              open: http://{shown}:{port}/{suffix}")
    if not token:
        print("              (no token: anyone who can reach this port can edit the vault)")
    uvicorn.run(build_app(vault_path, token, host=host, port=port), host=host, port=port,
                log_level="warning")
