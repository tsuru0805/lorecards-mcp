"""Injecting proxy: cards surface without the model having to ask for them.

``lorecards gateway --vault ~/cards --upstream https://api.openai.com`` sits in front of an
OpenAI-compatible or Anthropic Messages endpoint. Point any client that lets you set a base
URL at it. On each request it reads the recent turns out of the request body, asks the
engine which cards the conversation just triggered, and writes them into the prompt before
forwarding. Everything else is passed through untouched.

Two things it is careful about:

**Credentials.** ``Authorization``, ``x-api-key``, ``anthropic-version`` and every other
header travel to the upstream unchanged. No header is ever stored or logged, and neither is
any part of the model's reply. What *is* written to disk is the vault's own bookkeeping: the
dedupe ledger (which card keys were injected, at which turn) and the hit ledger, which
records the first 200 characters of the triggering message so the "recently surfaced" panel
can show it. ``--no-log`` turns the hit ledger off.

**Repeats.** A card that fires on five turns in a row would be paid for five times. Each
conversation gets a small ledger of which cards were injected at which turn, and a card is
not injected again until ``--reinject-after`` turns have passed.

If anything at all goes wrong on our side — an unreadable vault, a corrupt ledger, a body
shaped differently than expected — the request is forwarded unmodified. The proxy never
stands between the user and their model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

try:  # POSIX only; without it the ledger still uses unique temp names
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

try:  # mcp 2.x vendors its client as httpx2; mcp 1.x brings plain httpx
    import httpx
except ImportError:  # pragma: no cover - whichever one is installed
    import httpx2 as httpx

from . import engine, schema as sc, web

log = logging.getLogger("lorecards.gateway")

DEFAULT_PORT = 8765
LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
TOKEN_HEADER = "x-lorecards-token"     # never Authorization: that one belongs to the upstream
HIT_TEXT_CHARS = 200
DEFAULT_FRAMING = "A memory surfaces:\n\n"
DEFAULT_WINDOW = 3
DEFAULT_REINJECT_AFTER = 6
LEDGER_FILENAME = "injections.json"
MAX_CARDS_PER_CONVERSATION = 200
MAX_CONVERSATIONS = 500
CONVERSATION_HEADER = "x-lorecards-conversation"
INJECTED_HEADER = "X-Lorecards-Injected"

OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_PATH = "/v1/messages"

#: A per-process nonce, so we only ever strip blocks this process wrote. A user (or another
#: proxy) can paste the literal marker text into a message without losing it.
NONCE = secrets.token_hex(3)
MARK_OPEN = f"<!-- lorecards:{NONCE} -->"
MARK_CLOSE = f"<!-- /lorecards:{NONCE} -->"
_MARK_RE = re.compile(re.escape(MARK_OPEN) + r".*?" + re.escape(MARK_CLOSE) + r"\s*", re.S)
#: Headers that belong to one hop and must not be forwarded.
_HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
               "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer"}
#: Headers our own server writes; passing the upstream's copy through duplicates them.
_RESPONSE_DROP = _HOP_BY_HOP | {"date", "server"}


def strip_marks(text: str) -> str:
    """Remove anything this proxy injected earlier, so it never feeds itself."""
    return _MARK_RE.sub("", text or "")


def wrap(text: str) -> str:
    return f"{MARK_OPEN}\n{text}\n{MARK_CLOSE}"


# --------------------------------------------------------------------------- message shapes


def text_of(content: Any) -> str:
    """The plain text of a message, ignoring tool calls, images and our own injections."""
    if isinstance(content, str):
        return strip_marks(content).strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(strip_marks(block["text"]))
            elif isinstance(block, str):
                parts.append(strip_marks(block))
        return "\n".join(p for p in parts if p.strip()).strip()
    return ""


def conversation_turns(messages: list[dict], window: int) -> tuple[str, list[dict], int]:
    """``(current user message, recent turns, turn index)``.

    The turn index is how many user messages the request carries, which is the only clock
    a stateless proxy has.
    """
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    query = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            query = text_of(m.get("content"))
            break
    history: list[dict] = []
    for m in messages[:-1] if messages else []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        text = text_of(m.get("content"))
        if text:
            history.append({"role": m["role"], "text": text})
    return query, history[-(max(window, 0) * 2):], len(users)


def inject_into_user(messages: list[dict], block: str) -> bool:
    """Put the block in front of the last user message's text."""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = block + "\n\n" + content
            return True
        if isinstance(content, list):
            m["content"] = [{"type": "text", "text": block}] + content
            return True
        return False
    return False


