# 06 Agent 容器（LangChain Deep Agents）

本文件描述運行在 Kubernetes Pod 內的 Agent 容器設計，基於 **LangChain Deep Agents** SDK，取代原 OpenClaw Runtime。包含 Deep Agents Runtime、FastAPI HTTP 層、MCP 相容介面、工作區初始化與恢復機制。

> **變更紀錄**：
> - Phase 2 原設計使用 OpenClaw Runtime + MCP，現改為 LangChain Deep Agents。
> - 儲存層原設計使用 PVC（EBS），現改為 **AWS S3**（透過自建 S3Backend + CompositeBackend），消除閒置成本與 AZ 限制。
>
> Deep Agents 提供：內建規劃（`write_todos`）、檔案操作、Shell 執行、子 Agent 派生（`task`）、長期記憶（LangGraph Store）、Human-in-the-loop。

---

## 0. 設計決策：為何選擇 Deep Agents

| 面向 | OpenClaw Runtime（原設計） | LangChain Deep Agents（新設計） |
|------|--------------------------|-------------------------------|
| Agent 核心 | 需自建 TaskExecutor + StateManager | 內建 planning、file ops、subagent、memory |
| 記憶持久化 | 自行 JSON 序列化至 PVC | LangGraph Store（跨 thread）+ S3 |
| 子任務派生 | 無原生支援 | 內建 `task` 工具，自動 context 隔離 |
| 模型支援 | 需自行整合 | model-agnostic，支援所有 tool-calling 模型 |
| LLM 呼叫 | 自行串接 API | 透過 `langchain.chat_models.init_chat_model` |
| Human-in-the-loop | 需自建 | 內建 `interrupt_on` + LangGraph checkpoint |
| 生態系 | 封閉 | LangChain / LangGraph 生態完整 |

**取捨**：Deep Agents 是 opinionated framework，自由度略低於完全自建，但大幅降低開發與維護成本。若未來需替換，可透過 FastAPI 適配層隔離。

---

## 1. Agent 容器基礎

### 1.1 容器鏡像構成

```dockerfile
# Dockerfile.agent
FROM python:3.13-slim

WORKDIR /app

# 系統依賴
RUN apt-get update && apt-get install -y \
    git \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Python 依賴
COPY requirements-agent.txt .
RUN pip install --no-cache-dir -r requirements-agent.txt

# 應用代碼
COPY app/ /app/app/
COPY docker/entrypoint-agent.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 健康檢查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.agent.main"]
```

**requirements-agent.txt**（核心依賴）：

```
deepagents>=0.1.0
langchain>=0.3.0
langgraph>=0.4.0
langgraph-checkpoint-postgres>=2.0.0
langgraph-store>=0.1.0
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
httpx>=0.28.0
psycopg[binary]>=3.2.0
pydantic>=2.10.0
pyyaml>=6.0
duckduckgo-search>=7.0.0
ddgs>=7.0.0
prometheus-client>=0.21.0
boto3>=1.35.0
aioboto3>=13.0.0
```

### 1.2 啟動入口

```bash
# docker/entrypoint-agent.sh
#!/bin/bash

set -e

echo "=== Agent Container Startup (Deep Agents + S3) ==="
echo "USER_ID: $USER_ID"
echo "POD_NAME: $POD_NAME"
echo "ORCHESTRATOR_URL: $ORCHESTRATOR_URL"
echo "S3_BUCKET: $S3_WORKSPACE_BUCKET"

# 1. 驗證 AWS credentials 可用（S3 作為儲存後端）
echo "Verifying S3 access..."
if ! aws s3 ls "s3://${S3_WORKSPACE_BUCKET}/users/${USER_ID}/" --region $AWS_REGION 2>/dev/null; then
    echo "S3 prefix does not exist yet — will be created on first write"
fi
echo "S3 access verified"

# 2. 建立本地暫存目錄（Pod 生命週期內的 ephemeral storage）
LOCAL_CACHE="/tmp/agent-cache"
mkdir -p "$LOCAL_CACHE/skills"
mkdir -p "$LOCAL_CACHE/logs"
echo "Local cache ready at $LOCAL_CACHE"

# 3. 啟動 Agent runtime
echo "Starting Deep Agents runtime..."
exec python -m app.agent.main
```

---

## 2. 整體架構

### 2.1 Agent Pod 內部架構

