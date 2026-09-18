"""
Database utilities for trade persistence.
RiskManager handles most DB operations; this provides connection pooling.
"""
import sqlite3
from contextlib import contextmanager
from config.settings import settings


@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_database():
    """Ensure all tables exist. Called on startup."""
    from src.risk_manager import risk_manager
    # RiskManager init creates tables
    _ = risk_manager