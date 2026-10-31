# Wyoming VBAN Satellite

Use VBAN audio sources as voice assistant satellites for Home Assistant.

## What is VBAN?

VBAN is a protocol for streaming audio over a local network via UDP.
It is used by software like VoiceMeeter, VBAN Receptor, and various
embedded devices (ESP32, etc.).

## How it works

This addon receives audio from a VBAN stream and bridges it to the
Home Assistant voice pipeline using the Wyoming protocol. This means
any VBAN-capable microphone on your network can act as a voice
satellite.

Optionally, TTS responses can be sent back to a VBAN output stream.

## Configuration

Each satellite has a few top-level fields and one block per feature. In the
configuration form, each block is a line that opens on click. Every block
must be present, but can be empty (`{}`): its fields then take their
defaults (`vban_output: {}` = no VBAN output).

```yaml
satellites:
  - name: Bureau
    wyoming_port: 10700
    output: media_player        # vban (default) or media_player
    microphone:
      vban_port: 6980
      stream_name: AtomBureau
    vban_output: {}             # no VBAN output
    media_player:
      entity: media_player.nestmini7039
    followup: {}                # defaults: on, beep, 8 s
    waiting_sounds: {}
    camera_phrase: {}
  - name: Salon
    wyoming_port: 10701
    microphone:
      vban_port: 6980
      stream_name: AtomSalon
    vban_output:
      enabled: true
      address: 192.168.1.51
    media_player: {}
    followup: {}
    waiting_sounds: {}
    camera_phrase: {}
publish_exchanges: true
debug_logging: false
```

- **name**: Name shown in Home Assistant
- **wyoming_port**: TCP port for the Wyoming protocol, unique per satellite (e.g. 10700, 10701…)
- **output**: where responses are played: `vban` (default, `vban_output` block) or `media_player` (`media_player` block, with the VBAN output as fallback if enabled)

### Microphone (`microphone` block)

- **vban_port**: UDP port to listen on (e.g. 6980)
- **vban_mode**: `unicast` (default) or `multicast`
- **multicast_group**: Multicast group address (e.g. `239.0.0.1`), required when mode is `multicast` (optional, ignored in `unicast` mode)
- **stream_name**: VBAN stream name routed to this satellite. Leave empty to accept any stream not claimed by another satellite on the same port.

#### Several satellites on the same VBAN port

Several satellites can share one `microphone.vban_port` (e.g. two ESP32
microphones both sending to port 6980). The app opens a single UDP socket
per port and routes each packet by its stream name:

- a packet goes to the satellite whose `stream_name` matches it exactly;
- if no satellite claims that stream name, it goes to the satellite(s) on that port with an empty `stream_name`;
- otherwise it is ignored (logged once per stream).

Give each microphone a distinct VBAN stream name and set it on its
satellite. Satellites sharing a port must use the same
`vban_mode` (and multicast group). If several satellites on a
shared port have no stream name, each of them receives every unclaimed
stream (a warning is logged at startup).

Example: `Bureau` with `stream_name: AtomBureau` and
`Salon` with `stream_name: AtomSalon`, both on port 6980.

### VBAN output (`vban_output` block)

- **enabled**: Send responses back via VBAN (default: `false`)
- **mode**: `unicast` (default) or `multicast`
- **address**: Target IP address (required when enabled)
- **port**: Target UDP port (default: 6980)
- **stream_name**: Name of the outgoing VBAN stream (default: `TTS1`)
- **volume**: Volume multiplier for TTS audio, 0.0–2.0 (default: 1.0). Values above 1.0 clip loud samples.
- **batch_packets**: Number of VBAN packets (256 samples, 16 ms at 16 kHz) sent per wake-up, 1–8 (default: 1). 1 sends one packet every 16 ms, the smoothest stream for receivers with a small UDP queue (e.g. ESP32). Higher values send bursts, which are less sensitive to timer jitter on a busy host but may overflow small receivers.

### Media player output (`media_player` block)

Instead of VBAN, a satellite can play its responses on any Home Assistant
`media_player` (e.g. a Google Nest speaker via Cast). The microphone stays
the VBAN source.

Set `output: media_player`, then in the block:

- **entity**: Target entity, e.g. `media_player.nestmini7039` (required when `output` is `media_player`)
- **http_port**: Port of the small HTTP server that serves the response to the speaker (default: 10800 + satellite position in the list, i.e. 10800 for the first satellite, 10801 for the second…)
- **http_host**: LAN IP of the Home Assistant host, used in the URL given to the speaker. Leave empty to auto-detect.
- **mic_mute_tail_ms**: How long the microphone stays muted after the response has played, 0–5000 ms (default: 250). See below.

