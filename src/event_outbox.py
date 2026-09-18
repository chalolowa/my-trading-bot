"""Durable local event journal and best-effort MongoDB synchronizer."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventOutbox:
    """SQLite-backed outbox. Local append is independent of MongoDB health."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.DB_PATH
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_outbox (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    aggregate_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    synced_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_outbox_pending "
                "ON event_outbox (synced_at, created_at)"
            )

    def append(self, event_type: str, payload: dict[str, Any],
               aggregate_id: int | str | None = None) -> str:
        event_id = str(uuid.uuid4())
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO event_outbox
                    (event_id, event_type, aggregate_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    str(aggregate_id) if aggregate_id is not None else None,
                    json.dumps(payload, default=str, sort_keys=True),
                    _utc_now(),
                ),
            )
        return event_id

    def pending(self, limit: int) -> list[sqlite3.Row]:
        with self._lock, self._connection() as connection:
            return connection.execute(
                """
                SELECT * FROM event_outbox
                WHERE synced_at IS NULL
                ORDER BY created_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def mark_synced(self, event_id: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE event_outbox SET synced_at = ?, last_error = NULL "
                "WHERE event_id = ?",
                (_utc_now(), event_id),
            )

    def mark_failed(self, event_id: str, error: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE event_outbox SET attempts = attempts + 1, last_error = ? "
                "WHERE event_id = ?",
                (error[:1000], event_id),
            )

    def pending_count(self) -> int:
        with self._lock, self._connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM event_outbox WHERE synced_at IS NULL"
            ).fetchone()[0]


class MongoEventSynchronizer:
    """Uploads acknowledged outbox events without ever blocking trading."""

    def __init__(self, outbox: EventOutbox) -> None:
        self.outbox = outbox
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not settings.MONGO_URI:
            logger.info("MongoDB synchronization disabled: MONGO_URI is not set")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="mongo-event-sync")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.sync_once)
            except Exception:
                logger.exception("MongoDB synchronization attempt failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.OUTBOX_SYNC_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    def sync_once(self) -> int:
        """Synchronize one batch; failures remain pending for a later retry."""
        if not settings.MONGO_URI:
            return 0
        try:
            from pymongo import MongoClient
        except ImportError:
            logger.warning("MongoDB synchronization unavailable: install pymongo")
            return 0

        client = MongoClient(settings.MONGO_URI, serverSelectionTimeoutMS=3000)
        try:
            collection = client[settings.MONGO_DATABASE][settings.MONGO_COLLECTION]
            uploaded = 0
            for row in self.outbox.pending(settings.OUTBOX_BATCH_SIZE):
                document = {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "aggregate_id": row["aggregate_id"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                    "synced_at": _utc_now(),
                }
                try:
                    result = collection.replace_one(
                        {"_id": row["event_id"]},
                        {"_id": row["event_id"], **document},
                        upsert=True,
                    )
                    if result.acknowledged:
                        self.outbox.mark_synced(row["event_id"])
                        uploaded += 1
                except Exception as exc:
                    self.outbox.mark_failed(row["event_id"], str(exc))
            return uploaded
        finally:
            client.close()


event_outbox = EventOutbox()
mongo_synchronizer = MongoEventSynchronizer(event_outbox)
