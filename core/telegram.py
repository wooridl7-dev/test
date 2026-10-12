"""Minimal Telegram Bot API client, standard library only.

Just what the messenger bot needs: long polling (``getUpdates``), sending a
message and editing it. No ``python-telegram-bot`` dependency, so the
packaged app and the Docker image stay as they are, and a test can point the
client at a local fake server (*api_base*).

The bot token is a password. It sits in every request URL, so it is scrubbed
from every error text this module raises or logs.

Pure transport: it knows nothing about downloads (that is
``interfaces/bot.py``). Safe to call from several threads (each call opens its
own connection).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

__all__ = ["TelegramClient", "TelegramError", "MAX_TEXT"]

logger = logging.getLogger(__name__)

#: Telegram refuses a text longer than 4096 characters; stay below it.
MAX_TEXT = 4000

_DEFAULT_API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    """A call that did not succeed.

    Attributes:
        code: Telegram's ``error_code`` (HTTP status), or ``None`` when the
            server could not be reached or answered something unreadable.
        retry_after: Seconds Telegram asks to wait (flood control, code 429).
    """

    def __init__(
        self, description: str, code: Optional[int] = None, retry_after: Optional[float] = None
    ) -> None:
        super().__init__(description)
        self.description = description
        self.code = code
        self.retry_after = retry_after

    @property
    def fatal(self) -> bool:
        """The token is wrong (401/404) or another process polls the same bot
        (409): retrying cannot help."""
        return self.code in (401, 404, 409)


class TelegramClient:
    """Calls to one bot (*token*) of the Telegram Bot API.

    Args:
        token: Bot token from @BotFather (``123456:ABC...``).
        api_base: Server to call; only a test changes it.
        timeout: Seconds to wait for an ordinary call. A long poll waits its
            own ``timeout`` plus this.
    """

    def __init__(
        self, token: str, *, api_base: str = _DEFAULT_API_BASE, timeout: float = 15.0
    ) -> None:
        if not token or not token.strip():
            raise ValueError("a bot token is required")
        self._token = token.strip()
        self._base = api_base.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return f"TelegramClient(api_base={self._base!r})"

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    def call(
        self, method: str, params: Optional[dict] = None, *, timeout: Optional[float] = None
    ) -> Any:
        """Call a Bot API *method* and return its ``result``.

        Raises:
            TelegramError: network trouble, an unreadable answer, or
                ``ok: false`` from Telegram.
        """
        request = urllib.request.Request(
            f"{self._base}/bot{self._token}/{method}",
            data=json.dumps(params or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # Telegram explains a refusal in the body of a 4xx/5xx answer.
            body = exc.read()
            if not body:
                raise TelegramError(f"{method}: HTTP {exc.code}", code=exc.code) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelegramError(f"{method}: {self._scrub(str(exc))}") from None
        try:
            answer = json.loads(body)
        except ValueError:
            raise TelegramError(f"{method}: the answer was not JSON") from None
        if not isinstance(answer, dict):
            raise TelegramError(f"{method}: unexpected answer")
        if not answer.get("ok"):
            parameters = answer.get("parameters") or {}
            raise TelegramError(
                self._scrub(f"{method}: {answer.get('description', 'unknown error')}"),
                code=answer.get("error_code"),
                retry_after=parameters.get("retry_after"),
            )
        return answer.get("result")

    # --- the few methods the bot uses --------------------------------------

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: Optional[int] = None, *, timeout: int = 30) -> list[dict]:
        """Wait up to *timeout* seconds for new messages (long polling)."""
        params: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = self.call("getUpdates", params, timeout=timeout + self._timeout)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str, *, reply_to: Optional[int] = None) -> dict:
        """Send plain text (no markup, so no escaping); returns the message."""
        params: dict = {
            "chat_id": chat_id,
            "text": text[:MAX_TEXT],
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", params)

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> bool:
        """Replace a message's text. ``False`` when it already said that
        (Telegram answers "message is not modified"), which is not an error."""
        try:
            self.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text[:MAX_TEXT],
                    "disable_web_page_preview": True,
                },
            )
        except TelegramError as exc:
            if "message is not modified" in exc.description:
                return False
            raise
        return True
