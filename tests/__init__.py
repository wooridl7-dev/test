"""Keep the test run away from the user's real run-log folder.

``run_download`` writes a log file per run (core.run_log); tests that call
it would otherwise fill ``~/.ytdlp-downloader/logs``.
"""

import atexit
import os
import shutil
import tempfile

if "YTDLP_LOG_DIR" not in os.environ:
    _LOG_DIR = tempfile.mkdtemp(prefix="ytdlp-test-logs-")
    os.environ["YTDLP_LOG_DIR"] = _LOG_DIR
    atexit.register(shutil.rmtree, _LOG_DIR, ignore_errors=True)

# Likewise the cookie health record (core.cookie_health).
if "YTDLP_COOKIE_HEALTH_FILE" not in os.environ:
    _HEALTH_DIR = tempfile.mkdtemp(prefix="ytdlp-test-health-")
    os.environ["YTDLP_COOKIE_HEALTH_FILE"] = os.path.join(_HEALTH_DIR, "cookie_health.json")
    atexit.register(shutil.rmtree, _HEALTH_DIR, ignore_errors=True)
