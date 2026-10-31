"""Runs the satellites of the app; can be reloaded with new options."""

import asyncio
import audioop
import collections
import contextvars
import dataclasses
import logging
import math
import time
import wave
import weakref
from typing import Any, Optional

from wyoming.server import AsyncServer
from wyoming.zeroconf import HomeAssistantZeroconf

from .config import (
    AppOptions,
    ConfigError,
    DebugMirror,
    SatelliteConfig,
    validate_mirrors,
)
from .ha_ws import HaStateWatcher
from .mirror import SatelliteMirrors
from .media_player import MediaPlayerOutput, build_wav, detect_local_ip
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
    make_beep,
    make_satellite_info,
    notify_search,
    notify_vision,
)
from .tts_phrase import TtsPhrase, remove_legacy_cache, remove_stale_phrases
from .vban import VbanPacket, VbanReceiver, VbanSender

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


class ExchangePublisher:
    """Publishes voice exchanges as HA events; keeps the last ones for the
    configuration interface. Settings can change while running."""

    HISTORY = 20

    def __init__(self, enabled: bool, watcher: Optional[HaStateWatcher]):
        self.enabled = enabled
        self.watcher = watcher
        self.history: collections.deque[dict] = collections.deque(maxlen=self.HISTORY)

    async def fire_event(self, event_type: str, data: dict) -> bool:
        self.history.append({"time": time.time(), **data})
        if not self.enabled or self.watcher is None:
            return False
        return await self.watcher.fire_event(event_type, data)


# Satellite settings that need the satellite to be restarted when they
# change (sockets, servers, outputs created at start); the others are
# applied while it runs
RESTART_FIELDS = (
    "name", "wyoming_port", "vban_receive_port", "vban_receive_mode",
    "vban_receive_multicast_group", "vban_receive_stream_name", "tts_output",
    "tts_vban_enabled", "tts_vban_mode", "tts_vban_batch_packets",
    "tts_http_port", "tts_http_host", "followup_enabled",
)


def needs_restart(old: SatelliteConfig, new: SatelliteConfig) -> bool:
    return any(getattr(old, f) != getattr(new, f) for f in RESTART_FIELDS)


