# Changelog

## 3.11.0

- **Wake word clips are validated by hand, one by one.** Nothing is classified automatically, in either direction; only validated clips (good or false) go to WakeForge's inbox.
  - New suggestion `probablement_bon` (a normal sentence and an answer); every suggestion has its reason (silence ensuite, bip capté, conversation captée, phrase normale…). A suggestion is never a validation.
  - **Revue mode** (default in the Mot-clé tab): the unsorted clips, "probablement faux" first, then the oldest first; each one plays when it shows; room, time, transcript, Jarvis's response, suggestion and reason; big buttons **OK, c'était le mot-clé** / **Pas OK, faux déclenchement**, plus **Ignorer** and **Passer**; keys O, N, I, Space (listen again), → (skip); next clip played after each choice; **Annuler le dernier choix**; "12 à revoir · 3 faits aujourd'hui"; full-width buttons on a phone.
  - The validation buttons only work once the clip has been heard (in the list too).
  - The "Marquer le dernier déclenchement comme faux" button is removed; the `jarvis_faux_declenchement` event now flags the clip for the review instead of classifying it.
  - `wake_clips.notify_review` (off by default): a Home Assistant notification while more than 20 clips wait, with a link to the app.

## 3.10.0

- **New: wake word clips**, to retrain the wake word model on what really triggers it (`wake_clips` block, off by default):
  - each detection in a wake run keeps the audio sent to Home Assistant (what openWakeWord heard) from 2.5 s before to 0.5 s after it, placed by the detection's `timestamp` when the satellite started the run (follow-up mode), otherwise by its arrival minus the measured delay; counted in samples sent, so a microphone cut (Atom playing) doesn't shorten it; no clip for follow-up runs (started at ASR);
  - one WAV and one JSON per clip in `/data/wake_clips`: satellite, time, wake word, RMS and peak, transcript and response that followed (or the STT error), and a suggestion (`probablement_faux` when nothing was said after the wake word, otherwise `a_verifier`); never sorted by the app;
  - retention: `retention_days` (30) for unsorted and ignored clips; `max_clips` (2000), unsorted ones going first; per satellite `wake_clips: false` to leave one out;
  - **Mot-clé tab**: clips by room and status, waveform, play, **Bon mot-clé** / **Faux déclenchement** / **Ignorer** (keys B, F, I, arrows, Space), counts of good and false triggers per room and day, **Marquer le dernier déclenchement comme faux**;
  - sorted clips copied to `/share/wakeforge/inbox/<wake word without -N>/positifs|negatifs/` with `manifest.json` (path, status, sha256, satellite, date, transcript), moved when sorted again, removed when ignored. The app now maps `/share` read-write;
  - Home Assistant event `jarvis_faux_declenchement` (`satellite`): marks that satellite's last clip as a false trigger, e.g. from a "ce n'était pas pour toi" sentence.

## 3.9.3

The last points of the review.