```
┌─────────────────────────────────────────────────────────────────┐
│  Agent Pod (:8080)                                              │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  FastAPI HTTP Layer                                       │  │
│  │  ├── /health, /readiness          (K8s probes)            │  │
│  │  ├── /api/v1/chat                 (主要對話入口)            │  │
│  │  ├── /api/v1/chat/stream          (串流回應)               │  │
│  │  ├── /api/v1/files/upload         (檔案上傳至 PVC)         │  │
│  │  ├── /api/v1/files/download       (從 PVC 下載檔案)        │  │
│  │  ├── /api/v1/files/list           (列出 PVC 目錄)          │  │
│  │  ├── /api/v1/files/delete         (刪除 PVC 檔案或目錄)    │  │
│  │  ├── /api/v1/files/mkdir          (建立 PVC 目錄)          │  │
│  │  ├── /mcp/execute                 (MCP 相容介面)           │  │
│  │  └── /api/v1/agent/status         (任務狀態查詢)           │  │
│  └───────────────────────┬───────────────────────────────────┘  │
│                          │                                      │
│  ┌───────────────────────▼───────────────────────────────────┐  │
│  │  DeepAgentsRuntime                                        │  │
│  │  ├── create_deep_agent()          (LangChain Deep Agents) │  │
│  │  ├── CompositeBackend                                       │  │
│  │  │   ├── default: S3Backend      → AWS S3 (使用者檔案)     │  │
│  │  │   └── /memories/: StoreBackend → LangGraph Store        │  │
│  │  ├── Checkpointer (PostgresSaver) → 共用 Orchestrator DB   │  │
│  │  ├── Custom Tools                 → 業務工具               │  │
│  │  └── Subagents                    → 子 Agent 派生          │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Ephemeral Storage: /tmp/agent-cache (Pod 生命週期)        │  │
│  │  ├── skills/         (技能定義快取)                        │  │
│  │  └── logs/           (本地日誌)                            │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘

          │ (非 PVC，直接透過 boto3)
          ▼
┌─────────────────────────────────────────────────────────────┐
│  AWS S3: s3://agent-workspaces-{account}/users/{user_id}/   │
│  ├── data/              (使用者共用資料 — 永久，跨 Pod)      │
│  ├── skills/            (技能定義 — 永久)                    │
│  ├── memories/          (由 LangGraph Store 管理，見 Layer 2) │
│  └── sessions/{sid}/    (session 專屬工作目錄，刪除時清除)    │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 與系統其他元件的關係

```
Client → Gateway (:8000)
           │
           ├── /api/v1/workspaces/ensure  → Orchestrator (建立/恢復 Pod)
           │
           └── /api/v1/chat, /mcp/*       → Agent Pod (:8080)
                                            │
                                            ├── FastAPI 接收 HTTP
                                            ├── Deep Agents 處理請求
                                            ├── S3 讀寫使用者檔案（boto3）
                                            └── PostgreSQL 存 checkpoint/memory
```

---

## 3. Deep Agents Runtime

### 3.1 Runtime 核心實現

```python
# app/agent/runtime.py
import logging
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from app.agent.backends.s3 import S3Backend
from app.agent.tools import get_workspace_tools
from app.agent.config import AgentConfig

logger = logging.getLogger(__name__)


class DeepAgentsRuntime:
    """基於 LangChain Deep Agents 的 Agent Runtime（S3 儲存）。"""

    def __init__(
        self,
        user_id: str,
        orchestrator_url: str,
        pod_name: str,
        config: AgentConfig,
    ):
        self.user_id = user_id
        self.orchestrator_url = orchestrator_url
        self.pod_name = pod_name
        self.config = config
        self.local_cache = Path(config.local_cache_path)

        self.agent = None
        self.checkpointer = None
        self.store = None
        self.s3_backend = None

    async def initialize(self) -> None:
        """初始化 Deep Agents runtime 所有元件。"""
        logger.info("Initializing Deep Agents runtime for user %s", self.user_id)

        # 1. 初始化本地暫存
        self.local_cache.mkdir(parents=True, exist_ok=True)
        (self.local_cache / "skills").mkdir(exist_ok=True)
        (self.local_cache / "logs").mkdir(exist_ok=True)

        # 2. 初始化 LangGraph Store（跨 thread 的長期記憶）
        self.store = AsyncPostgresStore(self.config.database_url)
        await self.store.setup()

        # 3. 初始化 Checkpointer（thread 內的短期記憶 + durable execution）
        self.checkpointer = AsyncPostgresSaver(self.config.database_url)
        await self.checkpointer.setup()

        # 4. 初始化 LLM model
        model = init_chat_model(
            model=self.config.model_name,
            model_provider=self.config.model_provider,
            temperature=self.config.temperature,
        )

        # 5. 初始化 S3Backend
        self.s3_backend = S3Backend(
            bucket=self.config.s3_bucket,
            prefix=f"users/{self.user_id}",
            region_name=self.config.s3_region,
        )

        # 6. 取得自訂工具
        tools = get_workspace_tools(
            s3_backend=self.s3_backend,
            user_id=self.user_id,
            orchestrator_url=self.orchestrator_url,
        )

        # 7. 定義子 Agent
        subagents = self._build_subagents(model)

        # 8. 建立 Deep Agent（CompositeBackend：S3 + Store）
        self.agent = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=self._build_system_prompt(),
            subagents=subagents,
            backend=lambda rt: CompositeBackend(
                # 預設：S3（使用者檔案永久保存，跨 Pod）
                default=self.s3_backend,
                # /memories/ 路徑 → LangGraph Store（結構化長期記憶）
                routes={
                    "/memories/": StoreBackend(
                        rt,
                        namespace=lambda ctx: ("memories", self.user_id),
                    ),
                },
            ),
            store=self.store,
            checkpointer=self.checkpointer,
            memory=[
                "/memories/instructions.txt",   # 使用者長期偏好
                "/memories/context.md",          # 跨 session 上下文
            ],
            skills=[str(self.local_cache / "skills")],
            interrupt_on={
                "write_file": False,
                "read_file": False,
                "edit_file": False,
                "execute": True,       # Shell 命令需視情況審批
            },
        )

        logger.info("Deep Agents runtime initialized (S3 bucket=%s)", self.config.s3_bucket)

    def _build_system_prompt(self) -> str:
        """建構系統提示詞。"""
        return f"""你是一個專屬於使用者 {self.user_id} 的 AI 助理。

你運行在一個 Kubernetes Pod 中，工作區資料永久保存於 AWS S3。

## 能力
- **規劃**：使用 write_todos 工具將複雜任務拆解為步驟
- **檔案操作**：可直接讀寫工作區內的檔案（存於 S3）
- **Shell 執行**：可在容器內執行 shell 命令
- **子任務派生**：可將子任務委派給專門的子 Agent
- **長期記憶**：可將重要資訊寫入 /memories/ 以跨 session 保存

## 工作區結構（S3）
- /data/              — 使用者共用資料（永久，所有 session 可讀寫）
- /memories/          — 跨 session 記憶（持久化，所有 session 可讀寫）
- /skills/            — 技能定義（永久）
- /sessions/{{sid}}/  — 各 session 專屬工作目錄（僅該 session 可寫入，session 刪除時清除）

## 存取規則
- 每個 session 的工作目錄在 /sessions/{{current_session_id}}/
- 可讀取其他 session 的資料夾，但不可寫入
- /data/ 和 /memories/ 為共用目錄，所有 session 皆可讀寫

## 注意事項
- 所有檔案操作透過 S3 後端，Pod 回收後資料完整保留
- Session 刪除時，對應的 /sessions/{{sid}}/ 會一併清除（Production: S3 prefix delete）
- 執行 shell 命令前請謹慎評估風險
- 重要發現請記錄到 /memories/ 以供未來使用
"""

    def _build_subagents(self, model) -> list[dict]:
        """定義子 Agent 配置。"""
        return [
            {
                "name": "data-analyst",
                "description": (
                    "專門處理資料分析任務。讀取資料檔案、執行統計分析、"
                    "生成視覺化圖表。適合處理 CSV/JSON 資料的分析需求。"
                ),
                "system_prompt": "你是一個資料分析專家。專注於讀取和分析 /data/ 目錄下的資料。",
                "tools": [],  # 繼承父 Agent 的檔案工具
            },
            {
                "name": "code-assistant",
                "description": (
                    "專門處理程式碼相關任務。撰寫、修改、除錯程式碼。"
                    "適合需要執行 shell 命令的開發任務。"
                ),
                "system_prompt": "你是一個程式開發助手。擅長撰寫和除錯程式碼，可執行 shell 命令驗證。",
                "tools": [],
            },
        ]

    async def invoke(
        self,
        message: str,
        session_id: str,
    ) -> dict[str, Any]:
        """處理一次使用者對話。"""
        result = self.agent.invoke(
            {
                "messages": [{"role": "user", "content": message}],
            },
            config={
                "configurable": {
                    "thread_id": session_id,
                    "user_id": self.user_id,
                },
            },
        )

        # 提取最後一條 AI 回應
        ai_message = result["messages"][-1]
        return {
            "content": ai_message.content,
            "session_id": session_id,
            "tool_calls": [
                {"name": tc.name, "args": tc.args}
                for tc in getattr(ai_message, "tool_calls", [])
            ] if hasattr(ai_message, "tool_calls") else [],
        }

    async def invoke_stream(self, message: str, session_id: str):
        """串流處理使用者對話。"""
        for chunk in self.agent.stream(
            {
                "messages": [{"role": "user", "content": message}],
            },
            config={
                "configurable": {
                    "thread_id": session_id,
                    "user_id": self.user_id,
                },
            },
            stream_mode="values",
        ):
            if chunk.get("messages"):
                yield chunk["messages"][-1]

    async def shutdown(self) -> None:
        """優雅關閉 — 保存進行中工作，釋放資源。

        觸發時機：K8s 發送 SIGTERM（Pod 被 reap 或手動刪除）。
        時間預算：terminationGracePeriodSeconds (30s) 內完成。
        """
        logger.info("Shutting down Deep Agents runtime")

        # 1) 標記為 shutting down — 拒絕新請求（readiness probe 回傳 503）
        self._shutting_down = True

        # 2) 等待進行中的請求完成（最多 10 秒）
        if self._active_requests > 0:
            logger.info("Waiting for %d active requests to complete", self._active_requests)
            await asyncio.wait_for(self._drain_event.wait(), timeout=10.0)

        # 3) 保存進行中的工作到 S3
        if self.s3_backend:
            try:
                await self.s3_backend.write_file(
                    "last_shutdown.json",
                    json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "reason": "sigterm"}),
                )
                logger.info("Shutdown state saved to S3")
            except Exception:
                logger.exception("Failed to save shutdown state to S3")

        # 4) 關閉 LangGraph checkpointer/store 連線
        if self.checkpointer:
            await self.checkpointer.close()
        if self.store:
            await self.store.close()

        # 5) 關閉 S3 backend
        if self.s3_backend:
            await self.s3_backend.close()

        logger.info("Deep Agents runtime shutdown complete")
```

### 3.2 Agent 配置

```python
# app/agent/config.py
import os
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Agent 容器配置（從環境變數讀取）。"""

    # 使用者身份
    user_id: str = field(default_factory=lambda: os.getenv("USER_ID", "unknown"))
    orchestrator_url: str = field(
        default_factory=lambda: os.getenv(
            "ORCHESTRATOR_URL",
            "http://orchestrator.agent-platform.svc.cluster.local:8080",
        )
    )
    pod_name: str = field(
        default_factory=lambda: os.getenv("POD_NAME", "unknown")
    )

    # LLM 模型
    model_name: str = field(
        default_factory=lambda: os.getenv("AGENT_MODEL", "claude-sonnet-4-5-20250929")
    )
    model_provider: str = field(
        default_factory=lambda: os.getenv("AGENT_MODEL_PROVIDER", "anthropic")
    )
    temperature: float = field(
        default_factory=lambda: float(os.getenv("AGENT_TEMPERATURE", "0.1"))
    )

    # AWS S3（使用者工作區儲存）
    s3_bucket: str = field(
        default_factory=lambda: os.getenv("S3_WORKSPACE_BUCKET", "agent-workspaces")
    )
    s3_region: str = field(
        default_factory=lambda: os.getenv("AWS_REGION", "ap-northeast-1")
    )
    # AWS credentials 透過 K8s ServiceAccount + IAM Role (IRSA) 注入
    # 或透過環境變數 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY

    # 本地暫存（Pod 生命週期內的 ephemeral storage）
    local_cache_path: str = field(
        default_factory=lambda: os.getenv("LOCAL_CACHE_PATH", "/tmp/agent-cache")
    )

    # 資料庫（與 Orchestrator 共用 PostgreSQL）
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "AGENT_DATABASE_URL",
            "postgresql://agent_user:password@postgres.agent-platform.svc.cluster.local:5432/agent_platform",
        )
    )

    # 閒置偵測
    idle_timeout_minutes: int = field(
        default_factory=lambda: int(os.getenv("IDLE_TIMEOUT_MINUTES", "30"))
    )
    idle_check_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("IDLE_CHECK_INTERVAL_SECONDS", "60"))
    )

    # Agent recursion limit (max tool call rounds)
    recursion_limit: int = field(
        default_factory=lambda: int(os.getenv("AGENT_RECURSION_LIMIT", "10"))
    )

    # Shared workspaces mount base path
    shared_base_path: str = field(
        default_factory=lambda: os.getenv("SHARED_BASE_PATH", "/shared")
    )

    # LLM API Keys（依 provider 設定，透過 K8s Secret 注入）
    # ANTHROPIC_API_KEY, OPENAI_API_KEY 等
```

---

## 4. FastAPI HTTP 層

### 4.1 主應用入口

```python
# app/agent/main.py
import asyncio
import signal
import logging
import argparse

from app.agent.runtime import DeepAgentsRuntime
from app.agent.config import AgentConfig
from app.agent.http_server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    """Agent 容器應用入口。"""
    config = AgentConfig()

    logger.info("=== Deep Agents Container ===")
    logger.info("User: %s", config.user_id)
    logger.info("S3 Bucket: %s", config.s3_bucket)
    logger.info("Model: %s (%s)", config.model_name, config.model_provider)

    # 初始化 Runtime
    runtime = DeepAgentsRuntime(
        user_id=config.user_id,
        orchestrator_url=config.orchestrator_url,
        pod_name=config.pod_name,
        config=config,
    )
    await runtime.initialize()

    # 建立 FastAPI app
    app = create_app(runtime)

    # 啟動 HTTP server
    import uvicorn

    shutdown_event = asyncio.Event()

    def handle_signal(signum: int):
        logger.info("Received signal %s, initiating shutdown", signum)
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for signum in [signal.SIGTERM, signal.SIGINT]:
        loop.add_signal_handler(signum, lambda sig=signum: handle_signal(sig))

    server_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )
    server = uvicorn.Server(server_config)

    # 並行運行 HTTP server 與 idle checker
    idle_task = asyncio.create_task(_idle_checker(runtime, config, shutdown_event))

    try:
        await server.serve()
    finally:
        shutdown_event.set()
        await idle_task
        await runtime.shutdown()


async def _idle_checker(runtime, config, shutdown_event):
    """背景任務：偵測閒置超時並通知 Orchestrator 回收。"""
    import httpx
    from datetime import datetime, timezone

    last_active = datetime.now(timezone.utc)

    while not shutdown_event.is_set():
        await asyncio.sleep(config.idle_check_interval_seconds)

        # 檢查是否有新的對話活動
        # （由 FastAPI middleware 更新 _last_active_at）
        from app.agent.http_server import get_last_active_at
        current_active = get_last_active_at()
        if current_active != last_active:
            last_active = current_active
            continue

        idle_seconds = (datetime.now(timezone.utc) - last_active).total_seconds()
        if idle_seconds >= config.idle_timeout_minutes * 60:
            logger.warning(
                "Idle timeout (%ds). Notifying Orchestrator to reap.",
                idle_seconds,
            )
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        f"{config.orchestrator_url}/api/v1/orchestrator/reap-self",
                        json={"user_id": config.user_id},
                    )
            except Exception:
                logger.exception("Failed to notify Orchestrator for self-reap")
            break


if __name__ == "__main__":
    asyncio.run(main())
```

### 4.2 FastAPI HTTP Server

```python
# app/agent/http_server.py
import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.runtime import DeepAgentsRuntime

# 全域活動時間戳（供 idle checker 讀取）
_last_active_at: datetime = datetime.now(timezone.utc)


def get_last_active_at() -> datetime:
    return _last_active_at


def create_app(runtime: DeepAgentsRuntime) -> FastAPI:
    """建立 FastAPI 應用。"""
    app = FastAPI(title="Deep Agent Container", version="2.0")

    # ── Middleware：追蹤活動時間 ────────────────────────────────

    @app.middleware("http")
    async def track_activity(request: Request, call_next):
        global _last_active_at
        if request.url.path not in ("/health", "/readiness"):
            _last_active_at = datetime.now(timezone.utc)
        return await call_next(request)

    # ── K8s Probes ─────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": runtime.user_id,
            "idle_seconds": int(
                (datetime.now(timezone.utc) - _last_active_at).total_seconds()
            ),
        }

    @app.get("/readiness")
    async def readiness():
        s3_ok = runtime.s3_backend is not None
        agent_ok = runtime.agent is not None
        return {
            "status": "ready" if (s3_ok and agent_ok) else "not_ready",
            "dependencies": {
                "s3_backend": "initialized" if s3_ok else "missing",
                "agent": "initialized" if agent_ok else "initializing",
            },
        }

    # ── 主要對話 API ──────────────────────────────────────────

    class ChatRequest(BaseModel):
        message: str
        session_id: str

    class ChatResponse(BaseModel):
        content: str
        session_id: str
        tool_calls: list[dict[str, Any]] = []

    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        """同步對話介面。"""
        result = await runtime.invoke(
            message=req.message,
            session_id=req.session_id,
        )
        return ChatResponse(**result)

    @app.post("/api/v1/chat/stream")
    async def chat_stream(req: ChatRequest):
        """串流對話介面（SSE）。

        Event types:
        - event: content      — AI response text chunk
        - event: tool_call    — AI requesting a tool call
        - event: tool_result  — tool execution result
        - event: file         — file produced by tool (image, document, etc.)
        - event: error        — agent error (recursion limit, LLM failure, etc.)
        - : thinking          — SSE comment keepalive during LLM thinking phase
        - data: [DONE]        — stream complete
        """
        async def generate():
            async for chunk, metadata in runtime.invoke_stream(
                message=req.message,
                session_id=req.session_id,
            ):
                role = getattr(chunk, "type", "unknown")
                content = getattr(chunk, "content", "")
                tool_calls = getattr(chunk, "tool_calls", None)

                if role == "human":
                    continue

                if role in ("ai", "AIMessageChunk") and content and content.strip():
                    data = json.dumps({"content": content}, ensure_ascii=False)
                    yield f"event: content\ndata: {data}\n\n"
                elif role in ("ai", "AIMessageChunk") and not content and not tool_calls:
                    # Thinking phase: yield SSE comment as keepalive
                    yield ": thinking\n\n"

                if role in ("ai", "AIMessageChunk") and tool_calls:
                    for tc in tool_calls:
                        data = json.dumps(
                            {"name": tc["name"], "args": tc.get("args", {})},
                            ensure_ascii=False,
                        )
                        yield f"event: tool_call\ndata: {data}\n\n"

                if role == "tool" and content:
                    data = json.dumps(
                        {"tool_name": getattr(chunk, "name", None), "content": content},
                        ensure_ascii=False,
                    )
                    yield f"event: tool_result\ndata: {data}\n\n"

                    # Detect file outputs and emit file events
                    files = _extract_files(content)
                    for filepath in files:
                        if not filepath.startswith("sessions/"):
                            filepath = f"sessions/{req.session_id}/{filepath}"
                        ext = Path(filepath).suffix.lower()
                        file_type = "image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp") else "document"
                        fdata = json.dumps(
                            {"path": filepath, "type": file_type, "name": Path(filepath).name},
                            ensure_ascii=False,
                        )
                        yield f"event: file\ndata: {fdata}\n\n"

            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
        )

    # ── MCP 相容介面（向後相容） ───────────────────────────────

    class MCPRequest(BaseModel):
        jsonrpc: str = "2.0"
        method: str
        params: dict[str, Any] = {}
        id: str | None = None

    class MCPResponse(BaseModel):
        jsonrpc: str = "2.0"
        result: Any | None = None
        error: dict[str, Any] | None = None
        id: str | None = None

    @app.post("/mcp/execute", response_model=MCPResponse)
    async def mcp_execute(req: MCPRequest):
        """MCP 相容介面：將 MCP method 映射到 Deep Agent 的自然語言呼叫。"""
        try:
            # 將 MCP 結構化請求轉譯為自然語言
            prompt = _mcp_to_prompt(req.method, req.params)
            result = await runtime.invoke(
                message=prompt,
                session_id=f"mcp-{req.id or 'default'}",
            )
            return MCPResponse(
                result={"content": result["content"]},
                id=req.id,
            )
        except Exception as e:
            return MCPResponse(
                error={"code": -1, "message": str(e)},
                id=req.id,
            )

    def _mcp_to_prompt(method: str, params: dict) -> str:
        """將 MCP 結構化請求轉譯為自然語言 prompt。"""
        if method == "execute_task":
            return f"請執行以下任務：{params.get('task_type', 'general')}。參數：{params.get('parameters', {})}"
        elif method == "read_file":
            return f"請讀取檔案 {params.get('path')} 的內容"
        elif method == "write_file":
            return f"請將以下內容寫入檔案 {params.get('path')}：{params.get('content', '')}"
        elif method == "list_files":
            return f"請列出 {params.get('path', '/data/')} 目錄下的所有檔案"
        else:
            return str(params)

    # ── 任務狀態查詢 ──────────────────────────────────────────

    @app.get("/api/v1/agent/status")
    async def agent_status():
        """查詢 Agent 當前狀態。"""
        return {
            "user_id": runtime.user_id,
            "s3_bucket": runtime.config.s3_bucket,
            "model": runtime.config.model_name,
            "status": "ready",
        }

    return app
```

---

## 5. S3Backend 實作

### 5.1 自訂 S3Backend

Deep Agents 的 FilesystemBackend 只支援本地磁碟。我們自建 `S3Backend`，實作 Deep Agents backend 介面，直接透過 `aioboto3` 存取 S3：

```python
# app/agent/backends/s3.py
import logging
from typing import AsyncIterator

import aioboto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)


class S3Backend:
    """Deep Agents filesystem backend backed by AWS S3。

    不依賴 FUSE，直接透過 S3 API 做檔案操作。
    每個使用者對應 S3 prefix：s3://{bucket}/users/{user_id}/
    """

    def __init__(
        self,
        bucket: str,
        prefix: str,
        region_name: str = "ap-northeast-1",
    ):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.region_name = region_name
        self._session = aioboto3.Session()

    def _key(self, path: str) -> str:
        """將相對路徑轉為 S3 object key。"""
        clean = path.lstrip("/")
        return f"{self.prefix}/{clean}"

    # ── 讀取 ──────────────────────────────────────────────────

    async def read_file(self, path: str) -> str:
        """讀取檔案內容。"""
        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            resp = await s3.get_object(
                Bucket=self.bucket, Key=self._key(path)
            )
            content = await resp["Body"].read()
            return content.decode("utf-8")

    async def read_file_bytes(self, path: str) -> bytes:
        """讀取檔案二進位內容。"""
        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            resp = await s3.get_object(
                Bucket=self.bucket, Key=self._key(path)
            )
            return await resp["Body"].read()

    # ── 寫入 ──────────────────────────────────────────────────

    async def write_file(self, path: str, content: str) -> None:
        """寫入文字檔案。"""
        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=self._key(path),
                Body=content.encode("utf-8"),
                ContentType="text/plain; charset=utf-8",
            )

    async def write_file_bytes(self, path: str, data: bytes) -> None:
        """寫入二進位檔案。"""
        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=self._key(path),
                Body=data,
            )

    # ── 編輯 ──────────────────────────────────────────────────

    async def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        """精確字串替換。"""
        content = await self.read_file(path)
        if old_str not in content:
            raise ValueError(f"String not found in {path}")
        new_content = content.replace(old_str, new_str, 1)
        await self.write_file(path, new_content)
        return f"Replaced 1 occurrence in {path}"

    # ── 列出 / 刪除 ──────────────────────────────────────────

    async def ls(self, path: str = "") -> list[dict]:
        """列出目錄內容。"""
        prefix = self._key(path)
        if not prefix.endswith("/"):
            prefix += "/"

        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            items = []
            async for page in paginator.paginate(
                Bucket=self.bucket, Prefix=prefix, Delimiter="/"
            ):
                # 檔案
                for obj in page.get("Contents", []):
                    name = obj["Key"].removeprefix(prefix)
                    if name:  # 排除 prefix 本身
                        items.append({
                            "name": name,
                            "type": "file",
                            "size": obj["Size"],
                            "last_modified": obj["LastModified"].isoformat(),
                        })
                # 子目錄
                for pfx in page.get("CommonPrefixes", []):
                    name = pfx["Prefix"].removeprefix(prefix).rstrip("/")
                    if name:
                        items.append({"name": name, "type": "dir"})
            return items

    async def delete_file(self, path: str) -> None:
        """刪除檔案。"""
        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            await s3.delete_object(Bucket=self.bucket, Key=self._key(path))

    async def file_exists(self, path: str) -> bool:
        """檢查檔案是否存在。"""
        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            try:
                await s3.head_object(Bucket=self.bucket, Key=self._key(path))
                return True
            except s3.exceptions.ClientError:
                return False

    # ── Glob / Grep（供 Deep Agents 內建工具用） ─────────────

    async def glob(self, pattern: str, path: str = "") -> list[str]:
        """簡易 glob：列出所有匹配的檔案 key。"""
        import fnmatch
        prefix = self._key(path)
        if not prefix.endswith("/"):
            prefix += "/"

        async with self._session.client(
            "s3", region_name=self.region_name
        ) as s3:
            keys = []
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=self.bucket, Prefix=prefix
            ):
                for obj in page.get("Contents", []):
                    rel = obj["Key"].removeprefix(prefix)
                    if rel and fnmatch.fnmatch(rel, pattern):
                        keys.append(rel)
            return keys

    async def grep(
        self, pattern: str, path: str = "", ignore_case: bool = False
    ) -> list[dict]:
        """搜尋檔案內容。"""
        import re
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
        files = await self.glob("**/*", path)
        results = []
        for f in files[:50]:  # 限制搜尋量
            try:
                content = await self.read_file(f)
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        results.append({"file": f, "line": i, "content": line})
            except Exception:
                continue
        return results
