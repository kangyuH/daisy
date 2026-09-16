from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional


def lark_bin() -> str:
    lark = shutil.which("lark-cli")
    if not lark:
        raise RuntimeError("lark-cli not found in PATH")
    return lark


def cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("LARKSUITE_CLI_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("LARKSUITE_CLI_NO_SKILLS_NOTIFIER", "1")
    return env


class LarkCliError(RuntimeError):
    def __init__(self, message: str, *, returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_cli(
    args: list[str],
    *,
    timeout: float = 120,
    input_text: Optional[str] = None,
) -> dict[str, Any]:
    """Run lark-cli and parse JSON stdout. Raises LarkCliError on failure."""
    cmd = [lark_bin(), *args]
    if "--json" not in cmd and not any(a.startswith("--format") for a in cmd):
        cmd.append("--json")

    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        env=cli_env(),
        timeout=timeout,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise LarkCliError(
            f"lark-cli exited {proc.returncode}: {stderr or stdout[:500]}",
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    if not stdout:
        return {"ok": True, "data": None, "raw": ""}

    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, dict):
            return parsed
        return {"ok": True, "data": parsed}
    except json.JSONDecodeError:
        last: Optional[dict[str, Any]] = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                last = obj
        if last is not None:
            return last
        raise LarkCliError(
            "lark-cli returned non-JSON stdout",
            returncode=0,
            stdout=stdout,
            stderr=stderr,
        )


async def run_cli_async(
    args: list[str],
    *,
    timeout: float = 120,
    input_text: Optional[str] = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(run_cli, args, timeout=timeout, input_text=input_text)


def sync_cli_secret(app_id: str, app_secret: str, *, profile_name: str) -> None:
    """Sync app secret into a named lark-cli profile (append; does not wipe other apps)."""
    if not profile_name or profile_name == app_id:
        raise ValueError("profile_name must be a non-empty name different from app_id")
    proc = subprocess.run(
        [
            lark_bin(),
            "config",
            "init",
            "--app-id",
            app_id,
            "--app-secret-stdin",
            "--brand",
            "feishu",
            "--name",
            profile_name,
        ],
        input=app_secret,
        text=True,
        capture_output=True,
        env=cli_env(),
        timeout=30,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"lark-cli config init failed (exit {proc.returncode}): {err}")


def clear_stale_bus(app_id: str) -> list[str]:
    """Remove leftover bus lock/socket if the recorded pid is dead (NFS orphan)."""
    notes: list[str] = []
    bus_dir = Path.home() / ".lark-cli" / "events" / app_id
    pid_file = bus_dir / "bus.pid"
    if not pid_file.is_file():
        return notes

    raw = pid_file.read_text(encoding="utf-8", errors="replace").splitlines()
    pid_s = raw[0].strip() if raw else ""
    if pid_s.isdigit() and Path(f"/proc/{pid_s}").exists():
        notes.append(f"bus pid {pid_s} still alive for {app_id}; not clearing")
        return notes

    for name in ("bus.alive.lock", "bus.fork.lock", "bus.pid", "bus.sock"):
        p = bus_dir / name
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            notes.append(f"could not remove {p}: {exc}")
    notes.append(f"cleared stale event bus files for {app_id} (old pid={pid_s or 'unknown'})")
    return notes
