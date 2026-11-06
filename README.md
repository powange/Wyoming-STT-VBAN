# Wyoming VBAN Satellite

Home Assistant addon that bridges VBAN audio sources to the voice assistant pipeline via the Wyoming protocol.

Use any VBAN-capable microphone on your network (VoiceMeeter, ESP32, VBAN Receptor, etc.) as a voice satellite for Home Assistant. Supports **multiple satellites** from a single addon.

## Features

- **Multiple satellites** — as many VBAN microphones as you need, each with its own Wyoming port; several can share one VBAN port (routed by stream name)
- **VBAN audio reception** — unicast and multicast (IGMP), any format resampled to 16 kHz 16-bit mono
- **Camera microphone** — the audio of an RTSP stream (e.g. go2rtc) as a satellite's microphone
- **Responses** on a VBAN speaker (e.g. an ESP32 Atom) or on any Home Assistant media player (e.g. a Google Nest)
- **Follow-up without wake word** when Jarvis asks a question
- **Announcements while waiting** — sounds or spoken phrases while the response is prepared (see below)
- **Exchanges published** as Home Assistant events (`jarvis_echange`), **debug audio copies** as VBAN streams
- **Wake word clips**: the audio of each detection kept and sorted in the interface (good / false trigger), sorted clips dropped in `/share/wakeforge/inbox` for retraining the model
- **Host networking** — full UDP multicast support in Docker

## Microphone calibration

The **Calibrage** tab of the app's interface sets each microphone's level so
the wake word and the transcription get a good signal: loud enough from a
distance, without clipping, without raising the background noise too much
(false wake-ups).

- **Live vu-meter** (about 10 times a second) before and after the app's processing: RMS and peak in dBFS, clipped samples (%).
- **Assistant**: 5 s of silence (background noise), then a sentence read for 8 s at the usual distance (voice level, peak). Result: signal-to-noise ratio and a proposed **microphone volume** for a voice around −20 dBFS RMS with peaks under −3 dBFS.
- **Device advice** in plain words: raise the ESPHome gain (`mic_gain` of the codec, gain of the `vban_sender`) when the voice arrives under −40 dBFS or the volume would exceed the maximum; lower it when the audio already clips on arrival. The tool doesn't change the device: that gain also affects other receivers of the raw stream.
- **Listen** to the 8 s voice before and after the proposed volume, then **Apply**.
- **A. Noise suppression**: the sentence is replayed offline through each level (off, light, medium, strong) with the proposed volume. A table shows the background left, the voice level and the voice/noise ratio for each, with a listen button per level. Proposed: the weakest level bringing the background under −55 dBFS or gaining 6 dB on that ratio (off if none gains more than 3 dB). A level lowering the voice by more than 3 dB is flagged and left out.
- **B. Automatic gain**: two 6 s takes of the sentence, near the microphone (≈ 50 cm) then far (≈ 3 m). A gap over 10 dB proposes the auto gain, aiming at the near voice (bounded to −24…−14 dBFS); otherwise the fixed volume is enough. Warning when the far voice would stay under −45 dBFS even with the +20 dB of the auto gain. Listen before / after for both takes.
- **C. Wake word test**: say "Dis-moi Jarvis" 5 times in 20 s. The audio, processed with the current then with the proposed settings, is sent to the openWakeWord app (`core-openwakeword:10400`, model `dis_moi_jarvis-6` by default, chosen from the models it offers) over its own Wyoming connection, never through Home Assistant's pipeline: the assistant doesn't wake up. Result side by side, e.g. "current 3/5, proposed 5/5". openWakeWord sends no confidence score, only detections.
- **Summary** of the whole proposal with an **Apply all** button. Each step also has its own **Apply**.

**Apply** writes to the app's configuration (`microphone.volume`,
`noise_suppression`, `auto_gain`, `auto_gain_target_db`, applied without
restart), or to the satellite's Home Assistant entities when it follows them
(see below).

The calibration never interrupts the audio sent to Home Assistant; the
offline processing runs apart from the live stream. Recordings stay in
memory (never on disk): a step's recordings are deleted when it is applied,
all of them with **Apply all**, when leaving the tab, or after 15 minutes.
The wake word test take isn't kept.

## Home Assistant microphone entities

Home Assistant creates *Mic volume*, *Auto gain* and *Noise suppression
level* entities for each Wyoming satellite, but doesn't pass them to the
pipeline (only ESPHome satellites get them): on their own, they have no
effect on the audio. With **`microphone.follow_ha_entities: true`** (off by
default), the app applies them itself, live, to its own clean-up:

| HA entity | App setting |
|---|---|
| Mic volume (0.1–10) | `volume`, same value |
| Noise suppression `off` / `low` / `medium` / `high` / `max` | `off` / `light` / `medium` / `strong` / `strong` |
| Auto gain `0` | auto gain off |
| Auto gain `1`–`31` | auto gain on, aiming at −N dBFS (bounded to −31…−3) |

The entities are found through Home Assistant's registries (the Wyoming
device named like the satellite, then its entities by `unique_id`), so
renaming them doesn't matter. Unknown / unavailable states are ignored; if
Home Assistant can't be reached, the last values stay (or the app's settings
if none were ever read) and the app retries. The interface then shows the
three settings read-only ("piloté par Home Assistant"), and the status tab
shows where the settings come from and when they were last synchronized.

