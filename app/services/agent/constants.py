from __future__ import annotations

MODE_AUTO_RESEARCH = "auto_research"
MODE_RESEARCH = "research"
MODE_PLAN = "plan"
MODE_EXEC = "exec"

AGENT_MODES = frozenset(
    {MODE_AUTO_RESEARCH, MODE_RESEARCH, MODE_PLAN, MODE_EXEC}
)

# Ledger slash cmds (and CLI phase for plan/exec) — not including auto_research.
LEDGER_COMMANDS = frozenset(
    {MODE_RESEARCH, MODE_PLAN, MODE_EXEC, "stop", "status"}
)

# Backward-compat aliases used by older call sites / docs.
PHASE_RESEARCH = MODE_RESEARCH
PHASE_PLAN = MODE_PLAN
PHASE_EXEC = MODE_EXEC


def normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m not in AGENT_MODES:
        raise ValueError(f"invalid agent mode {mode!r}")
    return m


def cli_phase_for_mode(mode: str) -> str:
    """Cursor CLI phase: auto_research runs as research (no --mode plan)."""
    m = normalize_mode(mode)
    if m == MODE_AUTO_RESEARCH:
        return MODE_RESEARCH
    return m


def is_auto_research_mode(mode: str) -> bool:
    return (mode or "").strip().lower() == MODE_AUTO_RESEARCH


def is_interactive_mode(mode: str) -> bool:
    m = (mode or "").strip().lower()
    return m in {MODE_RESEARCH, MODE_PLAN, MODE_EXEC}
