# Changelog

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
