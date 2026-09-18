"""Application configuration loaded from environment variables."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # MT5 credentials are optional at import time; the client reports a useful
    # error when a live operation is attempted without them.
    MT5_LOGIN: int | None = Field(default=None)
    MT5_PASSWORD: str | None = Field(default=None)
    MT5_SERVER: str | None = Field(default=None)
    MT5_PATH: str | None = Field(default=None)

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Trading Parameters
    RISK_PER_TRADE_PCT: float = Field(1.0, description="Risk % per trade")
    MAX_OPEN_TRADES: int = Field(3, description="Max concurrent day trades")
    BASE_CURRENCY: str = Field("USD", description="Account currency")

    # Session Times (UTC-5 / EST)
    MARKET_OPEN_HOUR: int = 8  # 8:00 AM EST (London pre-open)
    MARKET_CLOSE_HOUR: int = 16  # 4:00 PM EST (close before 5 PM)
    CLOSE_BUFFER_MINUTES: int = 30  # Close all 30 min before market close

    # Scanner
    SCANNER_INTERVAL_MIN: int = 30
    MIN_ATR_PIPS: float = 10.0
    MAX_SPREAD_PIPS: float = 3.0
    MIN_ADX: float = 20.0

    # Dashboard
    DASHBOARD_PORT: int = 8000
    DASHBOARD_HOST: str = "0.0.0.0"

    # Database
    DB_PATH: str = Field("data/trades.db", description="SQLite path")
    MONGO_URI: str | None = None
    MONGO_DATABASE: str = "mt5_tradebot"
    MONGO_COLLECTION: str = "events"
    OUTBOX_SYNC_INTERVAL_SECONDS: float = 5.0
    OUTBOX_BATCH_SIZE: int = 100
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8001


settings = Settings()