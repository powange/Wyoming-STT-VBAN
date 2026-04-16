"""Entry point for Wyoming VBAN Satellite."""

import argparse
import asyncio
import logging
import sys
from functools import partial

from wyoming.server import AsyncServer

from .const import DEFAULT_VBAN_PORT
from .satellite import VbanSatelliteHandler, make_satellite_info
from .vban import VbanReceiver, VbanSender

_LOGGER = logging.getLogger(__name__)


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
    satellite_info = make_satellite_info(
        name=args.name,
        has_tts_output=vban_sender is not None,
    )

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

    await server.run(handler_factory)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
