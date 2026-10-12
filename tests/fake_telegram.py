"""A tiny local stand-in for ``api.telegram.org``, for the Telegram tests.

Serves ``POST /bot<token>/<method>`` on 127.0.0.1 with the few methods the bot
uses. A wrong token gets Telegram's 401. Every call is recorded in ``calls``;
a test can replace a method's answer with ``handlers[method] = fn(params) ->
(http_status, body)`` where *body* is a dict (sent as JSON) or raw ``bytes``.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

TOKEN = "123456:TEST-TOKEN-NOT-REAL"


class FakeTelegram:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.calls: list[tuple[str, dict]] = []
        self.handlers: dict[str, Callable[[dict], tuple[int, dict]]] = {}
        self._updates: list[dict] = []
        self._next_update = 1
        self._next_message = 500
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    params = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    params = {}
                prefix = f"/bot{fake.token}/"
                if not self.path.startswith(prefix):
                    status, body = 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
                else:
                    method = self.path[len(prefix):]
                    with fake._lock:
                        fake.calls.append((method, params))
                    status, body = fake._answer(method, params)
                data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        # A short poll interval keeps shutdown() (called after every test) quick.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FakeTelegram":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    # --- what the "server" does ------------------------------------------------

    def push_message(self, chat_id: int, text: str, message_id: int = 1) -> None:
        """A user message that the next ``getUpdates`` delivers."""
        with self._lock:
            self._updates.append({
                "update_id": self._next_update,
                "message": {"message_id": message_id, "chat": {"id": chat_id}, "text": text},
            })
            self._next_update += 1

    def calls_of(self, method: str) -> list[dict]:
        with self._lock:
            return [params for name, params in self.calls if name == method]

    def _answer(self, method: str, params: dict) -> tuple[int, dict]:
        handler = self.handlers.get(method)
        if handler is not None:
            return handler(params)
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "TestBot"}}
        if method == "sendMessage":
            with self._lock:
                self._next_message += 1
                message_id = self._next_message
            return 200, {"ok": True, "result": {"message_id": message_id, "chat": {"id": params.get("chat_id")}}}
        if method == "editMessageText":
            return 200, {"ok": True, "result": True}
        if method == "getUpdates":
            deadline = time.monotonic() + min(float(params.get("timeout", 0)), 0.2)
            while time.monotonic() < deadline:
                with self._lock:
                    if self._updates:
                        break
                time.sleep(0.01)
            with self._lock:
                offset = params.get("offset")
                if offset is not None:
                    self._updates = [u for u in self._updates if u["update_id"] >= offset]
                return 200, {"ok": True, "result": list(self._updates)}
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}
