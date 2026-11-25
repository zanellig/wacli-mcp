"""Local speech-to-text for voice notes, audio and video, with faster-whisper.

faster-whisper is an optional dependency (the `transcribe` and `transcribe-cuda` extras). This module
imports it on first use, so the server starts quickly and runs without it.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from wacli_mcp.settings import Settings

INSTALL_HINT = (
    "Transcription needs the optional faster-whisper dependencies. Reinstall wacli-mcp with the `transcribe` "
    'extra, for example `uv tool install --python 3.13 "wacli-mcp[transcribe]"`, or with '
    "`wacli-mcp[transcribe-cuda]` for an NVIDIA GPU on Windows, then restart your agent. "
    "See the README's Transcription section."
)
# Part of every transcript's cache key. Increase it when decoding or transcription changes enough
# that transcripts saved by earlier versions should be redone.
TRANSCRIPT_FORMAT = 1
# Decoded audio is held in memory as 4-byte samples at 16 kHz, about 230 MB per hour.
MAX_AUDIO_SECONDS = 60 * 60
_REQUIRED_MODULES = ("faster_whisper", "av", "numpy")
_SAMPLE_RATE = 16_000
_GPU_ERROR_WORDS = ("cuda", "cublas", "cudnn", "gpu", ".dll", ".so")


class TranscriptionError(Exception):
    """A file could not be decoded or transcribed."""


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    duration_seconds: float
    model: str
    device: str


class Transcriber(Protocol):
    """Turns an audio or video file into text. `transcribe` blocks; call it from a worker thread."""

    @property
    def cache_tag(self) -> str:
        """Identifies the model, language and format, so a saved transcript is reused only for the same setup."""
        ...

    def unavailable_reason(self) -> str | None: ...

    def status(self) -> dict[str, Any]: ...

    def transcribe(self, path: Path) -> Transcript: ...


def decode_audio(path: Path, max_seconds: float = MAX_AUDIO_SECONDS) -> Any:
    """Decode an audio or video file to 16 kHz mono float32 samples, the input Whisper expects.

    faster-whisper's own decoder passes an argument that PyAV 15 removed, so this calls PyAV directly.
    Stops with an error once the audio passes `max_seconds`, before it can use much memory.
    """
    av: Any = importlib.import_module("av")
    np: Any = importlib.import_module("numpy")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=_SAMPLE_RATE)
    limit = int(max_seconds * _SAMPLE_RATE)
    chunks: list[Any] = []
    total = 0

    def keep(frames: Any) -> None:
        nonlocal total
        for out in frames:
            samples = out.to_ndarray().reshape(-1)
            total += len(samples)
            if total > limit:
                raise TranscriptionError(f"the recording is longer than {max_seconds / 60:g} minutes")
            chunks.append(samples)

    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise TranscriptionError("the file has no audio track")
        for frame in container.decode(container.streams.audio[0]):
            # Some voice notes carry timestamps the resampler rejects; it does not need them.
            frame.pts = None
            keep(resampler.resample(frame))
        keep(resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


_cuda_libraries_exposed = False


def _expose_cuda_libraries() -> None:
    """On Windows, let CTranslate2 load the cuBLAS and cuDNN DLLs that the `transcribe-cuda` extra installs.

    Those wheels put their DLLs in `site-packages/nvidia/<library>/bin`, which Windows does not search.
    """
    global _cuda_libraries_exposed
    if _cuda_libraries_exposed or sys.platform != "win32":
        return
    _cuda_libraries_exposed = True
    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        return
    for root in spec.submodule_search_locations:
        for bin_dir in sorted(Path(root).glob("*/bin")):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def _cuda_device_count() -> int:
    try:
        return int(importlib.import_module("ctranslate2").get_cuda_device_count())
    except (ImportError, RuntimeError, OSError):
        return 0


class WhisperTranscriber:
    """Runs a faster-whisper model in this process. The model loads on first use and stays loaded."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # One transcription at a time: they share the model, and a GPU gains nothing from running two.
        self._lock = threading.Lock()
        self._model: Any = None
        self._device: str | None = None
        self._gpu_error: str | None = None

    @property
    def cache_tag(self) -> str:
        model, language = self._settings.transcribe_model, self._settings.transcribe_language or "auto"
        # Readable, plus a digest of the exact values: two model paths can look alike once shortened.
        digest = hashlib.sha256(f"{model}|{language}|{TRANSCRIPT_FORMAT}".encode()).hexdigest()[:10]
        readable = re.sub(r"[^A-Za-z0-9.-]+", "-", f"{Path(model).name}-{language}").strip("-")
        return f"{readable}-{digest}"

    def unavailable_reason(self) -> str | None:
        if all(importlib.util.find_spec(name) is not None for name in _REQUIRED_MODULES):
            return None
        return INSTALL_HINT

    def status(self) -> dict[str, Any]:
        reason = self.unavailable_reason()
        info: dict[str, Any] = {
            "available": reason is None,
            "model": self._settings.transcribe_model,
            "language": self._settings.transcribe_language or "auto-detect",
            "device": self._device or f"{self._settings.transcribe_device} (model not loaded yet)",
        }
        if reason:
            info["how_to_enable"] = reason
        if self._gpu_error:
            info["gpu_error"] = self._gpu_error
        return info

    def transcribe(self, path: Path) -> Transcript:
        with self._lock:
            audio = decode_audio(path)
            self._ensure_model()
            try:
                return self._run(audio)
            except RuntimeError as exc:
                # CUDA problems such as a missing cuBLAS DLL often surface only once inference starts.
                # Other errors belong to this file and must not move every later file to the CPU.
                gpu_failure = any(word in str(exc).lower() for word in _GPU_ERROR_WORDS)
                if self._device != "cuda" or self._settings.transcribe_device != "auto" or not gpu_failure:
                    raise
                self._gpu_error = str(exc)
                self._create("cpu")
                return self._run(audio)

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        device = self._settings.transcribe_device
        if device == "auto":
            device = "cuda" if _cuda_device_count() > 0 else "cpu"
        if device == "cuda":
            _expose_cuda_libraries()
            try:
                self._create("cuda")
                return
            except (RuntimeError, ValueError, OSError) as exc:
                if self._settings.transcribe_device == "cuda":
                    raise
                self._gpu_error = str(exc)
        self._create("cpu")

    def _create(self, device: str) -> None:
        faster_whisper: Any = importlib.import_module("faster_whisper")
        self._model = None
        self._model = faster_whisper.WhisperModel(
            self._settings.transcribe_model,
            device=device,
            compute_type="float16" if device == "cuda" else "int8",
        )
        self._device = device

    def _run(self, audio: Any) -> Transcript:
        device = self._device or "cpu"
        if not len(audio):
            return Transcript("", None, 0.0, self._settings.transcribe_model, device)
        # `vad_filter` skips silence, where Whisper tends to invent text.
        segments, info = self._model.transcribe(audio, language=self._settings.transcribe_language, vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return Transcript(
            text=text,
            language=getattr(info, "language", None),
            duration_seconds=round(float(getattr(info, "duration", 0.0)), 1),
            model=self._settings.transcribe_model,
            device=device,
        )
