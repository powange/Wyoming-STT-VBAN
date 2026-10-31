"""Entry point for Wyoming VBAN Satellite."""

import argparse
import asyncio
import contextvars
import dataclasses
import logging
import signal
import sys
import wave
from typing import Optional

from wyoming.server import AsyncServer
from wyoming.zeroconf import HomeAssistantZeroconf

from .config import (
    DEFAULT_SEARCH_TEXT,
    DEFAULT_THINKING_TEXT,
    DEFAULT_TTS_ENGINE,
    DEFAULT_TTS_VOICE,
    DEFAULT_VISION_PHRASE,
    ConfigError,
    DebugMirror,
    SatelliteConfig,
    load_options,
    validate,
    validate_mirrors,
)
from .const import DEFAULT_VBAN_PORT
from .ha_ws import HaStateWatcher
from .mirror import SatelliteMirrors
from .media_player import MediaPlayerOutput, detect_local_ip
from .satellite import (
    LISTEN_END_NOTES,
    SEARCH_NOTES,
    THINKING_NOTES,
    Announcement,
    Cue,
    FollowupSettings,
    MicMute,
    VbanSatelliteHandler,
    WaitingCues,
    make_satellite_info,
    notify_search,
    notify_vision,
)
from .tts_phrase import TtsPhrase, remove_legacy_cache, remove_stale_phrases
from .vban import VbanReceiver, VbanSender

_LOGGER = logging.getLogger(__name__)

# Name of the satellite owning the current task, prefixed to log lines
# (all satellites run in one process). Tasks inherit it from the task
# that created them.
_SATELLITE_NAME: contextvars.ContextVar[str] = contextvars.ContextVar(
    "satellite_name", default=""
)


class _SatelliteLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = _SATELLITE_NAME.get()
        record.satellite = f"[{name}] " if name else ""
        return True


