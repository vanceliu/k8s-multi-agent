"""Memory Service — SQLite FTS5 indexed workspace memory system.

Manages memories/ directory on PVC with full-text search indexing.
Source of truth: Markdown files with YAML frontmatter.
Index: SQLite FTS5 (local, can be rebuilt from files at any time).
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

# SQLite schema for memory index
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    tags        TEXT DEFAULT '',
    summary     TEXT NOT NULL,
    content     TEXT NOT NULL,
    recall_count    INTEGER DEFAULT 0,
    last_recalled   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_entries (type);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_entries (updated_at DESC);
"""

_FTS_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    name,
    summary,
    tags,
    content,
    content=memory_entries,
    content_rowid=id,
    tokenize='trigram case_sensitive 0'
);
"""

_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_memory_ai AFTER INSERT ON memory_entries BEGIN
    INSERT INTO memory_fts(rowid, name, summary, tags, content)
    VALUES (new.id, new.name, new.summary, new.tags, new.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_ad AFTER DELETE ON memory_entries BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, name, summary, tags, content)
    VALUES ('delete', old.id, old.name, old.summary, old.tags, old.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_au AFTER UPDATE ON memory_entries BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, name, summary, tags, content)
    VALUES ('delete', old.id, old.name, old.summary, old.tags, old.content);
    INSERT INTO memory_fts(rowid, name, summary, tags, content)
    VALUES (new.id, new.name, new.summary, new.tags, new.content);
END;
"""

# Frontmatter parsing regex
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class MemoryEntry:
    """A single memory entry."""
    id: int
    file_path: str
    name: str
    type: str
    tags: list[str]
    summary: str
    content: str
    recall_count: int
    last_recalled: Optional[str]
    created_at: str
    updated_at: str
    rank: float = 0.0


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text. Returns (metadata, body)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    frontmatter_str = match.group(1)
    body = match.group(2).strip()

    metadata = {}
    current_key = None
    current_value = None

    for line in frontmatter_str.split("\n"):
        if line.startswith("  ") or line.startswith("\t"):
            if current_key and current_value is not None:
                current_value += " " + line.strip()
        elif ":" in line:
            if current_key and current_value is not None:
                metadata[current_key] = current_value
            key, _, val = line.partition(":")
            current_key = key.strip()
            current_value = val.strip()
        else:
            if current_key and current_value is not None:
                metadata[current_key] = current_value
                current_key = None
                current_value = None

    if current_key and current_value is not None:
        metadata[current_key] = current_value

    # Parse tags from "[tag1, tag2]" format
    if "tags" in metadata:
        tags_str = metadata["tags"]
        if tags_str.startswith("[") and tags_str.endswith("]"):
            metadata["tags"] = [t.strip().strip("'\"") for t in tags_str[1:-1].split(",") if t.strip()]
        else:
            metadata["tags"] = [t.strip() for t in tags_str.split(",") if t.strip()]

    return metadata, body


def _extract_summary(body: str) -> str:
    """Extract first meaningful line as summary (before Why/How sections)."""
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("**Why:") or line.startswith("**How to apply:"):
            break
        return line[:200]
    return ""


def _sanitize_filename(name: str) -> str:
    """Convert memory name to safe filename."""
    safe = re.sub(r"[^\w\s\-]", "", name)
    safe = re.sub(r"\s+", "_", safe).strip("_")
    return safe[:80].lower()


