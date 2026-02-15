from __future__ import annotations

import asyncio
import os
from pathlib import Path

from telegram_lmstudio_brave_bot.config import load_settings


def _load_env_fallback(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def load_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except Exception:
        _load_env_fallback(Path(__file__).with_name(".env"))


def main() -> None:
    load_env()
    try:
        from telegram_lmstudio_brave_bot.bot import run_bot
    except ModuleNotFoundError as e:
        missing = getattr(e, "name", None) or str(e)
        raise SystemExit(
            "Missing dependency: "
            + str(missing)
            + "\n\n"
            + "You are likely running system Python without the virtualenv packages installed.\n"
            + "Run these commands in the project folder:\n"
            + "  python -m venv .venv\n"
            + "  .\\.venv\\Scripts\\Activate.ps1\n"
            + "  python -m pip install -r requirements.txt\n"
            + "  python .\\main.py\n\n"
            + "Or run directly with the venv interpreter:\n"
            + "  .\\.venv\\Scripts\\python.exe .\\main.py\n"
        )

    settings = load_settings()
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
