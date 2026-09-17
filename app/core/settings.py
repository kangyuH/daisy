from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ENV = ROOT / ".env"
DEFAULT_EVENT_KEY = "im.message.receive_v1"


class SettingsError(ValueError):
    """Missing or invalid runtime configuration."""


def load_dotenv(path: Path = DEFAULT_ENV) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    gateway_db_path: str = ""
    gateway_base_url: str = ""
    gateway_token: str = ""
    calibration_chat_id: str = ""
    lark_user_profile: str = ""
    default_bot_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            host=os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
            gateway_db_path=os.environ.get("GATEWAY_DB_PATH", "").strip(),
            gateway_base_url=os.environ.get("GATEWAY_BASE_URL", "").strip(),
            gateway_token=os.environ.get("GATEWAY_TOKEN", "").strip(),
            calibration_chat_id=os.environ.get("CALIBRATION_CHAT_ID", "").strip(),
            lark_user_profile=os.environ.get("LARK_USER_PROFILE", "").strip(),
            default_bot_id=os.environ.get("DEFAULT_BOT_ID", "").strip(),
        )


def get_settings() -> Settings:
    return Settings.from_env()


def calibration_chat_id() -> str:
    return get_settings().calibration_chat_id


def require_calibration_chat_id() -> str:
    """Return CALIBRATION_CHAT_ID or raise SettingsError if unset."""
    chat_id = calibration_chat_id()
    if not chat_id:
        raise SettingsError(
            "CALIBRATION_CHAT_ID is required "
            "(set it in .env or the environment; no built-in default)"
        )
    return chat_id


def gateway_base_url() -> str:
    s = get_settings()
    if s.gateway_base_url:
        return s.gateway_base_url.rstrip("/")
    return f"http://127.0.0.1:{s.port}"


def user_cli_profile_args() -> list[str]:
    p = get_settings().lark_user_profile
    return ["--profile", p] if p else []


def auth_user_open_id() -> str | None:
    try:
        from app.infra.lark.cli import run_cli

        args = ["auth", "status", *user_cli_profile_args()]
        status = run_cli(args)
        identities = status.get("identities") or {}
        user = identities.get("user") or {}
        oid = str(user.get("openId") or user.get("open_id") or "").strip()
        return oid or None
    except Exception:
        return None


def default_bot_id_from_app(app_id: str) -> str:
    suffix = app_id
    if suffix.startswith("cli_"):
        suffix = suffix[4:]
    candidate = f"bot_{suffix[-8:]}" if len(suffix) >= 8 else f"bot_{suffix}"
    if candidate == app_id:
        candidate = f"bot_{suffix}"
    return candidate
