# Changelog

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
