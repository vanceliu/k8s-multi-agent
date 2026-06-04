"""POC configuration — YAML defaults + environment variable overrides."""

import os
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _load_yaml() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_cfg = _load_yaml()


def _get(yaml_path: str, env_key: str, default=None, cast=None):
    """Resolve value: env var > yaml > default."""
    env_val = os.getenv(env_key)
    if env_val is not None:
        return cast(env_val) if cast else env_val

    keys = yaml_path.split(".")
    node = _cfg
    for k in keys:
        if isinstance(node, dict):
            node = node.get(k)
        else:
            node = None
            break

    if node is not None:
        return cast(node) if cast else node
    return cast(default) if cast and default is not None else default


# Auth
STATIC_TOKEN = _get("auth.static_token", "POC_STATIC_TOKEN", "poc-test-token-12345")
ADMIN_STATIC_TOKEN = _get("auth.admin_static_token", "POC_ADMIN_TOKEN", "poc-admin-token-12345")

# DB
DATABASE_URL = _get(
    "db.database_url",
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/claw_data",
)

# K8s
K8S_NAMESPACE = _get("k8s.namespace", "K8S_NAMESPACE", "agent-platform")
AGENT_IMAGE = _get("k8s.agent_image", "AGENT_IMAGE", "localhost/k8s-agent-runtime:latest")
AGENT_PORT = _get("k8s.agent_port", "AGENT_PORT", 8080, cast=int)

# Orchestrator
ORCHESTRATOR_HOST = _get("services.orchestrator_host", "ORCHESTRATOR_HOST", "http://localhost:8080")

# Admin
ADMIN_HOST = _get("services.admin_host", "ADMIN_HOST", "http://localhost:8090")

# Storage Service
STORAGE_SERVICE_HOST = _get("services.storage_service_host", "STORAGE_SERVICE_HOST", "http://localhost:8091")
PVC_FILE_SIZE_LIMIT_MB = _get("storage.pvc_file_size_limit_mb", "PVC_FILE_SIZE_LIMIT_MB", 10, cast=int)

# Lifecycle
IDLE_TIMEOUT_MINUTES = _get("lifecycle.idle_timeout_minutes", "IDLE_TIMEOUT_MINUTES", 10, cast=int)
REAP_CHECK_INTERVAL_SECONDS = _get("lifecycle.reap_check_interval_seconds", "REAP_CHECK_INTERVAL_SECONDS", 30, cast=int)

# Resource defaults — PVC
DEFAULT_PVC_SIZE_GB = _get("storage.default_pvc_size_gb", "DEFAULT_PVC_SIZE_GB", 1, cast=int)
DEFAULT_PVC_STORAGE_CLASS = _get("storage.default_pvc_storage_class", "PVC_STORAGE_CLASS", "standard")

# Resource defaults — Pod compute
DEFAULT_CPU_REQUEST = _get("pod.cpu_request", "POD_CPU_REQUEST", "100m")
DEFAULT_CPU_LIMIT = _get("pod.cpu_limit", "POD_CPU_LIMIT", "500m")
DEFAULT_MEMORY_REQUEST = _get("pod.memory_request", "POD_MEMORY_REQUEST", "256Mi")
DEFAULT_MEMORY_LIMIT = _get("pod.memory_limit", "POD_MEMORY_LIMIT", "1Gi")

# Pod spec — probes
LIVENESS_INITIAL_DELAY = _get("probes.liveness.initial_delay", "LIVENESS_INITIAL_DELAY", 30, cast=int)
LIVENESS_PERIOD = _get("probes.liveness.period", "LIVENESS_PERIOD", 10, cast=int)
LIVENESS_TIMEOUT = _get("probes.liveness.timeout", "LIVENESS_TIMEOUT", 5, cast=int)
LIVENESS_FAILURE_THRESHOLD = _get("probes.liveness.failure_threshold", "LIVENESS_FAILURE_THRESHOLD", 3, cast=int)

READINESS_INITIAL_DELAY = _get("probes.readiness.initial_delay", "READINESS_INITIAL_DELAY", 10, cast=int)
READINESS_PERIOD = _get("probes.readiness.period", "READINESS_PERIOD", 5, cast=int)
READINESS_TIMEOUT = _get("probes.readiness.timeout", "READINESS_TIMEOUT", 3, cast=int)
READINESS_FAILURE_THRESHOLD = _get("probes.readiness.failure_threshold", "READINESS_FAILURE_THRESHOLD", 3, cast=int)

# Pod spec — lifecycle
TERMINATION_GRACE_PERIOD_SECONDS = _get("pod.termination_grace_period_seconds", "TERMINATION_GRACE_PERIOD", 30, cast=int)
POD_RESTART_POLICY = _get("pod.restart_policy", "POD_RESTART_POLICY", "OnFailure")

# Cleanup Job
CLEANUP_JOB_IMAGE = _get("cleanup_job.image", "CLEANUP_JOB_IMAGE", "busybox:latest")
CLEANUP_JOB_TTL_SECONDS = _get("cleanup_job.ttl_seconds", "CLEANUP_JOB_TTL_SECONDS", 60, cast=int)

# CWA (Central Weather Administration) Open Data API
CWA_API_BASE = _get("cwa.api_base", "CWA_API_BASE", "https://opendata.cwa.gov.tw/api/v1/rest/datastore")
CWA_API_KEY = _get("cwa.api_key", "CWA_API_KEY", "")

# Agent Debug
AGENT_DEBUG = _get("agent.debug", "AGENT_DEBUG", "false")

# Agent LLM (supervisor)
AGENT_MODEL = _get("agent.supervisor.model", "AGENT_MODEL", "glm-5.1")
AGENT_MODEL_PROVIDER = _get("agent.supervisor.model_provider", "AGENT_MODEL_PROVIDER", "openai")
OPENAI_API_BASE = _get("agent.supervisor.api_base", "OPENAI_API_BASE", "https://api.z.ai/api/coding/paas/v4")
OPENAI_API_KEY = _get("agent.supervisor.api_key", "OPENAI_API_KEY", "")

# Agent LLM (sub-agents: research_agent, code_agent)
SUB_AGENT_MODEL = _get("agent.sub_agent.model", "SUB_AGENT_MODEL", "glm-5-turbo")
SUB_AGENT_MODEL_PROVIDER = _get("agent.sub_agent.model_provider", "SUB_AGENT_MODEL_PROVIDER") or AGENT_MODEL_PROVIDER
SUB_AGENT_API_BASE = _get("agent.sub_agent.api_base", "SUB_AGENT_API_BASE") or OPENAI_API_BASE
SUB_AGENT_API_KEY = _get("agent.sub_agent.api_key", "SUB_AGENT_API_KEY") or OPENAI_API_KEY

# Agent display mode
AGENT_DISPLAY_MODE = _get("agent.display_mode", "AGENT_DISPLAY_MODE", "full_history")

# LINE Channel
LINE_CHANNEL_SECRET = _get("im_channel.line.LINE_CHANNEL_SECRET", "LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = _get("im_channel.line.CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_ACCESS_TOKEN", "")