"""Application configuration loaded from environment variables."""
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # MT5 credentials are optional at import time; the client reports a useful
    # error when a live operation is attempted without them.
    MT5_LOGIN: int | None = Field(default=None)
    MT5_PASSWORD: str | None = Field(default=None)
    MT5_SERVER: str | None = Field(default=None)
    MT5_PATH: str | None = Field(
        default=r"C:\Program Files\MetaTrader 5\terminal64.exe",
        description="MetaTrader 5 terminal executable",
    )

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Trading Parameters
    RISK_PER_TRADE_PCT: float = Field(1.0, description="Risk % per trade")
    MAX_OPEN_TRADES: int = Field(3, description="Max concurrent day trades")
    BASE_CURRENCY: str = Field("USD", description="Account currency")

    # Forex weekly session and the bot's weekday day-trading close (US/Eastern).
    FOREX_WEEK_OPEN_HOUR: int = 17  # Sunday 5:00 PM Eastern
    FOREX_WEEK_CLOSE_HOUR: int = 17  # Friday 5:00 PM Eastern
    MARKET_OPEN_HOUR: int = 8  # Legacy setting; retained for configuration compatibility
    MARKET_CLOSE_HOUR: int = 16  # 4:00 PM Eastern
    CLOSE_BUFFER_MINUTES: int = 30  # Close all 30 min before market close (3:30 PM Eastern)

    # Scanner
    SCANNER_INTERVAL_MIN: int = 30
    MIN_ATR_PIPS: float = 10.0
    MAX_SPREAD_PIPS: float = 3.0
    MIN_ADX: float = 20.0

    # Database
    DB_PATH: str = Field(
        str(Path(__file__).resolve().parent.parent / "data" / "trades.db"),
        description="SQLite path",
    )
    MONGO_URI: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MONGO_URI", "MONGODB_URI"),
    )
    MONGO_DATABASE: str = "mt5_tradebot"
    MONGO_COLLECTION: str = "events"
    OUTBOX_SYNC_INTERVAL_SECONDS: float = 5.0
    OUTBOX_BATCH_SIZE: int = 100
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8001


settings = Settings()