```

### 5.2 S3 Bucket 規劃

```yaml
# S3 Bucket 結構
s3://agent-workspaces-{account_id}/
  users/
    {user_id_1}/
      data/              # 使用者共用資料（CSV、JSON、文件等）
        project-a/
        datasets/
      skills/            # 技能定義腳本
      memories/          # 跨 session 記憶（LangGraph Store 管理）
      sessions/          # 各 session 專屬工作目錄
        {session_id_1}/
        {session_id_2}/
    {user_id_2}/
      ...
```

**S3 Bucket 設定建議**：

```hcl
# Terraform: S3 Bucket 配置
resource "aws_s3_bucket" "agent_workspaces" {
  bucket = "agent-workspaces-${data.aws_caller_identity.current.account_id}"
}

# 版本控制（防止誤刪）
resource "aws_s3_bucket_versioning" "agent_workspaces" {
  bucket = aws_s3_bucket.agent_workspaces.id
  versioning_configuration { status = "Enabled" }
}

# 加密（SSE-S3）
resource "aws_s3_bucket_server_side_encryption_configuration" "agent_workspaces" {
  bucket = aws_s3_bucket.agent_workspaces.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
}
```

---

## 6. 自訂工具

### 6.1 業務工具定義

```python
# app/agent/tools.py
import asyncio
import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from app.agent.backends.s3 import S3Backend

