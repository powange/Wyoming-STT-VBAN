# Wyoming VBAN Satellite

Home Assistant addon that bridges VBAN audio sources to the voice assistant pipeline via the Wyoming protocol.

Use any VBAN-capable microphone on your network (VoiceMeeter, ESP32, VBAN Receptor, etc.) as a voice satellite for Home Assistant. Supports **multiple satellites** from a single addon.

## Features

- **Multiple satellites** — configure as many VBAN sources as you need, each with its own Wyoming port
- **VBAN audio reception** — unicast and multicast (IGMP)
- **Automatic resampling** — any VBAN source format is converted to 16kHz 16-bit mono for the voice pipeline
- **Optional TTS output** — send voice assistant responses back via VBAN to a remote speaker
- **Stream name filtering** — target a specific VBAN stream on a shared port
- **Host networking** — full UDP multicast support in Docker

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

The addon uses a `satellites` list. Each entry creates a separate Wyoming satellite.

### Per-satellite options

| Option | Default | Description |
|--------|---------|-------------|
| `name` | `VBAN Satellite 1` | Name shown in Home Assistant |
| `wyoming_port` | `10700` | Wyoming protocol TCP port (must be unique per satellite) |
| `vban_receive_port` | `6980` | UDP port to listen on |
| `vban_receive_mode` | `unicast` | `unicast` or `multicast` |
| `vban_receive_multicast_group` | | Multicast group (e.g. `239.0.0.1`). Required for multicast mode. |
| `vban_receive_stream_name` | | Filter by stream name. Empty = accept all. |
| `tts_vban_enabled` | `false` | Enable TTS audio output via VBAN |
| `tts_vban_mode` | `unicast` | `unicast` or `multicast` |
| `tts_vban_address` | | Target IP for TTS output |
| `tts_vban_port` | `6980` | Target UDP port |
| `tts_vban_stream_name` | `TTS1` | Outgoing VBAN stream name |
| `tts_vban_volume` | `1.0` | Volume multiplier for TTS audio (0.0–2.0). 0.5 = half, 1.5 = +50% louder (with clipping protection) |

### Global options

| Option | Default | Description |
|--------|---------|-------------|
| `debug_logging` | `false` | Enable verbose logging |

## Examples

### Single satellite — unicast

```yaml
satellites:
  - name: "Living Room Mic"
    wyoming_port: 10700
    vban_receive_port: 6980
    vban_receive_mode: unicast
    vban_receive_stream_name: "Stream1"
    tts_vban_enabled: false
```

### Two satellites — multicast + unicast

```yaml
satellites:
  - name: "Kitchen Mic"
    wyoming_port: 10700
    vban_receive_port: 6980
    vban_receive_mode: multicast
    vban_receive_multicast_group: "239.0.0.1"
    vban_receive_stream_name: "Kitchen"
    tts_vban_enabled: true
    tts_vban_mode: unicast
    tts_vban_address: "192.168.1.50"
    tts_vban_stream_name: "KitchenTTS"
  - name: "Office Mic"
    wyoming_port: 10701
    vban_receive_port: 6981
    vban_receive_mode: unicast
    vban_receive_stream_name: "Office"
    tts_vban_enabled: false
```

### Multicast mic, no speaker

```yaml
satellites:
  - name: "Garage Mic"
    wyoming_port: 10700
    vban_receive_port: 6980
    vban_receive_mode: multicast
    vban_receive_multicast_group: "239.0.0.1"
    tts_vban_enabled: false
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
