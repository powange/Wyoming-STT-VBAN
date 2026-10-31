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

### VBAN Receive (microphone input)

- **vban_receive_port**: UDP port to listen on (default: 6980)
- **vban_receive_mode**: `unicast` or `multicast`
- **vban_receive_multicast_group**: Multicast group address (e.g. `239.0.0.1`), required when mode is `multicast` (optional, ignored in `unicast` mode)
- **vban_receive_stream_name**: VBAN stream name routed to this satellite. Leave empty to accept any stream not claimed by another satellite on the same port.

#### Several satellites on the same VBAN port

Several satellites can share one `vban_receive_port` (e.g. two ESP32
microphones both sending to port 6980). The app opens a single UDP socket
per port and routes each packet by its stream name:

- a packet goes to the satellite whose `vban_receive_stream_name` matches it exactly;
- if no satellite claims that stream name, it goes to the satellite(s) on that port with an empty `vban_receive_stream_name`;
- otherwise it is ignored (logged once per stream).

Give each microphone a distinct VBAN stream name and set it on its
satellite. Satellites sharing a port must use the same
`vban_receive_mode` (and multicast group). If several satellites on a
shared port have no stream name, each of them receives every unclaimed
stream (a warning is logged at startup).

Example: `Bureau` with `vban_receive_stream_name: AtomBureau` and
`Salon` with `vban_receive_stream_name: AtomSalon`, both on port 6980.

### TTS VBAN Output (optional)

- **tts_vban_enabled**: Enable sending TTS audio back via VBAN
- **tts_vban_mode**: `unicast` or `multicast`
- **tts_vban_address**: Target IP address for TTS output
- **tts_vban_port**: Target UDP port (default: 6980)
- **tts_vban_stream_name**: Name of the outgoing VBAN stream
- **tts_vban_volume**: Volume multiplier for TTS audio, 0.0–2.0 (default: 1.0). Values above 1.0 clip loud samples.
- **tts_vban_batch_packets**: Number of VBAN packets (256 samples, 16 ms at 16 kHz) sent per wake-up, 1–8 (default: 1). 1 sends one packet every 16 ms, the smoothest stream for receivers with a small UDP queue (e.g. ESP32). Higher values send bursts, which are less sensitive to timer jitter on a busy host but may overflow small receivers.

### TTS output to a Home Assistant media player (optional)

Instead of VBAN, a satellite can play its responses on any Home Assistant
`media_player` (e.g. a Google Nest speaker via Cast). The microphone stays
the VBAN source.

- **tts_output**: `vban` (default) or `media_player`
- **tts_media_player**: Target entity, e.g. `media_player.nestmini7039` (required when `tts_output` is `media_player`)
- **tts_http_port**: Port of the small HTTP server that serves the response to the speaker (default: 10800 + satellite position in the list, i.e. 10800 for the first satellite, 10801 for the second…)
- **tts_http_host**: LAN IP of the Home Assistant host, used in the URL given to the speaker. Leave empty to auto-detect.
- **mic_mute_tail_ms**: How long the microphone stays muted after the response has played, 0–5000 ms (default: 250). See below.

How it works: the full response is buffered, turned into a WAV file served at
`http://<host>:<port>/tts/<id>.wav`, and played with the
`media_player.play_media` service. Once it has played, the satellite reports
it to Home Assistant. If `play_media` fails (error or 5 s timeout) and VBAN
output is configured (`tts_vban_enabled`), the response is sent to VBAN instead.

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

Example — the "Bureau" satellite keeps its VBAN microphone but answers on a
Nest Mini:

```yaml
- name: Bureau
  wyoming_port: 10700
  vban_receive_port: 6980
  vban_receive_mode: unicast
  vban_receive_stream_name: ""
  tts_vban_enabled: false
  tts_vban_mode: unicast
  tts_vban_address: ""
  tts_vban_port: 6980
  tts_vban_stream_name: TTS1
  tts_output: media_player
  tts_media_player: media_player.nestmini7039
```

### Follow-up without wake word

When a response is a question (its text ends with `?`, e.g. "Which light
do you want to turn off?"), the satellite listens for the answer right
away, without the wake word. After any other response, the wake word is
needed as usual.

- **followup_enabled**: Enable follow-ups (default: `true`)
- **followup_beep**: Play a short beep (150 ms, 880 Hz) on the satellite's output (VBAN or media player) when it starts listening for the answer (default: `true`)
- **followup_timeout_s**: If no speech is detected within this time, go back to wake word listening, 3–30 s (default: 8)

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

### Waiting sounds

While the response is being prepared (after your request is understood),
the satellite can play short, soft sounds on its output (VBAN or media
player), so you know the request was heard:

- a **thinking** sound (two falling notes) if the response takes longer than `thinking_delay_ms`;
- an **internet** sound (three rising notes) when a `jarvis_recherche` Home Assistant event is fired (e.g. by a script just before calling an internet search). The event doesn't say which satellite asked: the sound plays on the satellite(s) waiting for a response. At most once per request.

Fast commands (e.g. lights) play nothing. A sound that is playing when the
response arrives is not cut: the response follows it (sounds last
≤ 0.4 s), and no further sound is played. The microphone is replaced by
silence while a sound plays.

Options (per satellite):

- **thinking_sound_enabled**: default `true`
- **search_sound_enabled**: default `true`
- **thinking_delay_ms**: 300–5000 (default: 1000)
- **thinking_sound** / **search_sound**: WAV file replacing the built-in sound, e.g. `/share/jarvis/thinking.wav` (the app has read access to `/share`). Keep it short: the response waits for it to finish.

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

### Other

- **satellite_name**: Name shown in Home Assistant
- **wyoming_port**: TCP port for Wyoming protocol (default: 10700)
- **debug_logging**: Enable verbose logging

## Network requirements

- This addon uses **host networking** to properly receive UDP packets (unicast and multicast).
- For multicast, your network switches must support IGMP snooping or have multicast flooding enabled.

## Supported audio formats

The addon accepts VBAN PCM audio at any sample rate and channel count.
It automatically resamples to 16kHz 16-bit mono as required by the
Wyoming voice pipeline.
