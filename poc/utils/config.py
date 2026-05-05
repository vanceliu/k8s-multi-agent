"""POC configuration."""

import os

# Auth
STATIC_TOKEN = os.getenv("POC_STATIC_TOKEN", "REDACTED_USER_TOKEN")
ADMIN_STATIC_TOKEN = os.getenv("POC_ADMIN_TOKEN", "REDACTED_ADMIN_TOKEN")

# DB
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:REDACTED@localhost:5432/claw_data",
)

# K8s
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "agent-platform")
AGENT_IMAGE = os.getenv("AGENT_IMAGE", "localhost/k8s-agent-runtime:latest")
AGENT_PORT = 8080

# Orchestrator
ORCHESTRATOR_HOST = os.getenv("ORCHESTRATOR_HOST", "http://localhost:8080")

# Admin
ADMIN_HOST = os.getenv("ADMIN_HOST", "http://localhost:8090")

# Storage Service
STORAGE_SERVICE_HOST = os.getenv("STORAGE_SERVICE_HOST", "http://localhost:8091")
PVC_FILE_SIZE_LIMIT_MB = int(os.getenv("PVC_FILE_SIZE_LIMIT_MB", "10"))

# Lifecycle
IDLE_TIMEOUT_MINUTES = int(os.getenv("IDLE_TIMEOUT_MINUTES", "10"))
REAP_CHECK_INTERVAL_SECONDS = int(os.getenv("REAP_CHECK_INTERVAL_SECONDS", "30"))

# Resource defaults — PVC
DEFAULT_PVC_SIZE_GB = 1
DEFAULT_PVC_STORAGE_CLASS = os.getenv("PVC_STORAGE_CLASS", "standard")

# Resource defaults — Pod compute
DEFAULT_CPU_REQUEST = os.getenv("POD_CPU_REQUEST", "100m")
DEFAULT_CPU_LIMIT = os.getenv("POD_CPU_LIMIT", "500m")
DEFAULT_MEMORY_REQUEST = os.getenv("POD_MEMORY_REQUEST", "256Mi")
DEFAULT_MEMORY_LIMIT = os.getenv("POD_MEMORY_LIMIT", "1Gi")

# Pod spec — probes
LIVENESS_INITIAL_DELAY = int(os.getenv("LIVENESS_INITIAL_DELAY", "30"))
LIVENESS_PERIOD = int(os.getenv("LIVENESS_PERIOD", "10"))
LIVENESS_TIMEOUT = int(os.getenv("LIVENESS_TIMEOUT", "5"))
LIVENESS_FAILURE_THRESHOLD = int(os.getenv("LIVENESS_FAILURE_THRESHOLD", "3"))

READINESS_INITIAL_DELAY = int(os.getenv("READINESS_INITIAL_DELAY", "10"))
READINESS_PERIOD = int(os.getenv("READINESS_PERIOD", "5"))
READINESS_TIMEOUT = int(os.getenv("READINESS_TIMEOUT", "3"))
READINESS_FAILURE_THRESHOLD = int(os.getenv("READINESS_FAILURE_THRESHOLD", "3"))

# Pod spec — lifecycle
TERMINATION_GRACE_PERIOD_SECONDS = int(os.getenv("TERMINATION_GRACE_PERIOD", "30"))
POD_RESTART_POLICY = os.getenv("POD_RESTART_POLICY", "OnFailure")

# Cleanup Job
CLEANUP_JOB_IMAGE = os.getenv("CLEANUP_JOB_IMAGE", "busybox:latest")
CLEANUP_JOB_TTL_SECONDS = int(os.getenv("CLEANUP_JOB_TTL_SECONDS", "60"))

# CWA (Central Weather Administration) Open Data API
CWA_API_BASE = os.getenv("CWA_API_BASE", "https://opendata.cwa.gov.tw/api/v1/rest/datastore")
CWA_API_KEY = os.getenv("CWA_API_KEY", "REDACTED_CWA_KEY")

# Agent Debug
AGENT_DEBUG = os.getenv("AGENT_DEBUG", "false")

# Agent LLM (supervisor)
AGENT_MODEL = os.getenv("AGENT_MODEL", "glm-5.1")
AGENT_MODEL_PROVIDER = os.getenv("AGENT_MODEL_PROVIDER", "openai")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "REDACTED_API_BASE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "REDACTED_API_KEY")

# Agent LLM (sub-agents: research_agent, code_agent)
# Falls back to supervisor settings when not explicitly set.
SUB_AGENT_MODEL = os.getenv("SUB_AGENT_MODEL", "glm-5-turbo")
SUB_AGENT_MODEL_PROVIDER = os.getenv("SUB_AGENT_MODEL_PROVIDER", AGENT_MODEL_PROVIDER)
SUB_AGENT_API_BASE = os.getenv("SUB_AGENT_API_BASE", OPENAI_API_BASE)
SUB_AGENT_API_KEY = os.getenv("SUB_AGENT_API_KEY", OPENAI_API_KEY)

# Agent display mode: "normal" (content only) or "full_history" (content + tool_call + tool_result)
# Controls both streaming SSE output and history retrieval
AGENT_DISPLAY_MODE = os.getenv("AGENT_DISPLAY_MODE", "full_history")