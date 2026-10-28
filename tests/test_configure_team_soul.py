"""An update refreshes the managed SOUL body without adopting local additions."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ManagedSoulUpdate(unittest.TestCase):
    def test_refresh_preserves_local_prefix_and_custom_body(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "hermes"
            command = [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home", str(home),
                "--install-dir", str(ROOT / "tests/fixtures/hermes_stub"),
            ]
            env = {**os.environ, "GONCLOUD_DIRECTOR_PLUGINS": str(
                ROOT / "tests/fixtures/director-plugins-vacio.json")}

            def deploy(*extra):
                result = subprocess.run(command + list(extra), env=env,
                                        capture_output=True, text=True, timeout=120,
                                        check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

            deploy()
            soul = home / "profiles/claw/SOUL.md"
            record = home / "profiles/claw/goncloud-managed.json"
            expected_body = soul.read_text(encoding="utf-8")
            state = json.loads(record.read_text(encoding="utf-8"))
            old_body = "Retired managed instruction.\n" + expected_body
            prefix = "Keep this local director note.\n\n"
            state["files"]["SOUL.md"] = old_body
            record.write_text(json.dumps(state), encoding="utf-8")
            previous_record = record.read_bytes()
            soul.write_bytes((prefix + old_body).replace("\n", "\r\n").encode())

            deploy("--preserve-local-soul")
            self.assertEqual(soul.read_text(encoding="utf-8"), prefix + expected_body)
            self.assertEqual(json.loads(record.read_text(encoding="utf-8"))[
                "files"]["SOUL.md"], expected_body)
            before = (soul.read_bytes(), record.read_bytes())
            deploy("--preserve-local-soul")
            self.assertEqual((soul.read_bytes(), record.read_bytes()), before)

            record.write_bytes(previous_record)
            deploy("--preserve-local-soul")
            self.assertEqual((soul.read_bytes(), record.read_bytes()), before)

            custom = prefix + "Locally rewritten body.\n"
            soul.write_text(custom, encoding="utf-8")
            deploy("--preserve-local-soul")
            self.assertEqual(soul.read_text(encoding="utf-8"), custom)
            self.assertEqual(record.read_bytes(), before[1])


if __name__ == "__main__":
    unittest.main()
