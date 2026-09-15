"""Evidence redaction must neither leak credentials nor corrupt the envelope."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path.home() / "AppData/Local/hermes/hermes-agent"))
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))

import local_orchestrator  # noqa: E402
from subscriptions import redaction  # noqa: E402
from subscriptions.redaction import redact_text, sensitive_values  # noqa: E402

ENVELOPE = '{"status":"completed","summary":"stage done","evidence":["a b c"]}'
REVISION = "3f86ed75dad1933036c52018e991dbd839837126"

# Synthetic shapes only; none of these is a real credential.
SHAPED = {
    "slack bot": (
        "xoxb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
        "AbCdEfGhIjKlMnOpQrStUvWx",
    ),
    "slack user": (
        "xoxp-123456789012-1234567890123-fixture0123456789",
        "fixture0123456789",
    ),
    "google api": (
        "AIzaSyA-fixture0123456789abcdefghijklmnopq",
        "fixture0123456789abcdefghijklmnopq",
    ),
    "gitlab pat": ("glpat-fixture0123456789abcd", "fixture0123456789abcd"),
    "aws access": ("AKIAFIXTURE012345678", "FIXTURE012345678"),
    "basic auth": (
        "Authorization: Basic dXNlcjpmaXh0dXJlLXBhc3N3b3Jk",
        "dXNlcjpmaXh0dXJlLXBhc3N3b3Jk",
    ),
    "url token": (
        "https://example.test/hook?token=fixture-token-value&x=1",
        "fixture-token-value",
    ),
    "url key": (
        "https://example.test/api?x=1&api_key=fixture-key-value",
        "fixture-key-value",
    ),
    "github pat": (
        "ghp_fixture0123456789abcdefghijklmnopqrstuv",
        "fixture0123456789abcdefghijklmnopqrstuv",
    ),
    "xai": ("xai-fixture0123456789", "fixture0123456789"),
}


class RedactionTests(unittest.TestCase):
    def test_short_values_under_sensitive_keys_never_corrupt_evidence(self):
        # A one-to-five character "credential" is a switch or a placeholder;
        # replacing it would rewrite JSON keys and break the terminal envelope.
        for value in ("a", "us", "sta", "state", "123"):
            with self.subTest(value=value):
                self.assertEqual(sensitive_values({"ADV_API_KEY": value}), ())
                with patch.dict(os.environ, {"ADV_API_KEY": value}):
                    self.assertEqual(redact_text(ENVELOPE), ENVELOPE)
                    self.assertEqual(local_orchestrator._sanitize(ENVELOPE), ENVELOPE)
        self.assertEqual(sensitive_values({"ADV_API_KEY": "abcdef"}), ("abcdef",))
        self.assertEqual(
            sensitive_values({"ADV_TOKEN": "Bearer abcdef"}),
            ("Bearer abcdef", "abcdef"),
        )

    def test_redaction_is_a_fixed_point(self):
        text = ("token=redact secret=s3cr3t-value " * 20) + ENVELOPE
        with patch.dict(os.environ, {"X_TOKEN": "redact", "Y_SECRET": "s3cr3t-value"}):
            once = redact_text(text)
            self.assertNotIn("s3cr3t-value", once)
            # The pipeline redacts several times on the way to evidence; the
            # marker must not feed the next pass or the text grows without end.
            self.assertEqual(redact_text(once), once)
            self.assertEqual(redact_text(redact_text(once)), once)
            self.assertLessEqual(len(once), len(text) + 64)

    def test_common_credential_shapes_are_redacted_by_both_boundaries(self):
        for name, (sample, marker) in SHAPED.items():
            for boundary in (redact_text, local_orchestrator._sanitize):
                with self.subTest(shape=name, boundary=boundary.__name__):
                    cleaned = boundary(f"worker said: {sample} and moved on")
                    self.assertNotIn(marker, cleaned)
                    self.assertIn("worker said:", cleaned)

    def test_git_revisions_survive_redaction(self):
        # Evidence carries commit hashes; hex heuristics would erase them.
        for boundary in (redact_text, local_orchestrator._sanitize):
            with self.subTest(boundary=boundary.__name__):
                self.assertIn(REVISION, boundary(f"revision {REVISION} verified"))

    def test_the_orchestrator_shares_the_router_patterns(self):
        # Two copies drifted once (ghp_/xai- reached Telegram); one source now.
        self.assertIs(local_orchestrator._SENSITIVE, redaction.SENSITIVE_TEXT)


if __name__ == "__main__":
    unittest.main()