logger = logging.getLogger(__name__)


def get_workspace_tools(
    s3_backend: S3Backend,
    user_id: str,
    orchestrator_url: str,
) -> list:
    """回傳 Deep Agent 可用的自訂工具清單（S3 後端）。"""

    @tool
    def notify_orchestrator_activity(session_id: str) -> str:
        """通知 Orchestrator 更新 session 活動時間戳。
        在執行耗時任務後呼叫，避免被誤判為閒置。"""
        async def _notify():
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{orchestrator_url}/api/v1/orchestrator/activity",
                    json={"user_id": user_id, "session_id": session_id},
                )
        try:
            asyncio.get_event_loop().run_until_complete(_notify())
            return "Activity reported"
        except Exception as e:
            return f"Failed: {e}"

    @tool
    async def list_workspace_files(
        path: Annotated[str, "相對於工作區根目錄的路徑，例如 'data/'"] = "data/",
    ) -> str:
        """列出工作區指定目錄下的檔案（S3）。"""
        try:
            items = await s3_backend.ls(path)
            if not items:
                return "（空目錄）"
            lines = []
            for it in items:
                kind = "[DIR]" if it["type"] == "dir" else "     "
                size = it.get("size", "-")
                lines.append(f"{kind} {it['name']} ({size})")
            return "\n".join(lines)
        except Exception as e:
            return f"錯誤：{e}"

    @tool
    async def read_workspace_file(
        path: Annotated[str, "相對於工作區根目錄的檔案路徑"],
    ) -> str:
        """讀取工作區內的檔案內容（S3）。"""
        try:
            return await s3_backend.read_file(path)
        except Exception as e:
            return f"讀取失敗：{e}"

    @tool
    async def write_workspace_file(
        path: Annotated[str, "相對於工作區根目錄的檔案路徑"],
        content: Annotated[str, "要寫入的內容"],
    ) -> str:
        """將內容寫入工作區內的檔案（S3）。"""
        try:
            await s3_backend.write_file(path, content)
            return f"已寫入 {path}（{len(content)} 字元）"
        except Exception as e:
            return f"寫入失敗：{e}"

    return [
        notify_orchestrator_activity,
        list_workspace_files,
        read_workspace_file,
        write_workspace_file,
    ]
