from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from app.core.settings import agent_require_plan_before_exec
from app.services.agent.cli_runner import (
    build_agent_argv,
    kill_process_group,
    parse_session_id_from_log,
    spawn_agent,
    wait_agent,
)
from app.services.agent.constants import (
    MODE_AUTO_RESEARCH,
    MODE_EXEC,
    MODE_PLAN,
    MODE_RESEARCH,
    cli_phase_for_mode,
    is_auto_research_mode,
    is_interactive_mode,
    normalize_mode,
)
from app.services.agent.project_knowledge import (
    copy_project_yaml_to_workspace,
    existing_repo_paths,
    format_project_brief,
    load_project_yaml,
)
from app.services.agent.prompts import build_user_prompt, system_prompt_for_mode
from app.services.agent.research_board import (
    format_clues_followup,
    format_research_summary_followup,
)
from app.services.agent.plan_board import (
    extract_plan_markdown_from_log,
    format_plan_copy_followup,
    format_plan_missing_followup,
    persist_plan_markdown,
)
from app.services.im.outbound import reply_message
from app.services.im.reactions import add_typing_reaction, delete_reaction
from app.services.tasks.store import TaskStore


def _new_run_id() -> str:
    return secrets.token_hex(8)


def _file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "mtime_ns": 0, "sha256": ""}
    try:
        st = path.stat()
        data = path.read_bytes()
        return {
            "exists": True,
            "mtime_ns": int(st.st_mtime_ns),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    except OSError:
        return {"exists": False, "mtime_ns": 0, "sha256": ""}


def _file_changed_since(path: Path, before: dict[str, Any]) -> bool:
    after = _file_fingerprint(path)
    if not after["exists"]:
        return False
    if not before.get("exists"):
        return True
    return after["sha256"] != before.get("sha256") or after["mtime_ns"] > int(
        before.get("mtime_ns") or 0
    )


async def clear_task_typing_reaction(store: TaskStore, task_id: int) -> None:
    """Remove Typing emoji if any (default CLI app: profile=None)."""
    tid = int(task_id)
    task = store.get_task(tid, events_limit=1)
    if not task:
        return
    message_id = str(task.get("agent_typing_message_id") or "").strip()
    reaction_id = str(task.get("agent_typing_reaction_id") or "").strip()
    if message_id or reaction_id:
        try:
            store.update_agent_runtime(tid, clear_typing=True)
        except Exception:
            pass
    if not (message_id and reaction_id):
        return
    try:
        await delete_reaction(
            message_id=message_id,
            reaction_id=reaction_id,
            profile=None,
        )
    except Exception as exc:
        print(
            f"[agent] typing reaction delete failed task={tid}: {exc}",
            flush=True,
        )


async def mark_task_typing_reaction(
    store: TaskStore,
    task_id: int,
    *,
    message_id: str,
) -> Optional[str]:
    """Add Typing on message_id using CLI default app (no --profile)."""
    mid = (message_id or "").strip()
    if not mid:
        return None
    try:
        rid = await add_typing_reaction(message_id=mid, profile=None)
    except Exception as exc:
        print(
            f"[agent] typing reaction create failed task={task_id}: {exc}",
            flush=True,
        )
        return None
    try:
        store.update_agent_runtime(
            int(task_id),
            agent_typing_message_id=mid,
            agent_typing_reaction_id=rid,
            agent_typing_bot_id="",  # NULL — default app
        )
    except Exception as exc:
        print(
            f"[agent] typing reaction persist failed task={task_id}: {exc}",
            flush=True,
        )
        try:
            await delete_reaction(
                message_id=mid, reaction_id=rid, profile=None
            )
        except Exception:
            pass
        return None
    return rid


def _task_dir(store: TaskStore, task: dict[str, Any]) -> Path:
    raw = str(task.get("workspace_path") or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = store.workspace.root / str(task["id"])
        return p
    return store.workspace.task_dir(int(task["id"]))


def _followup(
    store: TaskStore,
    task_id: int,
    message: str,
    *,
    status: Optional[str] = None,
    actor: str = "agent",
) -> dict[str, Any]:
    return store.add_followup(
        task_id,
        message=message,
        event_type="note",
        status=status,
        actor=actor,
    )


async def _sync_followup_wrap(board_sync: Any, wrap: Optional[dict[str, Any]]) -> None:
    if board_sync is None or not isinstance(wrap, dict):
        return
    task_obj = wrap.get("task")
    event_obj = wrap.get("event")
    if task_obj and event_obj:
        try:
            await board_sync.sync_followup(task_obj, event_obj)
        except Exception:
            pass


async def _board_only(
    board_sync: Any,
    task: dict[str, Any],
    text: str,
    *,
    idempotency_key: str,
) -> Optional[dict[str, Any]]:
    if board_sync is None:
        return None
    try:
        return await board_sync.post_board_message(
            task, text, idempotency_key=idempotency_key
        )
    except Exception as exc:
        print(f"[agent] board-only post failed: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}


def prepare_workspace(
    store: TaskStore, task: dict[str, Any]
) -> tuple[Path, Optional[dict[str, Any]], list[str], str]:
    """Copy project yaml, return (task_dir, project, add_dirs, brief)."""
    d = _task_dir(store, task)
    d.mkdir(parents=True, exist_ok=True)
    (d / "scratch").mkdir(exist_ok=True)
    (d / "scratch" / "runs").mkdir(exist_ok=True)
    (d / "artifacts").mkdir(exist_ok=True)
    pid = str(task.get("project_id") or "").strip() or None
    copy_project_yaml_to_workspace(d, pid)
    project = load_project_yaml(pid) if pid else None
    repos = existing_repo_paths(project)
    add_dirs = [r["path"] for r in repos]
    brief = format_project_brief(project)
    if not pid:
        brief = "项目未绑定 / 无仓库线索。调研置信度应标为低。"
    elif not add_dirs:
        brief = (
            format_project_brief(project)
            + "\n注意：yaml 有 project_id 但本地无可用 repos.path。"
        )
    return d, project, add_dirs, brief


def kill_task_agent(store: TaskStore, task_id: int) -> dict[str, Any]:
    task = store.get_task(int(task_id), events_limit=1)
    if not task:
        raise KeyError(f"task {task_id} not found")
    pgid = task.get("agent_pgid")
    run_id = str(task.get("agent_run_id") or "").strip() or None
    killed = False
    if pgid is not None:
        try:
            kill_process_group(int(pgid))
            killed = True
        except Exception as exc:
            store.release_agent_run(int(task_id), force=True)
            return {
                "ok": False,
                "killed": False,
                "error": str(exc),
                "task_id": task_id,
            }
    store.release_agent_run(int(task_id), force=True)
    return {
        "ok": True,
        "killed": killed,
        "pgid": pgid,
        "run_id": run_id,
        "task_id": task_id,
    }


def _begin_phase(
    store: TaskStore,
    task_id: int,
    *,
    mode: str,
    rest: str = "",
    source: str = "manual",
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Claim run, prepare workspace, spawn CLI.
    Returns a handle dict for _finish_phase (includes proc) or rejected.
    Does NOT write interactive lifecycle followups (caller / finish handles channels).
    """
    tid = int(task_id)
    mode_n = normalize_mode(mode)
    phase = cli_phase_for_mode(mode_n)
    rid = (run_id or "").strip() or _new_run_id()

    task = store.get_task(tid, events_limit=5)
    if not task:
        raise KeyError(f"task {tid} not found")

    if mode_n == MODE_EXEC and agent_require_plan_before_exec():
        d0 = _task_dir(store, task)
        if not (d0 / "plan.md").is_file():
            return {
                "ok": False,
                "rejected": True,
                "reason": "plan_required",
                "spawned": False,
                "mode": mode_n,
                "message": (
                    "/exec 被拒绝：AGENT_REQUIRE_PLAN_BEFORE_EXEC=true "
                    "且工作区无 plan.md。请先 /plan。"
                ),
            }

    claim = store.try_claim_agent_run(tid, mode=mode_n, run_id=rid)
    if not claim.get("ok"):
        t = claim.get("task") or task
        return {
            "ok": False,
            "rejected": True,
            "reason": "already_running",
            "spawned": False,
            "mode": mode_n,
            "message": (
                f"任务已有 agent 在跑"
                f"（run={t.get('agent_run_id') or ''} "
                f"pgid={t.get('agent_pgid') or ''}）。"
                "请先 /stop 再发口令。"
            ),
        }

    claimed = True
    try:
        d, _project, add_dirs, brief = prepare_workspace(store, task)
        plan_exists = (d / "plan.md").is_file()
        plan_fp = _file_fingerprint(d / "plan.md")
        research_fp = _file_fingerprint(d / "research.md")
        started_at = time.time()

        for path in add_dirs:
            try:
                store.upsert_clue(
                    tid,
                    kind="git_repo",
                    ref_key=path,
                    one_liner=f"research add-dir: {path}",
                    relevance="related",
                    actor="agent",
                )
            except Exception:
                pass

        start_followup = None
        clues_event = None
        if is_auto_research_mode(mode_n):
            start_msg = (
                f"[{mode_n}] 已开始（source={source} run={rid}）。\n"
                f"project_id=`{task.get('project_id') or ''}`\n"
                f"repos: {', '.join(add_dirs) if add_dirs else '(none)'}"
            )
            start_followup = _followup(
                store, tid, start_msg, status="agent_running"
            )
            clues_event = _followup(
                store,
                tid,
                format_clues_followup(
                    project_id=str(task.get("project_id") or "") or None,
                    add_dirs=add_dirs,
                    brief=brief,
                ),
            )

        sys_p = system_prompt_for_mode(mode_n)
        user_p = build_user_prompt(
            mode=mode_n,
            task=task,
            project_brief=brief,
            rest=rest,
            plan_exists=plan_exists,
            source=source,
            run_id=rid,
        )
        full_prompt = f"{sys_p}\n\n---\n\n{user_p}"

        log_path = d / "scratch" / "runs" / f"{rid}_{mode_n}.jsonl"
        session_id = str(task.get("agent_session_id") or "").strip() or None

        argv = build_agent_argv(
            workspace=d,
            prompt=full_prompt,
            phase=phase,
            session_id=session_id,
            add_dirs=add_dirs,
            force=(mode_n == MODE_EXEC),
        )
        try:
            proc, pgid = spawn_agent(argv, cwd=d, log_path=log_path)
        except Exception as exc:
            store.release_agent_run(tid, run_id=rid)
            claimed = False
            return {
                "ok": False,
                "rejected": True,
                "reason": "spawn_failed",
                "spawned": False,
                "mode": mode_n,
                "run_id": rid,
                "message": f"[{mode_n}] spawn 失败：{exc}",
            }

        store.attach_agent_pgid(tid, run_id=rid, pgid=pgid)

        return {
            "ok": True,
            "spawned": True,
            "tid": tid,
            "mode": mode_n,
            "phase": phase,
            "source": source,
            "run_id": rid,
            "add_dirs": add_dirs,
            "d": d,
            "log_path": log_path,
            "session_id": session_id,
            "proc": proc,
            "pgid": pgid,
            "argv": argv,
            "start_event": start_followup,
            "clues_event": clues_event,
            "plan_fp": plan_fp,
            "research_fp": research_fp,
            "started_at": started_at,
            "interactive": is_interactive_mode(mode_n),
        }
    except Exception:
        if claimed:
            try:
                store.release_agent_run(tid, run_id=rid)
            except Exception:
                pass
        raise


def _finish_phase(store: TaskStore, begun: dict[str, Any]) -> dict[str, Any]:
    """Wait for spawned CLI and write summary / end followups (auto only)."""
    tid = int(begun["tid"])
    mode = str(begun["mode"])
    source = str(begun.get("source") or "manual")
    rid = str(begun["run_id"])
    proc = begun["proc"]
    pgid = int(begun["pgid"])
    d: Path = begun["d"]
    log_path: Path = begun["log_path"]
    session_id = begun.get("session_id")
    add_dirs: list[str] = list(begun.get("add_dirs") or [])
    argv = begun.get("argv") or []
    start = begun.get("start_event")
    clues_event = begun.get("clues_event")
    plan_fp = begun.get("plan_fp") or {"exists": False}
    research_fp = begun.get("research_fp") or {"exists": False}
    interactive = bool(begun.get("interactive"))

    try:
        code = wait_agent(proc, timeout=None)
    except Exception as exc:
        kill_process_group(pgid)
        try:
            store.release_agent_run(tid, run_id=rid)
        except Exception:
            store.release_agent_run(tid, force=True)
        end = None
        if not interactive:
            end = _followup(
                store,
                tid,
                f"[{mode}] 异常退出：{exc}",
                status="waiting_human",
            )
        return {
            "ok": False,
            "mode": mode,
            "run_id": rid,
            "error": str(exc),
            "start_event": start,
            "clues_event": clues_event,
            "end_event": end,
            "board_end_text": f"[{mode}] 异常退出：{exc}",
            "interactive": interactive,
        }

    new_sid = parse_session_id_from_log(log_path) or session_id
    try:
        store.release_agent_run(tid, run_id=rid)
    except Exception:
        store.release_agent_run(tid, force=True)
    if new_sid:
        try:
            store.update_agent_runtime(tid, agent_session_id=new_sid)
            store.upsert_clue(
                tid,
                kind="cursor_session",
                ref_key=new_sid,
                one_liner=f"agent {mode} session",
                relevance="primary",
                actor="agent",
            )
        except Exception:
            pass

    ok = code == 0
    research_summary_event = None
    plan_copy_event = None
    end = None
    board_end_text = None

    if mode == MODE_AUTO_RESEARCH:
        research_path = d / "research.md"
        produced = _file_changed_since(research_path, research_fp)
        missing = not produced
        research_summary_event = _followup(
            store,
            tid,
            format_research_summary_followup(
                research_path=research_path,
                returncode=int(code),
                missing=missing,
            ),
            status="waiting_human",
        )
        end_msg = (
            f"[auto_research] 结束 returncode={code} session=`{new_sid or ''}` "
            f"run=`{rid}`\n"
        )
        if missing:
            end_msg += "本轮未更新 research.md；未复用旧文件。"
        else:
            end_msg += f"调研三层摘要已写入台账 followup；全文见 `{research_path}`"
        if not add_dirs:
            end_msg += "\n注意：本轮无可用仓库线索，事实层置信度应偏低。"
        end = _followup(store, tid, end_msg)
    elif mode == MODE_RESEARCH:
        board_end_text = (
            f"[research] 结束 returncode={code} session=`{new_sid or ''}` "
            f"run=`{rid}`\nlog: `{log_path}`"
        )
    elif mode == MODE_PLAN:
        plan_path = d / "plan.md"
        plan_text = None
        extracted = extract_plan_markdown_from_log(log_path)
        if extracted:
            persist_plan_markdown(d, extracted)
            plan_text = extracted
            plan_path = d / "plan.md"
        elif _file_changed_since(plan_path, plan_fp):
            try:
                plan_text = plan_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except OSError:
                plan_text = None
        if plan_text:
            plan_copy_event = _followup(
                store,
                tid,
                format_plan_copy_followup(
                    plan_text=plan_text, plan_path=plan_path
                ),
                # interactive plan: do not auto-flip task status
                status=None if interactive else "waiting_human",
            )
            board_end_text = (
                f"[plan] 结束 returncode={code} session=`{new_sid or ''}` "
                f"run=`{rid}`\nplan.md 已落盘并同步台账；全文见 `{plan_path}`"
            )
            if not interactive:
                end = _followup(store, tid, board_end_text)
        else:
            missing_msg = format_plan_missing_followup(
                log_path=log_path, returncode=int(code)
            )
            if interactive:
                board_end_text = missing_msg
            else:
                plan_copy_event = _followup(
                    store, tid, missing_msg, status="waiting_human"
                )
                end = _followup(
                    store,
                    tid,
                    f"[plan] 结束 returncode={code} session=`{new_sid or ''}` "
                    f"run=`{rid}`\n未产出可同步的计划正文；见 `{log_path}`",
                )
    else:  # exec
        board_end_text = (
            f"[{mode}] 结束 returncode={code} session=`{new_sid or ''}` "
            f"run=`{rid}`\nlog: `{log_path}`"
        )
        if not interactive:
            end = _followup(
                store, tid, board_end_text, status="waiting_human"
            )

    return {
        "ok": ok,
        "mode": mode,
        "source": source,
        "run_id": rid,
        "returncode": code,
        "session_id": new_sid,
        "pgid": pgid,
        "log_path": str(log_path),
        "start_event": start,
        "clues_event": clues_event,
        "research_summary_event": research_summary_event,
        "plan_copy_event": plan_copy_event,
        "end_event": end,
        "board_end_text": board_end_text,
        "interactive": interactive,
        "argv_preview": argv[:12],
    }


def run_phase(
    store: TaskStore,
    task_id: int,
    *,
    mode: str = "",
    phase: str = "",
    rest: str = "",
    source: str = "manual",
    trigger: str = "",
    board_sync: Any = None,
) -> dict[str, Any]:
    """
    Run one agent phase synchronously (blocking CLI).
    Prefer `mode=`; `phase` kept for older call sites (research/plan/exec only).
    """
    del board_sync
    mode_n = (mode or "").strip() or (phase or "").strip()
    src = (source or trigger or "manual").strip() or "manual"
    begun = _begin_phase(
        store, task_id, mode=mode_n, rest=rest, source=src
    )
    if not begun.get("spawned"):
        return begun
    return _finish_phase(store, begun)


async def run_phase_async(
    store: TaskStore,
    task_id: int,
    *,
    mode: str = "",
    phase: str = "",
    rest: str = "",
    source: str = "manual",
    trigger: str = "",
    board_sync: Any = None,
    typing_message_id: Optional[str] = None,
) -> dict[str, Any]:
    mode_n = normalize_mode((mode or "").strip() or (phase or "").strip())
    src = (source or trigger or "manual").strip() or "manual"
    rid = _new_run_id()
    typing_marked = False

    # Claim first (via _begin); typing after claim inside begin path —
    # we mark typing after successful claim by peeking: mark before begin
    # only when we already know message; but claim is inside begin.
    # So: begin first without typing, then if spawned mark typing? Plan says
    # claim then typing. We'll mark after claim succeeds inside async wrapper
    # by splitting: call begin which claims+spawns, then we can't mark before
    # spawn easily without refactoring begin further.
    # Compromise matching plan: claim is inside begin; mark typing right after
    # claim would need begin to return before spawn. For simplicity mark on
    # typing_message_id / board root AFTER begin returns spawned=True is late.
    # Better: mark after claim by doing claim in async then spawn.
    #
    # Current _begin_phase does claim+spawn atomically. Mark typing after
    # successful claim is ideal; we'll mark using typing_message_id once
    # begun reports spawned (process already running — acceptable) OR
    # pre-mark only for known message after a lightweight claim.
    #
    # Implement: claim in async here, then mark typing, then spawn via
    # internal path. To minimize churn, mark typing immediately after
    # successful begin spawn using typing_message_id / board root — still
    # cleared in finally. For auto without typing_message_id, resolve board
    # root before begin.

    task0 = store.get_task(int(task_id), events_limit=1)
    if not task0:
        raise KeyError(f"task {task_id} not found")

    mid = (typing_message_id or "").strip()
    if not mid and is_auto_research_mode(mode_n):
        if board_sync is not None and not task0.get("board_message_id"):
            try:
                await board_sync.ensure_board(task0)
            except Exception as exc:
                print(
                    f"[agent] ensure_board for typing failed task={task_id}: {exc}",
                    flush=True,
                )
            task0 = store.get_task(int(task_id), events_limit=1) or task0
        mid = str(task0.get("board_message_id") or "").strip()

    try:
        begun = await asyncio.to_thread(
            _begin_phase,
            store,
            task_id,
            mode=mode_n,
            rest=rest,
            source=src,
            run_id=rid,
        )

        if begun.get("rejected"):
            # Reject before/during spawn: notify via board for interactive.
            msg = begun.get("message") or f"[{mode_n}] 被拒绝：{begun.get('reason')}"
            if is_interactive_mode(mode_n):
                fresh = store.get_task(int(task_id), events_limit=1) or task0
                await _board_only(
                    board_sync,
                    fresh,
                    msg,
                    idempotency_key=f"run-{rid}-reject",
                )
            else:
                # auto path: write followup for plan_required etc.
                if begun.get("reason") == "plan_required":
                    fu = _followup(
                        store, int(task_id), msg, status="waiting_human"
                    )
                    await _sync_followup_wrap(board_sync, fu)
                    begun["event"] = fu
                elif begun.get("reason") == "spawn_failed":
                    fu = _followup(
                        store, int(task_id), msg, status="waiting_human"
                    )
                    await _sync_followup_wrap(board_sync, fu)
                    begun["event"] = fu
            return begun

        if mid and begun.get("spawned"):
            rid_got = await mark_task_typing_reaction(
                store, int(task_id), message_id=mid
            )
            typing_marked = bool(rid_got)

        await _sync_followup_wrap(board_sync, begun.get("start_event"))
        await _sync_followup_wrap(board_sync, begun.get("clues_event"))

        if is_interactive_mode(mode_n) and begun.get("spawned"):
            fresh = store.get_task(int(task_id), events_limit=1) or task0
            await _board_only(
                board_sync,
                fresh,
                f"[{mode_n}] 已开始（source={src} run=`{rid}`）。",
                idempotency_key=f"run-{rid}-start",
            )

        if not begun.get("spawned"):
            return begun

        result = await asyncio.to_thread(_finish_phase, store, begun)
        for key in (
            "research_summary_event",
            "plan_copy_event",
            "end_event",
        ):
            await _sync_followup_wrap(board_sync, result.get(key))

        board_end = result.get("board_end_text")
        if board_end and result.get("interactive"):
            fresh = store.get_task(int(task_id), events_limit=1) or task0
            await _board_only(
                board_sync,
                fresh,
                str(board_end),
                idempotency_key=f"run-{rid}-end",
            )
            # plan copy followup already synced above when present
        return result
    finally:
        if typing_marked:
            await clear_task_typing_reaction(store, int(task_id))


async def handle_ledger_command(
    store: TaskStore,
    *,
    task: dict[str, Any],
    cmd: str,
    rest: str,
    message_id: str,
    board_sync: Any = None,
    reply_root_message_id: Optional[str] = None,
) -> dict[str, Any]:
    """Handle a validated ledger slash command (spawn in-process / stop)."""
    tid = int(task["id"])
    last = str(task.get("agent_last_command_message_id") or "")
    if message_id and message_id == last:
        return {"ok": True, "deduped": True, "reason": "same_message_id"}

    store.update_agent_runtime(
        tid, agent_last_command_message_id=message_id or None
    )

    if cmd == "status":
        msg = (
            f"status: task=#{tid} status=`{task.get('status')}` "
            f"mode=`{task.get('agent_phase') or ''}` "
            f"run=`{task.get('agent_run_id') or ''}` "
            f"pgid=`{task.get('agent_pgid') or ''}` "
            f"session=`{task.get('agent_session_id') or ''}`"
        )
        await _board_only(
            board_sync,
            task,
            msg,
            idempotency_key=f"status-{tid}-{message_id}"[:50],
        )
        return {"ok": True, "cmd": "status"}

    if cmd == "stop":
        out = await asyncio.to_thread(kill_task_agent, store, tid)
        await clear_task_typing_reaction(store, tid)
        await _board_only(
            board_sync,
            task,
            f"/stop 已处理：{out}",
            idempotency_key=f"stop-{tid}-{message_id}"[:50],
        )
        if reply_root_message_id:
            try:
                await reply_message(
                    message_id=reply_root_message_id,
                    text=f"/stop 已处理：{out}",
                    reply_in_thread=True,
                    as_user=False,
                    profile=None,
                    markdown=True,
                    idempotency_key=f"stop-ack-{tid}-{message_id}"[:50],
                )
            except Exception:
                pass
        return out

    if cmd in (MODE_RESEARCH, MODE_PLAN, MODE_EXEC):
        if not (rest or "").strip():
            msg = (
                f"/{cmd} 被拒绝：口令后须带具体指令"
                f"（例如 `/{cmd} 说明一下这个 tag`）。"
            )
            await _board_only(
                board_sync,
                task,
                msg,
                idempotency_key=f"empty-{tid}-{message_id}"[:50],
            )
            return {
                "ok": False,
                "rejected": True,
                "reason": "empty_rest",
            }

        # Fast path hint only; claim is authoritative inside run_phase_async.
        fresh = store.get_task(tid, events_limit=1) or task
        if fresh.get("agent_run_id") or fresh.get("agent_pgid") is not None:
            msg = (
                f"/{cmd} 被拒绝：agent 仍在运行"
                f"（run=`{fresh.get('agent_run_id') or ''}` "
                f"pgid=`{fresh.get('agent_pgid') or ''}`），请先 /stop。"
            )
            await _board_only(
                board_sync,
                fresh,
                msg,
                idempotency_key=f"busy-{tid}-{message_id}"[:50],
            )
            return {"ok": False, "rejected": True, "reason": "already_running"}

        async def _bg() -> None:
            await run_phase_async(
                store,
                tid,
                mode=cmd,
                rest=rest,
                source="ledger",
                board_sync=board_sync,
                typing_message_id=message_id,
            )

        asyncio.create_task(_bg())
        await _board_only(
            board_sync,
            task,
            f"/{cmd} 已受理，正在拉起 Cursor CLI…",
            idempotency_key=f"ack-{tid}-{message_id}"[:50],
        )
        return {
            "ok": True,
            "accepted": True,
            "cmd": cmd,
            "mode": cmd,
        }

    await _board_only(
        board_sync,
        task,
        f"不支持的口令：/{cmd}",
        idempotency_key=f"unk-{tid}-{message_id}"[:50],
    )
    return {"ok": False, "rejected": True, "reason": "unknown_cmd"}
