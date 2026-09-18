from __future__ import annotations

STATUS_NOTED = "noted"
STATUS_WAITING_HUMAN = "waiting_human"
STATUS_WAITING_EXTERNAL = "waiting_external"
STATUS_AGENT_RUNNING = "agent_running"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

# Canonical meanings — keep in sync with README「任务约定」.
STATUS_DESCRIPTIONS: dict[str, str] = {
    STATUS_NOTED: "已记下，尚未开始处理（新建默认）",
    STATUS_WAITING_HUMAN: "等人工处理或确认（含尚未动手，或 agent 已完成一轮后等人确认/决策）",
    STATUS_WAITING_EXTERNAL: "正在等协助方提供关键资源",
    STATUS_AGENT_RUNNING: "agent 正在处理",
    STATUS_BLOCKED: "阻塞（依赖未满足或条件不成立，暂无法推进）",
    STATUS_DONE: "完成且已反馈",
    STATUS_CANCELLED: "任务取消",
}

TASK_STATUSES = frozenset(STATUS_DESCRIPTIONS)

TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_CANCELLED})

KIND_READONLY = "readonly"
KIND_OPERATIONAL = "operational"

KIND_DESCRIPTIONS: dict[str, str] = {
    KIND_READONLY: "不涉及业务侧写操作：信息收集、查口径、查根因等；可写 scratch//tmp",
    KIND_OPERATIONAL: "需要执行操作（改数、跑 job、提 PR、动配置等）",
}

TASK_KINDS = frozenset(KIND_DESCRIPTIONS)

EVENT_CREATED = "created"
EVENT_NOTE = "note"
EVENT_REQUIREMENT = "requirement"

EVENT_TYPE_DESCRIPTIONS: dict[str, str] = {
    EVENT_CREATED: "任务创建",
    EVENT_NOTE: "跟进备注（催办、说明、进度等）",
    EVENT_REQUIREMENT: "追加/变更需求",
}

FOLLOWUP_EVENT_TYPES = frozenset({EVENT_NOTE, EVENT_REQUIREMENT})

# Task clue indexes (cursor session / lark / git / …) — not followups.
CLUE_KIND_CURSOR_SESSION = "cursor_session"
CLUE_KIND_LARK_THREAD = "lark_thread"
CLUE_KIND_LARK_MESSAGE = "lark_message"
CLUE_KIND_GIT_REPO = "git_repo"

CLUE_KIND_DESCRIPTIONS: dict[str, str] = {
    CLUE_KIND_CURSOR_SESSION: "Cursor Agent 会话（ref_key = CURSOR_CONVERSATION_ID）",
    CLUE_KIND_LARK_THREAD: "飞书话题 thread（占位）",
    CLUE_KIND_LARK_MESSAGE: "飞书消息（占位）",
    CLUE_KIND_GIT_REPO: "代码仓库引用（占位）",
}

CLUE_KINDS = frozenset(CLUE_KIND_DESCRIPTIONS)

CLUE_RELEVANCE_PRIMARY = "primary"
CLUE_RELEVANCE_RELATED = "related"
CLUE_RELEVANCE_WEAK = "weak"

# Higher index = stronger. Used for sort and demotion guards.
CLUE_RELEVANCE_RANK: dict[str, int] = {
    CLUE_RELEVANCE_WEAK: 0,
    CLUE_RELEVANCE_RELATED: 1,
    CLUE_RELEVANCE_PRIMARY: 2,
}

CLUE_RELEVANCE_DESCRIPTIONS: dict[str, str] = {
    CLUE_RELEVANCE_PRIMARY: "主线索：就是为这个 task 开的",
    CLUE_RELEVANCE_RELATED: "有实质关联，但不是主场",
    CLUE_RELEVANCE_WEAK: "零星提及 / 事后从历史里扒出",
}

CLUE_RELEVANCES = frozenset(CLUE_RELEVANCE_DESCRIPTIONS)
DEFAULT_CLUE_RELEVANCE = CLUE_RELEVANCE_RELATED


class TaskValidationError(ValueError):
    """Invalid kind/status/event_type for task APIs."""


class TaskConflictError(ValueError):
    """Illegal state transition (e.g. change status after terminal)."""


def validate_kind(kind: str) -> str:
    k = (kind or "").strip()
    if k not in TASK_KINDS:
        raise TaskValidationError(
            f"invalid kind {kind!r}; allowed: {sorted(TASK_KINDS)}"
        )
    return k


def validate_status(status: str) -> str:
    s = (status or "").strip()
    if s not in TASK_STATUSES:
        raise TaskValidationError(
            f"invalid status {status!r}; allowed: {sorted(TASK_STATUSES)}"
        )
    return s


def validate_followup_event_type(event_type: str) -> str:
    e = (event_type or "").strip()
    if e not in FOLLOWUP_EVENT_TYPES:
        raise TaskValidationError(
            f"invalid event_type {event_type!r}; allowed: {sorted(FOLLOWUP_EVENT_TYPES)}"
        )
    return e


def validate_clue_kind(kind: str) -> str:
    k = (kind or "").strip()
    if k not in CLUE_KINDS:
        raise TaskValidationError(
            f"invalid clue kind {kind!r}; allowed: {sorted(CLUE_KINDS)}"
        )
    return k


def validate_clue_relevance(relevance: str) -> str:
    r = (relevance or "").strip()
    if r not in CLUE_RELEVANCES:
        raise TaskValidationError(
            f"invalid clue relevance {relevance!r}; "
            f"allowed: {sorted(CLUE_RELEVANCES)}"
        )
    return r


def clue_relevance_rank(relevance: str) -> int:
    return CLUE_RELEVANCE_RANK[validate_clue_relevance(relevance)]


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