def _build_zeroconf_name(satellite_name: str, port: int) -> str:
    """Build a Zeroconf name unique per satellite.

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


def _load_cue(path: str, default: Optional[Cue]) -> Optional[Cue]:
    """WAV file if configured and readable, otherwise the default sound."""
    if path:
        try:
            cue = Cue.from_wav(path)
            _LOGGER.info("  Sound %s: %.2fs", path, cue.duration)
            if cue.duration > 1.0:
                _LOGGER.warning(
                    "  %s lasts %.1fs: the response waits for it to finish",
                    path, cue.duration,
                )
            return cue
        except (OSError, EOFError, wave.Error) as err:
            _LOGGER.warning("  Cannot read %s (%s), using the built-in sound", path, err)
    return default


class PhraseRegistry:
    """One TtsPhrase per distinct (text, engine, voice), shared by all
    satellites and signals."""

    def __init__(self, cache_dir: str, engine: str, voice: str):
        self.cache_dir = cache_dir
        self.engine = engine  # app-wide defaults
        self.voice = voice
        self._phrases: dict[tuple[str, str, str], TtsPhrase] = {}

    def get(self, text: str, engine: str = "", voice: str = "") -> TtsPhrase:
        key = (text, engine or self.engine, voice or self.voice)
        if key not in self._phrases:
            self._phrases[key] = TtsPhrase(*key, cache_dir=self.cache_dir)
        return self._phrases[key]

    @property
    def phrases(self) -> list[TtsPhrase]:
        return list(self._phrases.values())


def _announcement(
    mode: str, sound_file: str, builtin: Optional[Cue], text: str,
    cfg: SatelliteConfig, phrases: PhraseRegistry,
) -> Optional[Announcement]:
    """The announcement for one signal (None if its mode is "none")."""
    if mode == "none":
        return None
    sound = _load_cue(sound_file, builtin)
    phrase = None
    if mode in ("phrase", "sound_then_phrase"):
        phrase = phrases.get(text, cfg.vision_tts_engine, cfg.vision_tts_voice)
    return Announcement(
        sound=sound if mode in ("sound", "sound_then_phrase") else None,
        phrase=phrase,
        # Phrase not synthesized: this signal's sound (vision has no
        # built-in one: the thinking sound)
        fallback=sound or Cue.tones(THINKING_NOTES, 0.15),
    )


def _waiting_cues(cfg: SatelliteConfig, phrases: PhraseRegistry) -> WaitingCues:
    return WaitingCues(
        listen_end=_announcement(
            cfg.listen_end_mode, cfg.listen_end_sound,
            Cue.tones(LISTEN_END_NOTES, 0.12), "", cfg, phrases,
        ),
        thinking=_announcement(
            cfg.thinking_mode, cfg.thinking_sound, Cue.tones(THINKING_NOTES, 0.15),
            cfg.thinking_text, cfg, phrases,
        ),
        thinking_delay_s=cfg.thinking_delay_ms / 1000,
        search=_announcement(
            cfg.search_mode, cfg.search_sound, Cue.tones(SEARCH_NOTES, 0.2),
            cfg.search_text, cfg, phrases,
        ),
        vision=_announcement(
            cfg.vision_mode, cfg.vision_sound, None, cfg.vision_phrase, cfg, phrases,
        ),
    )


def _config_from_args(args: argparse.Namespace) -> SatelliteConfig:
    """Single-satellite configuration from command-line options."""
    values = {
        field.name: getattr(args, field.name)
        for field in dataclasses.fields(SatelliteConfig)
    }
    return SatelliteConfig(**values)


async def run_satellite(
    cfg: SatelliteConfig,
    vban_receiver: VbanReceiver,
    state_watcher: Optional[HaStateWatcher] = None,
    publish_exchanges: bool = False,
    waiting_cues: Optional[WaitingCues] = None,
    debug_mirrors: Optional[SatelliteMirrors] = None,
) -> None:
    """Run one satellite (Wyoming server, TTS outputs, Zeroconf) until cancelled."""
    _SATELLITE_NAME.set(cfg.name)

    _LOGGER.info(
        "Starting: Wyoming port %d, VBAN receive port %d stream '%s'",
        cfg.wyoming_port, cfg.vban_receive_port,
        cfg.vban_receive_stream_name or "<any>",
    )

    # VBAN sender (optional)
    vban_sender: Optional[VbanSender] = None
    if cfg.tts_vban_enabled:
        vban_sender = VbanSender(
            address=cfg.tts_vban_address,
            port=cfg.tts_vban_port,
            mode=cfg.tts_vban_mode,
            stream_name=cfg.tts_vban_stream_name,
            volume=cfg.tts_vban_volume,
            batch_packets=cfg.tts_vban_batch_packets,
        )
        _LOGGER.info(
            "  TTS VBAN: mode=%s address=%s:%d stream=%s volume=%.2f batch=%d",
            cfg.tts_vban_mode, cfg.tts_vban_address, cfg.tts_vban_port,
            cfg.tts_vban_stream_name, cfg.tts_vban_volume,
            cfg.tts_vban_batch_packets,
        )

    # Media player TTS output (optional)
    media_output: Optional[MediaPlayerOutput] = None
    mic_mute: Optional[MicMute] = None
    if cfg.tts_output == "media_player":
        http_host = cfg.tts_http_host or detect_local_ip()
        media_output = MediaPlayerOutput(
            entity_id=cfg.tts_media_player,
            http_host=http_host,
            http_port=cfg.tts_http_port,
        )
        media_output.state_watcher = state_watcher
        # Shared by all connections of this satellite
        mic_mute = MicMute(
            tail_s=cfg.mic_mute_tail_ms / 1000,
            max_muted_s=VbanSatelliteHandler.MAX_TTS_BUFFER_S,
        )
        _LOGGER.info(
            "  TTS output: media_player %s (HTTP %s:%d)%s, mic muted during "
            "playback + %d ms",
            cfg.tts_media_player, http_host, cfg.tts_http_port,
            ", VBAN fallback" if vban_sender else "", cfg.mic_mute_tail_ms,
        )

    satellite_info = make_satellite_info(name=cfg.name)
    followup = FollowupSettings(
        enabled=cfg.followup_enabled,
        beep=cfg.followup_beep,
        timeout_s=cfg.followup_timeout_s,
    )
    waiting_cues = waiting_cues or WaitingCues()
    _LOGGER.info(
        "  Announcements: listen end %s, thinking %s (after %d ms), search %s, "
        "vision %s",
        cfg.listen_end_mode, cfg.thinking_mode, cfg.thinking_delay_ms,
        cfg.search_mode, cfg.vision_mode,
    )
    _LOGGER.info(
        "  Follow-up without wake word: %s",
        f"on (beep={'on' if cfg.followup_beep else 'off'}, "
        f"timeout={cfg.followup_timeout_s}s)" if cfg.followup_enabled else "off",
    )

    # Handler factory — creates a new handler per connection
    def handler_factory(reader, writer):
        return VbanSatelliteHandler(
            reader=reader,
            writer=writer,
            satellite_info=satellite_info,
            vban_receiver=vban_receiver,
            vban_sender=vban_sender,
            media_output=media_output,
            vban_stream_name=cfg.vban_receive_stream_name,
            mic_mute=mic_mute,
            followup=followup,
            exchange_publisher=state_watcher if publish_exchanges else None,
            satellite_name=cfg.name,
            waiting_cues=waiting_cues,
            debug_mirrors=debug_mirrors,
        )

    server: Optional[AsyncServer] = None
    zeroconf: Optional[HomeAssistantZeroconf] = None
    try:
        if vban_sender:
            vban_sender.open()

        if media_output:
            try:
                await media_output.start()
            except OSError as err:
                raise ConfigError(
                    f"Cannot start TTS HTTP server on port {cfg.tts_http_port}: {err}"
                ) from err

        server = AsyncServer.from_uri(f"tcp://0.0.0.0:{cfg.wyoming_port}")
        await server.start(handler_factory)
        _LOGGER.info("Wyoming server listening on tcp://0.0.0.0:%d", cfg.wyoming_port)

        # Register via Zeroconf so HA auto-discovers the satellite.
        # Build a unique name per satellite (name + port) so multiple
        # satellites on the same host don't collide on the default MAC name.
        zeroconf = HomeAssistantZeroconf(
            port=cfg.wyoming_port,
            name=_build_zeroconf_name(cfg.name, cfg.wyoming_port),
        )
        await zeroconf.register_server()
        _LOGGER.info("Zeroconf registered: %s on port %d", zeroconf.name, cfg.wyoming_port)

        # Serve until cancelled. Not server.run(): cancelling serve_forever()
        # awaits Server.wait_closed(), which can hang on Python 3.12.
        await asyncio.Future()
    finally:
        if server is not None:
            await server.stop()

        # Close VBAN sender (cancels drain task, closes socket)
        if vban_sender:
            await vban_sender.close()

        if media_output:
            await media_output.stop()

        # Unregister Zeroconf service
        if zeroconf is not None:
            try:
                await zeroconf._aiozc.async_close()
                _LOGGER.debug("Zeroconf unregistered")
            except Exception as err:
                _LOGGER.debug("Zeroconf cleanup error: %s", err)


async def main() -> int:
    args = parse_args()

    debug = args.debug
    publish_exchanges = args.publish_exchanges
    announcements_tts_engine = args.announcements_tts_engine
    announcements_tts_voice = args.announcements_tts_voice
    mirror_options: list[DebugMirror] = []
    try:
        if args.options:
            app_options = load_options(args.options)
            satellites = app_options.satellites
            debug = debug or app_options.debug_logging
            publish_exchanges = app_options.publish_exchanges
            mirror_options = app_options.debug_mirrors
            announcements_tts_engine = app_options.announcements_tts_engine
            announcements_tts_voice = app_options.announcements_tts_voice
        else:
            if not args.name:
                raise ConfigError("--name is required without --options")
            satellites = [_config_from_args(args)]
    except (OSError, ValueError, TypeError, ConfigError) as err:
        logging.basicConfig(level=logging.INFO)
        _LOGGER.error("Invalid configuration: %s", err)
        return 1

    handler = logging.StreamHandler()
    handler.addFilter(_SatelliteLogFilter())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(satellite)s%(name)s %(levelname)s: %(message)s",
        handlers=[handler],
    )

    try:
        validate(satellites)
    except ConfigError as err:
        _LOGGER.error("Invalid configuration: %s", err)
        return 1

    _LOGGER.info("Wyoming VBAN Satellite starting %d satellite(s)", len(satellites))

    # One VBAN receiver (one UDP socket) per port, shared by the satellites
    # on that port and routing packets by stream name. validate() ensured
    # they agree on mode / multicast group.
    receivers: dict[int, VbanReceiver] = {}
    for sat in satellites:
        if sat.vban_receive_port not in receivers:
            receivers[sat.vban_receive_port] = VbanReceiver(
                port=sat.vban_receive_port,
                mode=sat.vban_receive_mode,
                multicast_group=sat.vban_receive_multicast_group,
            )

    # Debug copies of audio streams (debug_mirror option), per satellite.
    # mic_received copies are fed by the receiver, whether or not HA is
    # connected; the others by the satellite's Wyoming handler.
    mirrors: dict[str, SatelliteMirrors] = {}
    valid_mirrors = validate_mirrors(mirror_options, satellites)
    for sat in satellites:
        sat_mirrors = [m for m in valid_mirrors if m.satellite == sat.name]
        if sat_mirrors:
            mirrors[sat.name] = SatelliteMirrors(sat_mirrors)
            mirrors[sat.name].open()
            for raw in mirrors[sat.name].mic_received:
                receivers[sat.vban_receive_port].subscribe(
                    raw.on_packet, sat.vban_receive_stream_name,
                )

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

    # One HA WebSocket for the app: follows the media_players used for TTS
    # output (real start/end of playback, to unmute the mic on time) and
    # publishes voice exchanges as HA events
    media_players = [s.tts_media_player for s in satellites if s.tts_output == "media_player"]
    # Announcements; their phrases are synthesized by HA once, cached on disk
    phrase_registry = PhraseRegistry(
        args.cache_dir, announcements_tts_engine, announcements_tts_voice,
    )
    announcements = {sat.name: _waiting_cues(sat, phrase_registry) for sat in satellites}
    remove_stale_phrases(phrase_registry.phrases)
    remove_legacy_cache()
    phrase_tasks = [asyncio.create_task(p.run()) for p in phrase_registry.phrases]
    search_events = any(cues.search for cues in announcements.values())
    vision_events = any(cues.vision for cues in announcements.values())
    state_watcher = (
        HaStateWatcher(media_players)
        if media_players or publish_exchanges or search_events or vision_events
        else None
    )
    if state_watcher and search_events:
        # Internet search started (fired by script.demander_ia_externe)
        state_watcher.add_event_listener("jarvis_recherche", notify_search)
    if state_watcher and vision_events:
        # Camera look started (fired by script.regarder_la_camera)
        state_watcher.add_event_listener("jarvis_vision", notify_vision)
    _LOGGER.info(
        "Voice exchanges %s",
        "published as jarvis_echange events" if publish_exchanges else "not published",
    )
    watcher_task = (
        asyncio.create_task(state_watcher.run()) if state_watcher else None
    )

    # Receivers run for the lifetime of the app
    receiver_tasks = [
        asyncio.create_task(receiver.run()) for receiver in receivers.values()
    ]
    satellite_tasks = {
        asyncio.create_task(
            run_satellite(
                sat, receivers[sat.vban_receive_port], state_watcher, publish_exchanges,
                announcements[sat.name], mirrors.get(sat.name),
            )
        ): sat.name
        for sat in satellites
    }
    stop_wait_task = asyncio.create_task(stop_event.wait())

    exit_code = 0
    try:
        # Any satellite or receiver ending means a failure: stop everything
        # (as when each satellite was its own process).
        done, _pending = await asyncio.wait(
            {*receiver_tasks, *satellite_tasks, stop_wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_wait_task not in done:
            exit_code = 1
            for task in done:
                what = (
                    f"satellite '{satellite_tasks[task]}'"
                    if task in satellite_tasks else "VBAN receiver"
                )
                err = None if task.cancelled() else task.exception()
                _LOGGER.error(
                    "%s stopped (%s), stopping all satellites",
                    what, err or "no error",
                )
    finally:
        _LOGGER.info("Shutting down...")
        stop_wait_task.cancel()

        for task in satellite_tasks:
            task.cancel()
        await asyncio.gather(*satellite_tasks, return_exceptions=True)

        if watcher_task:
            watcher_task.cancel()
            await asyncio.gather(watcher_task, return_exceptions=True)

        for task in phrase_tasks:
            task.cancel()
        await asyncio.gather(*phrase_tasks, return_exceptions=True)

        for sat_mirrors in mirrors.values():
            await sat_mirrors.close()

        # Stop VBAN receivers
        for receiver in receivers.values():
            receiver.stop()
        for task in receiver_tasks:
            task.cancel()
        await asyncio.gather(*receiver_tasks, return_exceptions=True)

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