```

> **POC 實作差異**：POC 版本使用 `SandboxedShellTool` 和 `SandboxedPythonREPLTool` 取代 S3 檔案工具，
> 提供 workspace 邊界硬限制（cwd 強制為 session 目錄、escape pattern 攔截、open() patch）。
> Per-session agent cache 確保每個 session 的 tools 綁定到 `sessions/{session_id}/` 目錄。
> Production 版本應在 S3Backend 層面實作同等的 session 隔離。

---

## 7. 記憶與狀態持久化

### 7.1 三層記憶架構

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: Thread 記憶（短期）                                │
│  機制：LangGraph Checkpointer (PostgresSaver)               │
│  生命週期：一個 session（thread_id = session_id）             │
│  內容：對話歷史、工具呼叫結果、Agent 中間狀態                 │
│  Pod 回收後：保留在 PostgreSQL，下次恢復可繼續               │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: User 記憶（長期）                                  │
│  機制：LangGraph Store (AsyncPostgresStore)                  │
│  生命週期：永久（跨所有 session）                             │
│  路徑：/memories/ → StoreBackend namespace=(memories, uid)   │
│  內容：使用者偏好、常用指令、專案上下文                       │
│  Pod 回收後：完整保留在 PostgreSQL                            │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: 檔案系統（工作區）                                  │
│  機制：S3Backend → AWS S3 (透過 aioboto3)                   │
│  生命週期：永久（S3 物件不隨 Pod 刪除）                      │
│  路徑：/data/, /skills/, /memories/, /sessions/{sid}/        │
│  內容：使用者共用檔案、技能腳本、記憶、session 工作目錄       │
│  Pod 回收後：完整保留在 S3，無 AZ 限制                       │
│  Session 刪除時：/sessions/{sid}/ 透過 S3 prefix delete 清除 │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 狀態恢復流程

Pod 被回收後重新啟動時的恢復（無 PVC，全部從 PostgreSQL + S3 恢復）：

```
1. Pod 啟動 → 驗證 S3 存取權限（IRSA）
   ├── S3 bucket 可存取，使用者 prefix 存在或首次建立
   └── /data/, /skills/, /memories/, /sessions/ 全部保留在 S3

