"""Read-only connectivity probe. Does not import trading or send notifications.

Run from the project root: python scripts/audit_connections.py
Never prints connection strings, credentials, or server exception messages.
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from pymongo import MongoClient

from config.settings import settings


def main():
    values = dotenv_values(ROOT / ".env")
    result = {"application_mongo_enabled": bool(settings.MONGO_URI)}
    path = Path(settings.DB_PATH).resolve()
    if path.exists():
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            result["sqlite_integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
            result["sqlite_tables"] = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        finally:
            connection.close()
    else:
        result["sqlite"] = "configured database does not exist yet"

    uri = settings.MONGO_URI or values.get("MONGODB_URI")
    if not uri:
        result["mongodb"] = "no URI configured"
    else:
        client = None
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000,
                                 connectTimeoutMS=5000, socketTimeoutMS=5000)
            client.admin.command("ping")
            result["mongodb_ping"] = "passed"
            client[settings.MONGO_DATABASE][settings.MONGO_COLLECTION].find_one({}, {"_id": 1})
            result["mongodb_collection_read"] = "passed"
        except Exception as error:
            result["mongodb_error_type"] = type(error).__name__
            result["mongodb_error_code"] = getattr(error, "code", None)
        finally:
            if client is not None:
                client.close()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
