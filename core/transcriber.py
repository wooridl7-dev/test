"""Speech-to-text abstraction (stub).

This module defines the :class:`Transcriber` facade that future messenger
bot code will use to turn downloaded audio into text. The actual
transcription is *not* implemented yet; every backend currently raises
:class:`NotImplementedError`.

Design goals
------------
1. **Pluggable backends.** We want to be able to swap between:

   * ``faster-whisper`` - CPU/GPU-friendly CTranslate2 build of Whisper.
     Preferred for Linux/Docker deployment of the messenger bot.
   * ``whisper.cpp``    - C++ port that runs well on low-RAM boxes and
     on AMD Ryzen AI NPUs via the bundled runtime. Preferred for local
     Windows Ryzen AI deployment.
   * ``onnx``           - ONNX Runtime export of Whisper, useful on
     Ryzen AI / DirectML / other accelerators.

2. **No hard dependency.** Importing this module must succeed even when
   none of the optional backends are installed. The individual backend
   implementations lazy-import their libraries inside
   :meth:`Transcriber._load`.

3. **Single call surface.** Callers get one method:
   ``transcriber.transcribe(audio_path) -> str``. All backend-specific
   tuning (model size, device, language hints, VAD, etc.) flows through
   the constructor, so the messenger bot code does not change when the
   backend does.

Planned public API (not yet implemented)
----------------------------------------
.. code-block:: python

    from core.transcriber import Transcriber

    t = Transcriber(backend="faster-whisper", model="small", language="ko")
    text = t.transcribe("/path/to/audio.m4a")

The constructor must accept at minimum:

* ``backend``   : ``"faster-whisper" | "whisper.cpp" | "onnx"``
* ``model``     : model name or local path ("tiny", "base", "small", ...)
* ``language``  : optional BCP-47 hint, e.g. ``"ko"``; ``None`` = detect
* ``device``    : ``"cpu" | "cuda" | "npu" | "auto"``
* ``**kwargs``  : backend-specific overrides

And should expose:

* ``transcribe(audio_path: str | Path) -> str``
* ``transcribe_with_timestamps(audio_path) -> list[Segment]``
  (later - useful for subtitle generation and messenger replies with
  per-segment timestamps)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

__all__ = ["Transcriber", "TranscriberBackend"]

PathLike = Union[str, Path]

# Type alias for readability. Deliberately a plain str and not an Enum so
# new backends can be added without an import cycle.
TranscriberBackend = str  # "faster-whisper" | "whisper.cpp" | "onnx"


class Transcriber:
    """Facade over a speech-to-text backend.

    This is a stub. All transcription calls currently raise
    :class:`NotImplementedError`. The constructor does, however,
    remember its configuration so that callers can wire it up now and
    flesh out the backend later.
    """

    #: Backends we intend to support. Keep in sync with module docstring.
    SUPPORTED_BACKENDS: tuple[str, ...] = ("faster-whisper", "whisper.cpp", "onnx")

    def __init__(
        self,
        backend: TranscriberBackend = "faster-whisper",
        model: str = "small",
        language: Optional[str] = None,
        device: str = "auto",
        **backend_kwargs,
    ) -> None:
        if backend not in self.SUPPORTED_BACKENDS:
            raise ValueError(
                f"Unknown transcriber backend: {backend!r}. "
                f"Expected one of {self.SUPPORTED_BACKENDS}."
            )
        self.backend = backend
        self.model = model
        self.language = language
        self.device = device
        self.backend_kwargs = backend_kwargs
        self._loaded = False

    def _load(self) -> None:
        """Lazy-initialise the backend model. Override per backend.

        Will be responsible for the conditional imports
        (``faster_whisper``, ``whisper_cpp``, ``onnxruntime`` ...).
        """
        raise NotImplementedError(
            f"Transcriber backend {self.backend!r} is not implemented yet. "
            "See core/transcriber.py docstring for the planned design."
        )

    def transcribe(self, audio_path: PathLike) -> str:
        """Transcribe *audio_path* to plain text.

        Args:
            audio_path: Path to an audio file (wav/mp3/m4a/opus).

        Returns:
            The recognised text as a single string.

        Raises:
            NotImplementedError: Always, until a backend is wired up.
            FileNotFoundError: When the audio file does not exist
                (validated *before* the backend runs, even in the stub,
                so that tests of the bot's plumbing surface file errors
                in the same way as the real implementation will).
        """
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {path}")
        raise NotImplementedError(
            f"Transcriber.transcribe is not implemented yet "
            f"(backend={self.backend!r}). See module docstring."
        )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Transcriber(backend={self.backend!r}, model={self.model!r}, "
            f"language={self.language!r}, device={self.device!r})"
        )
