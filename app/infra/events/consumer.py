from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Set

import httpx

from app.core.settings import calibration_chat_id, gateway_base_url
from app.services.bots.models import BotConfig
from app.services.im.mentions import extract_mention_open_ids, should_enqueue, normalize_inbound_event
from app.infra.lark.cli import clear_stale_bus, cli_env, lark_bin, sync_cli_secret


TZ_CN = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def _log(bot_id: str, msg: str) -> None:
    print(f"[{_now_iso()}] [{bot_id}] {msg}", flush=True)


class EventConsumer:
    def __init__(
        self,
        bot: BotConfig,
        *,
        allowed_chats: Optional[Set[str]] = None,
    ) -> None:
        self.bot = bot
        self.self_open_id = bot.self_open_id
        self.allowed_chats: Set[str] = set(allowed_chats or bot.allowed_chats or [])
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.ready = False
        self.last_error: Optional[str] = None
        self.enqueued_count = 0
        self.skipped_count = 0
        self._calib_chat = calibration_chat_id()
        self._calib_waiters: list[asyncio.Future] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def set_allowed_chats(self, chats: list[str] | Set[str]) -> None:
        self.allowed_chats = set(chats)

    def set_open_ids(
        self,
        *,
        open_id: Optional[str] = None,
        self_open_id: Optional[str] = None,
    ) -> None:
        if open_id is not None:
            self.bot.open_id = open_id
        if self_open_id is not None:
            self.bot.self_open_id = self_open_id
            self.self_open_id = self_open_id

    def status(self) -> dict:
        return {
            "id": self.bot.id,
            "name": self.bot.name,
            "app_id": self.bot.app_id,
            "open_id": self.bot.open_id,
            "self_open_id": self.self_open_id,
            "allowed_chats": sorted(self.allowed_chats),
            "profile": self.profile,
            "event_key": self.bot.event_key,
            "running": self.running,
            "ready": self.ready,
            "pid": self.proc.pid if self.proc and self.running else None,
            "last_error": self.last_error,
            "enqueued_count": self.enqueued_count,
            "skipped_count": self.skipped_count,
        }

    @property
    def profile(self) -> str:
        return self.bot.id

    async def prepare(self) -> None:
        sync_cli_secret(self.bot.app_id, self.bot.app_secret, profile_name=self.profile)
        for note in clear_stale_bus(self.bot.app_id):
            _log(self.bot.id, note)

    async def start(self, *, prepare: bool = True) -> None:
        if prepare:
            await self.prepare()
        self.ready = False
        self.last_error = None
        self.proc = await asyncio.create_subprocess_exec(
            lark_bin(),
            "event",
            "consume",
            self.bot.event_key,
            "--as",
            "bot",
            "--profile",
            self.profile,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=cli_env(),
        )
        _log(
            self.bot.id,
            f"started event consume pid={self.proc.pid} "
            f"app_id={self.bot.app_id} key={self.bot.event_key} "
            f"bot_open_id={self.bot.open_id} self_open_id={self.self_open_id}",
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def wait_ready(self, timeout: float = 45.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.ready:
                return
            if self.last_error and "failed_precondition" in (self.last_error or ""):
                raise RuntimeError(self.last_error)
            if self.last_error and "another event bus" in (self.last_error or ""):
                raise RuntimeError(self.last_error)
            if self.proc and self.proc.returncode is not None:
                raise RuntimeError(
                    f"event consume exited early code={self.proc.returncode}: {self.last_error}"
                )
            await asyncio.sleep(0.3)
        raise TimeoutError(f"bot {self.bot.id} event consume not ready within {timeout}s")

    async def wait_calibration_mentions(self, timeout: float = 30.0) -> list[dict]:
        """Wait for next message event in calibration chat; return mention dicts."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._calib_waiters.append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            if fut in self._calib_waiters:
                self._calib_waiters.remove(fut)

    def _notify_calib(self, mentions: list[dict]) -> None:
        waiters = list(self._calib_waiters)
        self._calib_waiters.clear()
        for fut in waiters:
            if not fut.done():
                fut.set_result(mentions)

    def _auth_headers(self) -> dict[str, str]:
        token = os.environ.get("GATEWAY_TOKEN", "").strip()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def _http_enqueue(self, payload: dict[str, Any], idempotency_key: str) -> None:
        url = f"{gateway_base_url()}/queue/inbound/enqueue"
        body = {"payload": payload, "idempotency_key": idempotency_key}
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(url, json=body, headers=self._auth_headers())
                if resp.status_code >= 400:
                    raise RuntimeError(f"enqueue HTTP {resp.status_code}: {resp.text[:500]}")
                self.enqueued_count += 1
                _log(
                    self.bot.id,
                    f"enqueued via HTTP key={idempotency_key} status={resp.status_code}",
                )
                return
            except Exception as exc:
                last_exc = exc
                self.last_error = str(exc)
                _log(self.bot.id, f"enqueue attempt {attempt} failed: {exc}")
                await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"enqueue failed after retries: {last_exc}")

    def _handle_event_obj(self, obj: dict[str, Any]) -> None:
        event_type = obj.get("type") or obj.get("event_type")
        header = obj.get("header")
        if not event_type and isinstance(header, dict):
            event_type = header.get("event_type")

        has_message = bool(
            obj.get("message_id")
            or obj.get("message")
            or (isinstance(obj.get("event"), dict) and obj["event"].get("message"))
            or (isinstance(obj.get("data"), dict) and obj["data"].get("message"))
        )
        if not has_message:
            if event_type and "message" in str(event_type):
                pass
            else:
                return

        chat_id = str(obj.get("chat_id") or "")
        mention_ids = extract_mention_open_ids(obj)

        # Calibration chat: feed waiters, never enqueue to business queue
        if chat_id and chat_id == self._calib_chat:
            mentions_raw = obj.get("mentions") or []
            if isinstance(mentions_raw, list) and mentions_raw and self._calib_waiters:
                self._notify_calib([m for m in mentions_raw if isinstance(m, dict)])
            _log(self.bot.id, f"calib event mentions={mention_ids} (not enqueued)")
            return

        if chat_id and chat_id not in self.allowed_chats:
            self.skipped_count += 1
            _log(self.bot.id, f"skip (chat not bound) chat_id={chat_id}")
            return

        if not should_enqueue(
            mention_ids,
            self_open_id=self.self_open_id,
            bot_open_id=self.bot.open_id,
        ):
            self.skipped_count += 1
            _log(
                self.bot.id,
                f"skip (no @self/@bot) mentions={mention_ids}",
            )
            return

        payload = normalize_inbound_event(self.bot, obj)
        payload["matched_mentions"] = mention_ids
        mid = str(payload.get("message_id") or payload.get("event_id") or "")
        idem = f"{self.bot.id}:{mid}" if mid else f"{self.bot.id}:{_now_iso()}"
        asyncio.create_task(self._safe_enqueue(payload, idem))

    async def _safe_enqueue(self, payload: dict[str, Any], idem: str) -> None:
        try:
            await self._http_enqueue(payload, idem)
        except Exception as exc:
            self.last_error = str(exc)
            _log(self.bot.id, f"enqueue error: {exc}")

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            print(f"[{_now_iso()}] [{self.bot.id}] {text}", flush=True)
            text_s = text.strip()
            if not text_s.startswith("{"):
                continue
            try:
                obj = json.loads(text_s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                self._handle_event_obj(obj)

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if "[event] ready" in text:
                self.ready = True
            if "online_instance_cnt=" in text and "online_instance_cnt=0" not in text:
                self.last_error = text
            if '"ok": false' in text and "online_instance_cnt=0" not in text:
                self.last_error = text
            _log(self.bot.id, f"cli: {text}")

    async def stop(self) -> None:
        if not self.proc:
            return
        proc = self.proc
        if proc.returncode is None:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        _log(self.bot.id, f"event consume exited code={proc.returncode}")
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.proc = None
        self.ready = False
