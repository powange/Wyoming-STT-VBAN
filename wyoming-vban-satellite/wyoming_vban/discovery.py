"""Zeroconf (mDNS) discovery of the satellites by Home Assistant.

One responder for the whole app, each satellite registering its service on
it: wyoming's HomeAssistantZeroconf creates one responder (with its own
sockets) per satellite.
"""

import logging
import socket
from typing import Optional

from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE = "_wyoming._tcp.local."
MDNS_TARGET_IP = "224.0.0.251"


def local_ip() -> str:
    """Address of the interface that reaches the mDNS group (no packet sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        sock.connect((MDNS_TARGET_IP, 1))
        return sock.getsockname()[0]


class Discovery:
    """Registers each satellite's Wyoming service on one shared responder."""

    def __init__(self) -> None:
        self._zeroconf: Optional[AsyncZeroconf] = None
        self._services: dict[int, AsyncServiceInfo] = {}  # by Wyoming port

    async def register(self, name: str, port: int) -> str:
        """Announce a satellite; returns its service name. Raises on failure
        (no multicast route yet, name conflict…)."""
        info = AsyncServiceInfo(
            SERVICE_TYPE, f"{name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip())], port=port,
        )
        if self._zeroconf is None:
            self._zeroconf = AsyncZeroconf()
        await self._zeroconf.async_register_service(info)
        self._services[port] = info
        return info.name

    async def unregister(self, port: int) -> None:
        info = self._services.pop(port, None)
        if info is None or self._zeroconf is None:
            return
        try:
            await self._zeroconf.async_unregister_service(info)
        except Exception as err:  # noqa: BLE001 - leaving anyway
            _LOGGER.debug("Zeroconf unregister error: %s", err)
        if not self._services:  # nothing left to announce: free the sockets
            await self.close()

    async def close(self) -> None:
        if self._zeroconf is not None:
            zeroconf, self._zeroconf = self._zeroconf, None
            try:
                await zeroconf.async_close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Zeroconf close error: %s", err)
