"""Entry point for Wyoming VBAN Satellite."""

import argparse
import asyncio
import dataclasses
import logging
import signal
import sys

from .config import (
    DEFAULT_SEARCH_TEXT,
    DEFAULT_THINKING_TEXT,
    DEFAULT_TTS_ENGINE,
    DEFAULT_TTS_VOICE,
    DEFAULT_VISION_PHRASE,
    AppOptions,
    ConfigError,
    SatelliteConfig,
    load_options,
    validate,
)
from .const import DEFAULT_VBAN_PORT
from .runtime import AppRuntime, SatelliteLogFilter
from .webui import WebUI

_LOGGER = logging.getLogger(__name__)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wyoming VBAN Satellite — use VBAN audio sources as HA voice satellites"
    )

    # All satellites from the app options file (used by run.sh). Without it,
    # a single satellite is configured from the options below.
    parser.add_argument(
        "--options", help="App options JSON file (e.g. /data/options.json)",
    )

    # Wyoming
    parser.add_argument("--name", help="Satellite name shown in HA")
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
    parser.add_argument(
        "--tts-vban-volume",
        type=float,
        default=1.0,
        help="Volume multiplier for TTS audio (0.0-2.0, default 1.0)",
    )
    parser.add_argument(
        "--tts-vban-batch-packets",
        type=int,
        default=1,
        help="VBAN packets (256 samples each) sent per wake-up (1-8, default 1)",
    )

    # TTS output target
    parser.add_argument(
        "--tts-output",
        choices=["vban", "media_player"],
        default="vban",
        help="Where TTS responses are played (default: vban)",
    )
    parser.add_argument(
        "--tts-media-player", default="", help="media_player entity_id for TTS output",
    )
    parser.add_argument(
        "--tts-http-port",
        type=int,
        default=10800,
        help="Port of the HTTP server serving TTS WAV files to the media_player",
    )
    parser.add_argument(
        "--tts-http-host",
        default="",
        help="LAN IP put in TTS URLs (empty = auto-detect)",
    )
    parser.add_argument(
        "--mic-mute-tail-ms",
        type=int,
        default=250,
        help="media_player output: keep the mic muted this long after the "
        "response has played (default 250, 0 = no tail)",
    )

    # Follow-up: answer a question without the wake word
    parser.add_argument(
        "--followup-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After a response ending with '?', listen without the wake word",
    )
    parser.add_argument(
        "--followup-beep",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Beep on the satellite output before listening for a follow-up",
    )
    parser.add_argument(
        "--followup-timeout-s",
        type=int,
        default=8,
        help="Back to the wake word if no speech within this time (3-30)",
    )

    # Announcements while the response is prepared. Modes: none, sound,
    # phrase, sound_then_phrase ("" = default)
    parser.add_argument("--listen-end-mode", default="none", help="none or sound")
    parser.add_argument("--listen-end-sound", default="", help="WAV (default: built-in blip)")
    parser.add_argument("--thinking-mode", default="", help="Default: phrase")
    parser.add_argument("--thinking-delay-ms", type=int, default=1000)
    parser.add_argument("--thinking-text", default=DEFAULT_THINKING_TEXT)
    parser.add_argument("--thinking-sound", default="", help="WAV (default: built-in)")
    parser.add_argument("--search-mode", default="", help="Default: sound_then_phrase")
    parser.add_argument("--search-text", default=DEFAULT_SEARCH_TEXT)
    parser.add_argument("--search-sound", default="", help="WAV (default: built-in)")
    parser.add_argument("--vision-mode", default="", help="Default: phrase")
    parser.add_argument("--vision-phrase", default=DEFAULT_VISION_PHRASE)
    parser.add_argument("--vision-sound", default="", help="WAV (no built-in sound)")
    parser.add_argument("--vision-tts-engine", default="", help="Default: --announcements-tts-engine")
    parser.add_argument("--vision-tts-voice", default="", help="Default: --announcements-tts-voice")
    # Pre-3.2 switches (set the mode when it isn't set)
    parser.add_argument("--thinking-sound-enabled", action=argparse.BooleanOptionalAction)
    parser.add_argument("--search-sound-enabled", action=argparse.BooleanOptionalAction)
    parser.add_argument("--vision-phrase-enabled", action=argparse.BooleanOptionalAction)
    parser.add_argument("--announcements-tts-engine", default=DEFAULT_TTS_ENGINE)
    parser.add_argument("--announcements-tts-voice", default=DEFAULT_TTS_VOICE)
    parser.add_argument(
        "--ui-port", type=int, default=0,
        help="Port of the configuration UI (Home Assistant Ingress); 0 = off",
    )
    parser.add_argument(
        "--cache-dir", default="/data/annonces", help="Where synthesized phrases are cached",
    )

    # Publish each voice exchange as a HA event (needs the Supervisor API)
    parser.add_argument(
        "--publish-exchanges",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fire a jarvis_echange HA event for each voice exchange",
    )

    # Logging
    parser.add_argument("--debug", action="store_true", default=False)

    return parser.parse_args()


