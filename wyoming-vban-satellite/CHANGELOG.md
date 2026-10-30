# Changelog

## 2.13.0

- **New**: each voice exchange is published as a `jarvis_echange` event on the Home Assistant event bus, e.g. for a "last commands" dashboard: `{"satellite", "transcript", "response", "followup"}`
- HA doesn't put the text of voice exchanges on its event bus; the satellite receives it (`transcript`, then `synthesize`) and fires the event through the shared HA WebSocket connection (`fire_event`). `followup` is true for an answer given without the wake word.
- Announcements (TTS without a transcript) produce no event. When the WebSocket is down, the event is dropped (debug log), not queued.
- New app option `publish_exchanges` (default on) to turn it off.
- The HA WebSocket connection is now also opened when no satellite uses media_player output (without the state subscription), unless `publish_exchanges` is off.

## 2.12.2

- **Fix**: the HA configuration form refused to save a unicast satellite ("please fill in multicast group") — `vban_receive_multicast_group` was a required string, empty in unicast mode
- `vban_receive_multicast_group` is now optional (`str?`) and no longer in the default satellite options. Absent, null or empty is accepted in unicast mode, where the value is ignored (a leftover value no longer counts as a different setting for satellites sharing a port).
- Multicast mode without a group stops the app at startup with an explicit error.

## 2.12.1

- **Fix**: follow-up — the answer given right after the beep was lost
- 2.11.0 waited for the media_player to go back to idle after the beep before starting the ASR run. A Cast device stays `playing` after such a short sound, so the satellite waited ~5 s (end-of-playback timeout) with the mic paused: an immediate answer never reached HA, then the no-speech timeout expired.
- The ASR run now starts together with the beep, without waiting for it to play. The mic is replaced by silence for a fixed guard so the beep isn't captured: 0.45 s from `play_media`'s HTTP 200 (media_player), or beep + 0.4 s Atom prebuffer = 0.55 s from sending (VBAN).
- The mic audio of the guard (up to 0.6 s) is kept and sent first if speech has already started when the guard ends (last 128 ms above -45 dBFS); otherwise it is dropped.
- `followup_timeout_s` now starts when the mic opens.
- VBAN end-of-playback margin (receiver prebuffer) raised from 0.3 to 0.4 s.
- New logs: `Follow-up: beep sent, mic opens in X.XXs`, `Follow-up: listening`.

## 2.12.0