If Home Assistant one day passes these settings to the pipeline of Wyoming
satellites, the processing would be applied twice (by the app, then by Home
Assistant): turn the option off then.

## Microphone from a camera (RTSP)

A satellite's microphone can be the audio of a camera stream instead of a
VBAN stream, read with ffmpeg (video ignored). Prefer a go2rtc stream to the
camera itself (cameras accept few connections, and Frigate may already read
it through go2rtc):

```yaml
  - name: "VBAN Satellite Chambre"
    wyoming_port: 10702
    output: none
    microphone:
      source: rtsp
      rtsp_url: "rtsp://192.168.0.201:8554/chambre"
    vban_output: {}
    media_player: {}
    followup:
      enabled: false
    waiting_sounds: {}
    camera_phrase: {}
```

The stream is reconnected automatically (after 1, 2, 5, 10 then 30 s).
`output: none` lets you test the microphone, wake word and transcription
before choosing a speaker: **a satellite without output doesn't speak**
(responses aren't played; follow-ups are pointless, hence `enabled: false`).

## Announcements

Between the end of a request and the response, the satellite tells you what
is happening, without disturbing fast commands. Each signal is set per
satellite: off, sound, phrase, or sound then phrase; texts and sounds can be
changed.

| Situation | Default signal |
|---|---|
| End of listening (request recorded) | nothing (option: blip) |
| Home Assistant command (response in < 1 s) | nothing |
| Local AI thinking (no response after the delay) | phrase "Je réfléchis." |
| Internet search (`jarvis_recherche` HA event) | sound, then "Je cherche sur internet." |
| Camera look (`jarvis_vision` HA event) | phrase "Attends, j'observe." |

## Architecture

```
[VBAN Mic 1] ──UDP──▶ [Satellite 1] ──Wyoming :10700──▶ [HA Voice Pipeline]
[VBAN Mic 2] ──UDP──▶ [Satellite 2] ──Wyoming :10701──▶ [HA Voice Pipeline]
                                                              │
[VBAN Speaker] ◀──UDP── [Satellite 1] ◀──────Wyoming────── [TTS]
                          (optional)
```

## Installation

1. Add this repository to your Home Assistant addon store
2. Install **Wyoming VBAN Satellite**
3. Configure one or more VBAN satellites in the addon settings
4. Start the addon
5. Add each satellite in **Settings > Voice Assistants** (one per Wyoming port)

## Configuration

The app uses a `satellites` list; each entry creates a separate Wyoming
satellite, with a few fields and one block per feature (`microphone`,
`vban_output`, `media_player`, `followup`, `waiting_sounds`,
`camera_phrase`). Every block must be present but can be empty (`{}` =
defaults). See [DOCS.md](wyoming-vban-satellite/DOCS.md) for all options and
a full example.

### Global options

| Option | Default | Description |
|--------|---------|-------------|
| `publish_exchanges` | `true` | Fire a `jarvis_echange` HA event for each request and response |
| `announcements_tts_engine` | `tts.piper` | TTS entity synthesizing the spoken announcements |
| `announcements_tts_voice` | `fr_FR-siwis-medium` | Voice of the spoken announcements |
| `debug_mirror` | `[]` | Debug copies of audio streams as VBAN |
| `debug_logging` | `false` | Enable verbose logging |

## Examples

### Single satellite — VBAN microphone and speaker

```yaml
satellites:
  - name: "Living Room"
    wyoming_port: 10700
    microphone:
      vban_port: 6980
      stream_name: "Stream1"
    vban_output:
      enabled: true
      address: "192.168.1.50"
    media_player: {}
    followup: {}
    waiting_sounds: {}
    camera_phrase: {}
```

### Two satellites — one answering on a Google Nest, custom announcements

```yaml
satellites:
  - name: "Office"
    wyoming_port: 10700
    output: media_player
    microphone:
      vban_port: 6980
      stream_name: "Office"
    vban_output: {}
    media_player:
      entity: media_player.office_nest
    followup: {}
    waiting_sounds:
      listen_end_mode: sound
      thinking_text: "Un instant."
    camera_phrase:
      mode: none
  - name: "Kitchen"
    wyoming_port: 10701
    microphone:
      vban_port: 6980
      vban_mode: multicast
      multicast_group: "239.0.0.1"
      stream_name: "Kitchen"
    vban_output:
      enabled: true
      address: "192.168.1.51"
      stream_name: "KitchenTTS"
    media_player: {}
    followup: {}
    waiting_sounds:
      thinking_mode: sound
      search_mode: sound
    camera_phrase: {}
```

## Network requirements

- The addon runs with **host networking** to receive UDP multicast/unicast packets
- For multicast, your network switches must support IGMP snooping or have multicast flooding enabled
- Each satellite needs a **unique Wyoming port** (10700, 10701, etc.)

## Supported VBAN formats

The addon accepts any VBAN PCM audio:
- Sample rates: 6kHz to 705.6kHz
- Channels: mono or stereo (converted to mono)
- Bit depth: 8, 16, 24, 32-bit int or 32/64-bit float (converted to 16-bit)

## License

MIT
