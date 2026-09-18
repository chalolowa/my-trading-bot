"""
Market Scanner: Filters currency pairs every 30 minutes.
Ranks instruments by setup quality using ATR, spread, ADX, and trend alignment.
"""
from typing import Dict, Any, List
from datetime import datetime
import pandas as pd
from config.mt5_client import mt5_client, normalize_symbol
from src.technical_analysis import TechnicalAnalyzer
from src.market_hours import market_hours
from config.settings import settings
from src.strategy_engine import strategy_engine


class MarketScanner:
    def __init__(self):
        self.ta = TechnicalAnalyzer()
        self.instruments = strategy_engine.get_instruments()

    async def scan_all(self) -> List[Dict[str, Any]]:
        """
        Scan all configured instruments and return ranked opportunities.
        Runs every 30 minutes via APScheduler.
        """
        if not market_hours.is_market_open():
            return []

        results = []

        for instrument in self.instruments:
            try:
                score = await self._analyze_instrument(instrument)
                if score["tradable"]:
                    results.append(score)
            except Exception:
                continue

        # Sort by composite score (higher = better setup)
        results.sort(key=lambda x: x["composite_score"], reverse=True)
        return results

    async def _analyze_instrument(self, instrument: str) -> Dict[str, Any]:
        """Analyze single instrument for trade readiness."""
        # Fetch data
        df = mt5_client.copy_rates_from_pos(instrument, "M15", 0, 100)
        if df.empty or len(df) < 50:
            return {"tradable": False, "instrument": instrument, "reason": "insufficient_data"}

        # Calculate indicators
        df = self.ta.analyze_instrument(df, {})
        latest = df.iloc[-1]

        # Get current spread
        try:
            tick = mt5_client.get_tick(instrument)
            spread_pips = (float(tick.ask) - float(tick.bid)) * (
                100 if "JPY" in normalize_symbol(instrument) else 10000
            )
        except Exception:
            spread_pips = 999

        # Filters
        atr = latest.get("ATR_14", 0)
        adx = latest.get("ADX_14", 0)
        rsi = latest.get("RSI_14", 50)

        # Check filters
        tradable = True
        reasons = []

        if spread_pips > settings.MAX_SPREAD_PIPS:
            tradable = False
            reasons.append(f"Spread too high: {spread_pips:.1f} pips")

        pip_factor = 100 if "JPY" in normalize_symbol(instrument) else 10000
        if atr * pip_factor < settings.MIN_ATR_PIPS:
            tradable = False
            reasons.append(f"Volatility too low: ATR={atr:.5f}")

        if adx < settings.MIN_ADX:
            tradable = False
            reasons.append(f"No clear trend: ADX={adx:.1f}")

        # Composite scoring (0-100)
        score = 0
        if adx > 25:
            score += 30
        elif adx > 20:
            score += 20

        if 40 < rsi < 60:  # Balanced, ready for breakout
            score += 20
        elif 30 < rsi < 70:
            score += 10

        if spread_pips < 2.0:
            score += 25
        elif spread_pips < 3.0:
            score += 15

        if atr * pip_factor > 15:
            score += 25

        # Trend direction
        sma_9 = latest.get("SMA_9", 0)
        sma_21 = latest.get("SMA_21", 0)
        trend = "bullish" if sma_9 > sma_21 else "bearish" if sma_9 < sma_21 else "neutral"

        return {
            "tradable": tradable,
            "instrument": normalize_symbol(instrument),
            "composite_score": score,
            "trend": trend,
            "adx": round(adx, 1),
            "rsi": round(rsi, 1),
            "atr_pips": round(atr * pip_factor, 1),
            "spread_pips": round(spread_pips, 1),
            "sma_9": round(sma_9, 5),
            "sma_21": round(sma_21, 5),
            "reasons": reasons,
            "timestamp": datetime.now().isoformat()
        }

    def get_top_opportunities(self, n: int = 3) -> List[Dict[str, Any]]:
        """Get top N opportunities from last scan."""
        # This would cache results in production
        return []


scanner = MarketScanner()