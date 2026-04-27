"""Channel store — maps IM conversations to workspace sessions.

Inspired by deer-flow store.py. Uses JSON file for POC persistence.
Key format: "{channel_name}:{chat_id}" or "{channel_name}:{chat_id}:{topic_id}"
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ChannelStore:
    """Persists IM chat → workspace/session mappings via JSON file."""

    def __init__(self, store_path: str = "/tmp/channel_store.json"):
        self._path = Path(store_path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                logger.info("ChannelStore loaded %d mappings from %s", len(self._data), self._path)
            except Exception:
                logger.warning("Failed to load channel store, starting fresh")
                self._data = {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, default=str))
        tmp.replace(self._path)

    def _key(self, channel_name: str, chat_id: str, topic_id: str | None = None) -> str:
        if topic_id:
            return f"{channel_name}:{chat_id}:{topic_id}"
        return f"{channel_name}:{chat_id}"

    def get(
        self, channel_name: str, chat_id: str, topic_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up workspace/session mapping for a chat."""
        with self._lock:
            return self._data.get(self._key(channel_name, chat_id, topic_id))

    def put(
        self,
        channel_name: str,
        chat_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        topic_id: str | None = None,
    ) -> None:
        """Store or update a chat → workspace/session mapping."""
        key = self._key(channel_name, chat_id, topic_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self._data.get(key)
            self._data[key] = {
                "channel_name": channel_name,
                "chat_id": chat_id,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "topic_id": topic_id,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            }
            self._save()

    def remove(
        self, channel_name: str, chat_id: str, topic_id: str | None = None,
    ) -> bool:
        """Remove a mapping (e.g. /new command). Returns True if found."""
        key = self._key(channel_name, chat_id, topic_id)
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._save()
                return True
            return False

    def list_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._data)