def _config_from_args(args: argparse.Namespace) -> SatelliteConfig:
    """Single-satellite configuration from command-line options."""
    values = {
        field.name: getattr(args, field.name)
        for field in dataclasses.fields(SatelliteConfig)
    }
    return SatelliteConfig(**values)


async def main() -> int:
    args = parse_args()

    try:
        if args.options:
            options = load_options(args.options)
            options.debug_logging = options.debug_logging or args.debug
        else:
            if not args.name:
                raise ConfigError("--name is required without --options")
            options = AppOptions(
                satellites=[_config_from_args(args)],
                debug_logging=args.debug,
                publish_exchanges=args.publish_exchanges,
                announcements_tts_engine=args.announcements_tts_engine,
                announcements_tts_voice=args.announcements_tts_voice,
            )
    except (OSError, ValueError, TypeError, ConfigError) as err:
        logging.basicConfig(level=logging.INFO)
        _LOGGER.error("Invalid configuration: %s", err)
        return 1

    handler = logging.StreamHandler()
    handler.addFilter(SatelliteLogFilter())
    logging.basicConfig(
        level=logging.DEBUG if options.debug_logging else logging.INFO,
        format="%(asctime)s %(satellite)s%(name)s %(levelname)s: %(message)s",
        handlers=[handler],
    )

    try:
        validate(options.satellites)
    except ConfigError as err:
        _LOGGER.error("Invalid configuration: %s", err)
        return 1

    _LOGGER.info("Wyoming VBAN Satellite starting")

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

    runtime = AppRuntime(args.cache_dir)
    await runtime.start(options)

    # Configuration UI (only with the app options file: it saves them
    # through the Supervisor)
    web_ui = None
    if args.ui_port and args.options:
        web_ui = WebUI(runtime, args.ui_port)
        try:
            await web_ui.start()
        except OSError as err:
            _LOGGER.error("Configuration UI not started (port %d): %s", args.ui_port, err)
            web_ui = None

    exit_code = 0
    stop_wait = asyncio.create_task(stop_event.wait())
    fatal_wait = asyncio.create_task(runtime.fatal.wait())
    try:
        # A satellite or receiver ending on its own is a failure: stop
        # everything (the Supervisor reports the app as stopped)
        done, _pending = await asyncio.wait(
            {stop_wait, fatal_wait}, return_when=asyncio.FIRST_COMPLETED,
        )
        if fatal_wait in done:
            exit_code = 1
            _LOGGER.error("Stopping all satellites")
    finally:
        _LOGGER.info("Shutting down...")
        stop_wait.cancel()
        fatal_wait.cancel()
        if web_ui is not None:
            await web_ui.stop()
        await runtime.stop()

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