def inject_openai_system(body: dict, block: str) -> bool:
    body.setdefault("messages", []).append({"role": "system", "content": block})
    return True


def inject_anthropic_system(body: dict, block: str) -> bool:
    system = body.get("system")
    if system is None or isinstance(system, str):
        body["system"] = (system + "\n\n" + block) if system else block
    elif isinstance(system, list):
        system.append({"type": "text", "text": block})
    else:
        return False
    return True


def system_text(body: dict, flavour: str) -> str:
    """The system prompt as text, for the conversation fingerprint."""
    if flavour == "anthropic":
        return text_of(body.get("system"))
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "system":
            return text_of(m.get("content"))
    return ""


def conversation_id(body: dict, flavour: str, header: str | None) -> str:
    """A client-supplied id, or a fingerprint of how the conversation started."""
    if header and header.strip():
        return header.strip()[:128]
    first_user = ""
    for m in body.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "user":
            first_user = text_of(m.get("content"))
            break
    seed = system_text(body, flavour) + "\n" + first_user
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- dedupe ledger


def ledger_path(vault: str | Path) -> Path:
    return Path(vault).expanduser() / engine.STATE_DIRNAME / LEDGER_FILENAME


_ledger_lock = threading.RLock()


class _FileLock:
    """Hold an exclusive lock on the ledger across processes for a read-modify-write.

    Without it, two gateways sharing a vault can each read, then each write, and one of the
    two updates is simply gone. On a platform without ``fcntl`` this degrades to the
    in-process lock; the unique temp name still keeps the file itself intact.
    """

    def __init__(self, path: Path):
        self.path = path.with_suffix(".lock")
        self.handle = None

    def __enter__(self):
        _ledger_lock.acquire()
        if fcntl is None:
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a+")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            log.warning("could not lock the injection ledger: %r", e)
            self.handle = None
        return self

    def __exit__(self, *exc):
        try:
            if self.handle is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
        finally:
            self.handle = None
            _ledger_lock.release()


def load_ledger(vault: str | Path) -> dict:
    path = ledger_path(vault)
    if not path.is_file():
        return {"conversations": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("injection ledger unreadable, starting a fresh one: %r", e)
        return {"conversations": {}}
    if not isinstance(data, dict) or not isinstance(data.get("conversations"), dict):
        return {"conversations": {}}
    return data


def save_ledger(vault: str | Path, data: dict) -> None:
    """Atomic write: a half-written ledger would cost injections, not correctness."""
    path = ledger_path(vault)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # a name no other writer can collide with: two processes sharing one temp file
        # would otherwise interleave and leave torn JSON behind
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}."
                             f"{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not write the injection ledger: %r", e)


def _trim(data: dict) -> None:
    convs = data["conversations"]
    for conv in convs.values():
        cards = conv.get("cards", {})
        if len(cards) > MAX_CARDS_PER_CONVERSATION:
            keep = sorted(cards.items(), key=lambda kv: kv[1], reverse=True)[:MAX_CARDS_PER_CONVERSATION]
            conv["cards"] = dict(keep)
    if len(convs) > MAX_CONVERSATIONS:
        keep = sorted(convs.items(), key=lambda kv: kv[1].get("seen", 0),
                      reverse=True)[:MAX_CONVERSATIONS]
        data["conversations"] = dict(keep)


def pick_new(vault: str | Path, conversation: str, keys: list[str], turn: int,
             reinject_after: int) -> list[str]:
    """Which of these cards may be injected now, given what this conversation already saw."""
    if not keys:
        return []
    conv = load_ledger(vault)["conversations"].get(conversation) or {}
    cards = conv.get("cards", {})
    out = []
    for key in keys:
        last = cards.get(key)
        # `last > turn` means the client trimmed its history and the turn count went
        # backwards. Treat that as a reset and inject again, rather than staying silent
        # forever because of a number from a conversation that no longer exists.
        if (isinstance(last, (int, float)) and last <= turn
                and turn - last < max(reinject_after, 0)):
            continue
        out.append(key)
    return out


def record(vault: str | Path, conversation: str, keys: list[str], turn: int) -> None:
    if not keys:
        return
    with _FileLock(ledger_path(vault)):     # read-modify-write, so hold the lock across both
        data = load_ledger(vault)
        conv = data["conversations"].setdefault(conversation, {"cards": {}, "seen": 0})
        conv.setdefault("cards", {})
        for key in keys:
            conv["cards"][key] = turn
        conv["seen"] = time.time()
        _trim(data)
        save_ledger(vault, data)


