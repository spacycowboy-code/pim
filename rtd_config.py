"""Shared browser and output paths for the API and credential refresher."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def cookie_path():
    return Path(os.environ.get("PIM_RTD_COOKIES", HERE / "data/rtd/cookies.json")).expanduser()


def template_path():
    return Path(os.environ.get("PIM_RTD_TEMPLATES", cookie_path().parent / "templates.json")).expanduser()


def profile_path():
    return Path(os.environ.get("PIM_RTD_PROFILE", Path.home() / ".kiro/browser-profiles/ericsson-cf")).expanduser()


def browser_channels():
    value = os.environ.get("PIM_RTD_BROWSER", "auto").strip().lower()
    if value == "auto":
        return ["chrome", "msedge"] if sys.platform == "win32" else [None]
    if value == "chromium":
        return [None]  # Playwright-managed Chromium
    if value in ("chrome", "msedge"):
        return [value]
    raise ValueError("PIM_RTD_BROWSER 必须是 auto、chromium、chrome 或 msedge。")


def atomic_json(path, value):
    """Write private credential files atomically, including across processes."""
    import json
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pim-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