class MemoryService:
    """Workspace memory management with SQLite FTS5 indexing."""

    def __init__(self, workspace_id: str, memories_dir: str, index_path: Optional[str] = None):
        self.workspace_id = workspace_id
        self.memories_dir = memories_dir
        self.index_path = index_path or os.path.join(memories_dir, ".index", "memory.db")
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create index directory, open SQLite connection, ensure schema exists."""
        index_dir = os.path.dirname(self.index_path)
        os.makedirs(index_dir, exist_ok=True)
        os.makedirs(self.memories_dir, exist_ok=True)

        self._db = await aiosqlite.connect(self.index_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")

        await self._db.executescript(_SCHEMA_SQL)
        await self._db.executescript(_FTS_SCHEMA_SQL)
        await self._db.executescript(_FTS_TRIGGERS_SQL)
        await self._db.commit()

        logger.info("MemoryService initialized (index=%s)", self.index_path)

    async def close(self) -> None:
        """Close SQLite connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # ─── Search ───────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        type_filter: Optional[str] = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """FTS5 trigram search with fallback to LIKE for short queries."""
        if not self._db or not query.strip():
            return []

        safe_query = self._prepare_fts_query(query)

        # For queries too short for trigram (< 3 chars), use LIKE fallback
        if not safe_query:
            return await self._search_like(query.strip(), type_filter, limit)

        if type_filter:
            sql = """
                SELECT me.*, bm25(memory_fts) as rank
                FROM memory_fts
                JOIN memory_entries me ON memory_fts.rowid = me.id
                WHERE memory_fts MATCH ? AND me.type = ?
                ORDER BY rank
                LIMIT ?
            """
            params = (safe_query, type_filter, limit)
        else:
            sql = """
                SELECT me.*, bm25(memory_fts) as rank
                FROM memory_fts
                JOIN memory_entries me ON memory_fts.rowid = me.id
                WHERE memory_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            params = (safe_query, limit)

        try:
            async with self._lock:
                cursor = await self._db.execute(sql, params)
                rows = await cursor.fetchall()
            return [self._row_to_entry(row) for row in rows]
        except Exception as e:
            logger.warning("FTS search failed (%s), falling back to LIKE", e)
            return await self._search_like(query.strip(), type_filter, limit)

    async def _search_like(
        self,
        query: str,
        type_filter: Optional[str] = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """Fallback search using LIKE for queries too short for trigram."""
        pattern = f"%{query}%"
        if type_filter:
            sql = """
                SELECT *, 0.0 as rank FROM memory_entries
                WHERE (name LIKE ? OR summary LIKE ? OR content LIKE ? OR tags LIKE ?)
                  AND type = ?
                ORDER BY updated_at DESC
                LIMIT ?
            """
            params = (pattern, pattern, pattern, pattern, type_filter, limit)
        else:
            sql = """
                SELECT *, 0.0 as rank FROM memory_entries
                WHERE name LIKE ? OR summary LIKE ? OR content LIKE ? OR tags LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
            """
            params = (pattern, pattern, pattern, pattern, limit)

        async with self._lock:
            cursor = await self._db.execute(sql, params)
            rows = await cursor.fetchall()
        return [self._row_to_entry(row) for row in rows]

    async def recall(
        self,
        user_message: str,
        recent_messages: Optional[list[str]] = None,
        max_results: int = 3,
    ) -> Optional[str]:
        """Active memory recall: search and format relevant memories for context injection.

        Returns formatted context string or None if no relevant memories found.
        """
        query = user_message
        if recent_messages:
            query = " ".join(recent_messages[-3:]) + " " + user_message

        results = await self.search(query, limit=max_results)
        if not results:
            return None

        # Update recall counts
        await self._update_recall_counts([r.id for r in results])

        # Format for injection
        lines = ["相關記憶（自動召回，僅供參考）：\n"]
        for i, entry in enumerate(results, 1):
            lines.append(f"{i}. [{entry.name}] ({entry.type}) — {entry.summary}")
            # Include Why/How if present in content
            for content_line in entry.content.split("\n"):
                stripped = content_line.strip()
                if stripped.startswith("**Why:") or stripped.startswith("**How to apply:"):
                    lines.append(f"   {stripped}")

        return "\n".join(lines)

    # ─── CRUD ─────────────────────────────────────────────────────────

    async def store(
        self,
        name: str,
        type: str,
        tags: list[str],
        content: str,
        why: str = "",
        how: str = "",
    ) -> str:
        """Create a new memory (write file + index + update MEMORY_INDEX.md)."""
        filename = _sanitize_filename(name) + ".md"
        # Avoid collision
        file_path = filename
        full_path = os.path.join(self.memories_dir, file_path)
        counter = 1
        while os.path.exists(full_path):
            file_path = f"{_sanitize_filename(name)}_{counter}.md"
            full_path = os.path.join(self.memories_dir, file_path)
            counter += 1

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tags_str = ", ".join(tags) if tags else ""

        body_parts = [content]
        if why:
            body_parts.append(f"\n**Why:** {why}")
        if how:
            body_parts.append(f"**How to apply:** {how}")
        body = "\n".join(body_parts)

        md_content = (
            f"---\n"
            f"name: {name}\n"
            f"type: {type}\n"
            f"tags: [{tags_str}]\n"
            f"created: {now}\n"
            f"updated: {now}\n"
            f"---\n\n"
            f"{body}\n"
        )

        # Write file
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        # Index
        await self._index_entry(file_path, name, type, tags, _extract_summary(body), body, now, now)

        # Update MEMORY_INDEX.md
        await self._sync_memory_index()

        logger.info("Memory saved: %s (%s)", name, file_path)
        return file_path

    async def update(
        self,
        file_path: str,
        content: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        """Update an existing memory file and re-index."""
        full_path = os.path.join(self.memories_dir, file_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Memory file not found: {file_path}")

        with open(full_path, "r", encoding="utf-8") as f:
            text = f.read()

        metadata, old_body = _parse_frontmatter(text)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if content is not None:
            new_body = content
        else:
            new_body = old_body

        if tags is not None:
            metadata["tags"] = tags

        metadata["updated"] = now
        tags_list = metadata.get("tags", [])
        if isinstance(tags_list, str):
            tags_list = [t.strip() for t in tags_list.split(",") if t.strip()]
        tags_str = ", ".join(tags_list)

        md_content = (
            f"---\n"
            f"name: {metadata.get('name', '')}\n"
            f"type: {metadata.get('type', 'reference')}\n"
            f"tags: [{tags_str}]\n"
            f"created: {metadata.get('created', now)}\n"
            f"updated: {now}\n"
            f"---\n\n"
            f"{new_body}\n"
        )

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        # Re-index
        await self._index_entry(
            file_path,
            metadata.get("name", ""),
            metadata.get("type", "reference"),
            tags_list,
            _extract_summary(new_body),
            new_body,
            metadata.get("created", now),
            now,
        )

        await self._sync_memory_index()
        logger.info("Memory updated: %s", file_path)

    async def delete(self, file_path: str) -> None:
        """Delete a memory file and remove from index."""
        full_path = os.path.join(self.memories_dir, file_path)
        if os.path.exists(full_path):
            os.remove(full_path)

        async with self._lock:
            await self._db.execute("DELETE FROM memory_entries WHERE file_path = ?", (file_path,))
            await self._db.commit()

        await self._sync_memory_index()
        logger.info("Memory deleted: %s", file_path)

    # ─── Index Management ─────────────────────────────────────────────

    async def reindex(self) -> int:
        """Full reindex: scan memories/*.md and rebuild SQLite index."""
        if not self._db:
            return 0

        md_files = list(Path(self.memories_dir).glob("*.md"))
        # Exclude MEMORY_INDEX.md
        md_files = [f for f in md_files if f.name != "MEMORY_INDEX.md"]

        # Get existing entries for incremental check
        async with self._lock:
            cursor = await self._db.execute("SELECT file_path, updated_at FROM memory_entries")
            existing = {row[0]: row[1] for row in await cursor.fetchall()}

        indexed = 0
        for md_file in md_files:
            file_path = md_file.name
            try:
                text = md_file.read_text(encoding="utf-8")
                metadata, body = _parse_frontmatter(text)

                if not metadata.get("name"):
                    continue

                # Skip if unchanged (incremental)
                file_updated = metadata.get("updated", "")
                if file_path in existing and existing[file_path] == file_updated:
                    indexed += 1
                    continue

                tags = metadata.get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]

                await self._index_entry(
                    file_path=file_path,
                    name=metadata.get("name", ""),
                    type=metadata.get("type", "reference"),
                    tags=tags,
                    summary=_extract_summary(body),
                    content=body,
                    created_at=metadata.get("created", ""),
                    updated_at=metadata.get("updated", ""),
                )
                indexed += 1
            except Exception as e:
                logger.warning("Failed to index %s: %s", file_path, e)

        # Remove entries for deleted files
        current_files = {f.name for f in md_files}
        stale = [fp for fp in existing if fp not in current_files]
        if stale:
            async with self._lock:
                for fp in stale:
                    await self._db.execute("DELETE FROM memory_entries WHERE file_path = ?", (fp,))
                await self._db.commit()

        logger.info("Reindex complete: %d entries indexed, %d stale removed", indexed, len(stale))
        return indexed

    async def count(self) -> int:
        """Return total number of indexed memories."""
        if not self._db:
            return 0
        async with self._lock:
            cursor = await self._db.execute("SELECT COUNT(*) FROM memory_entries")
            row = await cursor.fetchone()
            return row[0] if row else 0

    # ─── Internal Helpers ─────────────────────────────────────────────

    async def _index_entry(
        self,
        file_path: str,
        name: str,
        type: str,
        tags: list[str],
        summary: str,
        content: str,
        created_at: str,
        updated_at: str,
    ) -> None:
        """UPSERT a memory entry into SQLite."""
        tags_str = ", ".join(tags)
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO memory_entries (file_path, name, type, tags, summary, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    name=excluded.name,
                    type=excluded.type,
                    tags=excluded.tags,
                    summary=excluded.summary,
                    content=excluded.content,
                    updated_at=excluded.updated_at
                """,
                (file_path, name, type, tags_str, summary, content, created_at, updated_at),
            )
            await self._db.commit()

    async def _update_recall_counts(self, entry_ids: list[int]) -> None:
        """Increment recall_count and update last_recalled for given entries."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            for eid in entry_ids:
                await self._db.execute(
                    "UPDATE memory_entries SET recall_count = recall_count + 1, last_recalled = ? WHERE id = ?",
                    (now, eid),
                )
            await self._db.commit()

    async def _sync_memory_index(self) -> None:
        """Regenerate MEMORY_INDEX.md from current DB state."""
        if not self._db:
            return

        async with self._lock:
            cursor = await self._db.execute(
                "SELECT file_path, name, type, tags, summary FROM memory_entries ORDER BY updated_at DESC"
            )
            rows = await cursor.fetchall()

        lines = ["# Memory Index\n", ""]
        for row in rows:
            file_path, name, type_, tags, summary = row
            tag_str = f" #{type_}"
            if tags:
                for t in tags.split(", "):
                    if t and t != type_:
                        tag_str += f" #{t}"
            lines.append(f"- [{name}]({file_path}) — {summary}{tag_str}")

        index_path = os.path.join(self.memories_dir, "MEMORY_INDEX.md")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _prepare_fts_query(self, query: str) -> str:
        """Prepare user query for FTS5 trigram MATCH.

        Trigram tokenizer requires minimum 3 characters per token.
        For shorter tokens, we skip them (they won't match anyway).
        Multiple tokens are joined with OR for broader matching.
        """
        # Remove FTS5 operators that could cause syntax errors
        cleaned = re.sub(r'["\(\)\*\+\-\^{}]', " ", query)
        # Split into tokens
        tokens = [t.strip() for t in cleaned.split() if t.strip() and len(t.strip()) >= 3]
        if not tokens:
            # If all tokens are < 3 chars, try the full query as one phrase
            full = cleaned.strip()
            if len(full) >= 3:
                return full
            return ""
        if len(tokens) == 1:
            return tokens[0]
        # Join with OR for broader matching
        return " OR ".join(tokens[:10])

    def _row_to_entry(self, row) -> MemoryEntry:
        """Convert a DB row to MemoryEntry dataclass."""
        tags_str = row[4] if row[4] else ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        return MemoryEntry(
            id=row[0],
            file_path=row[1],
            name=row[2],
            type=row[3],
            tags=tags,
            summary=row[5],
            content=row[6],
            recall_count=row[7],
            last_recalled=row[8],
            created_at=row[9],
            updated_at=row[10],
            rank=row[11] if len(row) > 11 else 0.0,
        )
