"""
Technical Analysis module using pandas-ta.
Calculates SMA, RSI, MACD, ATR, ADX and detects crossovers.
"""
import pandas as pd
import pandas_ta as ta
from typing import Dict, Any, Optional, Tuple


class TechnicalAnalyzer:
    """
    Computes technical indicators and generates signal states.
    All methods accept a DataFrame and return enriched DataFrame.
    """

    @staticmethod
    def add_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
        df[f"SMA_{period}"] = ta.sma(df["close"], length=period)
        return df

    @staticmethod
    def add_ema(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
        df[f"EMA_{period}"] = ta.ema(df["close"], length=period)
        return df

    @staticmethod
    def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        df[f"RSI_{period}"] = ta.rsi(df["close"], length=period)
        return df

    @staticmethod
    def add_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
        macd = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
        df = pd.concat([df, macd], axis=1)
        return df

    @staticmethod
    def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        df[f"ATR_{period}"] = ta.atr(df["high"], df["low"], df["close"], length=period)
        return df

    @staticmethod
    def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        adx = ta.adx(df["high"], df["low"], df["close"], length=period)
        df = pd.concat([df, adx], axis=1)
        return df

    @staticmethod
    def add_bollinger(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> pd.DataFrame:
        bbands = ta.bbands(df["close"], length=period, std=std)
        df = pd.concat([df, bbands], axis=1)
        return df

    @staticmethod
    def detect_crossover(
            df: pd.DataFrame,
            fast_col: str,
            slow_col: str
    ) -> pd.DataFrame:
        """
        Detects crossovers between two series.
        Adds columns: crossover_up, crossover_down
        """
        df["crossover_up"] = (
                (df[fast_col] > df[slow_col]) &
                (df[fast_col].shift(1) <= df[slow_col].shift(1))
        )
        df["crossover_down"] = (
                (df[fast_col] < df[slow_col]) &
                (df[fast_col].shift(1) >= df[slow_col].shift(1))
        )
        return df

    @classmethod
    def analyze_instrument(
            cls,
            df: pd.DataFrame,
            strategy_config: Dict[str, Any]
    ) -> pd.DataFrame:
        """
        Full pipeline: compute all indicators required by strategy.json
        """
        if df.empty or len(df) < 50:
            return df

        # Add all common indicators
        df = cls.add_sma(df, 9)
        df = cls.add_sma(df, 21)
        df = cls.add_rsi(df, 14)
        df = cls.add_atr(df, 14)
        df = cls.add_adx(df, 14)
        df = cls.add_macd(df)

        # Detect SMA crossovers
        df = cls.detect_crossover(df, "SMA_9", "SMA_21")

        return df

    @classmethod
    def get_latest_signal(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """Extract latest indicator values and signal states."""
        if df.empty:
            return {}

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        return {
            "sma_9": latest.get("SMA_9"),
            "sma_21": latest.get("SMA_21"),
            "rsi_14": latest.get("RSI_14"),
            "atr_14": latest.get("ATR_14"),
            "adx_14": latest.get("ADX_14"),
            "macd": latest.get("MACD_12_26_9"),
            "macd_signal": latest.get("MACDs_12_26_9"),
            "crossover_up": bool(latest.get("crossover_up", False)),
            "crossover_down": bool(latest.get("crossover_down", False)),
            "close": latest["close"],
            "timestamp": latest.name.isoformat() if hasattr(latest.name, 'isoformat') else str(latest.name)
        }