How it works: the full response is buffered, turned into a WAV file served at
`http://<host>:<port>/tts/<id>.wav`, and played with the
`media_player.play_media` service. Once it has played, the satellite reports
it to Home Assistant. If `play_media` fails (error or 5 s timeout) and VBAN
output is configured (`vban_output` enabled), the response is sent to VBAN instead.

The speaker must be able to reach the HTTP port on the Home Assistant host.
Responses start playing only once fully generated, plus about a second for
Cast to start.

While a response plays on the media player, the satellite sends silence
instead of the microphone audio, so the speaker's voice cannot trigger the
wake word (Home Assistant starts listening again before the response has
played). The microphone is muted as soon as Home Assistant starts sending
the response, and unmuted `mic_mute_tail_ms` after the media player has
finished playing it. Increase the tail if the end of a response still
triggers false wake-ups; lower it if the satellite misses commands spoken
right after a response.

The end of playback is read from the media player state (`playing` →
`idle`…) through the Home Assistant WebSocket API (one connection for the
app, reconnecting automatically). If the WebSocket is not connected, or the
media player is not seen playing within 5 s, the end is estimated from the
response duration + 1.5 s for Cast to start, and a warning is logged (at
most once a minute).

Example: the "Bureau" satellite at the top of this section keeps its VBAN
microphone but answers on a Nest Mini.

### Follow-up without wake word (`followup` block)

When a response is a question (its text ends with `?`, e.g. "Which light
do you want to turn off?"), the satellite listens for the answer right
away, without the wake word. After any other response, the wake word is
needed as usual.

- **enabled**: Enable follow-ups (default: `true`)
- **beep**: Play a short beep (150 ms, 880 Hz) on the satellite's output (VBAN or media player) when it starts listening for the answer (default: `true`)
- **timeout_s**: If no speech is detected within this time, go back to wake word listening, 3–30 s (default: 8)

At most 3 follow-ups in a row; then the wake word is required again.

The follow-up run starts together with the beep. The microphone is replaced
by silence while the beep plays (0.45 s on a media player, 0.55 s on VBAN),
so the beep isn't captured; if you already started answering by then, the
audio recorded during that time is sent first. The no-speech timeout starts
once the microphone is open.

How it works: Home Assistant does not tell Wyoming satellites to continue
the conversation, and its automatic restart (`restart_on_end`) always
restarts at the wake word stage. With follow-ups enabled, the satellite
starts each pipeline run itself: once the response has played, it asks for
a run starting at the wake word stage, or at the speech-to-text stage after
a question (the conversation keeps its context, so the answer is
understood). In the Home Assistant debug view, a follow-up run starts at
`stt-start`, without `wake_word-start`. With follow-ups disabled, the
satellite behaves as before (Home Assistant restarts wake word detection
itself).

Wake word detection now resumes only once the response has played. If a
run ends without a spoken response nor an error (empty response), the
satellite goes back to wake word listening after 60 s.

### Waiting sounds (`waiting_sounds` block)

While the response is being prepared (after your request is understood),
the satellite can play short, soft sounds on its output (VBAN or media
player), so you know the request was heard:

- a **thinking** sound (two falling notes) if the response takes longer than `thinking_delay_ms`;
- an **internet** sound (three rising notes) when a `jarvis_recherche` Home Assistant event is fired (e.g. by a script just before calling an internet search). The event doesn't say which satellite asked: the sound plays on the satellite(s) waiting for a response. At most once per request.

Fast commands (e.g. lights) play nothing. A sound that is playing when the
response arrives is not cut: the response follows it (sounds last
≤ 0.4 s), and no further sound is played. The microphone is replaced by
silence while a sound plays.

Fields:

- **thinking**: thinking sound, default `true`
- **search**: internet search sound, default `true`
- **thinking_delay_ms**: 300–5000 (default: 1000)
- **thinking_file** / **search_file**: WAV file replacing the built-in sound, e.g. `/share/jarvis/thinking.wav` (the app has read access to `/share`). Keep it short: the response waits for it to finish.

### "Attends, je regarde." on a camera look (`camera_phrase` block)

When a `jarvis_vision` Home Assistant event is fired (e.g. by a script just
before taking a camera snapshot), the satellite(s) waiting for a response
**say** a short phrase instead of the waiting sounds. The phrase is
synthesized once by Home Assistant's TTS (at startup, and again when the
options change), then cached in `/data`, so it plays immediately. A response
arriving during the phrase waits for it to finish.

