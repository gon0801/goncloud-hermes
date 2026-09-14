from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "configure_subscriptions", ROOT / "scripts/configure_subscriptions.py"
)
module = importlib.util.module_from_spec(SPEC)
sys.modules["configure_subscriptions"] = module
SPEC.loader.exec_module(module)

MigrationError = module.MigrationError
migrate_launcher_secrets = module.migrate_launcher_secrets
migrate_one = module.migrate_one

DEEPSEEK_SECRET = "sk-fixture-deepseek-abcdef"
GLM_SECRET = "fixture-glm-token-123456"


def seeded_launchers(tmp_path: Path):
    deepseek = tmp_path / "deepseek.cmd"
    deepseek.write_text(
        "@echo off\r\n"
        'set "ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic"\r\n'
        f'set "ANTHROPIC_AUTH_TOKEN={DEEPSEEK_SECRET}"\r\n'
        "claude --model deepseek-v4-pro %*\r\n",
        encoding="utf-8",
        newline="",
    )
    glm = tmp_path / "glm-code.ps1"
    glm.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$env:ANTHROPIC_AUTH_TOKEN = '{GLM_SECRET}'\n"
        "$env:ANTHROPIC_BASE_URL = 'https://api.z.ai/api/anthropic'\n",
        encoding="utf-8",
    )
    return glm, deepseek


def seeded_deepseek_launcher(tmp_path: Path) -> Path:
    _glm, deepseek = seeded_launchers(tmp_path)
    return deepseek


def private_env(tmp_path: Path) -> Path:
    return tmp_path / "subscription-router.env"


def passing_probe():
    return lambda name, value: None


def failing_probe():
    def probe(name, value):
        raise MigrationError("replacement probe failed")

    return probe


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_migration_removes_plaintext_only_after_probe(self):
        glm, deepseek = seeded_launchers(self.root)
        stream = io.StringIO()

        with redirect_stdout(stream):
            result = migrate_launcher_secrets(
                glm, deepseek, private_env(self.root), passing_probe()
            )

        self.assertEqual(result.names, ("DEEPSEEK_API_KEY", "GLM_API_KEY"))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN=", glm.read_text(encoding="utf-8"))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN=", deepseek.read_text(encoding="utf-8"))
        printed = stream.getvalue()
        self.assertNotIn(DEEPSEEK_SECRET, printed)
        self.assertNotIn(GLM_SECRET, printed)
        self.assertNotIn(DEEPSEEK_SECRET[:10], printed)
        self.assertNotIn(GLM_SECRET[:10], printed)
        stored = private_env(self.root).read_text(encoding="utf-8")
        self.assertIn("DEEPSEEK_API_KEY=", stored)
        self.assertIn("GLM_API_KEY=", stored)
        self.assertIn(DEEPSEEK_SECRET, stored)
        self.assertIn("GLM_BASE_URL=https://api.z.ai/api/anthropic", stored)

    def test_failed_probe_preserves_launcher(self):
        launcher = seeded_deepseek_launcher(self.root)
        before = launcher.read_bytes()

        with self.assertRaises(MigrationError):
            migrate_one(
                launcher,
                private_env(self.root),
                failing_probe(),
                secret_name="DEEPSEEK_API_KEY",
            )

        self.assertEqual(launcher.read_bytes(), before)

    def test_recoverable_backup_is_kept_outside_the_launchers(self):
        glm, deepseek = seeded_launchers(self.root)

        result = migrate_launcher_secrets(
            glm, deepseek, private_env(self.root), passing_probe()
        )

        self.assertTrue(result.backups)
        for backup in result.backups:
            self.assertTrue(backup.exists())
            original = backup.read_text(encoding="utf-8")
            self.assertIn("ANTHROPIC_AUTH_TOKEN", original)

    def test_apikey_sidecar_is_neutered_after_verified_migration(self):
        glm = self.root / "glm-code.ps1"
        glm.write_text(
            "$key = (Get-Content $KeyFile -Raw).Trim()\n"
            "$env:ANTHROPIC_AUTH_TOKEN = $key\n",
            encoding="utf-8",
        )
        sidecar = self.root / "apikey.txt"
        sidecar.write_text(GLM_SECRET + "\n", encoding="utf-8")

        result = migrate_one(
            glm,
            private_env(self.root),
            passing_probe(),
            secret_name="GLM_API_KEY",
            sidecar=sidecar,
        )

        self.assertEqual(result.names, ("GLM_API_KEY",))
        self.assertNotIn(GLM_SECRET, sidecar.read_text(encoding="utf-8"))
        self.assertIn("GLM_API_KEY", private_env(self.root).read_text(encoding="utf-8"))

    def test_missing_credential_is_reported_without_guessing(self):
        empty = self.root / "empty.cmd"
        empty.write_text("@echo off\r\n", encoding="utf-8", newline="")

        with self.assertRaisesRegex(MigrationError, "no credential"):
            migrate_one(
                empty,
                private_env(self.root),
                passing_probe(),
                secret_name="DEEPSEEK_API_KEY",
            )
        self.assertEqual(empty.read_bytes(), b"@echo off\r\n")

    def test_private_env_write_is_atomic_and_restricted(self):
        target = private_env(self.root)
        module.write_private_env(target, {"DEEPSEEK_API_KEY": DEEPSEEK_SECRET})
        first = target.read_text(encoding="utf-8")

        self.assertIn("DEEPSEEK_API_KEY=", first)
        self.assertFalse(target.with_suffix(".env.tmp").exists())
        # Rewriting replaces content instead of appending duplicates.
        module.write_private_env(
            target, {"DEEPSEEK_API_KEY": DEEPSEEK_SECRET, "GLM_API_KEY": GLM_SECRET}
        )
        second = target.read_text(encoding="utf-8")
        self.assertEqual(second.count("DEEPSEEK_API_KEY="), 1)
        self.assertIn("GLM_API_KEY=", second)


class DeploymentTests(unittest.TestCase):
    def test_configure_team_copies_subscriptions_and_routing_source(self):
        upstream = Path(
            os.environ.get(
                "HERMES_INSTALL_DIR",
                Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/hermes-agent",
            )
        )
        self.assertTrue(
            (upstream / "hermes_cli/profiles.py").is_file(),
            "Run tests with the installed Hermes runtime",
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "hermes-test"
            command = [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(runtime),
                "--install-dir",
                str(upstream),
            ]
            env = {**os.environ, "PYTHONUTF8": "1"}
            first = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=180
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            claw = runtime / "profiles" / "claw"
            self.assertTrue(
                (claw / "plugins/goncloud-team/subscriptions/router.py").is_file()
            )
            self.assertTrue(
                (
                    claw / "plugins/goncloud-team/subscriptions/adapters/deepseek.py"
                ).is_file()
            )
            routing = claw / "plugins/goncloud-team/model-routing.source.json"
            self.assertTrue(routing.is_file())
            self.assertEqual(
                json.loads(routing.read_text(encoding="utf-8"))["columns"],
                ["Primary", "F1", "F2", "F3", "F4", "F5", "F6", "F7"],
            )
            secrets = claw / "subscription-router.env"
            second = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=180
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(secrets.exists())
            self.assertTrue(routing.is_file())


if __name__ == "__main__":
    unittest.main()
