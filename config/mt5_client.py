"""Small, synchronous adapter around the MetaTrader5 Python package.

The package is imported lazily because MT5 is normally installed on the
Windows trading host, while this project is also linted and tested on Linux.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from config.settings import settings

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
    def __init__(self) -> None:
        self._connected = False

    @property
    def api(self):
        if _mt5 is None:
            raise RuntimeError("MetaTrader5 is not installed; run this on the Windows MT5 host")
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
        if not self.api.initialize(**kwargs):
            code, message = self.api.last_error()
            raise RuntimeError(f"MT5 initialize failed ({code}): {message}")
        self._connected = True

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

    def order_send(self, request: dict[str, Any]):
        self.connect()
        result = self.api.order_send(request)
        if result is None:
            raise RuntimeError(f"MT5 order_send returned no result: {self.api.last_error()}")
        if result.retcode != self.api.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 order rejected ({result.retcode}): {result.comment}")
        return result

    def place_market_order(self, symbol: str, direction: str, volume: float,
                           stop_loss: float | None = None,
                           take_profit: float | None = None,
                           comment: str = "mt5-tradebot"):
        name = self._symbol(symbol)
        tick = self.get_tick(name)
        is_buy = direction.lower() in {"long", "buy"}
        request = {
            "action": self.api.TRADE_ACTION_DEAL,
            "symbol": name,
            "volume": float(volume),
            "type": self.api.ORDER_TYPE_BUY if is_buy else self.api.ORDER_TYPE_SELL,
            "price": float(tick.ask if is_buy else tick.bid),
            "deviation": 20,
            "magic": 20240918,
            "comment": comment,
            "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": self.api.ORDER_FILLING_IOC,
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
        tick = self.get_tick(position.symbol)
        is_buy = position.type == self.api.POSITION_TYPE_BUY
        request = {
            "action": self.api.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": float(position.volume),
            "type": self.api.ORDER_TYPE_SELL if is_buy else self.api.ORDER_TYPE_BUY,
            "position": int(position.ticket),
            "price": float(tick.bid if is_buy else tick.ask),
            "deviation": 20,
            "magic": 20240918,
            "comment": "mt5-tradebot-close",
            "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": self.api.ORDER_FILLING_IOC,
        }
        return self.order_send(request)


mt5_client = MT5Client()
