"""Evidence redaction must neither leak credentials nor corrupt the envelope."""

from __future__ import annotations

import os
import subprocess
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
                # The instruction is a secret too, with the same floor: a
                # two-letter assignment must not rewrite "status".
                self.assertEqual(
                    local_orchestrator._sanitize(ENVELOPE, value), ENVELOPE
                )
        self.assertEqual(sensitive_values({"ADV_API_KEY": "abcdef"}), ("abcdef",))
        self.assertEqual(
            sensitive_values({"ADV_TOKEN": "Bearer abcdef"}),
            ("Bearer abcdef", "abcdef"),
        )

    def test_redaction_is_a_fixed_point_at_both_boundaries(self):
        text = ("token=redact secret=s3cr3t-value " * 20) + ENVELOPE
        for boundary in (redact_text, local_orchestrator._sanitize):
            with (
                self.subTest(boundary=boundary.__name__),
                patch.dict(
                    os.environ, {"X_TOKEN": "redact", "Y_SECRET": "s3cr3t-value"}
                ),
            ):
                once = boundary(text)
                self.assertNotIn("s3cr3t-value", once)
                # Both boundaries redact several times on the way to evidence
                # and to Telegram; the marker must not feed the next pass.
                self.assertEqual(boundary(once), once)
                self.assertEqual(boundary(boundary(once)), once)
                self.assertLessEqual(len(once), len(text) + 64)

    def test_case_sensitive_prefixes_leave_prose_alone(self):
        # AWS ids and HTTP Basic are upper-case shapes; lower-case prose that
        # happens to share the letters is evidence, not a credential.
        prose = "akiafixture012345678 uses basic authenticationflowcontrol here"
        for boundary in (redact_text, local_orchestrator._sanitize):
            with self.subTest(boundary=boundary.__name__):
                self.assertEqual(boundary(prose), prose)

    def test_common_credential_shapes_are_redacted_by_both_boundaries(self):
        for name, (sample, marker) in SHAPED.items():
            for boundary in (redact_text, local_orchestrator._sanitize):
                with self.subTest(shape=name, boundary=boundary.__name__):
                    cleaned = boundary(f"worker said: {sample} and moved on")
                    self.assertNotIn(marker, cleaned)
                    self.assertIn("worker said:", cleaned)

    def test_email_addresses_are_ordinary_data(self):
        # Live 2026-09-27: the model's fill_input("#login", "<email>") reached
        # Hermes as "[redacted]      " and the Odoo login failed for hours.
        # Only credentials are redacted; an address is not one.
        call = 'fill_input("#login", "owner@example.test")'
        for boundary in (redact_text, local_orchestrator._sanitize):
            with self.subTest(boundary=boundary.__name__):
                self.assertEqual(boundary(call), call)

    def test_a_password_inside_a_url_is_still_redacted(self):
        # Grok review 2026-09-27: the email shape used to catch this by luck.
        url = "https://admin:S3cretPass@odoo.example.com/web"
        for boundary in (redact_text, local_orchestrator._sanitize):
            with self.subTest(boundary=boundary.__name__):
                cleaned = boundary(url)
                self.assertNotIn("S3cretPass", cleaned)
                self.assertIn("odoo.example.com/web", cleaned)

    def test_git_revisions_survive_redaction(self):
        # Evidence carries commit hashes; hex heuristics would erase them.
        for boundary in (redact_text, local_orchestrator._sanitize):
            with self.subTest(boundary=boundary.__name__):
                self.assertIn(REVISION, boundary(f"revision {REVISION} verified"))

    def test_an_adversarial_single_line_redacts_in_linear_time(self):
        # A minified blob with no "@" used to backtrack from every word
        # boundary (about 20 min per MB), past the 600 s stage lease, which
        # then skipped the health record that re-arms a quarantine. The regex
        # holds the GIL, so the bound is enforced on a child process.
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import local_orchestrator; from subscriptions.redaction import redact_text; "
            "blob = 'a.' * 100_000; "
            "assert redact_text(blob) == blob; "
            "assert local_orchestrator._sanitize(blob) == blob; print('linear')"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(ROOT / "plugins/goncloud-team")],
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "PYTHONPATH": str(
                        Path.home() / "AppData/Local/hermes/hermes-agent"
                    ),
                },
            )
        except subprocess.TimeoutExpired:
            self.fail("redaction of a 200 KB single line did not finish in 10 s")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "linear")

    def test_the_orchestrator_shares_the_router_patterns(self):
        # Two copies drifted once (ghp_/xai- reached Telegram); one source now.
        self.assertIs(local_orchestrator._SENSITIVE, redaction.SENSITIVE_TEXT)


if __name__ == "__main__":
    unittest.main()
