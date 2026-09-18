"""
Strategy Engine reads strategy.json and evaluates entry/exit conditions
against live market data. Fully dynamic — edit JSON to change strategy.
"""
import json
import os
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import pandas as pd
from src.technical_analysis import TechnicalAnalyzer
from config.settings import settings


class StrategyEngine:
    def __init__(self, config_path: str = "config/strategy.json"):
        self.config = self._load_config(config_path)
        self.ta = TechnicalAnalyzer()

    def _load_config(self, path: str) -> Dict[str, Any]:
        with open(path, 'r') as f:
            return json.load(f)

    def reload_config(self):
        """Hot-reload strategy without restarting bot."""
        self.config = self._load_config("config/strategy.json")

    def evaluate_entry(self, df: pd.DataFrame, direction: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Evaluate if entry conditions are met for given direction (long/short).
        Returns (should_enter, metadata).
        """
        if df.empty or len(df) < 30:
            return False, {"reason": "insufficient_data"}

        df = self.ta.analyze_instrument(df, self.config)
        signal = self.ta.get_latest_signal(df)

        if not signal:
            return False, {"reason": "no_signal"}

        rules = self.config["entry_rules"].get(direction, {})
        conditions = rules.get("conditions", [])
        require_all = rules.get("require_all", True)

        condition_results = []
        for cond in conditions:
            result = self._check_condition(cond, signal, df)
            condition_results.append({
                "indicator": cond.get("indicator"),
                "result": result
            })

        met_conditions = [c for c in condition_results if c["result"]]
        should_enter = len(met_conditions) == len(conditions) if require_all else len(met_conditions) > 0

        metadata = {
            "signal": signal,
            "conditions": condition_results,
            "all_met": should_enter,
            "strategy": self.config["name"]
        }

        return should_enter, metadata

    def _check_condition(
            self,
            cond: Dict[str, Any],
            signal: Dict[str, Any],
            df: pd.DataFrame
    ) -> bool:
        """Evaluate a single condition from strategy.json."""
        indicator = cond["indicator"]
        period = cond.get("period", 14)
        comparison = cond["comparison"]

        # Get indicator value
        key = f"{indicator}_{period}" if indicator != "SMA" else f"{indicator}_{period}"
        if indicator == "SMA" and comparison in ["crosses_above", "crosses_below"]:
            # Crossover conditions use the dataframe directly
            latest = df.iloc[-1]
            if comparison == "crosses_above":
                return bool(latest.get("crossover_up", False))
            else:
                return bool(latest.get("crossover_down", False))

        value = signal.get(key.lower() if indicator != "SMA" else key)
        if value is None or pd.isna(value):
            return False

        # Numeric comparisons
        ref_value = cond.get("value")
        if comparison == ">":
            return value > ref_value
        elif comparison == "<":
            return value < ref_value
        elif comparison == ">=":
            return value >= ref_value
        elif comparison == "<=":
            return value <= ref_value
        elif comparison == "==":
            return value == ref_value

        return False

    def calculate_stop_loss(
            self,
            entry_price: float,
            direction: str,
            df: pd.DataFrame
    ) -> float:
        """Calculate stop loss based on strategy.json exit rules."""
        sl_config = self.config["exit_rules"]["stop_loss"]
        latest = df.iloc[-1]

        if sl_config["type"] == "ATR_MULTIPLE":
            atr = latest.get(f"ATR_{sl_config['atr_period']}", entry_price * 0.001)
            distance = atr * sl_config["atr_multiplier"]

            if direction == "long":
                return entry_price - distance
            else:
                return entry_price + distance

        return entry_price * 0.99 if direction == "long" else entry_price * 1.01

    def calculate_take_profit(
            self,
            entry_price: float,
            stop_loss: float,
            direction: str
    ) -> float:
        """Calculate take profit based on risk:reward ratio."""
        tp_config = self.config["exit_rules"]["take_profit"]
        risk = abs(entry_price - stop_loss)

        if tp_config["type"] == "RISK_REWARD":
            reward = risk * tp_config["risk_reward_ratio"]
            if direction == "long":
                return entry_price + reward
            else:
                return entry_price - reward

        return entry_price * 1.02 if direction == "long" else entry_price * 0.98

    def get_instruments(self) -> list[str]:
        return [item.replace("_", "").upper() for item in self.config.get("instruments", [])]


# Singleton
strategy_engine = StrategyEngine()