2. DeepAgentsRuntime.initialize()
   ├── PostgresStore 連線 → Layer 2 長期記憶自動載入
   ├── PostgresSaver 連線 → Layer 1 thread 狀態可透過 thread_id 恢復
   └── S3Backend 初始化 → Layer 3 檔案系統可用（透過 S3 API）

3. 使用者發送新訊息，指定 session_id
   ├── 若 thread_id 存在 → Checkpointer 載入歷史繼續對話
   └── 若新 thread_id → 全新對話，但 /memories/ 的長期記憶可用
```

> **與 PVC 的差異**：S3 沒有 AZ 限制，新 Pod 可排程到任何節點；無閒置 EBS 成本；
> 但 S3 不支援 POSIX 原子操作，需透過 S3Backend 的 API 呼叫。

### 7.3 資料庫使用策略

Agent 與 Orchestrator **共用同一個 PostgreSQL**，但使用不同用途：

| 元件 | 使用的表 | 說明 |
|------|---------|------|
| Orchestrator | users, workspaces, sessions, activity_logs, pod_states | 業務狀態管理 |
| Agent (Checkpointer) | checkpoints, checkpoint_blobs, checkpoint_writes | LangGraph 自動建立，存 thread 狀態 |
| Agent (Store) | store | LangGraph 自動建立，存長期記憶 |

無需額外建表，LangGraph 的 `setup()` 會自動建立所需的 schema。

> **注意**：原設計中的 `pvc_states` 表已不需要（S3 無對等概念），Phase 1 可移除此表。

---

## 8. 與 Orchestrator / Gateway 的介面變更

### 8.1 API 變更對照

| 原介面 (MCP) | 新介面 (Deep Agents) | 說明 |
|-------------|---------------------|------|
| `POST /mcp/execute` | `POST /api/v1/chat` | 主要對話入口 |
| - | `POST /api/v1/chat/stream` | 新增串流回應 |
| `POST /mcp/execute` (method=list_files) | `POST /api/v1/chat` (自然語言) | MCP 相容仍可用 |
| `GET /health` | `GET /health` | 不變 |
| `GET /readiness` | `GET /readiness` | 不變 |

### 8.2 Gateway 路由更新

Gateway 需要增加對 `/api/v1/chat` 和 `/api/v1/chat/stream` 的路由：

```python
# Gateway 路由更新（poc/gateway/main.py）
# 新增路由：

