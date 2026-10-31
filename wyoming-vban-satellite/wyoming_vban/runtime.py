"""Runs the satellites of the app; can be reloaded with new options."""

import asyncio
import contextvars
import logging
import wave
from typing import Optional

from wyoming.server import AsyncServer
from wyoming.zeroconf import HomeAssistantZeroconf

from .config import (
    AppOptions,
    ConfigError,
    SatelliteConfig,
    validate_mirrors,
)
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


class SatelliteLogFilter(logging.Filter):
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




class AppRuntime:
    """Everything running for one configuration: VBAN receivers, satellites,
    HA WebSocket, announcement phrases and debug copies.

    reload() swaps in a new configuration without restarting the app.
    """

    STARTUP_CHECK_S = 1.5  # a satellite failing to start does so by then

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.options: Optional[AppOptions] = None
        # A satellite or receiver stopped on its own (outside a reload)
        self.fatal = asyncio.Event()
        self.failure = ""
        self._reloading = False
        self._stopping = False
        self._reload_failed = asyncio.Event()
        self._receivers: dict[int, VbanReceiver] = {}
        self._mirrors: dict[str, SatelliteMirrors] = {}
        self._phrases: Optional[PhraseRegistry] = None
        self._watcher: Optional[HaStateWatcher] = None
        self._receiver_tasks: list[asyncio.Task] = []
        self._satellite_tasks: dict[asyncio.Task, str] = {}
        self._other_tasks: list[asyncio.Task] = []  # watcher, phrases

    @property
    def watcher(self) -> Optional[HaStateWatcher]:
        return self._watcher

    @property
    def phrases(self) -> list[TtsPhrase]:
        return self._phrases.phrases if self._phrases else []

    async def start(self, options: AppOptions) -> None:
        """Start everything for these (validated) options."""
        self.options = options
        satellites = options.satellites
        logging.getLogger().setLevel(logging.DEBUG if options.debug_logging else logging.INFO)
        _LOGGER.info("Starting %d satellite(s)", len(satellites))

        # One VBAN receiver (one UDP socket) per port, shared by the
        # satellites on that port and routing packets by stream name.
        # validate() ensured they agree on mode / multicast group.
        for sat in satellites:
            if sat.vban_receive_port not in self._receivers:
                self._receivers[sat.vban_receive_port] = VbanReceiver(
                    port=sat.vban_receive_port,
                    mode=sat.vban_receive_mode,
                    multicast_group=sat.vban_receive_multicast_group,
                )

        # Debug copies of audio streams (debug_mirror option), per satellite.
        # mic_received copies are fed by the receiver, whether or not HA is
        # connected; the others by the satellite's Wyoming handler.
        valid_mirrors = validate_mirrors(options.debug_mirrors, satellites)
        for sat in satellites:
            sat_mirrors = [m for m in valid_mirrors if m.satellite == sat.name]
            if sat_mirrors:
                self._mirrors[sat.name] = SatelliteMirrors(sat_mirrors)
                self._mirrors[sat.name].open()
                for raw in self._mirrors[sat.name].mic_received:
                    self._receivers[sat.vban_receive_port].subscribe(
                        raw.on_packet, sat.vban_receive_stream_name,
                    )

        # Announcements; their phrases are synthesized by HA once, cached
        self._phrases = PhraseRegistry(
            self.cache_dir, options.announcements_tts_engine,
            options.announcements_tts_voice,
        )
        announcements = {sat.name: _waiting_cues(sat, self._phrases) for sat in satellites}
        remove_stale_phrases(self._phrases.phrases)
        remove_legacy_cache()
        self._other_tasks = [asyncio.create_task(p.run()) for p in self._phrases.phrases]

        # One HA WebSocket for the app: follows the media_players used for
        # TTS output, publishes voice exchanges, receives jarvis_* events
        media_players = [s.tts_media_player for s in satellites if s.tts_output == "media_player"]
        search_events = any(cues.search for cues in announcements.values())
        vision_events = any(cues.vision for cues in announcements.values())
        if media_players or options.publish_exchanges or search_events or vision_events:
            self._watcher = HaStateWatcher(media_players)
            if search_events:
                # Internet search started (fired by script.demander_ia_externe)
                self._watcher.add_event_listener("jarvis_recherche", notify_search)
            if vision_events:
                # Camera look started (fired by script.regarder_la_camera)
                self._watcher.add_event_listener("jarvis_vision", notify_vision)
            self._other_tasks.append(asyncio.create_task(self._watcher.run()))
        _LOGGER.info(
            "Voice exchanges %s",
            "published as jarvis_echange events" if options.publish_exchanges
            else "not published",
        )

        for port, receiver in self._receivers.items():
            task = asyncio.create_task(receiver.run())
            task.add_done_callback(
                lambda t, port=port: self._on_task_done(t, f"VBAN receiver (port {port})")
            )
            self._receiver_tasks.append(task)
        for sat in satellites:
            task = asyncio.create_task(
                run_satellite(
                    sat, self._receivers[sat.vban_receive_port], self._watcher,
                    options.publish_exchanges, announcements[sat.name],
                    self._mirrors.get(sat.name),
                )
            )
            task.add_done_callback(
                lambda t, name=sat.name: self._on_task_done(t, f"satellite '{name}'")
            )
            self._satellite_tasks[task] = sat.name

    def _on_task_done(self, task: asyncio.Task, what: str) -> None:
        """A satellite or receiver stopped: a failure unless we stopped it."""
        if self._stopping or task.cancelled():
            return
        self.failure = f"{what} stopped ({task.exception() or 'no error'})"
        _LOGGER.error("%s", self.failure)
        if self._reloading:
            self._reload_failed.set()
        else:
            self.fatal.set()

    async def stop(self) -> None:
        """Stop everything (the app keeps running)."""
        self._stopping = True
        try:
            for task in self._satellite_tasks:
                task.cancel()
            await asyncio.gather(*self._satellite_tasks, return_exceptions=True)
            for task in self._other_tasks:
                task.cancel()
            await asyncio.gather(*self._other_tasks, return_exceptions=True)
            for sat_mirrors in self._mirrors.values():
                await sat_mirrors.close()
            for receiver in self._receivers.values():
                receiver.stop()
            for task in self._receiver_tasks:
                task.cancel()
            await asyncio.gather(*self._receiver_tasks, return_exceptions=True)
        finally:
            self._satellite_tasks = {}
            self._other_tasks = []
            self._receiver_tasks = []
            self._receivers = {}
            self._mirrors = {}
            self._watcher = None
            self._phrases = None
            self._stopping = False

    async def reload(self, options: AppOptions) -> Optional[str]:
        """Apply new (validated) options without restarting the app.

        If a satellite fails to start (e.g. port in use), the previous
        options are restored. Returns the error, or None on success.
        """
        previous = self.options
        _LOGGER.info("Applying new configuration")
        self._reloading = True
        self._reload_failed.clear()
        try:
            await self.stop()
            await self.start(options)
            try:
                await asyncio.wait_for(self._reload_failed.wait(), self.STARTUP_CHECK_S)
            except asyncio.TimeoutError:
                _LOGGER.info("New configuration applied")
                return None
            error = self.failure
            _LOGGER.error("New configuration failed (%s): restoring the previous one", error)
            await self.stop()
            if previous is not None:
                await self.start(previous)
            return error
        finally:
            self._reloading = False
