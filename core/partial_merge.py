"""Merge what a stopped live recording left behind into one playable file.

yt-dlp merges the video and audio of a ``--live-from-start`` recording
only when the download finishes. Stopped earlier (취소 / 종료, Ctrl+C,
the stall watchdog), it leaves one ``<name>.f<format>.<ext>.part`` file
per format, plus fragments it downloaded but had not appended yet
(``...part-FragN``). :func:`merge_partial_recording` remuxes those into
``<name> (부분 녹화).<ext>`` with ffmpeg. The originals are left as they
are, so downloading the same URL again resumes from them.

Pure logic: no tkinter, no ``print()``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from core.cookies import no_window_kwargs, yt_dlp_env

__all__ = ["MERGE_TIMEOUT", "PARTIAL_SUFFIX", "find_parts", "merge_partial_recording"]

logger = logging.getLogger(__name__)

PARTIAL_SUFFIX = " (부분 녹화)"
# Seconds one merge may take, copying leftover fragments included. The
# GUI's 종료 waits for the merges within this bound, so one never outlives
# the app and leaves ffmpeg or its temp copies behind. A plain remux of a
# 12-hour 1080p recording takes a few minutes.
MERGE_TIMEOUT = 20 * 60

LogCallback = Callable[[str, str], None]


def find_parts(target: Path, format_id: Optional[str] = None) -> list[Path]:
    """``.part`` files yt-dlp wrote for the final file *target*.

    ``X.mp4`` -> ``X.f137.mp4.part`` and ``X.f140.mp4.part`` (one per
    format), or ``X.mp4.part`` for a single combined format. They may be
    empty: with several fragments at a time, yt-dlp appends nothing until
    the recording ends and the data is all in fragments.

    With *format_id* (``"137+140"``, ``"95"``) only that download's parts
    count, not leftovers of an earlier try at another quality that share
    the name (the default template is just the title).
    """
    stem = re.escape(target.stem)
    ids = [i for i in (format_id or "").split("+") if i]
    if len(ids) > 1:
        alternatives = "|".join(re.escape(i) for i in ids)
        pattern = re.compile(stem + rf"\.f(?:{alternatives})\.[^.]+\.part")
    elif ids:
        pattern = re.compile(re.escape(target.name) + r"\.part")
    else:
        pattern = re.compile(stem + r"(?:\.f[^.]+)?\.[^.]+\.part")
    return sorted(
        p for p in target.parent.iterdir() if pattern.fullmatch(p.name) and p.is_file()
    )


def _pending_fragments(part: Path) -> list[Path]:
    """Fragments downloaded for *part* but not appended to it yet, in order.

    yt-dlp records the last appended fragment in ``<name>.ytdl``; a
    finished fragment is ``<part>-FragN`` (``-FragN.part`` while it is
    still downloading, which is incomplete and skipped).
    """
    appended = 0
    ytdl = part.with_name(part.name[: -len(".part")] + ".ytdl")
    try:
        state = json.loads(ytdl.read_text(encoding="utf-8"))
        appended = int(state["downloader"]["current_fragment"]["index"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    frag_re = re.compile(re.escape(part.name) + r"-Frag(\d+)")
    pending = []
    for path in part.parent.iterdir():
        match = frag_re.fullmatch(path.name)
        if match and int(match.group(1)) > appended:
            pending.append((int(match.group(1)), path))
    return [path for _, path in sorted(pending)]


def _free_name(path: Path) -> Path:
    """*path*, or ``<stem> 2<ext>``, ``<stem> 3<ext>``... if it exists."""
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
        n += 1
    return candidate


def find_ffmpeg() -> Optional[str]:
    """ffmpeg on the yt-dlp child ``PATH`` (the bundled one in the exe)."""
    return shutil.which("ffmpeg", path=yt_dlp_env().get("PATH"))


def _copy_until(src: Path, dst, deadline: float) -> None:
    """Append *src* to the open file *dst*; ``TimeoutError`` past *deadline*."""
    with open(src, "rb") as fh:
        while chunk := fh.read(1 << 20):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{MERGE_TIMEOUT // 60}분 안에 끝나지 않았습니다")
            dst.write(chunk)


def merge_partial_recording(
    target: str, log: LogCallback, format_id: Optional[str] = None
) -> Optional[str]:
    """Remux the partial recording of *target* into one file.

    *target* is the final file name yt-dlp would have produced and
    *format_id* the formats it was downloading (see :func:`find_parts`).
    Returns the merged file's path, or ``None`` when there was nothing to
    merge or merging failed (reported through *log*). Gives up after
    ``MERGE_TIMEOUT`` seconds.
    """
    deadline = time.monotonic() + MERGE_TIMEOUT
    target_path = Path(target)
    try:
        # (part, fragments still to append) for every format with data.
        sources = [
            (part, pending)
            for part in find_parts(target_path, format_id)
            for pending in [_pending_fragments(part)]
            if pending or part.stat().st_size > 0
        ]
    except OSError:
        return None
    if not sources:
        return None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log("⚠️ ffmpeg 를 찾지 못해 받은 부분을 합치지 못했습니다. "
            ".part 파일은 저장 폴더에 남아 있습니다.", "warning")
        return None

    out = _free_name(target_path.with_name(target_path.stem + PARTIAL_SUFFIX + target_path.suffix))
    temps: list[Path] = []
    try:
        inputs: list[Path] = []
        for part, pending in sources:
            if not pending:
                inputs.append(part)
                continue
            # Append the leftover fragments to a copy, so the original
            # .part still matches its .ytdl for resuming.
            tmp = out.with_name(f"{out.name}.{len(temps)}.tmp")
            temps.append(tmp)
            with open(tmp, "wb") as dst:
                for src in (part, *pending):
                    _copy_until(src, dst, deadline)
            inputs.append(tmp)

        leftover = sum(len(pending) for _, pending in sources)
        log(
            f"\U0001f9e9 받은 부분을 합치는 중 ({len(sources)}개 파일"
            + (f", 이어 붙이지 못했던 조각 {leftover}개 포함" if leftover else "")
            + f") → {out.name}",
            "info",
        )
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-n"]
        for path in inputs:
            cmd += ["-i", str(path)]
        for index in range(len(inputs)):
            cmd += ["-map", str(index)]
        cmd += ["-c", "copy", str(out)]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(1.0, deadline - time.monotonic()), **no_window_kwargs(),
        )
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            detail = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
            log(f"✗ 받은 부분 합치기 실패: {detail[0]}", "error")
            out.unlink(missing_ok=True)
            return None
    except BaseException as exc:
        # Also on Ctrl+C: subprocess.run has killed ffmpeg by now, and a
        # half-written file must not look like a finished recording.
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        if not isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise
        if isinstance(exc, subprocess.TimeoutExpired):  # its text is the whole argv
            exc = TimeoutError(f"{MERGE_TIMEOUT // 60}분 안에 끝나지 않았습니다")
        log(f"✗ 받은 부분 합치기 실패: {exc}", "error")
        return None
    finally:
        for tmp in temps:
            try:
                tmp.unlink()
            except OSError:
                logger.debug("could not remove %s", tmp, exc_info=True)

    log(f"✓ 받은 부분을 저장했습니다: {out}", "info")
    log("  원본 .part 파일은 같은 주소를 다시 받을 때 이어받기에 쓰입니다. "
        "필요 없으면 지워도 됩니다.", "info")
    return str(out)
