"""Shared test utilities, independent of upstream's unrelated tests package."""

import os
import time
from pathlib import Path

#: Every deploy under test reads this fake Muse login, never the owner's key.
FAKE_MUSE_KEY = "FAKE-MUSE-LOGIN-KEY"
os.environ["GONCLOUD_MUSE_LOGIN"] = str(
    Path(__file__).parent / "fixtures" / "muse-login.json"
)


def cleanup_temp_directory(directory):
    # Windows can briefly report a nonempty directory while file deletions
    # settle. Retry that specific error; persistent failures still fail the test.
    for attempt in range(20):
        try:
            directory.cleanup()
            return
        except OSError as error:
            if os.name != "nt" or error.winerror != 145 or attempt == 19:
                raise
            time.sleep(0.05)
