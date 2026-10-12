"""Every variable the docker README offers must reach the bot container.

Found on 2026-10-07 while writing the remote-Linux guide: ``docker/.env`` only feeds the
``${...}`` substitutions of docker-compose.yml, so a variable that the compose file does not
list never reaches the container. ``YTDLP_BOT_MAX_PARALLEL`` and five more were documented in
docker/README.md but not passed on: set in ``docker/.env`` they were silently ignored.

Docker is not installed on the development PC, so this reads the two files as text; it does not
replace ``docker compose config`` on a machine that has docker.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

DOCKER = Path(__file__).resolve().parent.parent / "docker"

# Offered in the README table but deliberately not listed in the compose file:
# the default (``cookies.txt`` in the working directory) is the file compose mounts at /app.
NOT_PASSED_ON = {"YTDLP_COOKIE_FILE"}


def readme_variables() -> set[str]:
    text = (DOCKER / "README.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `(YTDLP_[A-Z_]+)`", text, flags=re.MULTILINE))


def compose_environment() -> dict[str, str]:
    """The ``environment:`` entries of the downloader service: ``NAME: value`` lines."""
    text = (DOCKER / "docker-compose.yml").read_text(encoding="utf-8")
    entries = re.findall(r"^\s+(YTDLP_[A-Z_]+):\s*(.*)$", text, flags=re.MULTILINE)
    return dict(entries)


class ComposeEnvironmentTest(unittest.TestCase):
    def test_the_readme_table_is_read(self) -> None:
        names = readme_variables()
        self.assertIn("YTDLP_BOT_MAX_PARALLEL", names)
        self.assertGreaterEqual(len(names), 12)

    def test_every_documented_variable_is_passed_to_the_container(self) -> None:
        missing = readme_variables() - set(compose_environment()) - NOT_PASSED_ON
        self.assertEqual(missing, set(), "documented but never reaches the bot container")

    def test_the_optional_ones_default_to_empty_so_the_bot_default_applies(self) -> None:
        env = compose_environment()
        for name in ("YTDLP_BOT_MAX_PARALLEL", "YTDLP_BOT_MAX_QUEUED", "YTDLP_BOT_ALLOWED_HOSTS",
                     "YTDLP_AUDIO_LANG", "YTDLP_FRAGMENT_RETRIES", "YTDLP_STALL_MINUTES"):
            with self.subTest(name):
                self.assertEqual(env[name], '"${%s:-}"' % name)

    def test_the_two_required_ones_still_stop_compose_when_missing(self) -> None:
        env = compose_environment()
        for name in ("YTDLP_BOT_TOKEN", "YTDLP_BOT_ALLOWED_CHATS"):
            self.assertIn("${%s:?" % name, env[name])

    def test_the_run_logs_are_kept_on_the_host(self) -> None:
        # Found the same day: the logs are the evidence of every failure analysis, and in the
        # container they were lost whenever it was recreated. The path is where the app writes
        # them for root (the image's user): ~/.ytdlp-downloader/logs.
        text = (DOCKER / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^\s+- \./data/logs:/root/\.ytdlp-downloader/logs\s*$")
        self.assertIn("docker/data/logs", (DOCKER / "README.md").read_text(encoding="utf-8"))

    def test_an_empty_value_is_the_default_for_the_bot(self) -> None:
        # What the compose defaults rely on: the bot reads an empty variable as unset.
        import os
        from unittest import mock

        from interfaces import bot

        empty = {name: "" for name in (
            "YTDLP_BOT_MAX_PARALLEL", "YTDLP_BOT_MAX_QUEUED", "YTDLP_BOT_ALLOWED_HOSTS",
            "YTDLP_AUDIO_LANG", "YTDLP_FRAGMENT_RETRIES", "YTDLP_STALL_MINUTES")}
        clean = {k: v for k, v in os.environ.items() if not k.startswith("YTDLP_")}
        with mock.patch.dict(os.environ, {**clean, **empty, "YTDLP_AUTH_METHOD": "none"}, clear=True):
            config = bot.config_from_env()
        reference_env = {k: v for k, v in clean.items()}
        with mock.patch.dict(os.environ, {**reference_env, "YTDLP_AUTH_METHOD": "none"}, clear=True):
            reference = bot.config_from_env()
        for attr in ("max_parallel", "max_queued", "allowed_hosts", "audio_lang",
                     "fragment_retries", "stall_minutes"):
            self.assertEqual(getattr(config, attr), getattr(reference, attr), attr)


if __name__ == "__main__":
    unittest.main()
