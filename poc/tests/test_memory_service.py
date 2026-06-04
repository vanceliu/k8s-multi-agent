"""Unit tests for MemoryService (SQLite FTS5)."""

import asyncio
import os
import tempfile

import pytest
import pytest_asyncio

from poc.agent.memory_service import MemoryService

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def memory_service():
    """Create a MemoryService with a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        memories_dir = os.path.join(tmpdir, "memories")
        index_path = os.path.join(memories_dir, ".index", "memory.db")
        svc = MemoryService(
            workspace_id="test-ws",
            memories_dir=memories_dir,
            index_path=index_path,
        )
        await svc.initialize()
        yield svc
        await svc.close()


@pytest.mark.asyncio
async def test_save_and_search(memory_service):
    """Save a memory and verify it can be found via FTS search."""
    file_path = await memory_service.store(
        name="使用者偏好",
        type="user",
        tags=["語言", "風格"],
        content="使用者偏好繁體中文回覆，喜歡簡潔風格。",
        why="使用者明確要求",
        how="所有回覆用繁中，避免冗長",
    )

    assert file_path.endswith(".md")
    assert os.path.exists(os.path.join(memory_service.memories_dir, file_path))

    # Search by content keyword
    results = await memory_service.search("繁體中文")
    assert len(results) >= 1
    assert results[0].name == "使用者偏好"
    assert results[0].type == "user"

    # Search by tag
    results = await memory_service.search("語言")
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_save_and_count(memory_service):
    """Save multiple memories and verify count."""
    await memory_service.store("記憶一", "project", ["tag1"], "內容一", "", "")
    await memory_service.store("記憶二", "feedback", ["tag2"], "內容二", "", "")
    await memory_service.store("記憶三", "reference", ["tag3"], "內容三", "", "")

    count = await memory_service.count()
    assert count == 3


@pytest.mark.asyncio
async def test_update(memory_service):
    """Update a memory and verify changes are indexed."""
    file_path = await memory_service.store(
        name="專案截止日",
        type="project",
        tags=["deadline"],
        content="Phase 1 截止日 2026-06-01",
        why="PM 確認",
        how="排程時注意",
    )

    await memory_service.update(file_path, content="Phase 1 截止日延至 2026-07-01")

    results = await memory_service.search("2026-07-01")
    assert len(results) >= 1
    assert "2026-07-01" in results[0].content


@pytest.mark.asyncio
async def test_delete(memory_service):
    """Delete a memory and verify it's removed from index."""
    file_path = await memory_service.store(
        name="暫時記憶",
        type="reference",
        tags=[],
        content="這個記憶會被刪除",
        why="",
        how="",
    )

    await memory_service.delete(file_path)

    assert not os.path.exists(os.path.join(memory_service.memories_dir, file_path))
    results = await memory_service.search("會被刪除")
    assert len(results) == 0


@pytest.mark.asyncio
async def test_type_filter(memory_service):
    """Search with type filter."""
    await memory_service.store("用戶資料", "user", [], "使用者是後端工程師", "", "")
    await memory_service.store("專案資料", "project", [], "使用者正在做後端重構", "", "")

    # Search all
    results = await memory_service.search("後端")
    assert len(results) == 2

    # Filter by type
    results = await memory_service.search("後端", type_filter="user")
    assert len(results) == 1
    assert results[0].type == "user"


@pytest.mark.asyncio
async def test_recall(memory_service):
    """Test active memory recall formatting."""
    await memory_service.store(
        name="Auth 架構",
        type="project",
        tags=["architecture"],
        content="Auth 放在 Gateway，Admin 獨立 Pod。",
        why="關注點分離",
        how="設計新元件時遵循此分層",
    )

    context = await memory_service.recall("Gateway 的認證怎麼做的？")
    assert context is not None
    assert "Auth 架構" in context
    assert "Gateway" in context


@pytest.mark.asyncio
async def test_recall_no_results(memory_service):
    """Recall returns None when no relevant memories exist."""
    context = await memory_service.recall("完全不相關的查詢 xyz123")
    assert context is None


@pytest.mark.asyncio
async def test_reindex_from_files(memory_service):
    """Reindex picks up manually created markdown files."""
    # Manually write a memory file
    md_content = """---
name: 手動記憶
type: feedback
tags: [test]
created: 2026-05-15
updated: 2026-05-15
---

不要在回覆結尾加摘要。

**Why:** 使用者覺得冗餘
**How to apply:** 回覆結束直接停止
"""
    file_path = os.path.join(memory_service.memories_dir, "manual_memory.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    count = await memory_service.reindex()
    assert count >= 1

    results = await memory_service.search("摘要")
    assert len(results) >= 1
    assert results[0].name == "手動記憶"


@pytest.mark.asyncio
async def test_memory_index_md_sync(memory_service):
    """MEMORY_INDEX.md is updated after save/delete."""
    await memory_service.store("測試記憶", "user", ["tag"], "內容", "", "")

    index_path = os.path.join(memory_service.memories_dir, "MEMORY_INDEX.md")
    assert os.path.exists(index_path)

    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "測試記憶" in content


@pytest.mark.asyncio
async def test_duplicate_filename_handling(memory_service):
    """Saving memories with same name creates unique filenames."""
    path1 = await memory_service.store("同名記憶", "user", [], "內容一", "", "")
    path2 = await memory_service.store("同名記憶", "user", [], "內容二", "", "")

    assert path1 != path2
    assert await memory_service.count() == 2