# 主要對話介面
proxy_routes = [
    ("/api/v1/chat", "POST"),          # 同步對話
    ("/api/v1/chat/stream", "POST"),   # 串流對話
    ("/api/v1/agent/status", "GET"),   # Agent 狀態
    ("/mcp/execute", "POST"),          # MCP 相容（向後相容）
]
```

### 8.3 Orchestrator 變更

Orchestrator 不需大幅修改，但需注意：

1. **PVC 管理遷移至 Storage Service**：Orchestrator 的 PVC 建立委派給獨立 Storage Service（:8091），Orchestrator 僅管理 Pod + Service。Production 改用 S3 後不再需要 PVC。
2. **新增 S3 相關環境變數**：`S3_WORKSPACE_BUCKET`、`AWS_REGION`
3. **新增 IAM Role (IRSA)**：Agent Pod 需要透過 K8s ServiceAccount 綁定 IAM Role

4. **K8s Secret**：需建立包含 LLM API Keys 的 Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agent-api-keys
  namespace: agent-platform
type: Opaque
stringData:
  ANTHROPIC_API_KEY: "sk-ant-..."
  # OPENAI_API_KEY: "sk-..."
```

5. **Pod YAML 更新**：注入 Secret、S3 環境變數、IRSA ServiceAccount

```yaml
spec:
  serviceAccountName: agent-s3-access  # IRSA 綁定
  containers:
  - name: agent
    env:
    - name: AGENT_DATABASE_URL
      value: "postgresql://agent_user:$(DB_PASSWORD)@postgres:5432/agent_platform"
    - name: AGENT_MODEL
      value: "claude-sonnet-4-5-20250929"
    - name: AGENT_MODEL_PROVIDER
      value: "anthropic"
    - name: S3_WORKSPACE_BUCKET
      value: "agent-workspaces-$(AWS_ACCOUNT_ID)"
    - name: AWS_REGION
      value: "ap-northeast-1"
    envFrom:
    - secretRef:
        name: agent-api-keys
```

6. **IRSA 設定**（Terraform）：

