"""Why a failed download failed, judged from yt-dlp's own words.

Only the cause is classified here; whether it says anything about the cookies
is a separate question (:func:`counts_against_cookie_health`). A cookie is
judged expired only when yt-dlp itself says its login was lost. A bot block,
an HTTP 403 (which may be a missing PO Token) or an access restriction on one
video says nothing about the cookie.

The wordings come from yt-dlp's source (``extractor/youtube/_base.py``,
``_video.py``, ``extractor/common.py``, read 2026-10-06) and from YouTube's own
texts that yt-dlp passes on. Few have been seen in a real run log (2026-10-07):
``HTTP Error 403: Forbidden`` (:attr:`FailureKind.PO_TOKEN`, and that one was a
transient failure), the ``No module named 'yt_dlp'`` traceback end
(:attr:`FailureKind.YT_DLP_MISSING`) and the network error text of
:attr:`FailureKind.NETWORK`, which real logs show only in its mid-run form.
``tests/test_failure.py`` labels where every sample came from; replace the
others with real lines once a run log has them.

Pure and dependency free, so it is easy to test and to call from a bot.
"""

from __future__ import annotations

import enum
import re
from typing import Iterable


class FailureKind(str, enum.Enum):
    """The cause of a failed run, as far as yt-dlp's output says."""

    #: yt-dlp lost the login (or the content needs one): re-extracting the
    #: cookies fixes it.
    AUTH_EXPIRED = "auth_expired"
    #: "Sign in to confirm you're not a bot". Not proof of an expired cookie.
    BOT_BLOCKED = "bot_blocked"
    #: PO Token wording or ``HTTP Error 403``. Check the PO Token provider
    #: before the cookies.
    PO_TOKEN = "po_token"
    #: Payment, members-only, private, age or region restriction.
    CONTENT_ACCESS = "content_access"
    #: YouTube throttles the account or session for a while.
    RATE_LIMITED = "rate_limited"
    #: The connection broke or the server did not answer (timeout, reset,
    #: short read, name lookup). Says nothing about the cookies.
    NETWORK = "network"
    #: yt-dlp itself did not start: its module is missing or the ``yt-dlp``
    #: launcher on the PATH is broken. Nothing else the run said matters.
    YT_DLP_MISSING = "yt_dlp_missing"
    #: Anything else.
    OTHER = "other"


def _norm(line: str) -> str:
    """Lower case, with typographic apostrophes made plain (YouTube writes
    "you’re")."""
    return line.replace("’", "'").replace("‘", "'").lower()


_BOT_WORD = re.compile(r"\bbot\b")

_AUTH_EXPIRED = (
    # A WARNING, printed when the cookies passed in lose their login mid-run
    # (rotated in the browser, or expired).
    "account cookies are no longer valid",
    # raise_login_required() and the YouTube "login details" check.
    "login details are needed to download",
    "only available for registered users",
)
_CONTENT_ACCESS = (
    "requires payment",
    "members-only", "members only", "join this channel", "channel's members",
    "private video", "video is private",
    "sign in if you've been granted access",
    "confirm your age", "age-restricted", "inappropriate for some users",
    "available in your country", "not available from your location",
    "geo restriction", "geo-restricted",
    "video unavailable",
)
# "HTTP Error 429: Too Many Requests" is the plain form of the same throttle; a real run log
# (2026-10-07) has it from the subtitle download, which makes yt-dlp stop the whole video.
_RATE_LIMITED = (
    "this content isn't available, try again later",
    "rate-limited by youtube",
    "http error 429",
    "too many requests",
)
_PO_TOKEN = (
    "po token",
    "http error 403",
)
# What the error says, not how yt-dlp wraps it: the same text sits in the
# "Got error: ...", "Unable to download webpage: ..." and "Giving up after N
# retries" lines. "timed out" covers Read/Connection timed out and "The read
# operation timed out"; "bytes read" is yt-dlp's IncompleteRead ("N bytes read,
# M more expected").
# Python could not even start yt-dlp (a source-mode run whose PATH has a stale
# ``yt-dlp`` launcher). The first is from a real run log (2026-10-07); the launcher
# message is distlib's, from memory.
_YT_DLP_MISSING = (
    "no module named 'yt_dlp'", 'no module named "yt_dlp"', "no module named yt_dlp",
    "fatal error in launcher",
)
_NETWORK = (
    "timed out", "bytes read",
    "connection reset", "connection aborted", "remote end closed connection",
    "getaddrinfo failed", "name or service not known", "temporary failure in name resolution",
    "failed to resolve", "network is unreachable", "no route to host",
)


