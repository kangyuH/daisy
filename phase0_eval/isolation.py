"""Restrict the dedicated runner process to file replay and a single model endpoint."""

from __future__ import annotations

import ipaddress
import os
import sys
from contextvars import ContextVar
from pathlib import Path

import httpx

_MODEL_URL: ContextVar[httpx.URL | None] = ContextVar("phase0_model_url", default=None)
_INSTALLED = False


class IsolationError(BaseException):
    """Escape retry and exception handlers in the worker, tools and HTTP client."""


def _deny(message: str) -> None:
    raise IsolationError(f"phase 0 isolation: {message}")


def _audit(event: str, arguments: tuple) -> None:
    if event.startswith(("subprocess.", "sqlite3.")) or event in {
        "os.system", "os.exec", "os.fork", "os.forkpty", "os.spawn", "os.posix_spawn",
    }:
        _deny(event)
    if event == "open" and isinstance(arguments[0], (str, bytes)):
        path = Path(os.fsdecode(arguments[0]))
        if path.name in {".env", "gateway.db", "gateway.db-shm", "gateway.db-wal"}:
            _deny(f"opening {path.name}")
        if ".lark-cli" in path.parts:
            _deny("opening Feishu credentials or state")
    if event in {
        "socket.gethostbyname", "socket.gethostbyaddr",
        "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
    }:
        _deny(event)
    if event == "socket.getaddrinfo":
        url = _MODEL_URL.get()
        if url is None or arguments[0] not in {url.host, url.raw_host}:
            _deny("unexpected name resolution")
    if event == "socket.connect":
        url = _MODEL_URL.get()
        address = arguments[1]
        if url is None or not isinstance(address, tuple) or address[1] != (url.port or 443):
            _deny("network outside designated model transport")
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            _deny("unexpected network target")
        if not ip.is_global:
            _deny("model resolved to a nonpublic address")


def install_runner_guard() -> None:
    """Install once in the dedicated CLI process; never run inside a production process."""
    global _INSTALLED
    if _INSTALLED:
        return
    sys.addaudithook(_audit)
    # The fallback pysqlite3 build may omit stdlib audit events. Also cover
    # the real methods explicitly so failures happen before configs are read.
    from app.infra import db
    from workers.client import GatewayClient
    from app.infra.lark import cli

    def blocked(*args, **kwargs):
        _deny("production Gateway, database or Feishu CLI call")

    db.connect_sync = blocked
    db.init_db_sync = blocked
    db.sqlite3.connect = blocked
    GatewayClient._request = blocked
    cli.run_cli = blocked
    cli.run_cli_async = blocked
    _INSTALLED = True


class ModelTransport(httpx.BaseTransport):
    def __init__(self, base_url: str):
        base = httpx.URL(base_url)
        host = base.host.lower().rstrip(".")
        if (
            base.scheme != "https" or not host or "." not in host or base.userinfo
            or base.query or base.fragment
            or any(host == domain or host.endswith("." + domain)
                   for domain in ("feishu.cn", "larksuite.com", "larkoffice.com"))
        ):
            raise ValueError("model base URL must be a credential-free HTTPS model endpoint")
        self.allowed = httpx.URL(str(base).rstrip("/") + "/chat/completions")
        self.transport = httpx.HTTPTransport(trust_env=False, retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url != self.allowed:
            _deny("request outside designated model endpoint")
        token = _MODEL_URL.set(self.allowed)
        try:
            # Fully buffer the nonstreaming model response while the allowance
            # is active. Do not follow redirects or use environment proxies.
            response = self.transport.handle_request(request)
            response.read()
            response.close()
            return response
        finally:
            _MODEL_URL.reset(token)

    def close(self) -> None:
        self.transport.close()