def next_turn(vault: str | Path, conversation: str) -> int:
    """A turn counter for callers with no turn count of their own (the Claude Code hook)."""
    with _FileLock(ledger_path(vault)):
        data = load_ledger(vault)
        conv = data["conversations"].setdefault(conversation, {"cards": {}, "seen": 0})
        conv["turn"] = int(conv.get("turn", 0)) + 1
        conv["seen"] = time.time()
        _trim(data)
        save_ledger(vault, data)
        return conv["turn"]


# --------------------------------------------------------------------------- the proxy


class Injector:
    """Decides what to add to one request body. Kept separate so it is easy to test."""

    def __init__(self, vault: str | Path, *, inject: str = "user", window: int = DEFAULT_WINDOW,
                 reinject_after: int = DEFAULT_REINJECT_AFTER,
                 budget_chars: int = engine.DEFAULT_BUDGET_CHARS, framing: str = DEFAULT_FRAMING,
                 log_hits: bool = True):
        self.vault = Path(vault).expanduser()
        self.inject = inject if inject in ("user", "system") else "user"
        self.window = window
        self.reinject_after = reinject_after
        self.budget_chars = budget_chars
        self.framing = framing
        self.log_hits = log_hits

    def apply(self, body: dict, flavour: str, conv_header: str | None) -> list[str]:
        """Mutate ``body`` in place. Returns the keys injected (empty when nothing was)."""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return []
        query, turns, turn = conversation_turns(messages, self.window)
        if not query:
            return []
        schema = sc.load_schema(self.vault)
        result = engine.consult(query, self.vault, turns=turns, budget_chars=self.budget_chars,
                                scan_depth=self.window, schema=schema)
        hits = result.get("hits") or []
        if not hits:
            return []
        conversation = conversation_id(body, flavour, conv_header)
        allowed = pick_new(self.vault, conversation, [c.key for c in hits], turn,
                           self.reinject_after)
        if not allowed:
            return []
        chosen = [c for c in hits if c.key in allowed]
        block = wrap(self.framing + "\n\n".join(
            f"### {c.title} [{c.kind}]\n{c.body.strip()}" for c in chosen))
        ok = (inject_into_user(messages, block) if self.inject == "user"
              else (inject_anthropic_system(body, block) if flavour == "anthropic"
                    else inject_openai_system(body, block)))
        if not ok:
            return []
        record(self.vault, conversation, [c.key for c in chosen], turn)
        if self.log_hits:
            engine.log_hits(self.vault, "gateway", chosen, text=query)
        return [c.key for c in chosen]


def _forward_headers(request: Request) -> dict[str, str]:
    """Every header except the hop-by-hop ones and our own. Credentials pass through
    untouched; none are logged."""
    return {k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() != TOKEN_HEADER}


def upstream_path(request: Request) -> str | None:
    """The request path exactly as it arrived, or ``None`` if it tries to climb out.

    ``raw_path`` keeps percent-encoding intact, so a path segment containing ``%2F`` reaches
    the upstream as the client wrote it instead of turning into a directory separator. A
    segment that decodes to ``..`` is refused: it would walk out of the upstream's prefix.
    """
    raw = request.scope.get("raw_path")
    path = raw.decode("latin-1") if isinstance(raw, bytes) else request.url.path
    path = path.split("?", 1)[0]
    for segment in path.split("/"):
        # decode once before judging: `..%2F..%2Fadmin` is one segment to a router but
        # three path components to anything that unquotes it downstream
        for part in unquote(segment).replace("\\", "/").split("/"):
            if part.strip() in ("..", "."):
                return None
    return path


def check_upstream(upstream: str) -> str:
    """Validate ``--upstream``: an http(s) origin, nothing else."""
    url = (upstream or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"--upstream must be an http(s) URL, got {upstream!r}")
    return url


