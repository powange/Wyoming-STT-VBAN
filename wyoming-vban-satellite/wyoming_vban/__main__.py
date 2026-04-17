"""Entry point for Wyoming VBAN Satellite."""

import argparse
import asyncio
import logging
import signal
import sys

from wyoming.server import AsyncServer
from wyoming.zeroconf import HomeAssistantZeroconf

from .const import DEFAULT_VBAN_PORT
from .satellite import VbanSatelliteHandler, make_satellite_info
from .vban import VbanReceiver, VbanSender

_LOGGER = logging.getLogger(__name__)


def _build_zeroconf_name(satellite_name: str, port: int) -> str:
    """Build a Zeroconf name unique per satellite process.

    The default Wyoming Zeroconf name is just the host MAC address, which
    collides when multiple satellites run on the same machine. We append
    the port to disambiguate and sanitize the satellite name for mDNS.
    """
    # Keep only alphanumerics and hyphens (mDNS label restrictions)
    safe_name = "".join(c if c.isalnum() or c == "-" else "-" for c in satellite_name)
    safe_name = safe_name.strip("-").lower() or "satellite"
    return f"{safe_name}-{port}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wyoming VBAN Satellite — use VBAN audio sources as HA voice satellites"
    )

    # Wyoming
    parser.add_argument("--name", required=True, help="Satellite name shown in HA")
    parser.add_argument("--wyoming-port", type=int, default=10700, help="Wyoming TCP port")

    # VBAN receive (microphone)
    parser.add_argument("--vban-receive-port", type=int, default=DEFAULT_VBAN_PORT)
    parser.add_argument("--vban-receive-mode", choices=["unicast", "multicast"], default="unicast")
    parser.add_argument("--vban-receive-multicast-group", default="")
    parser.add_argument("--vban-receive-stream-name", default="", help="Filter by VBAN stream name (empty = accept all)")

    # VBAN send (TTS output) — optional
    parser.add_argument("--tts-vban-enabled", action="store_true", default=False)
    parser.add_argument("--tts-vban-mode", choices=["unicast", "multicast"], default="unicast")
    parser.add_argument("--tts-vban-address", default="", help="Target IP for TTS VBAN output")
    parser.add_argument("--tts-vban-port", type=int, default=DEFAULT_VBAN_PORT)
    parser.add_argument("--tts-vban-stream-name", default="TTS1")

    # Logging
    parser.add_argument("--debug", action="store_true", default=False)

    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    _LOGGER.info("Wyoming VBAN Satellite starting")
    _LOGGER.info("  Name: %s", args.name)
    _LOGGER.info("  Wyoming port: %d", args.wyoming_port)
    _LOGGER.info("  VBAN receive: mode=%s port=%d", args.vban_receive_mode, args.vban_receive_port)

    if args.vban_receive_mode == "multicast":
        if not args.vban_receive_multicast_group:
            _LOGGER.error("Multicast mode requires --vban-receive-multicast-group")
            sys.exit(1)
        _LOGGER.info("  VBAN multicast group: %s", args.vban_receive_multicast_group)

    if args.vban_receive_stream_name:
        _LOGGER.info("  VBAN stream filter: '%s'", args.vban_receive_stream_name)

    # VBAN receiver
    vban_receiver = VbanReceiver(
        port=args.vban_receive_port,
        mode=args.vban_receive_mode,
        multicast_group=args.vban_receive_multicast_group,
        stream_name_filter=args.vban_receive_stream_name,
    )

    # VBAN sender (optional)
    vban_sender = None
    if args.tts_vban_enabled:
        if not args.tts_vban_address:
            _LOGGER.error("TTS VBAN enabled but no --tts-vban-address specified")
            sys.exit(1)
        vban_sender = VbanSender(
            address=args.tts_vban_address,
            port=args.tts_vban_port,
            mode=args.tts_vban_mode,
            stream_name=args.tts_vban_stream_name,
        )
        _LOGGER.info(
            "  TTS VBAN: mode=%s address=%s:%d stream=%s",
            args.tts_vban_mode, args.tts_vban_address,
            args.tts_vban_port, args.tts_vban_stream_name,
        )

    # Open TTS sender socket
    if vban_sender:
        vban_sender.open()

    # Build satellite info
    satellite_info = make_satellite_info(name=args.name)

    # Handler factory — creates a new handler per connection
    def handler_factory(reader, writer):
        return VbanSatelliteHandler(
            reader=reader,
            writer=writer,
            satellite_info=satellite_info,
            vban_receiver=vban_receiver,
            vban_sender=vban_sender,
        )

    # Start Wyoming server
    server = AsyncServer.from_uri(f"tcp://0.0.0.0:{args.wyoming_port}")
    _LOGGER.info("Wyoming server listening on tcp://0.0.0.0:%d", args.wyoming_port)

    # Register via Zeroconf so HA auto-discovers the satellite.
    # Build a unique name per satellite (MAC + port) so multiple satellites
    # running on the same host don't collide on the default MAC-only name.
    zeroconf_name = _build_zeroconf_name(args.name, args.wyoming_port)
    zeroconf = HomeAssistantZeroconf(
        port=args.wyoming_port,
        name=zeroconf_name,
    )
    await zeroconf.register_server()
    _LOGGER.info("Zeroconf registered: %s on port %d", zeroconf.name, args.wyoming_port)

    # Start VBAN receiver in the background (runs for the lifetime of the app)
    receiver_task = asyncio.create_task(vban_receiver.run())

    # Install signal handlers so SIGTERM / SIGINT trigger a clean shutdown
    # (HAOS sends SIGTERM to stop the container).
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signame: str) -> None:
        _LOGGER.info("Received %s, stopping...", signame)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except NotImplementedError:
            # Signal handlers not supported on this platform (e.g. Windows)
            pass

    server_task = asyncio.create_task(server.run(handler_factory))
    stop_wait_task = asyncio.create_task(stop_event.wait())

    try:
        done, _pending = await asyncio.wait(
            {server_task, stop_wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_wait_task in done:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
        else:
            stop_wait_task.cancel()
    finally:
        _LOGGER.info("Shutting down...")

        # Stop VBAN receiver
        vban_receiver.stop()
        receiver_task.cancel()
        try:
            await receiver_task
        except asyncio.CancelledError:
            pass

        # Close VBAN sender (cancels drain task, closes socket)
        if vban_sender:
            await vban_sender.close()

        # Unregister Zeroconf service
        try:
            await zeroconf._aiozc.async_close()
            _LOGGER.debug("Zeroconf unregistered")
        except Exception as err:
            _LOGGER.debug("Zeroconf cleanup error: %s", err)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
