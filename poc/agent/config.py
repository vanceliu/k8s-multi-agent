"""Agent container configuration (from environment variables).

Mirrors doc 06 §3.2 AgentConfig, with S3 fields replaced by local PVC paths.
"""

import os
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Agent 容器配置（從環境變數讀取）。"""

    # Workspace identity
    workspace_id: str = field(
        default_factory=lambda: os.getenv("WORKSPACE_ID", "unknown")
    )
    workspace_path: str = field(
        default_factory=lambda: os.getenv(
            "WORKSPACE_PATH",
            f"/workspace/{os.getenv('WORKSPACE_ID', 'unknown')}",
        )
    )
    orchestrator_url: str = field(
        default_factory=lambda: os.getenv(
            "ORCHESTRATOR_URL",
            "http://orchestrator.agent-platform.svc.cluster.local",
        )
    )
    pod_name: str = field(
        default_factory=lambda: os.getenv("POD_NAME", "unknown")
    )

    # LLM model (supervisor)
    model_name: str = field(
        default_factory=lambda: os.getenv("AGENT_MODEL", "qwen/qwen3.6-35b-a3b")
    )
    model_provider: str = field(
        default_factory=lambda: os.getenv("AGENT_MODEL_PROVIDER", "openai")
    )
    temperature: float = field(
        default_factory=lambda: float(os.getenv("AGENT_TEMPERATURE", "0.1"))
    )
    openai_api_base: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_BASE", "http://localhost:1234/v1")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )

    # LLM model (sub-agents: research_agent, code_agent)
    # Falls back to supervisor settings when not explicitly set.
    sub_agent_model_name: str = field(
        default_factory=lambda: os.getenv(
            "SUB_AGENT_MODEL",
            os.getenv("AGENT_MODEL", "qwen/qwen3.6-35b-a3b"),
        )
    )
    sub_agent_model_provider: str = field(
        default_factory=lambda: os.getenv(
            "SUB_AGENT_MODEL_PROVIDER",
            os.getenv("AGENT_MODEL_PROVIDER", "openai"),
        )
    )
    sub_agent_temperature: float = field(
        default_factory=lambda: float(os.getenv(
            "SUB_AGENT_TEMPERATURE",
            os.getenv("AGENT_TEMPERATURE", "0.1"),
        ))
    )
    sub_agent_api_base: str = field(
        default_factory=lambda: os.getenv(
            "SUB_AGENT_API_BASE",
            os.getenv("OPENAI_API_BASE", "http://localhost:1234/v1"),
        )
    )
    sub_agent_api_key: str = field(
        default_factory=lambda: os.getenv(
            "SUB_AGENT_API_KEY",
            os.getenv("OPENAI_API_KEY", ""),
        )
    )

    # Database (shared PostgreSQL with Orchestrator, for checkpointer)
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "AGENT_DATABASE_URL",
            "postgresql://postgres:postgres@host.docker.internal:5432/claw_data",
        )
    )

    # Idle detection
    idle_timeout_minutes: int = field(
        default_factory=lambda: int(os.getenv("IDLE_TIMEOUT_MINUTES", "10"))
    )
    idle_check_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("IDLE_CHECK_INTERVAL_SECONDS", "30"))
    )

    # Debug
    debug: bool = field(
        default_factory=lambda: os.getenv("AGENT_DEBUG", "false").lower() in ("true", "1", "yes")
    )

    # Agent recursion limit (max tool call rounds)
    recursion_limit: int = field(
        default_factory=lambda: int(os.getenv("AGENT_RECURSION_LIMIT", "50"))
    )

    # Display mode: "normal" (content only) or "full_history" (content + tool_call + tool_result)
    # Controls both streaming SSE output and history retrieval
    display_mode: str = field(
        default_factory=lambda: os.getenv("AGENT_DISPLAY_MODE", "normal")
    )

    # Shared workspaces mount base path
    shared_base_path: str = field(
        default_factory=lambda: os.getenv("SHARED_BASE_PATH", "/shared")
    )

    # Context Compaction
    compaction_enabled: bool = field(
        default_factory=lambda: os.getenv("COMPACTION_ENABLED", "true").lower() in ("true", "1", "yes")
    )
    compaction_threshold_ratio: float = field(
        default_factory=lambda: float(os.getenv("COMPACTION_THRESHOLD_RATIO", "0.75"))
    )
    compaction_target_ratio: float = field(
        default_factory=lambda: float(os.getenv("COMPACTION_TARGET_RATIO", "0.40"))
    )
    compaction_preserve_turns: int = field(
        default_factory=lambda: int(os.getenv("COMPACTION_PRESERVE_TURNS", "6"))
    )
    compaction_min_messages: int = field(
        default_factory=lambda: int(os.getenv("COMPACTION_MIN_MESSAGES", "20"))
    )
    compaction_memory_flush: bool = field(
        default_factory=lambda: os.getenv("COMPACTION_MEMORY_FLUSH", "true").lower() in ("true", "1", "yes")
    )