def build_gateway(vault: str | Path, upstream: str, *, inject: str = "user",
                  window: int = DEFAULT_WINDOW, reinject_after: int = DEFAULT_REINJECT_AFTER,
                  budget_chars: int = engine.DEFAULT_BUDGET_CHARS,
                  framing: str = DEFAULT_FRAMING, log_hits: bool = True,
                  token: str | None = None, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                  allowed_hosts: set[str] | None = ..., client: Any = None) -> Starlette:
    """Build the proxy app. ``client`` lets tests pass in an httpx client of their own."""
    base = check_upstream(upstream)
    injector = Injector(vault, inject=inject, window=window, reinject_after=reinject_after,
                        budget_chars=budget_chars, framing=framing, log_hits=log_hits)
    if allowed_hosts is ...:
        allowed_hosts = (None if host in ("0.0.0.0", "::") and token
                         else web.allowed_hosts_for(host, port))

    def http_client() -> Any:
        return client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))

    def refuse(request: Request) -> Response | None:
        """Host and token checks, before anything is forwarded or read."""
        host_header = (request.headers.get("host") or "").lower()
        if allowed_hosts is not None and host_header not in allowed_hosts:
            return JSONResponse({"error": {"message": f"lorecards: unexpected Host header "
                                                      f"{host_header!r}",
                                           "type": "forbidden"}}, status_code=403)
        if token:
            given = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(given, token):
                return JSONResponse({"error": {"message": f"lorecards: send {TOKEN_HEADER}: "
                                                          "<token>",
                                               "type": "unauthorized"}}, status_code=401)
        return None

    async def relay(request: Request, body: bytes | None, injected: list[str]) -> Response:
        """Stream the upstream response straight back, keeping its status and headers."""
        path = upstream_path(request)
        if path is None:
            return JSONResponse({"error": {"message": "lorecards: refusing a path that "
                                                      "climbs out of the upstream",
                                           "type": "bad_request"}}, status_code=400)
        url = base + path + (("?" + request.url.query) if request.url.query else "")
        headers = _forward_headers(request)
        if body is None:
            body = await request.body()
        cli = http_client()
        owned = client is None
        upstream_request = cli.build_request(request.method, url, headers=headers, content=body)
        try:
            response = await cli.send(upstream_request, stream=True)
        except httpx.HTTPError as e:
            if owned:
                await cli.aclose()
            log.warning("upstream request failed: %s", type(e).__name__)
            return JSONResponse({"error": {"message": f"lorecards: upstream unreachable ({type(e).__name__})",
                                           "type": "upstream_error"}}, status_code=502)

        async def stream():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:                      # client hung up, or we are done: drop the upstream
                await response.aclose()
                if owned:
                    await cli.aclose()

        out = {k: v for k, v in response.headers.items() if k.lower() not in _RESPONSE_DROP}
        # only claim an injection the upstream actually accepted
        if injected and 200 <= response.status_code < 300:
            out[INJECTED_HEADER] = ",".join(injected)
        return StreamingResponse(stream(), status_code=response.status_code, headers=out)

    async def chat(request: Request) -> Response:
        denied = refuse(request)
        if denied is not None:
            return denied
        flavour = "anthropic" if request.url.path.rstrip("/").endswith("/messages") else "openai"
        raw = await request.body()
        injected: list[str] = []
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                injected = injector.apply(body, flavour, request.headers.get(CONVERSATION_HEADER))
                if injected:
                    raw = json.dumps(body).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass                                    # not JSON: not ours to touch
        except Exception as e:                      # never let a card break a conversation
            log.warning("skipping injection for this request: %r", e)
            injected = []
        return await relay(request, raw, injected)

    async def passthrough(request: Request) -> Response:
        denied = refuse(request)
        if denied is not None:
            return denied
        return await relay(request, None, [])

    routes = [
        Route(OPENAI_PATH, chat, methods=["POST"]),
        Route(ANTHROPIC_PATH, chat, methods=["POST"]),
        Route("/{path:path}", passthrough,
              methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    ]
    app = Starlette(routes=routes)
    app.state.injector = injector
    return app


def run_gateway(vault: str | Path, upstream: str, *, host: str = "127.0.0.1",
                port: int = DEFAULT_PORT, token: str | None = None, **kwargs) -> None:
    """Serve the proxy. Refuses to listen beyond localhost without a token."""
    import uvicorn  # noqa: PLC0415

    upstream = check_upstream(upstream)
    if host not in LOOPBACK and not token:
        raise SystemExit(
            f"refusing to serve on {host} without --token: anyone who can reach that address "
            "could spend your API key and read your cards. Pass --token <secret>, or keep "
            "the default host.")
    vault_path = Path(vault).expanduser()
    vault_path.mkdir(parents=True, exist_ok=True)
    print(f"lorecards gateway  vault: {vault_path}")
    print(f"                   upstream: {upstream}")
    print(f"                   base URL for your client: http://{host}:{port}/v1")
    if token:
        print(f"                   clients must send  {TOKEN_HEADER}: <your token>")
    uvicorn.run(build_gateway(vault_path, upstream, token=token, host=host, port=port, **kwargs),
                host=host, port=port, log_level="warning")
