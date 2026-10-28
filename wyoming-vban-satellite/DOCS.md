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
- **vban_receive_multicast_group**: Multicast group address (e.g. `239.0.0.1`), required when mode is `multicast`
- **vban_receive_stream_name**: Filter by VBAN stream name. Leave empty to accept any stream.

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

How it works: the full response is buffered, turned into a WAV file served at
`http://<host>:<port>/tts/<id>.wav`, and played with the
`media_player.play_media` service. Once it has played, the satellite reports
it to Home Assistant. If `play_media` fails (error or 5 s timeout) and VBAN
output is configured (`tts_vban_enabled`), the response is sent to VBAN instead.

The speaker must be able to reach the HTTP port on the Home Assistant host.
Responses start playing only once fully generated, plus about a second for
Cast to start.

Example — the "Bureau" satellite keeps its VBAN microphone but answers on a
Nest Mini:

```yaml
- name: Bureau
  wyoming_port: 10700
  vban_receive_port: 6980
  vban_receive_mode: unicast
  vban_receive_multicast_group: ""
  vban_receive_stream_name: ""
  tts_vban_enabled: false
  tts_vban_mode: unicast
  tts_vban_address: ""
  tts_vban_port: 6980
  tts_vban_stream_name: TTS1
  tts_output: media_player
  tts_media_player: media_player.nestmini7039
```

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
