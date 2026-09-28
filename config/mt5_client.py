"""Small, synchronous adapter around the MetaTrader5 Python package.

The package is imported lazily because MT5 is normally installed on the
Windows trading host, while this project is also linted and tested on Linux.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from config.settings import settings
from src.logger import _console

try:
    import MetaTrader5 as _mt5
except ImportError:  # pragma: no cover - expected on non-trading machines
    _mt5 = None


TIMEFRAMES = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


def normalize_symbol(symbol: str) -> str:
    """Return the broker symbol format used by this application."""
    return symbol.replace("_", "").upper()


class MT5Client:
    MAGIC = 20240918
    def __init__(self) -> None:
        self._connected = False

    @property
    def api(self):
        if _mt5 is None:
            raise RuntimeError(
                "MetaTrader5 Python package is not installed. "
                "Install it with: python -m pip install MetaTrader5"
            )
        return _mt5

    def connect(self) -> None:
        if self._connected:
            return
        kwargs: dict[str, Any] = {}
        if settings.MT5_LOGIN is not None:
            kwargs["login"] = settings.MT5_LOGIN
        if settings.MT5_PASSWORD:
            kwargs["password"] = settings.MT5_PASSWORD
        if settings.MT5_SERVER:
            kwargs["server"] = settings.MT5_SERVER
        if settings.MT5_PATH:
            kwargs["path"] = settings.MT5_PATH
        if settings.MT5_PATH:
            from pathlib import Path
            if not Path(settings.MT5_PATH).is_file():
                err_msg = f"MetaTrader 5 terminal was not found at {settings.MT5_PATH}"
                _console(f"MT5 connect failed: {err_msg}", level=logging.ERROR)
                raise FileNotFoundError(err_msg)
        try:
            if not self.api.initialize(**kwargs):
                code, message = self.api.last_error()
                err_msg = f"MT5 initialize failed ({code}): {message}"
                _console(f"MT5 connect failed: {err_msg}", level=logging.ERROR)
                raise RuntimeError(err_msg)
            self._connected = True
            account = self.api.account_info()
            login = getattr(account, "login", settings.MT5_LOGIN or "N/A")
            server = getattr(account, "server", settings.MT5_SERVER or "N/A")
            balance = getattr(account, "balance", 0.0)
            _console(f"MT5 connected successfully: login={login}, server={server}, balance={balance}")
        except Exception as exc:
            if not self._connected:
                _console(f"MT5 connect exception: {exc}", level=logging.ERROR)
            raise

    def shutdown(self) -> None:
        if self._connected:
            self.api.shutdown()
            self._connected = False

    def _symbol(self, symbol: str):
        self.connect()
        name = normalize_symbol(symbol)
        if not self.api.symbol_select(name, True):
            raise RuntimeError(f"MT5 symbol is unavailable: {name}")
        return name

    def get_symbol_info(self, symbol: str):
        """Get symbol metadata from MT5."""
        name = self._symbol(symbol)
        return self.api.symbol_info(name)

    def copy_rates_from_pos(self, symbol: str, timeframe: str, start_pos: int = 0,
                            count: int = 200) -> pd.DataFrame:
        name = self._symbol(symbol)
        timeframe_value = getattr(self.api, TIMEFRAMES.get(timeframe.upper(), "TIMEFRAME_M15"))
        rates = self.api.copy_rates_from_pos(name, timeframe_value, start_pos, count)
        if rates is None:
            code, message = self.api.last_error()
            raise RuntimeError(f"MT5 historical data failed for {name} ({code}): {message}")
        frame = pd.DataFrame(rates)
        if frame.empty:
            return frame
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        return frame.set_index("time")

    def get_tick(self, symbol: str):
        name = self._symbol(symbol)
        tick = self.api.symbol_info_tick(name)
        if tick is None:
            raise RuntimeError(f"MT5 tick unavailable for {name}")
        return tick

    def normalize_volume(self, symbol: str, volume: float) -> float:
        """Convert a calculated volume to the broker's permitted lot size."""
        info = self.get_symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol metadata unavailable for {symbol}")
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        minimum = float(getattr(info, "volume_min", step) or step)
        maximum = float(getattr(info, "volume_max", volume) or volume)
        raw_volume = float(volume)
        if raw_volume < minimum:
            return 0.0
        # Robust lot-step rounding using float tolerance
        steps_count = math.floor((raw_volume / step) + 1e-9)
        normalized = min(maximum, steps_count * step)
        step_str = f"{step:.8f}".rstrip("0")
        decimals = len(step_str.split(".")[1]) if "." in step_str else 2
        return round(normalized, decimals)

    def units_to_lots(self, symbol: str, units: float) -> float:
        info = self.get_symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol metadata unavailable for {symbol}")
        contract_size = float(getattr(info, "trade_contract_size", 100000) or 100000)
        return self.normalize_volume(symbol, float(units) / contract_size)

    def get_account_info(self):
        self.connect()
        account = self.api.account_info()
        if account is None:
            raise RuntimeError(f"MT5 account info unavailable: {self.api.last_error()}")
        return account

    def get_open_positions(self, symbol: str | None = None) -> list[Any]:
        self.connect()
        positions = self.api.positions_get(symbol=normalize_symbol(symbol)) if symbol else self.api.positions_get()
        if positions is None:
            code, message = self.api.last_error()
            raise RuntimeError(f"MT5 positions request failed ({code}): {message}")
        return list(positions)

    def get_bot_positions(self) -> list[Any]:
        """Return only positions owned by this application."""
        return [
            position for position in self.get_open_positions()
            if getattr(position, "magic", self.MAGIC) == self.MAGIC
        ]

    def _get_filling_mode(self, info: Any) -> int:
        """Select supported filling mode from symbol info."""
        filling_mode = getattr(info, "filling_mode", 0) if info is not None else 0
        ioc = getattr(_mt5, "ORDER_FILLING_IOC", 1) if _mt5 is not None else 1
        fok = getattr(_mt5, "ORDER_FILLING_FOK", 0) if _mt5 is not None else 0
        ret = getattr(_mt5, "ORDER_FILLING_RETURN", 2) if _mt5 is not None else 2
        # Check bitmask flags: SYMBOL_FILLING_IOC is 2, SYMBOL_FILLING_FOK is 1
        if filling_mode & 2:
            return ioc
        elif filling_mode & 1:
            return fok
        elif filling_mode == 0:
            return ioc
        return ret

    def order_send(self, request: dict[str, Any]):
        self.connect()
        _console(f"MT5 order_send outgoing request: {request}")
        result = self.api.order_send(request)
        if result is None:
            err = f"MT5 order_send returned no result: {self.api.last_error()}"
            _console(err, level=logging.ERROR)
            raise RuntimeError(err)
        retcode = getattr(result, "retcode", None)
        comment = getattr(result, "comment", "")
        price = getattr(result, "price", None)
        order = getattr(result, "order", None)
        if retcode != self.api.TRADE_RETCODE_DONE:
            err = f"MT5 order rejected (retcode={retcode}, comment='{comment}', price={price})"
            _console(err, level=logging.ERROR)
            raise RuntimeError(err)
        _console(f"MT5 order_send success: retcode={retcode}, order={order}, comment='{comment}', price={price}")
        return result

    def place_market_order(self, symbol: str, direction: str, volume: float,
                           stop_loss: float | None = None,
                           take_profit: float | None = None,
                           comment: str = "mt5-tradebot"):
        name = self._symbol(symbol)
        info = self.get_symbol_info(name)
        normalized_volume = self.normalize_volume(name, volume)
        if normalized_volume <= 0.0:
            err = f"Refusing order: normalized volume is 0.0 (requested {volume}) for {name}"
            _console(err, level=logging.WARNING)
            raise ValueError(err)

        tick = self.get_tick(name)
        is_buy = direction.lower() in {"long", "buy"}
        filling_mode = self._get_filling_mode(info)

        request = {
            "action": self.api.TRADE_ACTION_DEAL,
            "symbol": name,
            "volume": normalized_volume,
            "type": self.api.ORDER_TYPE_BUY if is_buy else self.api.ORDER_TYPE_SELL,
            "price": float(tick.ask if is_buy else tick.bid),
            "deviation": 20,
            "magic": self.MAGIC,
            "comment": comment,
            "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }
        if stop_loss is not None:
            request["sl"] = float(stop_loss)
        if take_profit is not None:
            request["tp"] = float(take_profit)
        return self.order_send(request)

    def close_position(self, ticket: int):
        positions = [p for p in self.get_open_positions() if int(p.ticket) == int(ticket)]
        if not positions:
            raise ValueError(f"Open MT5 position not found: {ticket}")
        position = positions[0]
        info = self.get_symbol_info(position.symbol)
        tick = self.get_tick(position.symbol)
        is_buy = position.type == self.api.POSITION_TYPE_BUY
        filling_mode = self._get_filling_mode(info)

        request = {
            "action": self.api.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(position.volume),
            "type": self.api.ORDER_TYPE_SELL if is_buy else self.api.ORDER_TYPE_BUY,
            "position": int(position.ticket),
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": 20,
            "magic": self.MAGIC,
            "comment": "mt5-tradebot-close",
            "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }
        return self.order_send(request)


mt5_client = MT5Client()
