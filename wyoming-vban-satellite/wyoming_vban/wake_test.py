"""Wake word test of the calibration: counts openWakeWord's detections.

The audio goes to the openWakeWord app over its own Wyoming connection,
never through Home Assistant's pipeline: the test can't wake the assistant.
openWakeWord's detection event has no confidence score (name and
timestamp only): the result is a number of detections.

The audio is sent in real time: openWakeWord's refractory period (no new
detection for 2 s after one) is counted in wall-clock time, so audio sent
faster would hide the repeated wake words.
"""

import asyncio
import logging
import time
from typing import Optional

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
from wyoming.wake import Detect, Detection, NotDetected

from .const import WYOMING_CHANNELS, WYOMING_RATE, WYOMING_WIDTH

_LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "core-openwakeword"
DEFAULT_PORT = 10400
DEFAULT_MODEL = "dis_moi_jarvis-6"
CHUNK_SAMPLES = 1024
TAIL_S = 1.5  # wait for late detections after the end of the audio
TIMEOUT_S = 5.0


class WakeTestError(Exception):
    """openWakeWord unreachable, or the model isn't there."""


async def list_models(host: str, port: int) -> list[str]:
    """Wake word models of the openWakeWord app."""
    try:
        async with AsyncTcpClient(host, port) as client:
            await asyncio.wait_for(client.write_event(Describe().event()), TIMEOUT_S)
            while True:
                event = await asyncio.wait_for(client.read_event(), TIMEOUT_S)
                if event is None:
                    raise WakeTestError("connexion fermée par openWakeWord")
                if Info.is_type(event.type):
                    info = Info.from_event(event)
                    return sorted({m.name for w in info.wake for m in w.models})
    except (OSError, asyncio.TimeoutError) as err:
        raise WakeTestError(
            f"openWakeWord injoignable ({host}:{port}) : {err or type(err).__name__}"
        ) from err


async def check_model(host: str, port: int, model: str) -> None:
    """openWakeWord falls back to its default model for an unknown name:
    check first, so the test doesn't measure another wake word."""
    models = await list_models(host, port)
    if model not in models:
        raise WakeTestError(
            f"modèle « {model} » absent d'openWakeWord (disponibles : "
            f"{', '.join(models) or 'aucun'})"
        )


async def count_detections(
    pcm: bytes, host: str, port: int, model: str, pace: float = 1.0,
) -> list[Optional[int]]:
    """Send 16 kHz mono 16-bit audio, return the detections' timestamps
    (ms into the audio). pace 1.0 is real time (needed, see above)."""
    detections: list[Optional[int]] = []
    chunk_bytes = CHUNK_SAMPLES * WYOMING_WIDTH
    try:
        async with AsyncTcpClient(host, port) as client:

            async def receive() -> None:
                while True:
                    event = await client.read_event()
                    if event is None or NotDetected.is_type(event.type):
                        return
                    if Detection.is_type(event.type):
                        detection = Detection.from_event(event)
                        if detection.name in (None, model):
                            detections.append(detection.timestamp)

            reader = asyncio.create_task(receive())
            try:
                await client.write_event(Detect(names=[model]).event())
                await client.write_event(AudioStart(
                    rate=WYOMING_RATE, width=WYOMING_WIDTH, channels=WYOMING_CHANNELS,
                ).event())
                start = time.monotonic()
                for i, offset in enumerate(range(0, len(pcm), chunk_bytes)):
                    await client.write_event(AudioChunk(
                        rate=WYOMING_RATE, width=WYOMING_WIDTH, channels=WYOMING_CHANNELS,
                        audio=pcm[offset:offset + chunk_bytes],
                    ).event())
                    if pace:
                        due = start + (i + 1) * CHUNK_SAMPLES / WYOMING_RATE * pace
                        await asyncio.sleep(max(0.0, due - time.monotonic()))
                await client.write_event(AudioStop().event())
                try:
                    await asyncio.wait_for(asyncio.shield(reader), TAIL_S)
                except asyncio.TimeoutError:
                    pass  # no end event after detections: the tail is over
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
    except (OSError, asyncio.TimeoutError) as err:
        raise WakeTestError(
            f"openWakeWord injoignable ({host}:{port}) : {err or type(err).__name__}"
        ) from err
    _LOGGER.debug("Wake word test: %d detection(s) of %s", len(detections), model)
    return detections
