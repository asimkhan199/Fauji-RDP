"""Centralized path resolution. The supervisor sets cwd to data/, so the bot's
own JSON state file (named bot-{symbol}-{magic}-{code}-hedges.json) lands there
without modifying FaujiBot.py."""
from __future__ import annotations
import os
import sys
from pathlib import Path


def _detect_install_root() -> Path:
    # When frozen by PyInstaller / launched from C:\FaujiBot\FaujiBot.exe
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    # When run from source: this file lives at <root>/supervisor/paths.py
    return Path(__file__).resolve().parent.parent


INSTALL_ROOT = _detect_install_root()
BOT_DIR = INSTALL_ROOT / "bot"
DATA_DIR = INSTALL_ROOT / "data"
SUPERVISOR_DIR = INSTALL_ROOT / "supervisor"
UI_DIR = SUPERVISOR_DIR / "ui"
CERTS_DIR = SUPERVISOR_DIR / "certs"

CONFIG_PATH = DATA_DIR / "config.json"
AUTH_PATH = DATA_DIR / "auth.json"
LOG_PATH = DATA_DIR / "supervisor.log"
BOT_LOG_PATH = DATA_DIR / "bot.log"

CERT_FILE = CERTS_DIR / "cert.pem"
KEY_FILE = CERTS_DIR / "key.pem"


def ensure_dirs() -> None:
    for d in (BOT_DIR, DATA_DIR, SUPERVISOR_DIR, UI_DIR, CERTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def chdir_data() -> None:
    """Bot writes its hedge JSON to cwd. Setting cwd=data/ keeps state organized
    without touching FaujiBot.py."""
    ensure_dirs()
    os.chdir(DATA_DIR)
