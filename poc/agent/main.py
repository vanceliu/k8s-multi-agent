"""Agent container entry point.

Mirrors doc 06 §4.1: asyncio main with signal handling,
starts runtime + HTTP server + idle checker concurrently.
"""

import asyncio
import logging
import signal

import httpx
import uvicorn

from poc.agent.config import AgentConfig
from poc.agent.http_server import create_app, get_last_active_at
from poc.agent.runtime import DeepAgentsRuntime

logger = logging.getLogger(__name__)


async def _idle_checker(
    config: AgentConfig,
    shutdown_event: asyncio.Event,
):
    """Background task: detect idle timeout and notify Orchestrator to reap."""
    from datetime import datetime, timezone

    last_active = get_last_active_at()

    while not shutdown_event.is_set():
        await asyncio.sleep(config.idle_check_interval_seconds)
        if shutdown_event.is_set():
            break

        current_active = get_last_active_at()
        if current_active != last_active:
            last_active = current_active
            continue

        idle_seconds = (datetime.now(timezone.utc) - last_active).total_seconds()
        if idle_seconds >= config.idle_timeout_minutes * 60:
            logger.warning(
                "Idle timeout (%.0fs >= %ds). Notifying Orchestrator to reap.",
                idle_seconds,
                config.idle_timeout_minutes * 60,
            )
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(
                        f"{config.orchestrator_url}/api/v1/orchestrator/reap-self",
                        json={"workspace_id": config.workspace_id},
                    )
            except Exception:
                logger.exception("Failed to notify Orchestrator for self-reap")
            break


async def main():
    """Agent container application entry point."""
    config = AgentConfig()

    log_level = logging.DEBUG if config.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    if config.debug:
        # Enable verbose logging for LangChain / httpx
        logging.getLogger("langchain").setLevel(logging.DEBUG)
        logging.getLogger("langgraph").setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("openai").setLevel(logging.DEBUG)

    logger.info("=== Deep Agents Container ===")
    logger.info("Workspace: %s", config.workspace_id)
    logger.info("Pod: %s", config.pod_name)
    logger.info("Model: %s (%s)", config.model_name, config.model_provider)
    logger.info("Workspace path: %s", config.workspace_path)

    # Initialize runtime
    runtime = DeepAgentsRuntime(config)
    await runtime.initialize()

    # Build FastAPI app
    app = create_app(runtime)

    # Setup shutdown event
    shutdown_event = asyncio.Event()

    def handle_signal(signum: int):
        logger.info("Received signal %s, initiating shutdown", signum)
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))

    # Start HTTP server
    server_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8080,
        log_level="debug" if config.debug else "info",
    )
    server = uvicorn.Server(server_config)

    # Run HTTP server + idle checker concurrently
    idle_task = asyncio.create_task(_idle_checker(config, shutdown_event))

    logger.info(
        "Agent started (idle_timeout=%dm, check_interval=%ds)",
        config.idle_timeout_minutes,
        config.idle_check_interval_seconds,
    )

    try:
        await server.serve()
    finally:
        shutdown_event.set()
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass
        await runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
