"""core.telegram against a local fake Telegram server (no network, no real token)."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.request
from unittest import mock

from core.telegram import MAX_TEXT, TelegramClient, TelegramError
from tests.fake_telegram import TOKEN, FakeTelegram


class _ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeTelegram()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = TelegramClient(TOKEN, api_base=self.server.api_base, timeout=5)


class CallTest(_ServerTest):
    def test_get_me(self) -> None:
        self.assertEqual(self.client.get_me()["username"], "TestBot")

    def test_send_message_posts_json_and_replies_to_a_message(self) -> None:
        sent = self.client.send_message(42, "안녕 ✓", reply_to=7)
        self.assertIn("message_id", sent)
        (params,) = self.server.calls_of("sendMessage")
        self.assertEqual(params["chat_id"], 42)
        self.assertEqual(params["text"], "안녕 ✓")
        self.assertEqual(params["reply_parameters"]["message_id"], 7)
        self.assertTrue(params["disable_web_page_preview"])

    def test_a_message_without_reply_has_no_reply_parameters(self) -> None:
        self.client.send_message(42, "hi")
        (params,) = self.server.calls_of("sendMessage")
        self.assertNotIn("reply_parameters", params)

    def test_a_long_text_is_cut_below_telegrams_limit(self) -> None:
        self.client.send_message(1, "x" * 10_000)
        self.client.edit_message_text(1, 5, "y" * 10_000)
        self.assertEqual(len(self.server.calls_of("sendMessage")[0]["text"]), MAX_TEXT)
        self.assertEqual(len(self.server.calls_of("editMessageText")[0]["text"]), MAX_TEXT)

    def test_edit_message_text(self) -> None:
        self.assertTrue(self.client.edit_message_text(42, 9, "새 글"))
        (params,) = self.server.calls_of("editMessageText")
        self.assertEqual((params["chat_id"], params["message_id"], params["text"]), (42, 9, "새 글"))

    def test_an_unchanged_edit_is_not_an_error(self) -> None:
        self.server.handlers["editMessageText"] = lambda _p: (
            400, {"ok": False, "error_code": 400,
                  "description": "Bad Request: message is not modified: specified new message "
                                 "content and reply markup are exactly the same"})
        self.assertFalse(self.client.edit_message_text(1, 2, "same"))

    def test_other_edit_errors_are_raised(self) -> None:
        self.server.handlers["editMessageText"] = lambda _p: (
            400, {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"})
        with self.assertRaises(TelegramError) as caught:
            self.client.edit_message_text(1, 2, "x")
        self.assertEqual(caught.exception.code, 400)
        self.assertFalse(caught.exception.fatal)

    def test_flood_control_carries_retry_after(self) -> None:
        self.server.handlers["sendMessage"] = lambda _p: (
            429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7",
                  "parameters": {"retry_after": 7}})
        with self.assertRaises(TelegramError) as caught:
            self.client.send_message(1, "x")
        self.assertEqual((caught.exception.code, caught.exception.retry_after), (429, 7))

    def test_get_updates_sends_the_offset_and_the_poll_length(self) -> None:
        self.server.push_message(10, "첫째")
        self.server.push_message(10, "둘째")
        updates = self.client.get_updates(None, timeout=1)
        self.assertEqual([u["message"]["text"] for u in updates], ["첫째", "둘째"])
        later = self.client.get_updates(updates[0]["update_id"] + 1, timeout=1)
        self.assertEqual([u["message"]["text"] for u in later], ["둘째"])
        params = self.server.calls_of("getUpdates")[-1]
        self.assertEqual(params["offset"], updates[0]["update_id"] + 1)
        self.assertEqual(params["timeout"], 1)
        self.assertEqual(params["allowed_updates"], ["message"])

    def test_no_updates_is_an_empty_list(self) -> None:
        self.assertEqual(self.client.get_updates(None, timeout=1), [])


class FailureTest(_ServerTest):
    def test_a_wrong_token_is_fatal(self) -> None:
        wrong = TelegramClient("999:WRONG", api_base=self.server.api_base, timeout=5)
        with self.assertRaises(TelegramError) as caught:
            wrong.get_me()
        self.assertEqual(caught.exception.code, 401)
        self.assertTrue(caught.exception.fatal)

    def test_another_poller_is_fatal_too(self) -> None:
        self.server.handlers["getUpdates"] = lambda _p: (
            409, {"ok": False, "error_code": 409,
                  "description": "Conflict: terminated by other getUpdates request"})
        with self.assertRaises(TelegramError) as caught:
            self.client.get_updates(None, timeout=1)
        self.assertTrue(caught.exception.fatal)

    def test_the_token_never_appears_in_an_error(self) -> None:
        # Telegram or a proxy may echo the request URL back.
        self.server.handlers["sendMessage"] = lambda _p: (
            500, {"ok": False, "error_code": 500, "description": f"oops at /bot{TOKEN}/sendMessage"})
        with self.assertRaises(TelegramError) as caught:
            self.client.send_message(1, "x")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN, caught.exception.description)

    def test_an_unreachable_server_is_an_error_without_a_code(self) -> None:
        # urllib's own message can carry the URL, and so the token.
        refused = urllib.error.URLError(f"cannot connect to https://x/bot{TOKEN}/getMe")
        with mock.patch.object(urllib.request, "urlopen", side_effect=refused):
            with self.assertRaises(TelegramError) as caught:
                self.client.get_me()
        self.assertIsNone(caught.exception.code)
        self.assertFalse(caught.exception.fatal)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("<token>", str(caught.exception))

    def test_a_timeout_is_an_error_without_a_code(self) -> None:
        with mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TelegramError) as caught:
                self.client.get_me()
        self.assertIsNone(caught.exception.code)
        self.assertFalse(caught.exception.fatal)

    def test_an_answer_that_is_not_json(self) -> None:
        # A proxy or captive portal answering with a web page.
        self.server.handlers["getMe"] = lambda _p: (200, b"<html>login required</html>")
        with self.assertRaises(TelegramError) as caught:
            self.client.get_me()
        self.assertIn("not JSON", caught.exception.description)

    def test_json_that_is_not_an_object(self) -> None:
        self.server.handlers["getMe"] = lambda _p: (200, b"[1, 2]")
        with self.assertRaises(TelegramError) as caught:
            self.client.get_me()
        self.assertIn("unexpected", caught.exception.description)

    def test_a_server_error_without_a_body(self) -> None:
        self.server.handlers["getMe"] = lambda _p: (502, {})
        with self.assertRaises(TelegramError) as caught:
            self.client.get_me()
        self.assertFalse(caught.exception.fatal)


class ConstructionTest(unittest.TestCase):
    def test_a_token_is_required(self) -> None:
        for token in ("", "   "):
            with self.assertRaises(ValueError):
                TelegramClient(token)

    def test_the_repr_hides_the_token(self) -> None:
        self.assertNotIn(TOKEN, repr(TelegramClient(TOKEN)))

    def test_the_token_is_trimmed(self) -> None:
        # A token copied from a .env file often has a trailing newline.
        with FakeTelegram() as server:
            self.assertEqual(
                TelegramClient(f"  {TOKEN}\n", api_base=server.api_base, timeout=5).get_me()["id"], 1)


if __name__ == "__main__":
    unittest.main()
