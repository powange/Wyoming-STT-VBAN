# Wyoming VBAN Satellite

Home Assistant addon that bridges VBAN audio sources to the voice assistant pipeline via the Wyoming protocol.

Use any VBAN-capable microphone on your network (VoiceMeeter, ESP32, VBAN Receptor, etc.) as a voice satellite for Home Assistant.

## Features

- **VBAN audio reception** — unicast and multicast (IGMP)
- **Automatic resampling** — any VBAN source format is converted to 16kHz 16-bit mono for the voice pipeline
- **Optional TTS output** — send voice assistant responses back via VBAN to a remote speaker
- **Stream name filtering** — target a specific VBAN stream on a shared port
- **Host networking** — full UDP multicast support in Docker

## Architecture

```
[VBAN Mic] ──UDP──▶ [Addon] ──Wyoming──▶ [HA Voice Pipeline]
                                               │
[VBAN Speaker] ◀──UDP── [Addon] ◀──Wyoming── [TTS]
                         (optional)
```

## Installation

1. Add this repository to your Home Assistant addon store
2. Install **Wyoming VBAN Satellite**
3. Configure the VBAN source settings
4. Start the addon
5. Add the satellite in **Settings > Voice Assistants**

## Configuration

### VBAN Receive (microphone)

| Option | Default | Description |
|--------|---------|-------------|
| `vban_receive_port` | `6980` | UDP port to listen on |
| `vban_receive_mode` | `unicast` | `unicast` or `multicast` |
| `vban_receive_multicast_group` | | Multicast group (e.g. `239.0.0.1`). Required for multicast mode. |
| `vban_receive_stream_name` | | Filter by stream name. Empty = accept all. |

### TTS VBAN Output (optional)

| Option | Default | Description |
|--------|---------|-------------|
| `tts_vban_enabled` | `false` | Enable TTS audio output via VBAN |
| `tts_vban_mode` | `unicast` | `unicast` or `multicast` |
| `tts_vban_address` | | Target IP for TTS output |
| `tts_vban_port` | `6980` | Target UDP port |
| `tts_vban_stream_name` | `TTS1` | Outgoing VBAN stream name |

### General

| Option | Default | Description |
|--------|---------|-------------|
| `satellite_name` | `VBAN Satellite` | Name shown in Home Assistant |
| `wyoming_port` | `10700` | Wyoming protocol TCP port |
| `debug_logging` | `false` | Enable verbose logging |

## Examples

### Simple unicast setup

A PC running VoiceMeeter sends a VBAN stream to the HA server IP on port 6980:

```yaml
vban_receive_port: 6980
vban_receive_mode: unicast
vban_receive_stream_name: "Stream1"
tts_vban_enabled: false
```

### Multicast with TTS return

An ESP32 mic broadcasts on a multicast group. TTS responses are sent back to a VBAN speaker:

```yaml
vban_receive_port: 6980
vban_receive_mode: multicast
vban_receive_multicast_group: "239.0.0.1"
vban_receive_stream_name: "Mic1"
tts_vban_enabled: true
tts_vban_mode: unicast
tts_vban_address: "192.168.1.50"
tts_vban_stream_name: "TTS1"
```

### Multicast mic, no speaker

A VBAN source in multicast, TTS handled by a media_player in HA:

```yaml
vban_receive_port: 6980
vban_receive_mode: multicast
vban_receive_multicast_group: "239.0.0.1"
tts_vban_enabled: false
```

## Network requirements

- The addon runs with **host networking** to receive UDP multicast/unicast packets
- For multicast, your network switches must support IGMP snooping or have multicast flooding enabled

## Supported VBAN formats

The addon accepts any VBAN PCM audio:
- Sample rates: 6kHz to 705.6kHz
- Channels: mono or stereo (converted to mono)
- Bit depth: 8, 16, 24, 32-bit int or 32/64-bit float (converted to 16-bit)

## License

MIT
