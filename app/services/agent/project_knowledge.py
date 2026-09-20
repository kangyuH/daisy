from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from app.infra.db import ROOT

DEFAULT_PROJECTS_DIR = ROOT / "knowledge" / "projects"


def projects_dir() -> Path:
    return DEFAULT_PROJECTS_DIR


def load_project_yaml(project_id: str) -> Optional[dict[str, Any]]:
    """Load knowledge/projects/<id>.yaml if present. Returns None if missing."""
    pid = (project_id or "").strip()
    if not pid:
        return None
    path = projects_dir() / f"{pid}.yaml"
    if not path.is_file():
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        # Minimal fallback: no PyYAML — return stub with path only.
        return {"id": pid, "raw_path": str(path), "repos": []}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def existing_repo_paths(project: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    """Return repos whose local path exists (skip missing / empty)."""
    if not project:
        return []
    repos = project.get("repos") or []
    out: list[dict[str, str]] = []
    if not isinstance(repos, list):
        return out
    for item in repos:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            continue
        out.append(
            {
                "path": str(p),
                "remote": str(item.get("remote") or "") or "",
                "evidence": str(item.get("evidence") or "") or "",
            }
        )
    return out


def copy_project_yaml_to_workspace(
    task_dir: Path, project_id: Optional[str]
) -> Optional[Path]:
    """Copy project yaml into task workspace as project.yaml. Returns dest or None."""
    pid = (project_id or "").strip()
    if not pid:
        return None
    src = projects_dir() / f"{pid}.yaml"
    if not src.is_file():
        return None
    task_dir.mkdir(parents=True, exist_ok=True)
    dest = task_dir / "project.yaml"
    shutil.copy2(src, dest)
    return dest


def format_project_brief(project: Optional[dict[str, Any]]) -> str:
    if not project:
        return "项目未绑定 / 无 project.yaml。"
    lines = [
        f"project_id: {project.get('id') or ''}",
        f"name: {project.get('name') or ''}",
    ]
    contacts = project.get("contacts") or []
    if isinstance(contacts, list) and contacts:
        lines.append("contacts:")
        for c in contacts[:8]:
            if isinstance(c, dict):
                lines.append(
                    f"  - {c.get('name') or ''} ({c.get('role') or ''})"
                )
    chats = project.get("chats") or []
    if isinstance(chats, list) and chats:
        lines.append("chats:")
        for c in chats[:8]:
            if isinstance(c, dict):
                lines.append(
                    f"  - {c.get('name') or ''} `{c.get('chat_id') or ''}`"
                )
    repos = existing_repo_paths(project)
    if repos:
        lines.append("repos (existing local paths):")
        for r in repos:
            remote = r.get("remote") or "(no remote)"
            lines.append(f"  - {r['path']}  remote={remote}")
    else:
        lines.append("repos: (none available on disk)")
    return "\n".join(lines)