```hcl
# IAM Role for Agent Pods
data "aws_iam_policy_document" "agent_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = ["system:serviceaccount:agent-platform:agent-s3-access"]
    }
  }
}

resource "aws_iam_role" "agent_role" {
  name               = "k8s-agent-s3-role"
  assume_role_policy = data.aws_iam_policy_document.agent_assume_role.json
}

resource "aws_iam_role_policy_attachment" "agent_s3" {
  role       = aws_iam_role.agent_role.name
  policy_arn = aws_iam_policy.agent_s3_policy.arn
}

# S3 存取策略（最小權限）
resource "aws_iam_policy" "agent_s3_policy" {
  name = "k8s-agent-s3-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
          "s3:ListBucket", "s3:HeadObject",
        ]
        Resource = [
          "${aws_s3_bucket.agent_workspaces.arn}",
          "${aws_s3_bucket.agent_workspaces.arn}/users/*",
        ]
      },
    ]
  })
}

# K8s ServiceAccount（綁定 IAM Role）
resource "kubernetes_service_account" "agent_s3" {
  metadata {
    name      = "agent-s3-access"
    namespace = "agent-platform"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.agent_role.arn
    }
  }
}
```

---

## 9. 容器資源配置

### 9.1 資源限制

Deep Agents 因包含 LLM API 呼叫（非本地推理），資源需求主要在網路 I/O 與記憶體（對話上下文）：

```yaml
# standard tier
requests:
  cpu: 200m
  memory: 512Mi
limits:
  cpu: 1000m
  memory: 2Gi

# premium tier
requests:
  cpu: 500m
  memory: 1Gi
limits:
  cpu: 2000m
  memory: 4Gi

# enterprise tier
requests:
  cpu: 1000m
  memory: 2Gi
limits:
  cpu: 4000m
  memory: 8Gi
```

> 相比原設計，記憶體需求提高（Deep Agents 的 context 管理 + LangGraph 狀態）。

### 9.2 存儲策略

**不再使用 PVC**。Agent Pod 僅使用 ephemeral storage（`/tmp/agent-cache`）作為 Pod 生命週期內的暫存：

```yaml
# Pod 不掛載任何 PVC
volumes:
- name: cache
  emptyDir:
    medium: ""        # 使用節點磁碟
    sizeLimit: 500Mi

volumeMounts:
- name: cache
  mountPath: /tmp/agent-cache
```

> 所有永久性資料透過 S3Backend 存取 AWS S3，Pod 可自由排程到任何 AZ 的任何節點。

---

## 10. 監控與指標

### 10.1 Prometheus 指標

```python
# app/agent/metrics.py
from prometheus_client import Counter, Histogram, Gauge

# 對話相關
chat_requests_total = Counter(
    "agent_chat_requests_total",
    "Total chat requests processed",
    ["user_id", "model"],
)

chat_duration_seconds = Histogram(
    "agent_chat_duration_seconds",
    "Chat request duration",
    ["user_id", "model"],
    buckets=[1, 5, 10, 30, 60, 120, 300],
)

# 工具呼叫
tool_calls_total = Counter(
    "agent_tool_calls_total",
    "Total tool calls by Deep Agent",
    ["user_id", "tool_name"],
)

# Token 使用（若 API 回報）
tokens_used_total = Counter(
    "agent_tokens_used_total",
    "Total tokens consumed",
    ["user_id", "model", "type"],  # type: input/output
)

# S3 操作
s3_operations_total = Counter(
    "agent_s3_operations_total",
    "S3 operations by type",
    ["user_id", "operation"],  # operation: get_object/put_object/list_objects/...
)

s3_operation_duration = Histogram(
    "agent_s3_operation_duration_seconds",
    "S3 operation latency",
    ["user_id", "operation"],
    buckets=[0.1, 0.5, 1, 2, 5, 10],
)

# 記憶
memory_operations = Counter(
    "agent_memory_operations_total",
    "Memory read/write operations",
    ["user_id", "layer"],  # layer: thread/store/s3
)
```

### 10.2 觀測性

- **LangSmith**：設定 `LANGSMITH_TRACING=true` 可自動追蹤所有 Agent 執行
- **結構化日誌**：所有日誌包含 `user_id`、`session_id`、`thread_id`
- **S3 CloudWatch**：啟用 S3 存取日誌 + CloudWatch 指標監控 latency/error rate

---

## 11. 容器構建與推送

```bash
# 構建鏡像
docker build \
  -f docker/Dockerfile.agent \
  -t your-registry/k8s-agent-deep:latest \
  -t your-registry/k8s-agent-deep:v2.0.0 \
  .

# 推送到倉庫
docker push your-registry/k8s-agent-deep:latest
docker push your-registry/k8s-agent-deep:v2.0.0
```

---

## 12. 遷移計畫

### 12.1 從 POC (Phase 0) 到 Phase 2

| 步驟 | 說明 | 依賴 |
|------|------|------|
| 2.1 | 安裝 `deepagents` 套件，驗證基本 `create_deep_agent` 可用 | Phase 1 (PostgreSQL) |
| 2.2 | 實作 `S3Backend`（aioboto3），驗證 S3 讀寫可用 | 2.1 |
| 2.3 | 實作 `DeepAgentsRuntime` + `CompositeBackend(S3Backend + StoreBackend)` | 2.2 |
| 2.4 | 實作 FastAPI HTTP 層 (`/api/v1/chat`) | 2.3 |
| 2.5 | 整合 `AsyncPostgresSaver` checkpointer | 2.4, Phase 1 |
| 2.6 | 整合 `AsyncPostgresStore` 長期記憶 | 2.5 |
| 2.7 | 實作 MCP 相容介面 (`/mcp/execute`) | 2.4 |
| 2.8 | 加入自訂工具 + 子 Agent 配置 | 2.6 |
| 2.9 | 設定 IRSA（IAM Role + K8s ServiceAccount）| 2.2 |
| 2.10 | 更新 K8s manifests (Secret, env vars, ServiceAccount) | 2.8, 2.9 |
| 2.11 | 更新 Gateway 路由 | 2.4 |
| 2.12 | E2E 測試：建立 workspace → 對話 → 閒置回收 → S3 資料保留 → 恢復 | 2.10 |

### 12.2 可獨立驗證的里程碑

- **2.1~2.4**：本地啟動 FastAPI + Deep Agents + S3Backend，可用 curl 發送對話請求
- **2.5~2.6**：對話記憶可跨 request 保留，Pod 重啟後從 PostgreSQL + S3 恢復
- **2.9~2.12**：完整 K8s 部署（含 IRSA），Gateway → Agent 對話流程暢通