def _is_bot(line: str) -> bool:
    return "not a bot" in line or (
        _BOT_WORD.search(line) is not None and ("confirm" in line or "detect" in line)
    )


def _is_network(line: str) -> bool:
    """A connection error that was given up on. Not a line that says yt-dlp is
    still retrying (that attempt may yet succeed, and a run that failed for
    another reason would be misread), and not the PO Token sidecar, whose
    unreachable server is its own matter (:func:`sidecar_unreachable`).
    ``Giving up after N retries`` alone proves nothing: the live URL refresh
    warning ends the same way."""
    if "retrying" in line or "pot:bgutil" in line:
        return False
    return any(phrase in line for phrase in _NETWORK)


def _has(phrases: tuple[str, ...]):
    return lambda line: any(phrase in line for phrase in phrases)


# Order matters: a line can say more than one thing. A yt-dlp that did not
# start beats everything (it said nothing about the video); a lost login is the
# strongest statement about the video; a bot block comes before the restrictions that also
# mention signing in; HTTP 403 is weaker still, and a bare network error is
# the weakest (it explains no other cause), so it is looked for last.
_RULES = (
    (FailureKind.YT_DLP_MISSING, _has(_YT_DLP_MISSING)),
    (FailureKind.AUTH_EXPIRED, _has(_AUTH_EXPIRED)),
    (FailureKind.BOT_BLOCKED, _is_bot),
    (FailureKind.CONTENT_ACCESS, _has(_CONTENT_ACCESS)),
    (FailureKind.RATE_LIMITED, _has(_RATE_LIMITED)),
    (FailureKind.PO_TOKEN, _has(_PO_TOKEN)),
    (FailureKind.NETWORK, _is_network),
)


def classify_failure(lines: Iterable[str]) -> FailureKind:
    """The cause of a failed run from its ``ERROR`` and ``WARNING`` lines.

    Pass only the lines that matter (not progress or file names, which could
    contain any word); :func:`FailureKind.OTHER` when none says anything known.
    """
    text = [_norm(line) for line in lines]
    for kind, matches in _RULES:
        if any(matches(line) for line in text):
            return kind
    return FailureKind.OTHER


def cookies_reported_invalid(lines: Iterable[str]) -> bool:
    """True when yt-dlp itself said the cookies it was given lost their login
    (``The provided YouTube account cookies are no longer valid``).

    A WARNING that can appear on a run that still succeeds, since a public
    video needs no login. Unlike :func:`classify_failure`, which also reads
    "login needed" errors, this is the one sentence that is certain.
    """
    return any("account cookies are no longer valid" in _norm(line) for line in lines)


def subtitle_download_failed(lines: Iterable[str]) -> bool:
    """True when yt-dlp stopped the video because a subtitle could not be
    downloaded, whatever the reason (``Unable to download video subtitles for
    'ko': HTTP Error 429`` is the one seen in a real log, 2026-10-07). yt-dlp
    treats that as fatal for the whole video unless told to ignore errors
    (``YoutubeDL._write_subtitles``), so the video can be had without them."""
    return any("unable to download video subtitles" in _norm(line) for line in lines)


def http_403_error(lines: Iterable[str]) -> bool:
    """True when yt-dlp itself stopped on ``HTTP Error 403: Forbidden`` (an
    ``ERROR`` line, as in three real run logs of 2026-10-07, each retried by hand
    minutes later with success). A WARNING that only says a missing PO Token
    "may yield HTTP Error 403" is not this: the run it belongs to may have failed
    for another reason."""
    return any(
        text.startswith("error") and "http error 403" in text
        for text in (_norm(line).lstrip() for line in lines)
    )


