"""Loads and validates data/config.json.

The bot's hardcoded defaults at FaujiBot.py:20-36 are the basis. magic_number
has no default in the bot (it's commented out at line 23), so we require it.
"""
from __future__ import annotations
import json
import threading
from typing import Any
from pydantic import BaseModel, Field, field_validator
from .paths import CONFIG_PATH


class BotConfig(BaseModel):
    # Required — bot crashes without it
    magic_number: int = Field(..., ge=1, le=2_147_483_647)
    symbol: str = Field(default="XAUUSDm", min_length=1, max_length=32)
    hedge_file_code: str = Field(default="ASP-ADEEL-D", min_length=1, max_length=64)

    # Common knobs (sane defaults from FaujiBot.py:20-36)
    lock_magic_number: bool = True
    max_allowed_grids: int = Field(default=1, ge=1, le=10)
    net_profit_target_usd: float = Field(default=5.0, ge=0.1, le=10000.0)
    initial_lot_size: float = Field(default=0.1, ge=0.01, le=100.0)

    # Free-form passthrough so the UI can edit any other field the bot reads
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _strip_symbol(cls, v: str) -> str:
        return v.strip()

    def to_bot_config(self) -> dict[str, Any]:
        """Flatten to the dict shape FaujiBot.MartingaleBot expects."""
        d: dict[str, Any] = self.model_dump(exclude={"extra"})
        d.update(self.extra or {})
        return d


_lock = threading.Lock()


def load() -> BotConfig | None:
    if not CONFIG_PATH.exists():
        return None
    with _lock, CONFIG_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return BotConfig(**raw)


def save(cfg: BotConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg.model_dump(), f, indent=2)


def update(patch: dict[str, Any]) -> BotConfig:
    cfg = load()
    if cfg is None:
        cfg = BotConfig(**patch)
    else:
        merged = cfg.model_dump()
        for k, v in patch.items():
            if k in BotConfig.model_fields:
                merged[k] = v
            else:
                merged.setdefault("extra", {})[k] = v
        cfg = BotConfig(**merged)
    save(cfg)
    return cfg


def is_configured() -> bool:
    try:
        return load() is not None
    except Exception:
        return False
