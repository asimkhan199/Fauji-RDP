"""Imports MartingaleBot and runs it in a daemon thread.

Critically: FaujiBot.py is NEVER modified. We only read its attributes and
flip its `is_running` flag. State that the original code persists to disk
(hedge JSON) lands in data/ because we chdir there before importing.
"""
from __future__ import annotations
import io
import logging
import sys
import threading
import time
import traceback
from collections import deque
from contextlib import redirect_stdout, redirect_stderr
from typing import Any

from .paths import BOT_DIR, BOT_LOG_PATH, chdir_data
from . import config_store

log = logging.getLogger("fauji.bot_manager")


class _RingLog(io.TextIOBase):
    """Captures the bot's print() output to memory + disk."""

    def __init__(self, ring: deque[str], file_path):
        super().__init__()
        self._ring = ring
        self._buf = ""
        self._fp = open(file_path, "a", encoding="utf-8", buffering=1)

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        self._fp.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                ts = time.strftime("%H:%M:%S")
                self._ring.append(f"[{ts}] {line}")
        return len(s)

    def flush(self) -> None:
        try:
            self._fp.flush()
        except Exception:
            pass


class BotManager:
    """Lifecycle wrapper. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._bot: Any = None  # MartingaleBot instance, lazy-imported
        self._state = "stopped"  # stopped | starting | running | paused | stopping | error
        self._error: str | None = None
        self._started_at: float | None = None
        self._logs: deque[str] = deque(maxlen=200)
        self._stdout_redirect: _RingLog | None = None

    # ---------- import on demand to avoid pulling MetaTrader5 at server boot ----------
    def _import_bot_class(self):
        # Make bot/ importable
        bot_path = str(BOT_DIR.resolve())
        if bot_path not in sys.path:
            sys.path.insert(0, bot_path)
        # Importing FaujiBot triggers `import MetaTrader5 as mt5` at module top.
        import FaujiBot  # type: ignore
        return FaujiBot.MartingaleBot

    # ---------- public API ----------
    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def logs(self) -> list[str]:
        return list(self._logs)

    def start(self) -> None:
        with self._lock:
            if self._state in ("running", "starting"):
                return
            cfg = config_store.load()
            if cfg is None:
                raise RuntimeError(
                    "No config.json. Complete the setup wizard first."
                )
            self._state = "starting"
            self._error = None
            self._started_at = time.time()
            t = threading.Thread(target=self._run, args=(cfg.to_bot_config(),), daemon=True, name="FaujiBotThread")
            self._thread = t
            t.start()

    def pause(self) -> None:
        """Soft pause — flips is_running off so the bot exits its loop, but keeps
        positions open at the broker. Same effect as Stop in the original code,
        we just label it Pause when the user expects to resume soon."""
        with self._lock:
            if self._bot is not None:
                try:
                    self._bot.is_running = False
                except Exception:
                    pass
            self._state = "paused"

    def stop(self) -> None:
        with self._lock:
            if self._bot is not None:
                try:
                    self._bot.is_running = False
                except Exception:
                    pass
            self._state = "stopping"
        # wait briefly for thread to wind down
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        with self._lock:
            self._state = "stopped"
            self._bot = None
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        b = self._bot
        snap: dict[str, Any] = {
            "state": self._state,
            "error": self._error,
            "started_at": self._started_at,
            "uptime_s": (time.time() - self._started_at) if self._started_at else 0,
        }
        if b is None:
            return snap
        # Read from the bot's own attributes — no logic change, just inspection.
        for attr in (
            "initial_equity",
            "bot_peak_equity",
            "market_behavior",
            "market_broader_trend",
            "market_condition",
        ):
            snap[attr] = getattr(b, attr, None)
        try:
            hs = getattr(b, "hedge_state", {}) or {}
            snap["hedge_baskets"] = len(hs.get("baskets", []))
        except Exception:
            snap["hedge_baskets"] = 0
        # Live equity / positions (best-effort; require MT5 connection)
        try:
            import MetaTrader5 as mt5  # type: ignore
            acct = mt5.account_info()
            if acct is not None:
                snap["equity"] = acct.equity
                snap["balance"] = acct.balance
                snap["margin_free"] = acct.margin_free
                snap["currency"] = acct.currency
            cfg = config_store.load()
            if cfg is not None:
                positions = mt5.positions_get(symbol=cfg.symbol) or []
                snap["positions"] = [
                    {
                        "ticket": p.ticket,
                        "type": "BUY" if p.type == 0 else "SELL",
                        "volume": p.volume,
                        "price_open": p.price_open,
                        "price_current": p.price_current,
                        "profit": p.profit,
                        "comment": p.comment,
                    }
                    for p in positions
                ]
                tick = mt5.symbol_info_tick(cfg.symbol)
                if tick is not None:
                    snap["bid"] = tick.bid
                    snap["ask"] = tick.ask
        except Exception:
            pass
        return snap

    # ---------- internals ----------
    def _run(self, cfg_dict: dict[str, Any]) -> None:
        chdir_data()
        self._stdout_redirect = _RingLog(self._logs, BOT_LOG_PATH)
        try:
            BotCls = self._import_bot_class()
            # Pre-flight: if MT5 isn't running / not logged in, fail fast with a
            # clear message instead of letting the bot spin in a retry loop.
            try:
                import MetaTrader5 as mt5  # type: ignore
                if not mt5.initialize():
                    err = mt5.last_error()
                    msg = (f"MT5 not ready ({err}). Open MetaTrader 5 from the "
                           f"Start menu and log into your broker, then click Play again.")
                    self._error = msg
                    self._state = "error"
                    print(f"[supervisor] {msg}", file=self._stdout_redirect)
                    return
            except Exception as e:
                self._error = f"MT5 import failed: {e}"
                self._state = "error"
                return

            # Hard safety net: inject every key the bot's default_config has,
            # in case config.json was written before these keys existed.
            _bot_defaults = {
                "bot_type": "FaujiBot",
                "symbol": "XAUUSDm",
                "hedge_file_code": "ASP-ADEEL-D",
                "max_allowed_drawdown_percent": 100.0,
                "lock_magic_number": True,
                "check_interval_seconds": 0.1,
                "grid_trailing_drop_percent": 30,
                "net_profit_target_usd": 5,
                "max_allowed_grids": 1,
                "initial_lot_size": 0.1,
            }
            for k, v in _bot_defaults.items():
                cfg_dict.setdefault(k, v)

            with redirect_stdout(self._stdout_redirect), redirect_stderr(self._stdout_redirect):
                self._bot = BotCls(cfg_dict)
                self._state = "running"
                # Original code calls .start() then .main_loop(). main_loop blocks
                # on `while self.is_running`. We let it run; pause/stop sets is_running=False.
                if hasattr(self._bot, "start"):
                    self._bot.start()
                # Try to restore hedge state if the helper exists (no-op if user
                # commented out _load_hedges in the file — it does, but the method
                # itself is still present at FaujiBot.py:1856 and safe to call).
                try:
                    if hasattr(self._bot, "_load_hedges"):
                        self._bot._load_hedges()
                except Exception as e:
                    print(f"[supervisor] _load_hedges failed (non-fatal): {e}")
                if hasattr(self._bot, "main_loop"):
                    self._bot.main_loop()
                else:
                    # fallback: tick loop
                    while getattr(self._bot, "is_running", False):
                        try:
                            self._bot.tick()
                        except Exception:
                            traceback.print_exc()
                        time.sleep(1)
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self._state = "error"
            traceback.print_exc(file=self._stdout_redirect)
            log.exception("Bot thread crashed")
        finally:
            try:
                if self._stdout_redirect is not None:
                    self._stdout_redirect.flush()
            except Exception:
                pass
            if self._state not in ("error", "paused"):
                self._state = "stopped"


# Singleton
manager = BotManager()