class SatelliteRunner:
    """One satellite: Wyoming server, TTS outputs, Zeroconf, debug copies.

    Its output objects are kept so that most settings apply while it runs.
    """

    LEVEL_WINDOW_S = 1.0  # microphone level = peak over this window

    def __init__(
        self,
        cfg: SatelliteConfig,
        receiver: VbanReceiver,
        watcher: Optional[HaStateWatcher],
        publisher: ExchangePublisher,
        cues: WaitingCues,
        mirrors: list[DebugMirror],
    ):
        self.cfg = cfg
        self.receiver = receiver
        self.watcher = watcher
        self.publisher = publisher
        self.cues = cues
        self.mirrors = SatelliteMirrors(mirrors)
        self.vban_sender: Optional[VbanSender] = None
        self.media_output: Optional[MediaPlayerOutput] = None
        self.mic_mute: Optional[MicMute] = None
        self.followup = FollowupSettings(
            enabled=cfg.followup_enabled, beep=cfg.followup_beep,
            timeout_s=cfg.followup_timeout_s,
        )
        self.handlers: weakref.WeakSet[VbanSatelliteHandler] = weakref.WeakSet()
        self.task: Optional[asyncio.Task] = None
        # Microphone level from the received VBAN packets
        self._levels: collections.deque[tuple[float, float]] = collections.deque()
        self.last_packet_at = 0.0

    # Lifecycle

    def start(self) -> asyncio.Task:
        self.mirrors.open()
        self._subscribe()
        self.task = asyncio.create_task(self._run())
        return self.task

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self._unsubscribe()
        await self.mirrors.close()

    def _subscribe(self) -> None:
        stream = self.cfg.vban_receive_stream_name
        self.receiver.subscribe(self._on_packet, stream)
        for raw in self.mirrors.mic_received:
            self.receiver.subscribe(raw.on_packet, stream)

    def _unsubscribe(self) -> None:
        self.receiver.unsubscribe(self._on_packet)
        for raw in self.mirrors.mic_received:
            self.receiver.unsubscribe(raw.on_packet)

    async def _run(self) -> None:
        """Run until cancelled (former run_satellite)."""
        cfg = self.cfg
        _SATELLITE_NAME.set(cfg.name)
        _LOGGER.info(
            "Starting: Wyoming port %d, VBAN receive port %d stream '%s'",
            cfg.wyoming_port, cfg.vban_receive_port,
            cfg.vban_receive_stream_name or "<any>",
        )

        if cfg.tts_vban_enabled:
            self.vban_sender = VbanSender(
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

        if cfg.tts_output == "media_player":
            http_host = cfg.tts_http_host or detect_local_ip()
            self.media_output = MediaPlayerOutput(
                entity_id=cfg.tts_media_player,
                http_host=http_host,
                http_port=cfg.tts_http_port,
            )
            self.media_output.state_watcher = self.watcher
            # Shared by all connections of this satellite
            self.mic_mute = MicMute(
                tail_s=cfg.mic_mute_tail_ms / 1000,
                max_muted_s=VbanSatelliteHandler.MAX_TTS_BUFFER_S,
            )
            _LOGGER.info(
                "  TTS output: media_player %s (HTTP %s:%d)%s, mic muted during "
                "playback + %d ms",
                cfg.tts_media_player, http_host, cfg.tts_http_port,
                ", VBAN fallback" if self.vban_sender else "", cfg.mic_mute_tail_ms,
            )

        self._log_settings()
        satellite_info = make_satellite_info(name=cfg.name)

        # Handler factory — creates a new handler per connection
        def handler_factory(reader, writer):
            handler = VbanSatelliteHandler(
                reader=reader,
                writer=writer,
                satellite_info=satellite_info,
                vban_receiver=self.receiver,
                vban_sender=self.vban_sender,
                media_output=self.media_output,
                vban_stream_name=cfg.vban_receive_stream_name,
                mic_mute=self.mic_mute,
                followup=self.followup,
                exchange_publisher=self.publisher,
                satellite_name=cfg.name,
                waiting_cues=self.cues,
                debug_mirrors=self.mirrors,
            )
            self.handlers.add(handler)
            return handler

        server: Optional[AsyncServer] = None
        zeroconf: Optional[HomeAssistantZeroconf] = None
        try:
            if self.vban_sender:
                self.vban_sender.open()

            if self.media_output:
                try:
                    await self.media_output.start()
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
            if self.vban_sender:
                await self.vban_sender.close()

            if self.media_output:
                await self.media_output.stop()

            # Unregister Zeroconf service
            if zeroconf is not None:
                try:
                    await zeroconf._aiozc.async_close()
                    _LOGGER.debug("Zeroconf unregistered")
                except Exception as err:
                    _LOGGER.debug("Zeroconf cleanup error: %s", err)

    def _log_settings(self) -> None:
        cfg = self.cfg
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

    # Live changes

    async def update(
        self, cfg: SatelliteConfig, cues: WaitingCues, mirrors: list[DebugMirror],
    ) -> None:
        """Apply settings that don't need a restart (see RESTART_FIELDS)."""
        self.cfg = cfg
        if self.vban_sender is not None:
            self.vban_sender.address = cfg.tts_vban_address
            self.vban_sender.port = cfg.tts_vban_port
            self.vban_sender.stream_name = cfg.tts_vban_stream_name
            self.vban_sender.volume = cfg.tts_vban_volume
        if self.media_output is not None:
            self.media_output.entity_id = cfg.tts_media_player
        if self.mic_mute is not None:
            self.mic_mute.tail_s = cfg.mic_mute_tail_ms / 1000
        self.followup.beep = cfg.followup_beep
        self.followup.timeout_s = cfg.followup_timeout_s
        # Handlers hold this object: change its content
        for field in dataclasses.fields(WaitingCues):
            setattr(self.cues, field.name, getattr(cues, field.name))
        self._unsubscribe()
        await self.mirrors.reconfigure(mirrors)
        self._subscribe()
        token = _SATELLITE_NAME.set(cfg.name)
        try:
            _LOGGER.info("Settings applied without restart")
            self._log_settings()
        finally:
            _SATELLITE_NAME.reset(token)

    # Status and tests

    def _on_packet(self, packet: VbanPacket) -> None:
        now = time.monotonic()
        self.last_packet_at = now
        width = packet.sample_width
        if width in (1, 2, 4) and len(packet.payload) >= width:
            rms = audioop.rms(packet.payload[: len(packet.payload) // width * width], width)
            db = 20 * math.log10(max(rms, 1) / (1 << (8 * width - 1)))
            self._levels.append((now, db))
        while self._levels and now - self._levels[0][0] > self.LEVEL_WINDOW_S:
            self._levels.popleft()

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        self._on_levels_expire(now)
        return {
            "name": self.cfg.name,
            "running": self.task is not None and not self.task.done(),
            "ha_connected": any(h.ha_ready for h in self.handlers),
            "streaming": any(h.is_streaming for h in self.handlers),
            "mic_level_db": round(max(db for _, db in self._levels), 1) if self._levels else None,
            "last_packet_s": round(now - self.last_packet_at, 1) if self.last_packet_at else None,
            "output": self.cfg.tts_output,
        }

    def _on_levels_expire(self, now: float) -> None:
        while self._levels and now - self._levels[0][0] > self.LEVEL_WINDOW_S:
            self._levels.popleft()

    async def test(self, signal: str) -> list[str]:
        """Play an announcement (or the follow-up beep) on the outputs."""
        if signal == "followup_beep":
            items = [(Cue(make_beep(22050), 22050), "follow-up beep")]
        else:
            announcement = getattr(self.cues, signal, None)
            if announcement is None:
                return []
            items = announcement.cues()
        played = []
        for cue, label in items:
            if not await self._play(cue):
                break
            played.append(label)
        return played

    async def _play(self, cue: Cue) -> bool:
        if self.media_output is not None:
            url = self.media_output.publish(
                build_wav(cue.pcm, cue.rate, cue.width, cue.channels)
            )
            if await self.media_output.play(url):
                await asyncio.sleep(cue.duration + VbanSatelliteHandler.CUE_CAST_START_S)
                return True
        if self.vban_sender is not None:
            try:
                self.vban_sender.send(
                    cue.pcm, sample_rate=cue.rate, width=cue.width, channels=cue.channels,
                )
            except (audioop.error, ValueError) as err:
                _LOGGER.warning("Test sound not sent: %s", err)
                return False
            await asyncio.sleep(cue.duration + 0.1)
            return True
        return False


class AppRuntime:
    """Everything running for one configuration: VBAN receivers, satellites,
    HA WebSocket, announcement phrases and debug copies.

    apply() changes the configuration without restarting the app, touching
    only what changed.
    """

    STARTUP_CHECK_S = 1.5  # a satellite failing to start does so by then

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.options: Optional[AppOptions] = None
        # A satellite or receiver stopped on its own (outside a change)
        self.fatal = asyncio.Event()
        self.failure = ""
        self._changing = False
        self._stopping = False
        self._change_failed = asyncio.Event()
        self._receivers: dict[int, VbanReceiver] = {}
        self._receiver_tasks: dict[int, asyncio.Task] = {}
        self._runners: dict[str, SatelliteRunner] = {}
        self._phrases: Optional[PhraseRegistry] = None
        self._phrase_tasks: dict[int, asyncio.Task] = {}  # id(phrase) -> task
        self._watcher: Optional[HaStateWatcher] = None
        self._watcher_task: Optional[asyncio.Task] = None
        self.publisher = ExchangePublisher(False, None)

    @property
    def watcher(self) -> Optional[HaStateWatcher]:
        return self._watcher

    @property
    def phrases(self) -> list[TtsPhrase]:
        return [p for p in (self._phrases.phrases if self._phrases else [])
                if id(p) in self._phrase_tasks]

    @property
    def runners(self) -> dict[str, SatelliteRunner]:
        return self._runners

    # Building blocks

    @staticmethod
    def _needs_watcher(options: AppOptions, cues: dict[str, WaitingCues]) -> bool:
        return bool(
            options.publish_exchanges
            or any(s.tts_output == "media_player" for s in options.satellites)
            or any(c.search or c.vision for c in cues.values())
        )

    @staticmethod
    def _media_players(options: AppOptions) -> list[str]:
        return [s.tts_media_player for s in options.satellites if s.tts_output == "media_player"]

    def _build_cues(self, options: AppOptions) -> dict[str, WaitingCues]:
        """Announcements of each satellite; reuses the known phrases."""
        registry = PhraseRegistry(
            self.cache_dir, options.announcements_tts_engine,
            options.announcements_tts_voice,
        )
        if self._phrases is not None:  # keep synthesized phrases
            registry._phrases.update(self._phrases._phrases)
        self._phrases = registry
        return {sat.name: _waiting_cues(sat, registry) for sat in options.satellites}

    def _sync_phrases(self, cues: dict[str, WaitingCues]) -> None:
        """Synthesize the phrases in use, drop the others."""
        used = {}
        for sat_cues in cues.values():
            for field in dataclasses.fields(WaitingCues):
                announcement = getattr(sat_cues, field.name)
                if isinstance(announcement, Announcement) and announcement.phrase:
                    used[id(announcement.phrase)] = announcement.phrase
        for key in list(self._phrase_tasks):
            if key not in used:
                self._phrase_tasks.pop(key).cancel()
        for key, phrase in used.items():
            if key not in self._phrase_tasks:
                self._phrase_tasks[key] = asyncio.create_task(phrase.run())
        if self._phrases is not None:
            self._phrases._phrases = {
                k: p for k, p in self._phrases._phrases.items() if id(p) in used
            }
        remove_stale_phrases(list(used.values()))

    async def _set_watcher(self, needed: bool, options: AppOptions) -> None:
        """Start or stop the HA WebSocket and attach it to the satellites.

        One HA WebSocket for the app: follows the media_players used for TTS
        output, publishes voice exchanges, receives jarvis_* events.
        """
        if needed and self._watcher is None:
            self._watcher = HaStateWatcher(self._media_players(options))
            # Internet search / camera look started (fired by the
            # demander_ia_externe / regarder_la_camera scripts)
            self._watcher.add_event_listener("jarvis_recherche", notify_search)
            self._watcher.add_event_listener("jarvis_vision", notify_vision)
            self._watcher_task = asyncio.create_task(self._watcher.run())
        elif not needed and self._watcher is not None:
            assert self._watcher_task is not None
            self._watcher_task.cancel()
            await asyncio.gather(self._watcher_task, return_exceptions=True)
            self._watcher = None
            self._watcher_task = None
        elif self._watcher is not None:
            await self._watcher.set_entity_ids(self._media_players(options))
        self.publisher.watcher = self._watcher
        for runner in self._runners.values():
            runner.watcher = self._watcher
            if runner.media_output is not None:
                runner.media_output.state_watcher = self._watcher

    def _start_receiver(self, port: int, sat: SatelliteConfig) -> None:
        receiver = VbanReceiver(
            port=port, mode=sat.vban_receive_mode,
            multicast_group=sat.vban_receive_multicast_group,
        )
        self._receivers[port] = receiver
        task = asyncio.create_task(receiver.run())
        task.add_done_callback(
            lambda t, port=port: self._on_task_done(t, f"VBAN receiver (port {port})")
        )
        self._receiver_tasks[port] = task

    async def _stop_receiver(self, port: int) -> None:
        self._receivers.pop(port).stop()
        task = self._receiver_tasks.pop(port)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _start_runner(
        self, sat: SatelliteConfig, cues: WaitingCues, mirrors: list[DebugMirror],
    ) -> None:
        runner = SatelliteRunner(
            sat, self._receivers[sat.vban_receive_port], self._watcher,
            self.publisher, cues, mirrors,
        )
        self._runners[sat.name] = runner
        runner.start().add_done_callback(
            lambda t, name=sat.name: self._on_task_done(t, f"satellite '{name}'")
        )

    async def _stop_runner(self, name: str) -> None:
        self._stopping = True
        try:
            await self._runners.pop(name).stop()
        finally:
            self._stopping = False

    def _on_task_done(self, task: asyncio.Task, what: str) -> None:
        """A satellite or receiver stopped: a failure unless we stopped it."""
        if self._stopping or task.cancelled():
            return
        self.failure = f"{what} stopped ({task.exception() or 'no error'})"
        _LOGGER.error("%s", self.failure)
        if self._changing:
            self._change_failed.set()
        else:
            self.fatal.set()

    # Whole configuration

    async def start(self, options: AppOptions) -> None:
        """Start everything for these (validated) options."""
        self.options = options
        logging.getLogger().setLevel(logging.DEBUG if options.debug_logging else logging.INFO)
        _LOGGER.info("Starting %d satellite(s)", len(options.satellites))
        cues = self._build_cues(options)
        self._sync_phrases(cues)
        remove_legacy_cache()

        await self._set_watcher(self._needs_watcher(options, cues), options)
        self.publisher.enabled = options.publish_exchanges
        _LOGGER.info(
            "Voice exchanges %s",
            "published as jarvis_echange events" if options.publish_exchanges
            else "not published",
        )

        # One VBAN receiver (one UDP socket) per port, shared by the
        # satellites on that port and routing packets by stream name.
        # validate() ensured they agree on mode / multicast group.
        for sat in options.satellites:
            if sat.vban_receive_port not in self._receivers:
                self._start_receiver(sat.vban_receive_port, sat)

        mirrors = validate_mirrors(options.debug_mirrors, options.satellites)
        for sat in options.satellites:
            self._start_runner(
                sat, cues[sat.name], [m for m in mirrors if m.satellite == sat.name],
            )

    async def stop(self) -> None:
        """Stop everything (the app keeps running)."""
        self._stopping = True
        try:
            for runner in self._runners.values():
                await runner.stop()
            for task in self._phrase_tasks.values():
                task.cancel()
            await asyncio.gather(*self._phrase_tasks.values(), return_exceptions=True)
            if self._watcher_task is not None:
                self._watcher_task.cancel()
                await asyncio.gather(self._watcher_task, return_exceptions=True)
            for port in list(self._receivers):
                await self._stop_receiver(port)
        finally:
            self._runners = {}
            self._phrase_tasks = {}
            self._phrases = None
            self._watcher = None
            self._watcher_task = None
            self.publisher.watcher = None
            self._stopping = False

    async def reload(self, options: AppOptions) -> Optional[str]:
        """Restart everything with new options (see apply)."""
        return await self._change(options, self._restart_all)

    async def apply(self, options: AppOptions) -> tuple[Optional[str], list[str]]:
        """Apply new (validated) options, restarting only what must be.

        Returns (error, what was done). On error (e.g. a port in use), the
        previous options are restored.
        """
        done: list[str] = []

        async def change(new: AppOptions) -> None:
            await self._apply_changes(new, done)

        error = await self._change(options, change)
        return error, done

    async def _change(self, options: AppOptions, change) -> Optional[str]:
        previous = self.options
        _LOGGER.info("Applying new configuration")
        self._changing = True
        self._change_failed.clear()
        try:
            await change(options)
            try:
                await asyncio.wait_for(self._change_failed.wait(), self.STARTUP_CHECK_S)
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
            self._changing = False

    async def _restart_all(self, options: AppOptions) -> None:
        await self.stop()
        await self.start(options)

    async def _apply_changes(self, new: AppOptions, done: list[str]) -> None:
        old = self.options
        assert old is not None
        cues = self._build_cues(new)
        self.options = new
        logging.getLogger().setLevel(logging.DEBUG if new.debug_logging else logging.INFO)
        self.publisher.enabled = new.publish_exchanges
        self._sync_phrases(cues)
        await self._set_watcher(self._needs_watcher(new, cues), new)

        old_sats = {s.name: s for s in old.satellites}
        new_sats = {s.name: s for s in new.satellites}
        restart = {
            name for name, sat in new_sats.items()
            if name in old_sats and needs_restart(old_sats[name], sat)
        }
        # Receivers whose port is no longer used, or whose mode/group changed
        # (their satellites restart with the new receiver)
        wanted = {}
        for sat in new.satellites:
            wanted.setdefault(sat.vban_receive_port, sat)
        changed_ports = {
            port for port, receiver in self._receivers.items()
            if port not in wanted
            or (receiver.mode, receiver.multicast_group)
            != (wanted[port].vban_receive_mode, wanted[port].vban_receive_multicast_group)
        }
        restart |= {
            name for name, sat in old_sats.items()
            if name in new_sats and sat.vban_receive_port in changed_ports
        }

        for name in list(self._runners):
            if name not in new_sats:
                await self._stop_runner(name)
                done.append(f"satellite '{name}' removed")
            elif name in restart:
                await self._stop_runner(name)
        self._stopping = True
        try:
            for port in changed_ports:
                await self._stop_receiver(port)
        finally:
            self._stopping = False
        for port, sat in wanted.items():
            if port not in self._receivers:
                self._start_receiver(port, sat)

        mirrors = validate_mirrors(new.debug_mirrors, new.satellites)
        for sat in new.satellites:
            sat_mirrors = [m for m in mirrors if m.satellite == sat.name]
            if sat.name not in self._runners:
                self._start_runner(sat, cues[sat.name], sat_mirrors)
                done.append(
                    f"satellite '{sat.name}' restarted" if sat.name in old_sats
                    else f"satellite '{sat.name}' added"
                )
            else:
                runner = self._runners[sat.name]
                if dataclasses.asdict(runner.cfg) != dataclasses.asdict(sat) or (
                    [dataclasses.asdict(m) for m in sat_mirrors]
                    != [dataclasses.asdict(m) for m in old_mirrors_of(old, sat.name)]
                ) or cues[sat.name] != runner.cues:
                    await runner.update(sat, cues[sat.name], sat_mirrors)
                    done.append(f"satellite '{sat.name}' updated without restart")

    def status(self) -> dict[str, Any]:
        return {
            "satellites": [runner.status() for runner in self._runners.values()],
            "ha_websocket": self._watcher.connected if self._watcher else None,
            "phrases": [
                {"text": p.text, "ready": p.cue is not None} for p in self.phrases
            ],
            "exchanges": list(self.publisher.history)[::-1],
        }


def old_mirrors_of(options: AppOptions, satellite: str) -> list[DebugMirror]:
    return [m for m in options.debug_mirrors if m.satellite == satellite]
