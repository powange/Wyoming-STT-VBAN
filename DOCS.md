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