- **Dependencies**: `wyoming` 1.8.0 → 1.10.2, `zeroconf` ≥ 0.151.5, `aiohttp` ≥ 3.14
- **Base image**: Python 3.11 / Alpine 3.18 → Python 3.12 / Alpine 3.24 (aarch64, amd64). armhf, armv7 and i386 use Python 3.12 / Alpine 3.22, the last base images published for 32-bit architectures. Python 3.12 is the latest version that still ships `audioop` (removed in 3.13).
- The Dockerfile no longer installs Alpine's own Python next to the base image's one (the app always ran on the base image's Python).

## 2.11.0

- **New**: follow-up without wake word — when a response is a question (text ending with `?`), the satellite listens for the answer right away, with a short beep (150 ms, 880 Hz) on its output
- HA computes `continue_conversation` but does not pass it to Wyoming satellites, and `restart_on_end` always restarts at the wake word stage. The satellite reads the response text from the `synthesize` event instead.
- A follow-up run can't be started while HA's auto-restarted wake run is active: sending `audio-stop` ends that run *without* an error, so HA restarts it again, and starting a run while another is active cancels the old one, whose run-end then makes HA reject the new run's audio. So with follow-ups enabled, the satellite drives the runs itself (`restart_on_end=False`): once the response has played (media_player end of playback, or VBAN sent + 0.3 s), it starts a wake run, or an ASR run after a question. Errors start a new wake run after 0.5 s.
- No speech within `followup_timeout_s` (default 8 s): the satellite sends `audio-stop` and goes back to the wake word. At most 3 follow-ups in a row.
- Announcements (TTS audio without a transcript) don't end a run. A run that ends with no spoken response nor error (empty response) is detected after 60 s.
- Wake word detection now resumes only once the response has played (previously HA restarted it as soon as the run ended); mic audio is no longer sent between runs.
- New per-satellite options `followup_enabled` (default on — off keeps the previous behavior), `followup_beep` (default on), `followup_timeout_s` (3–30, default 8).

## 2.10.0

- **Fix**: media_player output — unmute the microphone when the speaker has actually finished playing, instead of an estimate 1.5–2 s too long
- 2.9.1 kept the mic muted for WAV duration + 1.5 s (Cast start estimate) + tail from the `play_media` call. Cast actually starts in 0.2–0.8 s, so commands spoken right after a response were missed.
- The app now follows the media_player state through the HA WebSocket API (`ws://supervisor/core/websocket`, one connection shared by all satellites, `subscribe_trigger` on the TTS media_players only, reconnect with 1 s → 30 s backoff). After `play_media`: wait for the entity to be `playing` our file (max 5 s), then for it to stop playing it (max WAV duration + 5 s), then `mic_mute_tail_ms`; the mic is unmuted and `played` sent to HA.
- Fallback to the 2.9.1 estimate when the WebSocket is not connected or playback is not seen starting within 5 s, with a warning at most once a minute.
- `mic_mute_tail_ms` default lowered from 700 to 250 ms (existing configs that set it explicitly keep their value).
- A new response arriving during the wait still keeps the mic muted without a gap (the newer response owns the mute).

## 2.9.1

- **Fix**: media_player output — mute the microphone while the response plays, so the satellite no longer answers its own voice
- HA restarts wake word detection as soon as the pipeline run ends, before the response has played on the media_player. The speaker's voice reached the microphone, openWakeWord detected a false wake word and Whisper transcribed the end of the response (e.g. "Quelque temps de montrer").
- From `audio-start` (before `play_media`) until the response has played (duration + 1.5 s for Cast) plus `mic_mute_tail_ms`, the satellite sends silent audio chunks instead of the microphone (the stream keeps flowing so HA's wake word stage doesn't time out). Unmuted at once if `play_media` fails, if the response is empty, or if HA disconnects mid-response; safety unmute after 300 s.
- Mute state is shared by all connections of a satellite, so a reconnect from HA during playback keeps the mic muted.
- New per-satellite option `mic_mute_tail_ms` (0–5000, default 700).
- VBAN output is unchanged (the receiver is half-duplex).

## 2.9.0

- **Fix**: satellites sharing a `vban_receive_port` no longer steal each other's audio
- Each satellite used to run in its own process with its own UDP socket on the port (SO_REUSEADDR/SO_REUSEPORT). Linux spreads unicast datagrams across such sockets instead of duplicating them, so a satellite received part of another satellite's stream and dropped it (seen in production: the AtomBureau stream reaching the Salon satellite).
- All satellites now run in a single process with one VBAN socket per port, shared by the satellites on that port. Packets are routed by stream name: exact `vban_receive_stream_name` match first, otherwise to the satellite(s) of that port with an empty stream name (catch-all), otherwise ignored.
- Config is unchanged. New startup checks: satellites sharing a port must use the same receive mode / multicast group; warning when several satellites share a port without stream names; duplicate TTS HTTP ports are rejected.
- Log lines are prefixed with the satellite name. If one satellite fails (e.g. port in use), the whole app stops, as before.
- `run.sh` now just starts the process, which reads `/data/options.json` directly (defaults for missing/null options are applied in Python).
- Added tests (`tests/`, run with `python3 -m unittest discover tests`): two streams on one port, catch-all routing, config defaults and validation.

## 2.8.0

- **New**: per-satellite TTS output to a Home Assistant `media_player` (e.g. Google Nest via Cast), in addition to VBAN
- New options: `tts_output` (`vban` | `media_player`, default `vban` — existing configs unchanged), `tts_media_player`, `tts_http_port` (default 10800 + satellite index), `tts_http_host` (default: auto-detected LAN IP)
- The response is buffered as received (no resampling), served as a WAV by a small HTTP server (`/tts/<uuid>.wav`, last 5 kept), and played with `media_player.play_media` through the Supervisor API (`homeassistant_api: true`)
- The satellite sends the Wyoming `played` event once the WAV has played (duration + 1.5 s for Cast startup), so the satellite entity returns to idle when the speaker finishes. VBAN mode is unchanged (no `played`, HA uses its own timeout)
- If `play_media` fails (HTTP error or 5 s timeout), the response falls back to VBAN when it is configured
- New dependency: `aiohttp`

## 2.7.4

- **Fix**: app failed to start when `tts_vban_batch_packets` was missing from the config (e.g. configs created before 2.7.3)
- bashio returns the string `null` for a missing optional option, and `run.sh` passed `--tts-vban-batch-packets null`, which argparse rejected; both satellites stopped and the app went into error.
- `run.sh` now skips any optional option whose value is empty or `null` (`tts_vban_batch_packets`, `tts_vban_volume`, `tts_vban_address`, `vban_receive_multicast_group`, `vban_receive_stream_name`), so the Python default applies.

## 2.7.3

- **Fix**: smooth TTS VBAN output — send one packet every 16 ms instead of bursts of 4 every 64 ms
- ESP32 receivers (tiny lwIP UDP queue) dropped ~17% of packets from the 4-packet bursts.
- Pacing no longer sends batches back-to-back after a late wake-up: if a batch goes out more than half an interval late, the schedule restarts from that batch instead of catching up.
- New per-satellite option `tts_vban_batch_packets` (1–8, default 1) to restore larger batches if needed.

## 2.7.2

- **Fix**: restart the wake→TTS pipeline after a server error
- After an `error` event from HA (`duplicate_wake_up_detected`, `stt-no-text-recognized`, `wake_word_detection_aborted`), HA ends the pipeline and does not restart it, even with `restart_on_end=True`. The satellite kept streaming audio that HA rejected ("Unexpected event from satellite: audio-chunk"), openWakeWord received no session, and the satellite stayed deaf until the app restarted.
- The satellite now stops streaming and re-sends `run-pipeline` in the background (0.5s later) after an error. With two satellites hearing the same wake word, the one receiving `duplicate_wake_up_detected` goes back to listening.

## 2.7.1

- **Fix**: raise TTS send buffer cap from 10s to 60s
- HA sends the full TTS as a burst over localhost TCP (fills the buffer in ms while drain plays in real-time). A 10s cap truncated long TTS responses.

## 2.7.0

- Add `tts_vban_volume` per-satellite option (0.0–2.0, default 1.0)
- Applies a digital volume multiplier to TTS audio before sending via VBAN
- Uses `audioop.mul` which clips at 16-bit range — factor > 1.0 saturates loud samples gracefully instead of wrapping

## 2.6.0

Final robustness pass from third deep code review:

- **Fix**: `_send_packet` now checks `self._socket is None` before sendto (prevents AttributeError if close() fires mid-batch)
- **Fix**: Zeroconf name includes the satellite name + port, so multiple satellites on the same host no longer collide on default MAC-only name
- **Fix**: rate-limit "VBAN send error" logs to once per 5s (prevents log spam on persistent network failures, e.g. speaker unreachable)
- **Fix**: rate-limit subscriber callback errors in VBAN receiver for the same reason
- **Add**: SIGTERM/SIGINT handlers for clean shutdown (HAOS sends SIGTERM to stop containers)
- **Cleanup**: extract magic ratio (`OVERFLOW_RESET_RATIO`) as a named constant

## 2.5.2

Robustness fixes from second deep code review:

- **Fix**: cleanup VbanSender and Zeroconf properly on shutdown (was leaking socket and mDNS registration)
- **Fix**: make VbanSender.close() async and await the drain task to prevent stray execution after close
- **Fix**: rate-limit "Non-PCM VBAN codec" warning (was logging up to 62/s on non-PCM streams)
- **Fix**: catch audioop errors when forwarding TTS to VBAN (prevents handler crash on malformed audio)
- **Add**: hard cap on TTS send buffer (10s max) to prevent unbounded memory growth
- **Add**: validate unique wyoming_port across satellites in run.sh (fatal error if duplicate)

## 2.5.1

Fixes from deep code analysis:

- **Fix**: clear TTS send buffer on AudioStart to prevent audio from a previous stream leaking into a new one
- **Fix**: race condition in drain loop pre-buffer — re-check buffer length after clearing the event, avoids waiting 500ms when data is already available
- **Optim**: lower TTS pre-buffer from 100ms → 50ms (still absorbs LAN jitter, reduces latency)
- **Cleanup**: remove dead `resample_from_wyoming` function (unused since v2.3.0)

## 2.5.0

Fluidity improvements:

- **TTS drain loop**: batch 4 packets per wake-up (64ms) instead of 1 (16ms), reduces timer jitter impact 4x
- **TTS pre-buffer**: wait for 100ms of audio before emitting first packet, absorbs HA arrival jitter
- **Event-driven drain**: replaces 5ms polling loop with asyncio.Event — no CPU spin when idle
- **Mic queue size**: reduced from 100 (~1.6s) to 32 (~500ms) for lower max latency
- **Queue overflow**: drop new packets instead of oldest — preserves audio continuity for wake word detection
- **Non-blocking pipeline start**: 300ms grace delay before streaming runs in background task, doesn't block HA event reader

## 2.4.0

- Pace VBAN packet transmission at real-time audio rate
- TTS audio is now queued in a buffer and drained by a background task
- Emits one 256-sample packet every 16ms (at 16kHz) instead of bursting all packets at once
- Fixes choppy TTS playback caused by buffer overflow on the VBAN receiver

## 2.3.2

- Log INFO message "VBAN audio resumed" when audio returns after a silence/timeout

## 2.3.1

- Log error events received from HA (pipeline failures, STT errors, etc.)

## 2.3.0

- Resample TTS audio to match the VBAN speaker's configured rate (16kHz by default)
- VBAN speakers don't resample — they play at their configured rate
- Fixes choppy/cut TTS playback when Piper (22050Hz) is played on a 16kHz VBAN speaker
- Handles sample width and channel count conversion too

## 2.2.1

- Fix choppy TTS playback: use actual audio format from AudioChunk events
- Previously the VBAN sender hardcoded 16kHz, but Piper TTS outputs at 22050Hz
- Also log the TTS audio format when received for easier debugging

## 2.2.0

- Refactor VBAN receiver to a persistent pub/sub model
- Receiver runs once at application startup and stays alive for the whole lifetime
- Handlers subscribe/unsubscribe on connect/disconnect, no more socket restart
- Fixes race conditions and audio loss during HA reconnections

## 2.1.1

- Override AsyncEventHandler.run() to catch ConnectionError/OSError
- Suppresses "Task exception was never retrieved" warnings from the wyoming base class when HA disconnects between pipeline cycles

## 2.1.0

- Always declare snd (audio output) in satellite Info, even without TTS VBAN
- This allows HA to show "Media player for TTS" in satellite config
- TTS audio is silently discarded if no VBAN speaker is configured

## 2.0.2

- Fix ConnectionResetError when HA disconnects between pipeline cycles
- Only send StreamingStopped if connection is still active

## 2.0.1

- Fix pipeline crash: always use end_stage=TTS (HA crashes with KeyError 'tts_output' when end_stage=HANDLE)
- TTS audio is silently ignored if no VBAN speaker is configured

## 2.0.0

- Fix critical event ordering that prevented wake word detection from working
- HA only routes audio to openWakeWord AFTER processing RunPipeline
- New protocol flow: wait for second Describe (inside HA's pipeline loop), respond with Info, send RunPipeline, wait 300ms, then start streaming audio
- This ensures HA has set _is_pipeline_running=True before audio arrives

## 1.7.0

- Send run-pipeline event to HA after streaming starts (start_stage=wake, restart_on_end=true)
- This tells HA to start wake word detection on the audio stream — the missing piece for triggering openWakeWord
- Set end_stage based on whether TTS VBAN output is enabled (tts vs handle)

## 1.6.1

- Log peak audio level over 50-chunk windows instead of sampling one chunk
- No more missed voice peaks in diagnostic logs

## 1.6.0

- Add Zeroconf (mDNS) discovery so HA auto-discovers the satellite as a proper assist_satellite
- No more manual Wyoming Protocol integration needed — HA finds the satellite automatically

## 1.5.0

- Buffer small VBAN packets (256 samples/16ms) into larger Wyoming chunks (1024 samples/64ms)
- Matches the chunk size used by the official wyoming-satellite for reliable wake word detection

## 1.4.2

- Fix crash: use SndProgram in Info.snd instead of Satellite.snd_format (removed in wyoming 1.8.0)
- TTS speaker capability declared via SndProgram only when tts_vban_enabled is true

## 1.4.0

- Fix satellite discovery: declare only `satellite` in Info (no mic/snd programs)
- Matches official wyoming-satellite behavior so HA creates an assist_satellite entity instead of assist_microphone
- This fixes wake word detection not triggering

## 1.3.1

- Add diagnostic logging: warn when no VBAN audio is received, log chunk counts and sizes
- Helps diagnose connectivity issues between VBAN source and addon

## 1.3.0

- Handle pause-satellite event from HA (stop/resume streaming properly)
- Keep VBAN receiver alive across pause/resume cycles (avoids socket close/reopen)
- Drop audio packets when paused to prevent stale audio on resume
- Fix disconnect cleanup

## 1.2.2

- Declare mic and snd capabilities in Wyoming Info so HA recognizes the satellite as a full voice device
- Upgrade wyoming library from 1.5.4 to 1.8.0

## 1.2.1

- Handle Wyoming ping/pong keepalive from Home Assistant

## 1.2.0

- Add broadcast support (SO_BROADCAST) for VBAN sources sending to broadcast addresses (e.g. 192.168.0.255)

## 1.1.0

- Support multiple VBAN satellites from a single addon
- Each satellite has its own name, Wyoming port, and VBAN settings
- All satellites run in parallel with crash monitoring

## 1.0.0

- Initial release
- VBAN audio reception (unicast and multicast)
- Automatic resampling to 16kHz 16-bit mono
- Optional TTS output via VBAN
- Stream name filtering
- Host networking for full UDP support
