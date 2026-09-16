"""Entrypoint: python run_worker.py"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `workers` imports when launched from repo root
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.settings import load_dotenv  # noqa: E402
from workers.daemon import build_daemon_from_env  # noqa: E402


def main() -> None:
    load_dotenv()
    daemon = build_daemon_from_env()
    print(
        f"[run_worker] starting daemon worker_id={daemon.worker_id} "
        f"base={daemon.client.base_url}",
        flush=True,
    )
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        print("[run_worker] stopped", flush=True)


if __name__ == "__main__":
    main()
