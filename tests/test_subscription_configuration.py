from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "configure_subscriptions", ROOT / "scripts/configure_subscriptions.py"
)
module = importlib.util.module_from_spec(SPEC)
sys.modules["configure_subscriptions"] = module
SPEC.loader.exec_module(module)
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))
from subscriptions.types import AdapterHealth, FailureKind, InvocationResult  # noqa: E402
from subscriptions.process import ProcessEvidence  # noqa: E402
from tests.test_subscription_router import cleanup_temp_directory  # noqa: E402

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
    def probe(name, value):
        adapter = "deepseek" if name == "DEEPSEEK_API_KEY" else "zai"
        return InvocationResult(
            True,
            "GONCLOUD_PROBE_OK",
            adapter,
            "fixture-model",
            "api" if adapter == "deepseek" else "subscription",
        )

    return probe


def failing_probe():
    def probe(name, value):
        raise MigrationError("replacement probe failed")

    return probe


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
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

    def test_unverified_evidence_never_cleans_launcher_or_changes_store(self):
        launcher = seeded_deepseek_launcher(self.root)
        before = launcher.read_bytes()
        target = private_env(self.root)
        original_store = b"# previous store\r\nOTHER_KEY=preserved\r\n"
        target.write_bytes(original_store)
        evidence = [None, True, "GONCLOUD_PROBE_OK", AdapterHealth(True, "present")]
        for text in ("", "READY", " GONCLOUD_PROBE_OK", "GONCLOUD_PROBE_OK\nextra"):
            evidence.append(InvocationResult(True, text, "deepseek", "fixture", "api"))
        for failure in FailureKind:
            evidence.append(
                InvocationResult(
                    False,
                    "GONCLOUD_PROBE_OK",
                    "deepseek",
                    "fixture",
                    "api",
                    failure=failure,
                )
            )
        for result in evidence:
            with self.subTest(evidence=type(result).__name__):
                with self.assertRaisesRegex(
                    MigrationError, "verified live probe required"
                ):
                    migrate_one(
                        launcher,
                        target,
                        lambda *_: result,
                        secret_name="DEEPSEEK_API_KEY",
                    )
                self.assertEqual(launcher.read_bytes(), before)
                self.assertEqual(target.read_bytes(), original_store)

    def test_no_probe_rejects_before_writing_anything(self):
        launcher = seeded_deepseek_launcher(self.root)
        with self.assertRaisesRegex(MigrationError, "verified live probe required"):
            migrate_one(
                launcher, private_env(self.root), None, secret_name="DEEPSEEK_API_KEY"
            )
        self.assertFalse(private_env(self.root).exists())

    def test_cli_requires_verify_before_accessing_launchers(self):
        with (
            patch.object(sys, "argv", ["configure", "--migrate-launchers"]),
            patch.object(module, "migrate_launcher_secrets") as migrate,
            redirect_stdout(io.StringIO()),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            with self.assertRaises(SystemExit) as caught:
                module.main()
        self.assertEqual(caught.exception.code, 2)
        migrate.assert_not_called()

    def test_second_probe_failure_rolls_back_both_launchers_and_store(self):
        glm, deepseek = seeded_launchers(self.root)
        originals = [p.read_bytes() for p in (glm, deepseek)]

        def probe(name, value):
            if name == "GLM_API_KEY":
                raise TimeoutError("sk-sensitive owner@example.test C:/private")
            return passing_probe()(name, value)

        with self.assertRaises(MigrationError) as caught:
            migrate_launcher_secrets(glm, deepseek, private_env(self.root), probe)
        self.assertEqual([p.read_bytes() for p in (glm, deepseek)], originals)
        self.assertFalse(private_env(self.root).exists())
        self.assertNotIn("sk-sensitive", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_success_preserves_crlf_and_backs_up_current_bytes(self):
        launcher = seeded_deepseek_launcher(self.root)
        before = launcher.read_bytes()
        launcher.with_name(launcher.name + ".goncloud-backup").write_bytes(b"stale")
        result = migrate_one(
            launcher,
            private_env(self.root),
            passing_probe(),
            secret_name="DEEPSEEK_API_KEY",
        )
        self.assertEqual(
            launcher.read_bytes(),
            before.replace(
                f'set "ANTHROPIC_AUTH_TOKEN={DEEPSEEK_SECRET}"'.encode(),
                b"rem migrated to the private Hermes subscription store",
            ),
        )
        self.assertIn(before, [backup.read_bytes() for backup in result.backups])

    def test_cleanup_replace_failure_restores_bytes_and_keeps_recovery(self):
        glm, deepseek = seeded_launchers(self.root)
        originals = {p: p.read_bytes() for p in (glm, deepseek)}
        actual_replace = os.replace
        failed = False

        def replace(source, target):
            nonlocal failed
            if Path(target) == glm and not failed:
                failed = True
                raise OSError("private failure owner@example.test")
            return actual_replace(source, target)

        with patch.object(module.os, "replace", side_effect=replace):
            with self.assertRaises(MigrationError):
                migrate_launcher_secrets(
                    glm, deepseek, private_env(self.root), passing_probe()
                )
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertFalse(private_env(self.root).exists())

    def test_private_env_rejects_newline_injection(self):
        target = private_env(self.root)
        with self.assertRaises(MigrationError):
            module.write_private_env(target, {"GLM_API_KEY": "fixture\nOTHER=bad"})
        self.assertFalse(target.exists())

    def test_probe_exception_cannot_leak_through_migration_error(self):
        launcher = seeded_deepseek_launcher(self.root)

        def probe(*_args):
            raise MigrationError("sk-sensitive owner@example.test C:/private")

        with self.assertRaises(MigrationError) as caught:
            migrate_one(
                launcher, private_env(self.root), probe, secret_name="DEEPSEEK_API_KEY"
            )
        self.assertNotIn("sk-sensitive", str(caught.exception))
        self.assertNotIn("owner@example.test", str(caught.exception))

    def test_parallel_migrations_cannot_replace_store_under_pending_proof(self):
        glm, deepseek = seeded_launchers(self.root)
        entered, release = threading.Event(), threading.Event()

        def probe(name, value):
            entered.set()
            self.assertTrue(release.wait(10))
            return passing_probe()(name, value)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                migrate_one,
                deepseek,
                private_env(self.root),
                probe,
                secret_name="DEEPSEEK_API_KEY",
            )
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(MigrationError):
                    migrate_one(
                        glm,
                        private_env(self.root),
                        passing_probe(),
                        secret_name="GLM_API_KEY",
                    )
            finally:
                release.set()
            future.result(timeout=10)
        self.assertIn(GLM_SECRET, glm.read_text())
        self.assertNotIn(DEEPSEEK_SECRET, deepseek.read_text())

    def test_process_crash_during_cleanup_keeps_proven_store_and_exact_backups(self):
        glm, deepseek = seeded_launchers(self.root)
        originals = {p.name: p.read_bytes() for p in (glm, deepseek)}
        code = """
import os, sys
from pathlib import Path
from tests.test_subscription_configuration import module, passing_probe
root = Path(sys.argv[1])
write = module._atomic_write
def crash_after_write(path, data):
    write(path, data)
    if path == root / "deepseek.cmd":
        os._exit(73)
module._atomic_write = crash_after_write
module.migrate_launcher_secrets(root / "glm-code.ps1", root / "deepseek.cmd", root / "subscription-router.env", passing_probe())
"""
        result = subprocess.run(
            [sys.executable, "-", str(self.root)],
            input=code,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 73, result.stderr)
        stored = module.read_private_env(private_env(self.root))
        self.assertEqual(stored["DEEPSEEK_API_KEY"], DEEPSEEK_SECRET)
        self.assertEqual(stored["GLM_API_KEY"], GLM_SECRET)
        backups = [
            p.read_bytes()
            for p in self.root.glob("subscription-backup-*/*.goncloud-backup")
        ]
        for original in originals.values():
            self.assertIn(original, backups)
        self.assertEqual(glm.read_bytes(), originals[glm.name])
        migrate_launcher_secrets(glm, deepseek, private_env(self.root), passing_probe())
        self.assertNotIn(GLM_SECRET, glm.read_text())

    def test_atomic_write_failure_leaves_existing_store_and_no_temp_secrets(self):
        target = private_env(self.root)
        target.write_bytes(b"OTHER_KEY=preserved\n")
        with patch.object(module.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
        self.assertEqual(target.read_bytes(), b"OTHER_KEY=preserved\n")
        self.assertEqual(list(self.root.glob(".subscription-*")), [])

    def test_migration_preserves_store_edited_during_verification(self):
        launcher = seeded_deepseek_launcher(self.root)
        before = launcher.read_bytes()
        target = private_env(self.root)
        changed = b"OTHER_KEY=user-updated\n"

        def probe(name, value):
            target.write_bytes(changed)
            return passing_probe()(name, value)

        with self.assertRaises(MigrationError):
            migrate_one(launcher, target, probe, secret_name="DEEPSEEK_API_KEY")
        self.assertTrue(target.exists(), "rollback must preserve the user's new store")
        self.assertEqual(target.read_bytes(), changed)
        self.assertEqual(launcher.read_bytes(), before)

    def test_private_store_cannot_alias_sidecar_launcher(self):
        launcher = self.root / "glm.ps1"
        original = b"$env:ANTHROPIC_AUTH_TOKEN = $key\n"
        launcher.write_bytes(original)
        sidecar = self.root / "apikey.txt"
        sidecar.write_text(GLM_SECRET)
        with patch.object(
            module, "write_private_env", wraps=module.write_private_env
        ) as write:
            with self.assertRaises(MigrationError):
                migrate_one(
                    launcher,
                    launcher,
                    passing_probe(),
                    secret_name="GLM_API_KEY",
                    sidecar=sidecar,
                )
            write.assert_not_called()
        self.assertEqual(launcher.read_bytes(), original)
        self.assertEqual(sidecar.read_text(), GLM_SECRET)

    def test_private_store_and_backups_are_rejected_inside_any_git_checkout(self):
        launcher = seeded_deepseek_launcher(self.root)
        repo = self.root / "other-checkout"
        subprocess.run(
            ["git", "init", str(repo)], check=True, capture_output=True, timeout=10
        )
        target = repo / "private" / "router.env"
        with self.assertRaises(MigrationError):
            migrate_one(
                launcher, target, passing_probe(), secret_name="DEEPSEEK_API_KEY"
            )
        self.assertFalse(target.parent.exists())


class LiveEvidenceTests(unittest.TestCase):
    def test_real_probe_uses_persisted_fixtures_and_table_models_over_local_http(self):
        from tests.test_subscription_vendor_adapters import (
            FakeEndpoint,
            zai_ok_body,
            deepseek_ok_body,
        )

        with (
            tempfile.TemporaryDirectory() as tmp,
            FakeEndpoint(body=zai_ok_body("GONCLOUD_PROBE_OK")) as zai,
            FakeEndpoint(body=deepseek_ok_body("GONCLOUD_PROBE_OK")) as deepseek,
        ):
            target = Path(tmp) / "fixture.env"
            module.write_private_env(
                target,
                {
                    "GLM_API_KEY": GLM_SECRET,
                    "GLM_BASE_URL": zai.url,
                    "DEEPSEEK_API_KEY": DEEPSEEK_SECRET,
                    "DEEPSEEK_BASE_URL": deepseek.url,
                },
            )
            probe = module.build_real_probe(target)
            z_result = probe("GLM_API_KEY", GLM_SECRET)
            d_result = probe("DEEPSEEK_API_KEY", DEEPSEEK_SECRET)
            self.assertEqual(
                module.sanitize_live_result("zai", z_result),
                {
                    "adapter": "zai",
                    "status": "ready",
                    "failure": None,
                    "billing_mode": "subscription",
                },
            )
            self.assertEqual(
                module.sanitize_live_result("deepseek", d_result),
                {
                    "adapter": "deepseek",
                    "status": "ready",
                    "failure": None,
                    "billing_mode": "api",
                },
            )
            self.assertEqual(zai.requests[0]["authorization"], f"Bearer {GLM_SECRET}")
            self.assertEqual(
                deepseek.requests[0]["authorization"], f"Bearer {DEEPSEEK_SECRET}"
            )
            self.assertEqual(json.loads(zai.requests[0]["body"])["model"], "glm-5.3")
            self.assertEqual(
                json.loads(deepseek.requests[0]["body"])["model"], "deepseek-flash"
            )
            with self.assertRaises(MigrationError):
                probe("GLM_API_KEY", "different-fixture")
            self.assertEqual(len(zai.requests), 1)

    def test_live_matrix_uses_one_http_inference_per_family_and_private_store(self):
        from tests.test_subscription_vendor_adapters import (
            FakeEndpoint,
            zai_ok_body,
            deepseek_ok_body,
        )
        from subscriptions.adapters.zai import ZAIAdapter
        from subscriptions.adapters.deepseek import DeepSeekAdapter

        def registry(**kwargs):
            return {
                "zai": ZAIAdapter(
                    auth_token=kwargs["zai_auth_token"], base_url=kwargs["zai_base_url"]
                ),
                "deepseek": DeepSeekAdapter(
                    api_key=kwargs["deepseek_api_key"],
                    base_url=kwargs["deepseek_base_url"],
                ),
            }

        with (
            tempfile.TemporaryDirectory() as tmp,
            FakeEndpoint(body=zai_ok_body("GONCLOUD_PROBE_OK")) as zai,
            FakeEndpoint(
                status=402, body=deepseek_ok_body("sk-sensitive owner@example.test")
            ) as deepseek,
            patch(
                "subscriptions.adapters.build_adapter_registry", side_effect=registry
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            target = Path(tmp) / "fixture.env"
            module.write_private_env(
                target,
                {
                    "GLM_API_KEY": GLM_SECRET,
                    "GLM_BASE_URL": zai.url,
                    "DEEPSEEK_API_KEY": DEEPSEEK_SECRET,
                    "DEEPSEEK_BASE_URL": deepseek.url,
                },
            )
            result = module.run_live_probe_all(only="deepseek,zai", private_env=target)
        self.assertEqual(
            result["results"],
            [
                {
                    "adapter": "zai",
                    "status": "ready",
                    "failure": None,
                    "billing_mode": "subscription",
                },
                {
                    "adapter": "deepseek",
                    "status": "quarantined",
                    "failure": "quota",
                    "billing_mode": "api",
                },
            ],
        )
        self.assertEqual(len(zai.requests), 1)
        self.assertEqual(len(deepseek.requests), 1)
        self.assertNotIn("sk-sensitive", output.getvalue())
        self.assertEqual(result["passed"], 1)

    def test_sanitizer_allowlists_fields_and_never_preserves_raw_identity(self):
        raw = "sk-sensitive owner@example.test C:/private stdout stderr"
        cases = [
            (raw, "failed", "task_failure"),
            (ProcessEvidence(0, "GONCLOUD_PROBE_OK", raw, 0.1), "ready", None),
            (ProcessEvidence(1, raw, "401 " + raw, 0.1), "quarantined", "auth"),
            (
                InvocationResult(True, raw, "zai", raw, "api", session_id=raw),
                "failed",
                "task_failure",
            ),
            (AdapterHealth(True, raw, detail=raw), "failed", "task_failure"),
        ]
        for evidence, status, failure in cases:
            result = module.sanitize_live_result("zai", evidence)
            self.assertEqual(
                result,
                {
                    "adapter": "zai",
                    "status": status,
                    "failure": failure,
                    "billing_mode": "subscription",
                },
            )
            self.assertNotIn("sk-sensitive", repr(result))
        self.assertEqual(
            module.sanitize_live_result("deepseek", "GONCLOUD_PROBE_OK")[
                "billing_mode"
            ],
            "api",
        )
        self.assertNotIn(raw, repr(module.sanitize_live_result(raw, raw)))

    def test_live_matrix_requires_exact_marker_and_redacts_console_and_json(self):
        raw = "sk-sensitive owner@example.test C:/private"

        class Adapter:
            billing_mode = "subscription"
            timeout_seconds = 300
            probe_timeout_seconds = 30

            def probe(self, route):
                return AdapterHealth(True, raw, detail=raw)

            def invoke(self, request, route):
                self.request = request
                return InvocationResult(
                    True, raw, route.adapter, raw, "api", session_id=raw
                )

        adapter = Adapter()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module, "read_private_env", return_value={}),
            patch(
                "subscriptions.adapters.build_adapter_registry",
                return_value={"codex": adapter},
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            target = Path(tmp) / "report.json"
            result = module.run_live_probe_all(target, only="codex")
            serialized = target.read_text()
        expected = {
            "adapter": "codex",
            "status": "failed",
            "failure": "task_failure",
            "billing_mode": "subscription",
        }
        self.assertEqual(result["results"], [expected])
        self.assertEqual(result["passed"], 0)
        self.assertIn("GONCLOUD_PROBE_OK", adapter.request.prompt)
        self.assertEqual(adapter.request.max_steps, 1)
        for channel in (repr(result), serialized, output.getvalue()):
            self.assertNotIn(raw, channel)
            self.assertNotIn("sk-sensitive", channel)


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
