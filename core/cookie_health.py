"""Remember whether the YouTube login cookies still work.

A server that downloads for people finds out its cookies expired only when
downloads start failing, quietly. This module keeps the evidence in one small
JSON file so the operator can be told once, and so the expiry cycle can be
read later.

What it records (:class:`CookieHealth`):

* ``failure_count`` - confirmed expiries of the *current* cookie since its last
  confirmed success. Confirmed means yt-dlp itself said the login was lost
  (see :func:`core.failure.counts_against_cookie_health`): never a bot block,
  an HTTP 403 or a restriction on one video.
* ``last_success_at`` / ``last_failure_at`` / ``last_error_kind``.
* ``events`` - the last few changes, which is the expiry log.

"The same cookie" is a fingerprint of the login cookie's value
(:func:`cookie_identity`), because yt-dlp rewrites ``cookies.txt`` after every
run. A new login (re-extracted cookies) has a new fingerprint and starts with a
clean record, which is how a re-extraction counts as recovery. A cookie file
without a login is not tracked at all: it cannot expire.

Pure logic: no tkinter, no ``print()``. The file is
``$YTDLP_COOKIE_HEALTH_FILE``, else ``$XDG_STATE_HOME/ytdlp-bot/cookie_health.json``,
else ``~/.ytdlp-downloader/cookie_health.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Optional

from core.auth import Authenticator, BrowserCookieAuth, CookieFileAuth

__all__ = [
    "CookieHealth",
    "CookieHealthStore",
    "cookie_identity",
    "default_health_path",
    "login_cookie_expires_at",
]

logger = logging.getLogger(__name__)

HEALTH_ENV = "YTDLP_COOKIE_HEALTH_FILE"
MAX_EVENTS = 50
_FILE_VERSION = 1

# yt-dlp's own test for "logged in" (extractor/youtube/_base.py): LOGIN_INFO and
# one of the PAPISID cookies on youtube.com. LOGIN_INFO is what YouTube clears
# when it rotates the cookies.
_LOGIN_INFO = "LOGIN_INFO"
_SID_COOKIES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")


def default_health_path() -> Path:
    """Where the health record lives (see the module docstring)."""
    override = os.environ.get(HEALTH_ENV)
    if override:
        return Path(override).expanduser()
    state = os.environ.get("XDG_STATE_HOME")
    if state:
        return Path(state) / "ytdlp-bot" / "cookie_health.json"
    return Path.home() / ".ytdlp-downloader" / "cookie_health.json"


# ---------------------------------------------------------------- the cookie file


class _Cookie(NamedTuple):
    domain: str
    name: str
    value: str
    expires: int  # epoch seconds, 0 for a session cookie


def _read_cookies(path: os.PathLike | str) -> list[_Cookie]:
    """The cookies of a Netscape ``cookies.txt``; ``OSError`` if unreadable.
    Lines that are not cookies are skipped."""
    cookies: list[_Cookie] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            try:
                expires = int(fields[4])
            except ValueError:
                expires = 0
            cookies.append(_Cookie(fields[0], fields[5], fields[6], expires))
    return cookies


def _login_cookies(cookies: list[_Cookie]) -> Optional[tuple[_Cookie, _Cookie]]:
    """``(LOGIN_INFO, SAPISID-like)`` when the cookies hold a login, else ``None``."""
    youtube = [c for c in cookies if c.domain.lstrip(".").endswith("youtube.com") and c.value]
    login = next((c for c in youtube if c.name == _LOGIN_INFO), None)
    if login is None:
        return None
    for name in _SID_COOKIES:
        sid = next((c for c in youtube if c.name == name), None)
        if sid is not None:
            return login, sid
    return None


def cookie_identity(auth: Authenticator) -> str:
    """Which login a run uses, or ``""`` when there is nothing to track.

    A cookie file with a login is identified by a hash of its PAPISID value
    (stable while yt-dlp rewrites the file, different after a new login). A
    file without a login, or an unreadable one, gives ``""``: it cannot expire.
    Browser cookies are identified by browser and profile only, since their
    database is not read here.
    """
    if isinstance(auth, CookieFileAuth):
        try:
            found = _login_cookies(_read_cookies(auth.path))
        except OSError:
            return ""
        if found is None:
            return ""
        digest = hashlib.sha256(found[1].value.encode("utf-8")).hexdigest()
        return f"file:{digest[:12]}"
    if isinstance(auth, BrowserCookieAuth):
        return f"browser:{auth.browser}" + (f"/{auth.profile}" if auth.profile else "")
    return ""


def login_cookie_expires_at(path: os.PathLike | str) -> Optional[float]:
    """When the login in a ``cookies.txt`` runs out, from its ``expires`` column.

    The earlier of LOGIN_INFO and the PAPISID cookie; ``None`` when the file
    holds no login, is unreadable, or the cookies are session cookies. A hint
    only: it can warn before the first failing download, but a login that
    YouTube invalidated early still looks valid here.
    """
    try:
        found = _login_cookies(_read_cookies(path))
    except OSError:
        return None
    if found is None:
        return None
    dated = [c.expires for c in found if c.expires > 0]
    return float(min(dated)) if dated else None


# ---------------------------------------------------------------- the record


@dataclass
class CookieHealth:
    """What is known about the current cookie. See the module docstring."""

    cookie_id: str = ""
    last_success_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    failure_count: int = 0
    last_error_kind: str = ""
    events: list[dict] = field(default_factory=list)

    def is_healthy(self, cookie_id: Optional[str] = None, *, failure_threshold: int = 1) -> bool:
        """False once *failure_threshold* expiries were confirmed since the last
        confirmed success. With *cookie_id*, a different cookie than the recorded
        one counts as healthy: it has not been seen failing."""
        if cookie_id and self.cookie_id and cookie_id != self.cookie_id:
            return True
        return self.failure_count < failure_threshold

    @classmethod
    def from_dict(cls, data: Any) -> "CookieHealth":
        """A record from its stored form; unusable fields fall back to defaults."""
        if not isinstance(data, dict):
            raise TypeError("not an object")

        def number(key: str) -> Optional[float]:
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        count = data.get("failure_count")
        events = data.get("events")
        return cls(
            cookie_id=str(data.get("cookie_id") or ""),
            last_success_at=number("last_success_at"),
            last_failure_at=number("last_failure_at"),
            failure_count=count if isinstance(count, int) and count > 0 else 0,
            last_error_kind=str(data.get("last_error_kind") or ""),
            events=[e for e in events if isinstance(e, dict)][-MAX_EVENTS:]
            if isinstance(events, list) else [],
        )


class CookieHealthStore:
    """Thread-safe JSON file holding one :class:`CookieHealth`.

    Every call re-reads the file, so another process's changes are seen
    (writers are last-writer-wins). A file that is not valid JSON is moved to
    ``<name>.bak`` and the record starts over. Methods that change the record
    raise ``OSError`` when the file cannot be read or written, so a passing read
    error never makes them overwrite it.

    The methods that change the record say whether the verdict changed, which
    is what a notifier needs to alert once and to announce the recovery once.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_health_path()
        self._lock = threading.Lock()

    def load(self) -> CookieHealth:
        """The current record. An unreadable file reads as a clean one."""
        with self._lock:
            try:
                return self._load()
            except OSError:
                logger.warning("cookie health: cannot read %s", self.path, exc_info=True)
                return CookieHealth()

    def is_healthy(self, cookie_id: Optional[str] = None, *, failure_threshold: int = 1) -> bool:
        return self.load().is_healthy(cookie_id, failure_threshold=failure_threshold)

    def mark_invalid(
        self, cookie_id: str, error_kind: str, *, now: Optional[float] = None
    ) -> bool:
        """Record a confirmed expiry of *cookie_id*. True when this turned a healthy
        cookie unhealthy (the moment to alert); repeats only count."""
        if not cookie_id:
            raise ValueError("cookie_id is required")
        stamp = time.time() if now is None else now
        with self._lock:
            health = self._load()
            if health.cookie_id != cookie_id:
                # A different login: what was known about the old one is moot.
                health = self._fresh(health, cookie_id, stamp, "replaced")
            newly = health.failure_count == 0
            health.failure_count += 1
            health.last_failure_at = stamp
            health.last_error_kind = error_kind
            if newly:
                self._event(health, stamp, "invalid", error_kind=error_kind)
            self._save(health)
            return newly

    def mark_valid(self, cookie_id: str, *, now: Optional[float] = None) -> bool:
        """Record that *cookie_id* was seen authenticating. True when this turned an
        unhealthy cookie healthy (the moment to announce the recovery).

        Call it only on proof that the login works - a request that needs one.
        A public video or an archive skip says nothing, so nothing here calls
        it on an ordinary success.
        """
        if not cookie_id:
            raise ValueError("cookie_id is required")
        stamp = time.time() if now is None else now
        with self._lock:
            health = self._load()
            recovered = health.failure_count > 0  # of the old cookie too, if it is replaced
            if health.cookie_id != cookie_id:
                health = self._fresh(health, cookie_id, stamp, "replaced")
            health.failure_count = 0
            health.last_success_at = stamp
            if recovered:
                self._event(health, stamp, "valid")
            self._save(health)
            return recovered

    def sync(self, cookie_id: str, *, now: Optional[float] = None) -> bool:
        """Note which cookie is in use now; True when a new login replaced one
        that had expired (the re-extraction that fixes it - announce the recovery).

        Only a cookie file's login counts as a replacement. ``""`` (a cookie with
        no login) is ignored: yt-dlp rewrites the file without its login after it
        detects the expiry, which must not read as a fix. Browser cookies are
        ignored too: they have no fingerprint, so switching to them proves nothing.
        """
        if not cookie_id.startswith("file:"):
            return False
        stamp = time.time() if now is None else now
        with self._lock:
            health = self._load()
            if health.cookie_id == cookie_id:
                return False
            was_unhealthy = health.failure_count > 0
            self._save(self._fresh(health, cookie_id, stamp, "replaced"))
            return was_unhealthy

    def reset(self) -> None:
        """Forget everything."""
        with self._lock:
            self._save(CookieHealth())

    # -- internals

    @staticmethod
    def _event(health: CookieHealth, stamp: float, event: str, **extra: Any) -> None:
        health.events.append({"at": stamp, "event": event, "cookie_id": health.cookie_id, **extra})
        del health.events[:-MAX_EVENTS]

    def _fresh(
        self, old: CookieHealth, cookie_id: str, stamp: float, event: str
    ) -> CookieHealth:
        """A clean record for *cookie_id* that keeps the old one's event log."""
        health = CookieHealth(cookie_id=cookie_id, events=list(old.events))
        if old.cookie_id:  # the very first cookie is not a "replacement"
            self._event(health, stamp, event, replaced=old.cookie_id)
        return health

    def _load(self) -> CookieHealth:
        """The record on disk; raises ``OSError`` if the file cannot be read."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return CookieHealth()
        try:
            return CookieHealth.from_dict(json.loads(raw.decode("utf-8-sig")))
        except (ValueError, TypeError):
            backup = self.path.with_name(self.path.name + ".bak")
            logger.warning("cookie health: %s is unreadable, moved to %s", self.path, backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                logger.debug("cookie health: could not move %s aside", self.path, exc_info=True)
            return CookieHealth()

    def _save(self, health: CookieHealth) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _FILE_VERSION, **asdict(health)}
        # A uniquely named sibling swapped in, as in core.history: a crash
        # mid-write never leaves a truncated record.
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "x", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def _replace(src: Path, dst: Path, attempts: int = 5) -> None:
    """``os.replace``, retried briefly: on Windows it fails while another
    process has *dst* open."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)