- **Follow-up on/off without restarting the satellite.** Off: from the next wake word run, Home Assistant restarts the runs again (a question just asked no longer opens a follow-up). On: once no exchange is in progress, the connection to Home Assistant is closed; Home Assistant reconnects and the new connection drives the runs (a run sent while Home Assistant's own is active would lose its audio).
- The microphone mute during a response on a media player is lifted by force after 330 s instead of 300 s (exactly the longest response): a response near 5 minutes no longer reopens the microphone just before its end.
- **Wake word test**: the openWakeWord address is fixed by the app (`core-openwakeword:10400`), no longer taken from the request.
- Calibration: clipped samples are counted without a Python loop (threshold −0.07 dBFS instead of −0.01).

## 3.9.2

- **Fix (follow-up without wake word)**: the listening opened while the speaker was still playing the beep. With an Atom on VBAN (no microphone audio while it plays, playback buffered), Whisper transcribed the beep ("*bip*") and the no-speech timeout started before the microphone was even back, leaving about 1 s to answer. Now:
  - the listening opens when the beep has really played: VBAN, once sent plus the speaker latency; media player, from its state (playing, then no longer playing the beep), or else the beep's length plus the latency;
  - if the satellite cut its microphone stream, the listening waits for it to come back, plus `media_player.mic_mute_tail_ms`; at most 4 s after the beep (then a warning, and it listens anyway);
  - every microphone sample received before the opening is replaced by silence (the pre-roll of the beep guard is gone): no beep, no end of the response in the transcription;
  - `followup.timeout_s` counts from the opening;
  - log: `Follow-up: beep played, mic back after X.XX s, listening for N s`.
- **New** `followup.beep_latency_ms` (0–3000): speaker latency of the beep, per satellite (default 400 ms on VBAN, 1500 ms on a media player whose state can't be followed).

## 3.9.1

Friction points from the review.

- **A failed configuration is rolled back piecemeal**: only what the change touched (or what stopped) is restarted; the other satellites keep running, conversations included.
- **Port conflicts are refused when saving**, with a clear message: a satellite's `media_player.http_port` equal to another's Wyoming port, or any of them on 8743 (the configuration interface). They were only seen when a satellite failed to start.
- **A long spoken announcement no longer delays the response**: the response cuts it (a short sound, under 1 s, still finishes first).
- **Media player on a LAN without internet**: the host's address is found through the mDNS route when the internet one fails; if none, a clear error tells to set `media_player.http_host` (127.0.0.1 can't be reached by a Cast device).
- **Calibration**: shorter sentence to read (it was tight in 8 s); detections above the number of wake words show as "5/5 +1" with "false wake-up"; a **Réessayer** button when openWakeWord can't be reached (no need to measure the voice again).
- **Interface**: "Modifications non enregistrées" no longer replaces an error message at each keystroke; the last API messages in English are in French.
- **Logs**: range warnings use the option names of the interface (`vban_output.volume`, `media_player.mic_mute_tail_ms`, `followup.timeout_s`…); in follow-up mode, the wait for a response is logged (debug).
- The `microphone` block description (Supervisor's configuration tab) covers the camera source and the clean-up.

## 3.9.0

Improvements from the review.

- **A satellite that stops on its own is started again alone**, after 5, 10, 30 then 60 s (the delay grows while it keeps failing), instead of stopping the whole app. The Supervisor's **watchdog** restarts the app if it stops answering.
- **Status tab: recent problems** (a satellite stopped and started again, a configuration refused and rolled back, a discovery failure, a background task stopped).
- **Follow-up: empty responses end the run at once.** The satellite's `assist_satellite` state in Home Assistant going back to idle after a transcript is seen (60 s timeout kept as a fallback when Home Assistant can't be reached). Follow-up satellites therefore use the HA WebSocket.
- **New `microphone.multicast_interface`**: the address of the network interface joining the multicast group, for hosts with several interfaces (default: the system's choice).
- **Calibration picks up where it was** after a page reload (within the 15 minutes the recordings are kept).
- The TTS HTTP server answers **byte-range requests** (Sonos, DLNA renderers).
- Ready for **Python 3.13** (`audioop-lts` installed there; `audioop` was removed from Python).

## 3.8.2

Optimisations from the review.

- **Wake word test** about twice as fast (about 22 s instead of 42): the audio goes to openWakeWord while the wake words are said, processed live with the current and the proposed settings, instead of being replayed in real time afterwards.
- **One Zeroconf responder** for all the satellites (one per satellite before, each with its own sockets).
- **Microphone audio loop** (every 64 ms per satellite): packets waiting in the queue taken in one go, one copy per chunk, silence prepared once, level computed only when a log shows it. TTS chunks parsed once; debug copies skipped when none is configured.
- **VBAN**: subscribers kept as tuples (no list copy per packet); the sender's header built once per stream name.
- **HA microphone entities**: entity registry events (fired for any entity in HA) only trigger a new lookup when they can concern a followed entity; the followed entities are read with `subscribe_entities` (their states only) instead of every state of HA.
- **HA WebSocket**: changing the media player of a satellite replaces its subscription instead of reconnecting (the other subscriptions no longer drop for a moment).
- The follow-up beep is computed once.
- `microphone.stream_name` now applies without restarting the satellite.
- **Interface**: the live meter pauses while the browser tab is hidden; its fallback polling waits for each answer instead of piling requests up; expired calibrations are deleted every minute even when the page isn't used again.

## 3.8.1

Fixes from a full review of the app.

- **Follow-up**: an STT error arriving while the follow-up beep started could leave the microphone silenced for an hour (deaf to the wake word).
- **Automatic gain**: after a single chunk of digital silence (VBAN gap, muted microphone) it raised the room noise by +20 dB (false wake-ups). The background is now a percentile of the last 3 s, near-silent chunks ignored.
- **Noise suppression**: "strong" in a quiet room (about −63 dB) tripped the "clean-up outputs silence" safety net, which turned suppression off. The safety net now only reacts to an output really at zero while the microphone clearly picks up sound.
- **VBAN microphone**: 44.1 / 48 kHz streams were resampled packet by packet (a click every 5 ms, +0.8 % samples); the resampling is now continuous. 8-bit (unsigned) and float formats are converted correctly; stereo is averaged instead of summed (clipping); with more than 2 channels the first one is used. Each packet is converted once per satellite.
- **VBAN stream names** are made 16 ASCII characters (warning in the log): an accented output name stopped TTS without any log; a microphone name over 16 characters never matched. The VBAN send task logs its failures.
- **HA WebSocket**: an unexpected error (e.g. the Supervisor proxy closing at the wrong moment) could stop it for good; it now always reconnects. A subscription answered after its timeout is undone. Error messages are no longer empty for timeouts.
- **Configuration**: two satellites with the same name (case and spaces ignored) are refused; names, VBAN address, media player, HTTP host are trimmed. Changes are applied one at a time (a double click on Save, or a calibration Apply during a save, could mix them). An exception while applying now restores the previous configuration. Removing a satellite no longer restarts the next ones using the VBAN output. An invalid debug copy no longer counts as a change at every save.
- **Zeroconf**: a discovery failure no longer stops the whole app; the satellite works, added by hand.
- **Satellites**: an empty response no longer leaves the waiting announcements and the transcript pending (paired with the next response); only the run's response decides a follow-up and is published; question detection accepts `?»`, `?)` or an emoji after the `?`; a response replaced by another TTS stream hands its run end over; an unexpected error in the audio loop is logged and the pipeline restarted instead of leaving the satellite deaf; `played` is sent after VBAN playback too; a newer HA connection gets the audio alone (no double processing); the connection is closed on disconnect; the follow-up pre-roll no longer includes the beep; routine HA errors (no text recognized, duplicate wake-up) are logged at INFO; resume after a pause without Describe.
- **Phrases**: cache written atomically, cleaned when no phrase is left, unexpected TTS API answers retried.
- **VBAN receiver** retries after a network error; the socket is closed by its own task. **VBAN sender**: the last partial packet is sent (padded), no holes after a full buffer, an unknown sample rate is refused.
- **RTSP**: passwords containing `@` or `/` are masked in the logs.
- **Microphone clean-up**: DC removal near full scale shifts with saturation instead of skipping the chunk; changing the volume keeps the learnt noise profile and the settled gain.
- **Calibration**: the DC offset no longer counts as noise in the measures; a failed noise measure keeps the calibration; one measure at a time per satellite; expiry 15 min after the last action; leaving the tab during a measure no longer leaves the page inconsistent; the wake word model is checked before the countdown, which stops at once on an error; "applied ✓" follows the value applied; changing step A after applying B no longer fails; the live meter follows a restarted satellite and stops asking for a satellite that isn't running; API errors in French and in JSON; malformed requests give 400 instead of 500; wake test connections time out after 5 s.
- **Interface**: Apply in the calibration keeps the unsaved changes of the editor; numbers accept a decimal comma; leaving the Name field keeps the focus on the next one; debug copies follow a renamed satellite; export and import errors are shown.
- The CLI accepts `--tts-output none`. The app no longer maps Home Assistant's `/config` (unused).

## 3.8.0

- **New**: `microphone.follow_ha_entities` (off by default): the satellite's *Mic volume*, *Auto gain* and *Noise suppression level* entities in Home Assistant, which Home Assistant doesn't pass to Wyoming pipelines, are applied by the app to its own clean-up, live:
  - volume as is; noise `off`/`low`/`medium`/`high`/`max` → `off`/`light`/`medium`/`strong`/`strong`; auto gain `0` = off, `N` = aim at −N dBFS (bounded to −31…−3);
  - entities found through the device and entity registries (the Wyoming device named like the satellite, then the entities by `unique_id`): renaming them is fine; found again when the registries change and on each reconnection;
  - `unknown` / `unavailable` ignored; Home Assistant unreachable: the last values stay (or the app's settings), retried with a growing delay; the audio is never interrupted;
  - log `HA → microphone: volume …, auto gain … dBFS, noise suppression …` on each change; warning when the entities aren't found;
  - interface: the three settings read-only, "piloté par Home Assistant", with the values read; status tab: source of the microphone settings and time of the last synchronization.
- **New**: `microphone.auto_gain_target_db` (−31 to −3, default −18): the voice level aimed at by the automatic gain.
- **Calibration**, new steps after the volume, each with a proposal, listening and its own **Apply**:
  - **A. Noise suppression**: the sentence replayed offline through each level with the proposed volume; table of background left, voice and voice/noise ratio, listen per level; weakest level bringing the background under −55 dBFS or gaining 6 dB (off if none gains more than 3 dB); levels lowering the voice by more than 3 dB flagged and left out;
  - **B. Automatic gain**: near (≈ 50 cm) and far (≈ 3 m) takes of 6 s; gap over 10 dB → auto gain on, aiming at the near voice (bounded to −24…−14 dBFS), otherwise off; warning when the far voice stays under −45 dBFS even with +20 dB; listen before / after for both takes;
  - **C. Wake word test**: "Dis-moi Jarvis" 5 times in 20 s, processed with the current and the proposed settings, sent at the same time and in real time to openWakeWord (`core-openwakeword:10400`, model chosen from the ones it offers, `dis_moi_jarvis-6` by default) over its own Wyoming connection, never through the pipeline; detections side by side ("actuel 3/5, proposé 5/5"). openWakeWord's detection event has no confidence score: only the count is shown;
  - **Summary** card with **Apply all**;
  - **Apply** writes to the app's options, or to the satellite's Home Assistant entities when it follows them;
  - offline processing in a thread, fed in chunks like the live stream: the stream to Home Assistant isn't delayed; recordings in memory, a step's ones deleted when it is applied, all of them with **Apply all**, when leaving the tab, or after 15 minutes.
- Fix: the calibration's "after" preview processed the 8 s at once, which made the automatic gain see a single level.

## 3.7.0

- **New**: **Calibrage** tab in the interface, to set each microphone's level:
  - live vu-meter (about 10/s, Server-Sent Events with a polling fallback) before and after the app's processing: RMS and peak in dBFS, clipped samples;
  - assistant: 5 s of background noise, then 8 s of a read sentence; signal-to-noise ratio and a proposed microphone volume (voice around −20 dBFS RMS, peaks under −3 dBFS);
  - plain-language advice on the device gain (ESPHome `mic_gain` / `vban_sender` gain): raise it for a voice under −40 dBFS or a volume over the maximum, lower it when the audio clips on arrival; warnings for a low signal-to-noise ratio or a background raised enough to cause false wake-ups;
  - listen to the voice before and after the proposed volume; **Apply** writes `microphone.volume` (saved and applied without restart).
  - The audio sent to Home Assistant is never interrupted; recordings stay in memory and are deleted when applied, when leaving the tab, or after 15 minutes.
- **New**: `microphone.volume` (0.1–10, default 1.0), a fixed gain applied before noise suppression and automatic gain. Home Assistant's *Mic volume* / *Auto gain* / *Noise suppression level* entities for Wyoming satellites have no effect on the audio (not passed to the pipeline), hence this option in the app.
- Fix: the interface marked the configuration as modified right after loading when the debug copies list or a satellite block was missing.

## 3.6.2

- **Fix**: automatic gain made the satellite deaf — the cleaned audio was silence (-90 dB) as soon as `auto_gain` was on. pyspeex-noise 2.0.1's automatic gain outputs silence (it hands Speex its target level with the wrong type); it is no longer used. The app has its own automatic gain: speech brought to about -18 dB (gain -12 to +20 dB), adjusted only while speaking (background noise isn't boosted), rising slowly and dropping fast. Speex is still used for noise suppression.
- Safety net: if the clean-up outputs silence for 3 s while the microphone receives sound, it is bypassed (raw audio sent to Home Assistant), with an error in the log and a warning in the status tab.

## 3.6.1

- **Status panel**: when microphone clean-up is on, a second level meter (green, "Nettoyé") shows the cleaned audio right under the raw one ("Brut"), each with its value; the cleaned background noise is shown next to the raw one in the 10 s statistics.

## 3.6.0

- **New**: microphone clean-up, per satellite (`microphone` block), applied before the audio reaches Home Assistant and the wake word:
  - `noise_suppression`: `off` (default), `light`, `medium`, `strong` (Speex: learns the steady background noise — computer fan, fridge — during quiet moments and attenuates it by up to 6, 15 or 25 dB, voice kept);
  - `auto_gain`: brings the voice to a steady level (default off);
  - the DC offset (constant shift of ESP32 microphones) is now always removed.
- Applied live from the configuration interface (no restart); the status tab shows the background noise after clean-up next to the raw one; `mic_sent` debug copies carry the cleaned audio.
- New dependency: pyspeex-noise (compiled at build time on Alpine; the compiler is removed afterwards).
- Fix: running a single satellite from the command line failed since 3.5.0 (missing `--mic-source`, `--rtsp-url`, `--rtsp-transport` arguments).

## 3.5.4

- **Status panel — reliable microphone levels**: the level no longer includes the DC offset (a constant shift, common on ESP32 microphones, that isn't heard and inflated the reading). Each satellite now also shows, over the last 10 s, its **background noise** (quietest moments), its **peak** (e.g. a voice) and the **gap** between them, plus the DC offset when it is significant. The gap between voice and background is what matters for the wake word and transcription.

## 3.5.3

- **Configuration interface — clearer layout**:
  - Three visual levels: each satellite section is a card (Général, Micro, Sortie des réponses, Relance), each part of a section a sub-card (each announcement, the VBAN fallback speaker), and field labels are smaller and discreet, so it's clear which settings go together.
  - Announcements: one sub-card per signal (end of listening, thinking, internet search, camera look, phrase voice) with its title and Test button on one line, its mode, then its own fields; shown full width under the other sections, side by side as the width allows.
  - Mode buttons fit on one line (shorter "Son + phrase"), down to phone width; thinner margins on phones.

## 3.5.2

- **Fix**: after an update, the configuration interface could still show the previous version (page cached by the browser). The page is now sent with `Cache-Control: no-cache, no-store`, so a new version shows at once.
- The interface shows the app version next to its title.

## 3.5.1

- **Configuration interface redesigned**:
  - Tabs: *État* (status), *Satellites*, *Général*; save/import/export only on the configuration tabs; unsaved changes marked on the tabs.
  - Full width, sections flowing in columns; responsive down to phone width.
  - Satellites collapsed by default, with a summary (port, microphone, output); an added satellite opens.
  - Response output chosen with grouped buttons (VBAN / Media player / None), showing only the settings of the chosen output; the VBAN fallback of a media player is a switch.
  - Same for the microphone source (VBAN / camera) and each announcement mode; switches instead of checkboxes; settings hidden while their feature is off (follow-up, announcements without output…).
- Fix: phrase list shown as "[object HTMLDivElement]" in the status panel.

## 3.5.0

- **New**: microphone from a camera stream — `microphone.source: rtsp` with `rtsp_url` (e.g. a go2rtc stream) and `rtsp_transport` (`tcp` default, or `udp`). The audio is read by ffmpeg (video ignored, 16 kHz mono) and goes through the same chain as a VBAN microphone (mic mute, levels, debug copies, status).
- Reconnection: when ffmpeg stops or sends no audio for 5 s, it is restarted after 1, 2, 5, 10 then 30 s (back to 1 s once audio returns). Logs: `First RTSP audio from …`, `No RTSP audio received for N seconds`, `RTSP audio resumed`. Credentials in the URL are hidden in all logs. ffmpeg is terminated and reaped when the satellite or the app stops.
- A camera satellite with an empty `rtsp_url` doesn't start (clear error in the log); the other satellites do. `microphone.vban_port` is now optional (ignored with source `rtsp`).
- **New**: `output: none` — responses are not played anywhere (log: `Response not played (output: none)`); the run ends normally and the satellite goes back to wake word listening. Useful to test a microphone before choosing a speaker.
- Configuration interface: microphone source (VBAN / camera) and output "none".
- The image now includes ffmpeg.

## 3.4.0

- **Configuration interface — fine-grained apply**: saving applies only what changed. Most settings (announcements, follow-up beep/timeout, VBAN output address/port/stream/volume, media player entity, mic mute tail, debug copies, exchange publishing, TTS engine/voice, debug logging) apply while the satellite keeps running; settings tied to sockets or servers restart that satellite only; an added/removed satellite starts/stops alone; the Home Assistant connection is started, stopped or re-subscribed as needed. The message after saving lists what was done. A failure still restores the previous configuration.
- **Status panel**: per satellite, Home Assistant connection, microphone level and last audio received; Home Assistant connection; spoken phrases (ready or being synthesized); last exchanges (also when not published as HA events). Refreshed every 2 s.
- **Test buttons**: play each announcement (end of listening, thinking, internet search, camera) or the follow-up beep on the satellite's output.
- **Export / Import** the configuration as YAML (import is checked, loaded in the editor, then saved as usual). New dependency: PyYAML.

## 3.3.0

- **New**: configuration interface, opened from Home Assistant (Ingress: *Open web UI*, or the sidebar). Each satellite shows only the settings that apply to it (media player settings only for a media player output, VBAN output settings only when enabled, phrase/sound fields depending on each announcement mode…), with add/remove satellite and debug copies.
- **Save and apply without restarting the app**: options are saved through the Supervisor API (same options as the Configuration tab, validated by Home Assistant) and applied by restarting the satellites inside the app. If the new configuration fails to start (e.g. port in use), the previous one is restored, in the app and in the saved options, and the error is shown.
- Pre-3.2 announcement switches (`thinking`, `search`, `enabled`) are converted to explicit modes when saving from the interface.
- The interface only answers requests from Home Assistant's Ingress proxy (the app uses the host network, so its port is also reachable from the LAN).
- Internals: the satellites, receivers, HA WebSocket and announcements are now run by a reloadable runtime (`runtime.py`).

## 3.2.0

- **New**: configurable announcements while the response is prepared, per satellite. Each signal has a mode: `none`, `sound`, `phrase` (spoken text) or `sound_then_phrase`; every text and sound can be changed.
  - End of listening (`waiting_sounds.listen_end_mode`: `none` by default, or `sound`: new built-in blip, 1200 Hz, 60 ms)
  - Thinking (`waiting_sounds.thinking_mode`, default phrase "Je réfléchis." after `thinking_delay_ms`)
  - Internet search, `jarvis_recherche` (`waiting_sounds.search_mode`, default sound then "Je cherche sur internet.")
  - Camera look, `jarvis_vision` (`camera_phrase.mode`, default phrase, now "Attends, j'observe.")
- Priorities: a vision/search announcement replaces the thinking one if it hasn't started (otherwise it finishes first); at most one of each per request; a response arriving during an announcement lets it finish and drops the pending ones.
- Phrases synthesized by HA's TTS at startup, cached in `/data/annonces`; app options `announcements_tts_engine` (default `tts.piper`) and `announcements_tts_voice` (default `fr_FR-siwis-medium`). A failed synthesis logs a clear error and plays the signal's built-in sound instead; retries slow down to every 10 min.
- No configuration rewrite needed: the new fields are optional and live in the existing `waiting_sounds` and `camera_phrase` blocks (a new required block would have made the update fail, as in 3.0). The 3.1 switches `waiting_sounds.thinking` / `search` and `camera_phrase.enabled` are still read when the matching mode isn't set (on = sound, as in 3.1), with a warning: remove them to get the new spoken defaults.
- README updated to the 3.x configuration format.

## 3.1.0

- **New**: debug audio copies — the `debug_mirror` app option sends a copy of a satellite's audio as a VBAN stream to any address/port, e.g. to listen with VBAN Receptor or VoiceMeeter: `mic_received` (microphone as received), `mic_sent` (audio sent to HA, with mutes and guards), `tts` (responses, paced in real time). The satellite keeps working normally.
- Default VBAN stream names `<last word of the satellite name>-rx / -tx / -tts` (e.g. `Bureau-tx`), overridable. Invalid entries are ignored with a warning.
- App-level option with an empty default: existing 3.0 configurations stay valid.

## 3.0.0

- **Breaking — configuration format**: satellite options are grouped in blocks, shown in the configuration form as lines that open on click: `microphone`, `vban_output`, `media_player`, `followup`, `waiting_sounds`, `camera_phrase`. `tts_output` becomes `output`.
- **After updating, rewrite each satellite in the new format** (configuration tab → ⋮ → Edit in YAML); a 2.x configuration is rejected until then (Home Assistant's Supervisor drops the old option names and requires every block). See DOCS.md, "Upgrading from 2.x", for the name mapping and a full example. Every block must be present but can be empty (`{}` = defaults, `vban_output: {}` = no VBAN output).
- Home Assistant's form can't hide a field depending on another option; blocks are the closest: each feature's settings stay folded in its own line.
- Behavior unchanged. Error messages use the new option names (e.g. `vban_output.address`).

## 2.15.1

- **Config form**: satellite fields reordered by group (microphone, output choice, VBAN output, media player, follow-up, waiting sounds, camera phrase) and labelled with their group, e.g. "[Media player] Entity"; descriptions say which option a field depends on ("Only if output = media_player"…). All fields are now translated (English, French).
- No config change: same option names and types (Home Assistant's form can't hide fields depending on other options).

## 2.15.0

- **New**: "Attends, je regarde." — on the `jarvis_vision` HA event (fired before a camera snapshot), the satellite(s) waiting for a response say a phrase instead of the waiting sounds (no thinking/search sound after it in that run)
- The phrase is synthesized once by HA (`POST /api/tts_get_url` via the Supervisor, 16-bit mono WAV) and cached in `/data`, so it plays immediately; a changed text, engine or voice gives a new cache file (old ones are removed at startup). Synthesis is retried every 30 s while HA is not ready.
- A response arriving during the phrase waits for it to finish; the mic is silenced while it plays.
- New per-satellite options `vision_phrase_enabled` (default on), `vision_phrase` (default "Attends, je regarde."), `vision_tts_engine` (default `tts.piper`), `vision_tts_voice` (default `fr_FR-siwis-medium`).

## 2.14.0

- **New**: waiting sounds while the response is being prepared, on the satellite's output (media_player or VBAN)
  - **thinking** (660 → 520 Hz, 2 × 120 ms, soft) when no response `thinking_delay_ms` after the transcript (default 1 s): fast commands play nothing
  - **internet search** (520 → 660 → 880 Hz, 3 × 80 ms) on the `jarvis_recherche` HA event (subscribed through the shared HA WebSocket), on the satellite(s) between transcript and response; at most once per run, right after the thinking sound if it is playing
- The response never cuts a playing sound (it follows it, ≤ 0.4 s), and no further sound is queued once the response arrives. The mic is replaced by silence while a sound plays.
- New per-satellite options `thinking_sound_enabled`, `search_sound_enabled` (default on), `thinking_delay_ms` (300–5000, default 1000), `thinking_sound` / `search_sound` (optional WAV files replacing the built-in sounds; the app now has read access to `/share`).

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