def sidecar_unreachable(lines: Iterable[str]) -> bool:
    """True when yt-dlp could not reach the bgutil PO Token provider
    (``[pot:bgutil:http] Error reaching GET .../ping``)."""
    return any(
        "pot:bgutil" in line and "error reaching" in line
        for line in (_norm(text) for text in lines)
    )


def counts_against_cookie_health(kind: FailureKind, *, uses_cookies: bool) -> bool:
    """Whether this failure is evidence that the cookies expired.

    Only a lost login counts, and only when cookies were in use. A bot block
    can have other causes, a 403 may be a missing PO Token, and an access
    restriction concerns one video, so none of them changes the verdict.
    """
    return uses_cookies and kind is FailureKind.AUTH_EXPIRED


def failure_hint(
    kind: FailureKind,
    *,
    uses_cookies: bool = False,
    sidecar_down: bool = False,
    retried: bool = False,
) -> str:
    """One line telling the user what to check; ``""`` for an unknown cause.
    *retried*: the download was already tried again by itself after an HTTP 403."""
    if kind is FailureKind.AUTH_EXPIRED:
        if uses_cookies:
            return "쿠키의 로그인이 풀린 것으로 보입니다. 쿠키를 다시 추출하세요 ('쿠키 자동 갱신')."
        return "로그인이 필요한 영상입니다. 쿠키 인증을 설정하세요."
    if kind is FailureKind.BOT_BLOCKED:
        if uses_cookies:
            return (
                "YouTube 가 봇으로 판단해 막았습니다. 쿠키 만료로 단정할 수는 없습니다 — "
                "잠시 뒤 다시 시도하고, 계속되면 쿠키를 새로 받아 보세요."
            )
        return "YouTube 가 봇으로 판단해 막았습니다. 쿠키 인증을 설정하면 풀릴 수 있습니다."
    if kind is FailureKind.PO_TOKEN:
        again = (
            "자동으로 한 번 다시 시도했지만 같은 오류였습니다. 몇 분 뒤 다시 보내면 받아진 적이 있습니다."
            if retried
            else "일시적일 수 있으니 먼저 한 번 다시 시도해 보세요."
        )
        hint = (
            f"HTTP 403 또는 PO Token 문제로 보입니다. {again} "
            "계속되면 쿠키 재추출보다 PO Token(bgutil 사이드카) 상태를 먼저 점검하세요."
        )
        if sidecar_down:
            hint += " 실행 로그에 사이드카 응답 실패(Error reaching ... /ping)가 있습니다."
        return hint
    if kind is FailureKind.CONTENT_ACCESS:
        return (
            "결제·멤버 전용·비공개·연령/지역 제한 영상일 수 있습니다. "
            "쿠키 만료로 단정하지 않습니다. 계정의 시청 권한을 확인하세요."
        )
    if kind is FailureKind.RATE_LIMITED:
        return "YouTube 가 요청을 일시적으로 제한한 것으로 보입니다 (최대 1시간). 잠시 뒤 다시 시도하세요."
    if kind is FailureKind.YT_DLP_MISSING:
        return (
            "yt-dlp 를 실행하지 못했습니다 (yt_dlp 모듈이 없거나 PATH 의 yt-dlp 가 고장났습니다). "
            "소스로 실행 중이면 'python -m pip install -U yt-dlp' 를 하고, PATH 에서 어느 yt-dlp 가 "
            "먼저 잡히는지('where yt-dlp') 확인하세요. 배포본(.exe)은 yt-dlp 를 내장합니다."
        )
    if kind is FailureKind.NETWORK:
        return (
            "네트워크 연결이 끊기거나 서버가 응답하지 않아 실패했습니다. "
            "인터넷 연결을 확인하고 잠시 뒤 다시 시도하세요. 쿠키 문제는 아닌 것으로 보입니다."
        )
    return ""
