"""
Market session detection and day-trade enforcement.
Ensures no overnight positions and trades only during active sessions.
"""
from datetime import datetime, time, timedelta
from typing import Dict, Any
import pytz
from config.settings import settings


class MarketHours:
    """
    Forex market hours (EST/EDT):
    - Sydney: 5 PM - 2 AM EST
    - Tokyo: 7 PM - 4 AM EST
    - London: 3 AM - 12 PM EST
    - New York: 8 AM - 5 PM EST

    Day trading window: 8:00 AM - 4:00 PM EST (NY session)
    Close all: 4:30 PM EST (30 min buffer before 5 PM close)
    """

    def __init__(self):
        self.est = pytz.timezone("US/Eastern")
        self.utc = pytz.utc

    def now_est(self) -> datetime:
        return datetime.now(self.est)

    def is_market_open(self) -> bool:
        """Check if we're within trading hours."""
        now = self.now_est()
        current_time = now.time()
        open_time = time(settings.MARKET_OPEN_HOUR, 0)
        close_time = time(settings.MARKET_CLOSE_HOUR, 0)
        return open_time <= current_time <= close_time

    def is_close_time(self) -> bool:
        """Check if we should close all positions (30 min before market close)."""
        now = self.now_est()
        current_time = now.time()
        close_buffer = time(
            settings.MARKET_CLOSE_HOUR,
            60 - settings.CLOSE_BUFFER_MINUTES
        )
        market_close = time(settings.MARKET_CLOSE_HOUR + 1, 0)  # 5 PM

        return close_buffer <= current_time <= market_close

    def time_until_close(self) -> timedelta:
        """Get time remaining until forced close."""
        now = self.now_est()
        close_time = datetime.combine(
            now.date(),
            time(settings.MARKET_CLOSE_HOUR, 60 - settings.CLOSE_BUFFER_MINUTES)
        )
        close_time = self.est.localize(close_time)

        if now > close_time:
            # Market already closed for today
            return timedelta(0)
        return close_time - now

    def can_open_new_trade(self) -> Dict[str, Any]:
        """
        Comprehensive check before opening new trades.
        Returns dict with allowed flag and reason.
        """
        now = self.now_est()

        # Check market hours
        if not self.is_market_open():
            return {
                "allowed": False,
                "reason": f"Market closed. Trading hours: {settings.MARKET_OPEN_HOUR}:00-{settings.MARKET_CLOSE_HOUR}:00 EST"
            }

        # Check if too close to close time
        time_remaining = self.time_until_close()
        if time_remaining < timedelta(minutes=45):
            return {
                "allowed": False,
                "reason": f"Too close to market close. {time_remaining} remaining. No new trades."
            }

        # Check weekend (Friday after close, Sunday before open)
        weekday = now.weekday()
        if weekday == 4 and now.hour >= 17:  # Friday after 5 PM
            return {"allowed": False, "reason": "Weekend - markets closed"}
        if weekday == 6 and now.hour < 17:  # Sunday before 5 PM
            return {"allowed": False, "reason": "Weekend - markets closed"}

        return {
            "allowed": True,
            "reason": "OK",
            "time_until_close_minutes": time_remaining.total_seconds() / 60
        }

    def get_session(self) -> str:
        """Identify current forex session."""
        now = self.now_est()
        hour = now.hour

        if 8 <= hour < 12:
            return "NY-London Overlap (High Volatility)"
        elif 12 <= hour < 17:
            return "New York Session"
        elif 3 <= hour < 8:
            return "London Session"
        elif 19 <= hour < 24 or 0 <= hour < 4:
            return "Asia Session"
        else:
            return "Low Liquidity"


market_hours = MarketHours()