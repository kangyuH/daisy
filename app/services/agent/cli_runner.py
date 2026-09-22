from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Optional


def agent_bin() -> str:
    path = shutil.which("agent")
    if not path:
        raise RuntimeError("cursor agent CLI not found in PATH (expected `agent`)")
    return path


def build_agent_argv(
    *,
    workspace: Path,
    prompt: str,
    phase: str,
    session_id: Optional[str] = None,
    add_dirs: Optional[list[str]] = None,
    force: bool = False,
) -> list[str]:
    cmd = [
        agent_bin(),
        "-p",
        "--trust",
        "--workspace",
        str(workspace),
        "--output-format",
        "stream-json",
    ]
    if phase == "plan":
        cmd.extend(["--mode", "plan"])
    if session_id:
        cmd.extend(["--resume", session_id])
    for d in add_dirs or []:
        cmd.extend(["--add-dir", d])
    if force and phase == "exec":
        cmd.append("--force")
    cmd.append(prompt)
    return cmd


def read_proc_start(pid: int) -> Optional[str]:
    """Return /proc/<pid>/stat starttime field as string, or None."""
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm may contain spaces/parens; starttime is field 22 after closing ')'.
    close = text.rfind(")")
    if close < 0:
        return None
    parts = text[close + 1 :].split()
    if len(parts) < 20:
        return None
    return parts[19].strip() or None


def process_identity_matches(
    *,
    pid: Optional[int],
    pgid: Optional[int],
    proc_start: Optional[str],
) -> str:
    """
    Check whether the recorded agent process is still the same.

    Returns: alive | dead | unknown
    """
    recorded = (proc_start or "").strip()
    if pid is None or pgid is None or not recorded:
        return "unknown"
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    current = read_proc_start(int(pid))
    if current is None:
        return "unknown"
    if current != recorded:
        return "dead"
    try:
        if os.getpgid(int(pid)) != int(pgid):
            return "dead"
    except ProcessLookupError:
        return "dead"
    except OSError:
        return "unknown"
    return "alive"


def spawn_agent(
    argv: list[str],
    *,
    cwd: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> tuple[subprocess.Popen, int]:
    """
    Start agent in a new process group. Returns (proc, pgid).
    stdout/stderr go to log_path if set, else DEVNULL.
    """
    log_f = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Per-run logs are exclusive; overwrite so rounds never share a file.
        log_f = open(log_path, "wb")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_f else subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except Exception:
        if log_f:
            log_f.close()
        raise
    if log_f:
        # Keep file open for process lifetime; close after wait in caller via proc
        proc._agent_log_f = log_f  # type: ignore[attr-defined]
    pgid = os.getpgid(proc.pid)
    return proc, pgid


def wait_agent(
    proc: subprocess.Popen,
    *,
    timeout: Optional[float] = None,
) -> int:
    try:
        return int(proc.wait(timeout=timeout))
    finally:
        log_f = getattr(proc, "_agent_log_f", None)
        if log_f is not None:
            try:
                log_f.close()
            except Exception:
                pass


def kill_process_group(pgid: int, *, grace_sec: float = 5.0) -> None:
    if pgid <= 0:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    import time

    deadline = time.time() + grace_sec
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return


def parse_session_id_from_log(log_path: Path) -> Optional[str]:
    """Scan stream-json log for session_id / chatId."""
    if not log_path.is_file():
        return None
    last: Optional[str] = None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("session_id", "sessionId", "chat_id", "chatId"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                last = val.strip()
        # nested
        for nest_key in ("result", "message", "system"):
            nest = obj.get(nest_key)
            if isinstance(nest, dict):
                for key in ("session_id", "sessionId", "chat_id", "chatId"):
                    val = nest.get(key)
                    if isinstance(val, str) and val.strip():
                        last = val.strip()
    return last


def run_agent_blocking(
    *,
    workspace: Path,
    prompt: str,
    phase: str,
    session_id: Optional[str] = None,
    add_dirs: Optional[list[str]] = None,
    force: bool = False,
    log_path: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Spawn agent, wait, return {returncode, pgid, session_id, log_path}."""
    argv = build_agent_argv(
        workspace=workspace,
        prompt=prompt,
        phase=phase,
        session_id=session_id,
        add_dirs=add_dirs,
        force=force,
    )
    log = log_path or (workspace / "scratch" / f"agent_{phase}.log")
    proc, pgid = spawn_agent(argv, cwd=workspace, log_path=log)
    try:
        code = wait_agent(proc, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(pgid)
        wait_agent(proc, timeout=30)
        return {
            "returncode": -1,
            "pgid": pgid,
            "session_id": parse_session_id_from_log(log) or session_id,
            "log_path": str(log),
            "timed_out": True,
            "argv": argv,
        }
    sid = parse_session_id_from_log(log) or session_id
    return {
        "returncode": code,
        "pgid": pgid,
        "session_id": sid,
        "log_path": str(log),
        "timed_out": False,
        "argv": argv,
    }
