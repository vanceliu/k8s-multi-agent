"""Channel service — lifecycle management, channel registry, hot-reload.

Inspired by deer-flow service.py. Manages channel startup/shutdown and
provides singleton access.
"""

import importlib
import logging
from typing import Any

from poc.gateway.channels.base import Channel
from poc.gateway.channels.manager import ChannelManager
from poc.gateway.channels.message_bus import MessageBus
from poc.gateway.channels.store import ChannelStore

logger = logging.getLogger(__name__)

# Channel registry: name → module:class import path
_CHANNEL_REGISTRY: dict[str, str] = {
    "web": "poc.gateway.channels.adapters.web:WebChannel",
    # "slack": "poc.gateway.channels.adapters.slack:SlackChannel",
    # "line": "poc.gateway.channels.adapters.line:LINEChannel",
    # "teams": "poc.gateway.channels.adapters.teams:TeamsChannel",
}


def _resolve_class(import_path: str) -> type:
    """Dynamically import a class from 'module.path:ClassName' string."""
    module_path, class_name = import_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class ChannelService:
    """Top-level lifecycle manager for all IM channels."""

    def __init__(
        self,
        orchestrator_url: str,
        channels_config: dict[str, dict[str, Any]] | None = None,
        store_path: str = "/tmp/channel_store.json",
    ):
        self.bus = MessageBus()
        self.store = ChannelStore(store_path)
        self.manager = ChannelManager(
            bus=self.bus,
            store=self.store,
            orchestrator_url=orchestrator_url,
        )
        self._channels: dict[str, Channel] = {}
        self._config = channels_config or {}

    async def start(self) -> None:
        """Start the manager and all enabled channels."""
        await self.manager.start()

        for name, import_path in _CHANNEL_REGISTRY.items():
            channel_config = self._config.get(name, {})
            if not channel_config.get("enabled", name == "web"):
                logger.info("Channel '%s' is disabled, skipping", name)
                continue

            try:
                cls = _resolve_class(import_path)
                channel = cls(name=name, bus=self.bus, config=channel_config)
                await channel.start()
                self._channels[name] = channel
                logger.info("Channel '%s' started", name)
            except Exception:
                logger.exception("Failed to start channel '%s'", name)

    async def stop(self) -> None:
        """Stop all channels and the manager."""
        for name, channel in self._channels.items():
            try:
                await channel.stop()
                logger.info("Channel '%s' stopped", name)
            except Exception:
                logger.exception("Failed to stop channel '%s'", name)

        await self.manager.stop()
        self._channels.clear()

    async def restart_channel(self, name: str) -> bool:
        """Hot-reload a single channel."""
        if name in self._channels:
            await self._channels[name].stop()
            del self._channels[name]

        if name not in _CHANNEL_REGISTRY:
            logger.warning("Unknown channel: %s", name)
            return False

        channel_config = self._config.get(name, {})
        try:
            cls = _resolve_class(_CHANNEL_REGISTRY[name])
            channel = cls(name=name, bus=self.bus, config=channel_config)
            await channel.start()
            self._channels[name] = channel
            logger.info("Channel '%s' restarted", name)
            return True
        except Exception:
            logger.exception("Failed to restart channel '%s'", name)
            return False

    def get_channel(self, name: str) -> Channel | None:
        return self._channels.get(name)

    def get_status(self) -> dict[str, Any]:
        return {
            "channels": {
                name: {"running": ch.is_running}
                for name, ch in self._channels.items()
            },
            "manager_running": self.manager._running,
            "store_mappings": len(self.store.list_all()),
            "inbound_pending": self.bus.inbound_pending,
        }


# ── Module-level singleton ────────────────────────────────────────

_service: ChannelService | None = None


async def start_channel_service(
    orchestrator_url: str,
    channels_config: dict[str, dict[str, Any]] | None = None,
) -> ChannelService:
    global _service
    _service = ChannelService(
        orchestrator_url=orchestrator_url,
        channels_config=channels_config,
    )
    await _service.start()
    return _service


async def stop_channel_service() -> None:
    global _service
    if _service:
        await _service.stop()
        _service = None


def get_channel_service() -> ChannelService | None:
    return _service