- **enabled**: default `true`
- **text**: default `Attends, je regarde.`
- **tts_engine**: TTS entity, default `tts.piper`
- **tts_voice**: voice, default `fr_FR-siwis-medium`

If Home Assistant is not ready at startup, synthesis is retried every 30 s;
until then, the event is ignored.

### Voice exchanges as Home Assistant events

- **publish_exchanges** (app option, default: `true`): after each voice request, fire a `jarvis_echange` event on the Home Assistant event bus, e.g. to show the last commands on a dashboard:

```json
{"satellite": "Bureau", "transcript": "Allume la lumière",
 "response": "Allumé", "followup": false}
```

`followup` is `true` for an answer given without the wake word after a
question. Announcements produce no event. Events are dropped (not queued)
while the Home Assistant WebSocket connection is down. Set to `false` to
publish nothing. To check: Developer tools → Events → listen to
`jarvis_echange`.

### Debug audio copies (`debug_mirror` app option)

To check what a satellite hears or says, send a copy of one of its audio
streams as VBAN to a computer, and listen with VBAN Receptor or VoiceMeeter.
These are copies: the satellite keeps working normally.

```yaml
debug_mirror:
  - satellite: VBAN Satellite Bureau
    stream: mic_sent
    address: 192.168.0.42
    port: 6990
```

- **satellite**: satellite name, exactly as configured
- **stream**:
  - `mic_received`: the microphone as received from the device (sent even when Home Assistant is not connected)
  - `mic_sent`: the audio actually sent to Home Assistant (16 kHz, with the silences of mutes, follow-up beep and waiting sounds) — the most useful to understand why something wasn't heard
  - `tts`: the responses sent by Home Assistant (22.05 kHz, sent in real time)
- **address**, **port** (default 6980): where to send the copy
- **stream_name**: VBAN stream name, default last word of the satellite name + `-rx`, `-tx` or `-tts` (e.g. `Bureau-tx`)

Several entries can target the same address and port: the stream names tell
them apart. A wrong entry (unknown satellite or stream) is ignored with a
warning. Leave the list empty (`debug_mirror: []`) when not debugging.

### Other

- **debug_logging** (app option): Enable verbose logging

### Upgrading from 2.x

Version 3.0 groups the satellite options in blocks. A 2.x configuration is
rejected by Home Assistant until it is rewritten in the new format.

The update itself is installed, but the app can't restart: Home Assistant
reports `Error updating Wyoming VBAN Satellite: App ... has invalid options:
Missing option 'media_player' in satellites`, and a second attempt says
`No update available`. To finish: Configuration tab → ⋮ → Edit in YAML,
replace the content with the new format (see the example above and the
table below), save, then start the app. Option names:

| 2.x | 3.0 |
|---|---|
| `vban_receive_port`, `vban_receive_mode`, `vban_receive_multicast_group`, `vban_receive_stream_name` | `microphone`: `vban_port`, `vban_mode`, `multicast_group`, `stream_name` |
| `tts_output` | `output` |
| `tts_vban_enabled`, `tts_vban_mode`, `tts_vban_address`, `tts_vban_port`, `tts_vban_stream_name`, `tts_vban_volume`, `tts_vban_batch_packets` | `vban_output`: `enabled`, `mode`, `address`, `port`, `stream_name`, `volume`, `batch_packets` |
| `tts_media_player`, `tts_http_port`, `tts_http_host`, `mic_mute_tail_ms` | `media_player`: `entity`, `http_port`, `http_host`, `mic_mute_tail_ms` |
| `followup_enabled`, `followup_beep`, `followup_timeout_s` | `followup`: `enabled`, `beep`, `timeout_s` |
| `thinking_sound_enabled`, `thinking_delay_ms`, `thinking_sound`, `search_sound_enabled`, `search_sound` | `waiting_sounds`: `thinking`, `thinking_delay_ms`, `thinking_file`, `search`, `search_file` |
| `vision_phrase_enabled`, `vision_phrase`, `vision_tts_engine`, `vision_tts_voice` | `camera_phrase`: `enabled`, `text`, `tts_engine`, `tts_voice` |

## Network requirements

- This addon uses **host networking** to properly receive UDP packets (unicast and multicast).
- For multicast, your network switches must support IGMP snooping or have multicast flooding enabled.

## Supported audio formats

The addon accepts VBAN PCM audio at any sample rate and channel count.
It automatically resamples to 16kHz 16-bit mono as required by the
Wyoming voice pipeline.
