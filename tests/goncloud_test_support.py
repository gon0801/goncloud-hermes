"""Shared test utilities, independent of upstream's unrelated tests package."""

import os
import time


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
