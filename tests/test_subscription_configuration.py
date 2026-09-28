from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "configure_subscriptions", ROOT / "scripts/configure_subscriptions.py"
)
module = importlib.util.module_from_spec(SPEC)
sys.modules["configure_subscriptions"] = module
SPEC.loader.exec_module(module)
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))
sys.path.insert(0, str(ROOT / "tests"))
from subscriptions.types import AdapterHealth, FailureKind, InvocationResult  # noqa: E402
from subscriptions.process import ProcessEvidence  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

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

    @unittest.skipUnless(os.name == "nt", "Windows extended path aliases")
    def test_extended_private_store_alias_of_sidecar_is_rejected_prewrite(self):
        launcher = self.root / "glm.ps1"
        launcher.write_bytes(b"$env:ANTHROPIC_AUTH_TOKEN = $key\n")
        sidecar = self.root / "apikey.txt"
        sidecar.write_text(GLM_SECRET)
        before = sidecar.read_bytes()
        alias = Path("\\\\?\\" + str(sidecar))
        with self.assertRaises(MigrationError):
            migrate_one(
                launcher,
                alias,
                passing_probe(),
                secret_name="GLM_API_KEY",
                sidecar=sidecar,
            )
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertEqual(list(self.root.glob("subscription-backup-*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows stable directory handles")
    def test_parent_swap_at_first_secret_write_cannot_redirect_into_git(self):
        repo = self.root / "checkout"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        destination = self.root / "private"
        destination.mkdir()
        target = destination / "store.env"
        actual_fdopen = os.fdopen
        attempts = []

        def swap_before_content(descriptor, *args, **kwargs):
            try:
                destination.rename(self.root / "displaced-private")
            except OSError:
                attempts.append("blocked")
            else:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(destination), str(repo)],
                    check=True,
                    capture_output=True,
                )
                attempts.append("redirected")
            return actual_fdopen(descriptor, *args, **kwargs)

        try:
            with patch.object(module.os, "fdopen", side_effect=swap_before_content):
                module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
            self.assertEqual(attempts, ["blocked"])
            self.assertFalse((repo / "store.env").exists())
            self.assertEqual(list(repo.glob(".subscription-*")), [])
        finally:
            if "redirected" in attempts:
                os.rmdir(destination)

    def test_external_identical_launcher_is_not_owned_by_content_digest(self):
        glm, deepseek = seeded_launchers(self.root)
        target = private_env(self.root)
        publish = module._publish_exclusive
        external_identity = []

        def replace_identical_then_fail(source, destination):
            if Path(destination) == glm and not external_identity:
                external = self.root / "external-launcher"
                external.write_bytes(deepseek.read_bytes())
                os.replace(external, deepseek)
                external_identity.append(deepseek.stat().st_ino)
                raise OSError("fixture interruption")
            return publish(source, destination)

        with patch.object(
            module, "_publish_exclusive", side_effect=replace_identical_then_fail
        ):
            with self.assertRaises(MigrationError):
                migrate_launcher_secrets(glm, deepseek, target, passing_probe())
        self.assertEqual(deepseek.stat().st_ino, external_identity[0])
        self.assertNotIn(DEEPSEEK_SECRET.encode(), deepseek.read_bytes())
        self.assertTrue(target.exists())
        self.assertTrue(module._journal_path(target).exists())

    def test_external_creation_in_empty_publication_slot_is_never_overwritten(self):
        target = private_env(self.root)
        target.write_bytes(b"OTHER=old\n")
        publish = module._publish_exclusive
        external = b"OTHER=external writer\n"

        def win_publication_slot(source, destination):
            if Path(destination) == target:
                target.write_bytes(external)
            return publish(source, destination)

        with patch.object(
            module, "_publish_exclusive", side_effect=win_publication_slot
        ):
            with self.assertRaises((MigrationError, OSError)):
                module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
        self.assertEqual(target.read_bytes(), external)
        self.assertTrue(
            any(
                p.read_bytes() == b"OTHER=old\n" for p in self.root.rglob("displaced-*")
            )
        )

    @unittest.skipUnless(os.name == "nt", "Windows CRT handle transfer")
    def test_fstat_failure_after_crt_transfer_closes_descriptor(self):
        from subscriptions import private_files

        actual_close = os.close
        closed = []

        def close(descriptor):
            closed.append(descriptor)
            actual_close(descriptor)

        with (
            patch.object(private_files.os, "fstat", side_effect=OSError("fixture")),
            patch.object(private_files.os, "close", side_effect=close),
        ):
            with self.assertRaises(OSError):
                private_files.open_private_file(self.root / "empty-private")
        self.assertEqual(len(closed), 1)

    def test_posix_directory_sync_flushes_directory_descriptor(self):
        from subscriptions import private_files

        with (
            patch.object(private_files.os, "name", "posix"),
            patch.object(private_files.os, "open", return_value=71) as opened,
            patch.object(private_files.os, "fsync") as synced,
            patch.object(private_files.os, "close") as closed,
        ):
            private_files.sync_directory(self.root)
        self.assertEqual(opened.call_count, 1)
        synced.assert_called_once_with(71)
        closed.assert_called_once_with(71)

    def test_unsupported_ownership_platform_fails_before_any_write(self):
        launcher = seeded_deepseek_launcher(self.root)
        target = private_env(self.root)
        before = set(self.root.iterdir())
        with patch.object(module.os, "name", "posix"):
            with self.assertRaises(MigrationError):
                module._migrate_batch(
                    [(launcher, "DEEPSEEK_API_KEY", None)], target, passing_probe()
                )
        self.assertEqual(set(self.root.iterdir()), before)

    @unittest.skipUnless(os.name == "nt", "Windows alias normalization")
    def test_extended_prefix_uses_same_canonical_lock(self):
        glm, deepseek = seeded_launchers(self.root)
        target = private_env(self.root)
        alias = Path("\\\\?\\" + str(target))
        with module._migration_lock(target):
            with self.assertRaisesRegex(MigrationError, "migration already running"):
                migrate_one(
                    deepseek, alias, passing_probe(), secret_name="DEEPSEEK_API_KEY"
                )
        self.assertEqual(list(self.root.glob("subscription-backup-*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows aliases")
    def test_trailing_and_device_aliases_are_rejected_prewrite(self):
        launcher = seeded_deepseek_launcher(self.root)
        for target in [
            Path(str(private_env(self.root)) + "."),
            Path(str(private_env(self.root)) + " "),
            Path("\\\\.\\" + str(private_env(self.root))),
            Path(str(private_env(self.root)) + ":stream"),
        ]:
            with self.subTest(alias_kind=target.suffix):
                with self.assertRaises(MigrationError):
                    migrate_one(
                        launcher,
                        target,
                        passing_probe(),
                        secret_name="DEEPSEEK_API_KEY",
                    )
        self.assertEqual(list(self.root.glob("subscription-backup-*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows relative aliases")
    def test_relative_components_are_validated_before_normalization_or_writes(self):
        original_directory = Path.cwd()
        try:
            os.chdir(self.root)
            for spelling in ("store.env.", "store.env "):
                with self.subTest(spelling=spelling):
                    with self.assertRaises(MigrationError):
                        module.write_private_env(
                            Path(spelling), {"GLM_API_KEY": GLM_SECRET}
                        )
                    self.assertEqual(list(self.root.iterdir()), [])
            for spelling in ("unsafe. /store.env", "NUL", "NUL/store.env"):
                with self.subTest(spelling=spelling):
                    with self.assertRaises(MigrationError):
                        module._checked_path(Path(spelling))
        finally:
            os.chdir(original_directory)

    @unittest.skipUnless(os.name == "nt", "Windows object ownership")
    def test_existing_writable_handle_prevents_public_object_mutation(self):
        launcher = seeded_deepseek_launcher(self.root)
        target = private_env(self.root)
        target.write_bytes(b"OTHER=original\n")
        with target.open("r+b") as external_handle:
            with self.assertRaises(MigrationError):
                migrate_one(
                    launcher, target, passing_probe(), secret_name="DEEPSEEK_API_KEY"
                )
            external_handle.seek(0)
            external_handle.write(b"OTHER=external\n")
            external_handle.truncate()
            external_handle.flush()
        self.assertEqual(target.read_bytes(), b"OTHER=external\n")
        self.assertIn(DEEPSEEK_SECRET, launcher.read_text())

    @unittest.skipUnless(os.name == "nt", "Windows rename by owned handle")
    def test_external_write_at_exact_owned_rename_boundary_is_denied(self):
        from subscriptions import private_files

        launcher = seeded_deepseek_launcher(self.root)
        target = private_env(self.root)
        target.write_bytes(b"OTHER=original\n")
        rename = private_files._rename_handle
        attempts = []

        def race_after_ownership(descriptor, destination, directory_descriptor):
            if Path(destination).name == "displaced-0":
                with self.assertRaises(OSError):
                    target.write_bytes(b"OTHER=external\n")
                attempts.append(True)
            return rename(descriptor, destination, directory_descriptor)

        with patch.object(
            private_files, "_rename_handle", side_effect=race_after_ownership
        ):
            migrate_one(
                launcher, target, passing_probe(), secret_name="DEEPSEEK_API_KEY"
            )
        self.assertEqual(attempts, [True])
        self.assertIn("DEEPSEEK_API_KEY", target.read_text())
        self.assertTrue(
            any(
                path.read_bytes() == b"OTHER=original\n"
                for path in self.root.rglob("displaced-0")
            )
        )

    def test_external_store_wins_at_recovery_retirement_boundary(self):
        launcher = seeded_deepseek_launcher(self.root)
        target = private_env(self.root)
        move = module._move_owned
        external = b"OTHER=external recovery edit\n"

        def race_before_owned_move(source, destination, expected):
            if Path(source) == target and Path(destination).name.startswith("retired-"):
                replacement = self.root / "external-store"
                replacement.write_bytes(external)
                os.replace(replacement, target)
            return move(source, destination, expected)

        with patch.object(module, "_move_owned", side_effect=race_before_owned_move):
            with self.assertRaises(MigrationError):
                migrate_one(
                    launcher, target, failing_probe(), secret_name="DEEPSEEK_API_KEY"
                )
        self.assertEqual(target.read_bytes(), external)
        self.assertIn(DEEPSEEK_SECRET, launcher.read_text())
        self.assertTrue(module._journal_path(target).exists())

    def test_interrupt_after_every_original_move_restores_exact_objects(self):
        for victim in ("subscription-router.env", "deepseek.cmd", "glm-code.ps1"):
            for interrupt in (KeyboardInterrupt, SystemExit):
                with self.subTest(victim=victim, interruption=interrupt.__name__):
                    glm, deepseek = seeded_launchers(self.root)
                    target = private_env(self.root)
                    target.write_bytes(b"OTHER=original\n")
                    originals = {
                        path: (path.read_bytes(), path.stat().st_ino)
                        for path in (glm, deepseek, target)
                    }
                    move = module._move_owned
                    stopped = False

                    def stop_after_move(source, destination, expected):
                        nonlocal stopped
                        move(source, destination, expected)
                        if Path(source).name == victim and not stopped:
                            stopped = True
                            raise interrupt()

                    with patch.object(
                        module, "_move_owned", side_effect=stop_after_move
                    ):
                        with self.assertRaises(interrupt):
                            migrate_launcher_secrets(
                                glm, deepseek, target, passing_probe()
                            )
                    self.assertEqual(
                        {
                            path: (path.read_bytes(), path.stat().st_ino)
                            for path in originals
                        },
                        originals,
                    )
                    self.assertFalse(module._journal_path(target).exists())

    @unittest.skipUnless(os.name == "nt", "Windows pinned parent before creation")
    def test_directory_swap_before_candidate_open_is_blocked(self):
        target_dir = self.root / "private"
        target_dir.mkdir()
        target = target_dir / "store.env"
        open_private = module.open_private_file
        attempts = []

        def swap_before_open(path, **kwargs):
            with self.assertRaises(OSError):
                target_dir.rename(self.root / "displaced")
            attempts.append(True)
            return open_private(path, **kwargs)

        with patch.object(module, "open_private_file", side_effect=swap_before_open):
            module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
        self.assertEqual(attempts, [True])

    @unittest.skipUnless(os.name == "nt", "Windows mutable reparse-point boundary")
    def test_reparse_mutation_of_pinned_parent_never_writes_into_git(self):
        import struct
        import win32file
        import win32con

        repo = self.root / "checkout"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        target_dir = self.root / "private"
        target_dir.mkdir()
        target = target_dir / "store.env"
        create = module.create_private_directory
        mutated = []
        tag = 0xA0000003

        def reparse_handle():
            return win32file.CreateFile(
                str(target_dir),
                win32con.GENERIC_WRITE,
                7,
                None,
                win32con.OPEN_EXISTING,
                0x02200000,
                None,
            )

        def change_parent_before_transaction_directory(path):
            substitute = ("\\??\\" + str(repo)).encode("utf-16-le")
            display = str(repo).encode("utf-16-le")
            names = substitute + b"\0\0" + display + b"\0\0"
            data = (
                struct.pack(
                    "<IHHHHHH",
                    tag,
                    8 + len(names),
                    0,
                    0,
                    len(substitute),
                    len(substitute) + 2,
                    len(display),
                )
                + names
            )
            handle = reparse_handle()
            try:
                win32file.DeviceIoControl(handle, 0x900A4, data, 0)
            finally:
                handle.Close()
            mutated.append(True)
            return create(path)

        try:
            with patch.object(
                module,
                "create_private_directory",
                side_effect=change_parent_before_transaction_directory,
            ):
                try:
                    module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
                except (OSError, MigrationError):
                    pass  # A no-follow refusal is also safe before any Git write.
            self.assertEqual(
                mutated, [True], "exercise actual reparse mutation, not a mock"
            )
            self.assertFalse((repo / "store.env").exists())
            self.assertEqual(list(repo.glob("subscription-backup-*")), [])
        finally:
            if mutated:
                handle = reparse_handle()
                try:
                    win32file.DeviceIoControl(
                        handle, 0x900AC, struct.pack("<IHH", tag, 0, 0), 0
                    )
                finally:
                    handle.Close()

    def test_move_and_publish_sync_both_directory_boundaries(self):
        from subscriptions import private_files

        target = private_env(self.root)
        target.write_bytes(b"OTHER=original\n")
        with patch.object(private_files, "sync_directory") as synced:
            module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
        called = [args.args[0] for args in synced.call_args_list]
        self.assertGreaterEqual(called.count(self.root), 3)
        self.assertTrue(
            any(
                path.parent == self.root
                and path.name.startswith("subscription-backup-")
                for path in called
            )
        )

    def test_posix_directory_sync_ignores_only_unsupported_errors(self):
        import errno
        from subscriptions import private_files

        for error in (errno.EINVAL, errno.ENOTSUP, errno.EIO):
            with (
                patch.object(private_files.os, "name", "posix"),
                patch.object(private_files.os, "open", return_value=71),
                patch.object(
                    private_files.os, "fsync", side_effect=OSError(error, "fixture")
                ),
                patch.object(private_files.os, "close") as closed,
            ):
                if error == errno.EIO:
                    with self.assertRaises(OSError):
                        private_files.sync_directory(self.root)
                else:
                    private_files.sync_directory(self.root)
                closed.assert_called_once_with(71)

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
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(
            module.read_private_env(target), {"DEEPSEEK_API_KEY": DEEPSEEK_SECRET}
        )
        first = target.read_text(encoding="utf-8")

        self.assertIn("DEEPSEEK_API_KEY=", first)
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(target.with_suffix(".env.tmp").exists())
        # Rewriting replaces content instead of appending duplicates.
        module.write_private_env(
            target, {"DEEPSEEK_API_KEY": DEEPSEEK_SECRET, "GLM_API_KEY": GLM_SECRET}
        )
        second = target.read_text(encoding="utf-8")
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(
            module.read_private_env(target),
            {"DEEPSEEK_API_KEY": DEEPSEEK_SECRET, "GLM_API_KEY": GLM_SECRET},
        )
        self.assertEqual(second.count("DEEPSEEK_API_KEY="), 1)
        self.assertIn("GLM_API_KEY=", second)

    def test_published_store_still_rejects_external_hardlinks(self):
        target = private_env(self.root)
        module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
        before = target.read_bytes()
        os.link(target, self.root / "external-link.env")
        with self.assertRaises(MigrationError):
            module.read_private_env(target)
        with self.assertRaises(MigrationError):
            module.write_private_env(target, {"DEEPSEEK_API_KEY": DEEPSEEK_SECRET})
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership-preserving restoration")
    def test_posix_changed_object_is_restored_without_residual_hardlink(self):
        from subscriptions import private_files

        target = private_env(self.root)
        target.write_bytes(b"OTHER=before\n")
        expected = module._snapshot(target)[1]
        target.write_bytes(b"OTHER=external edit\n")
        transaction = self.root / "private-transaction"
        transaction.mkdir(mode=0o700)
        with self.assertRaises(OSError):
            private_files.move_owned(target, transaction / "displaced", expected)
        self.assertEqual(target.read_bytes(), b"OTHER=external edit\n")
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(module.read_private_env(target), {"OTHER": "external edit"})

    @unittest.skipUnless(os.name == "posix", "POSIX exclusive publication")
    def test_posix_exclusive_publication_preserves_existing_destination(self):
        from subscriptions import private_files

        source, target = self.root / "candidate", self.root / "destination"
        source.write_bytes(b"candidate")
        target.write_bytes(b"external")
        with self.assertRaises(FileExistsError):
            private_files.publish_exclusive(source, target)
        self.assertEqual(source.read_bytes(), b"candidate")
        self.assertEqual(target.read_bytes(), b"external")
        self.assertEqual(source.stat().st_nlink, 1)
        self.assertEqual(target.stat().st_nlink, 1)

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
        actual_replace = module._publish_exclusive
        failed = False

        def replace(source, target):
            nonlocal failed
            if Path(target) == glm and not failed:
                failed = True
                raise OSError("private failure owner@example.test")
            return actual_replace(source, target)

        with patch.object(module, "_publish_exclusive", side_effect=replace):
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
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_subscription_configuration import module, passing_probe
root = Path(sys.argv[1])
write = module._publish_exclusive
def crash_after_write(source, path, **kwargs):
    write(source, path, **kwargs)
    if path == root / "deepseek.cmd":
        os._exit(73)
module._publish_exclusive = crash_after_write
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
        publish = module._publish_exclusive

        def reject_candidate(source, destination):
            if Path(source).name.startswith(".subscription-"):
                raise OSError("fixture")
            return publish(source, destination)

        with patch.object(module, "_publish_exclusive", side_effect=reject_candidate):
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

    @unittest.skipUnless(os.name == "nt", "Windows DACL contract")
    def test_private_temp_and_replaced_store_have_protected_owner_only_dacl(self):
        import win32security

        subprocess.run(
            ["icacls", str(self.root), "/grant", "*S-1-1-0:(OI)(CI)RX"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        target = private_env(self.root)
        target.write_bytes(b"old fixture")

        def assert_private(path):
            descriptor = win32security.GetFileSecurity(
                str(path),
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION,
            )
            owner = descriptor.GetSecurityDescriptorOwner()
            acl = descriptor.GetSecurityDescriptorDacl()
            self.assertTrue(
                descriptor.GetSecurityDescriptorControl()[0]
                & win32security.SE_DACL_PROTECTED,
                "DACL must block inherited grants",
            )
            self.assertEqual(acl.GetAceCount(), 1, "only the owner may access the file")
            self.assertEqual(acl.GetAce(0)[0][0], win32security.ACCESS_ALLOWED_ACE_TYPE)
            self.assertEqual(acl.GetAce(0)[1] & 0x1F01FF, 0x1F01FF)
            self.assertTrue(
                acl.GetAce(0)[2] == owner, "ACE must bind to the file owner"
            )

        opened = []
        actual_fdopen = os.fdopen

        def fdopen(descriptor, *args, **kwargs):
            handle = actual_fdopen(descriptor, *args, **kwargs)
            try:
                temporary = next(self.root.rglob(".subscription-*"))
                self.assertEqual(temporary.stat().st_size, 0)
                assert_private(temporary)
                opened.append(temporary)
            except BaseException:
                handle.close()
                raise
            return handle

        with patch.object(module.os, "fdopen", side_effect=fdopen):
            module.write_private_env(target, {"GLM_API_KEY": GLM_SECRET})
        self.assertEqual(len(opened), 1)
        assert_private(target)
        for backup in self.root.rglob("displaced-*"):
            assert_private(backup)

    def test_symlink_store_is_rejected_before_any_credential_write(self):
        target = self.root / "canonical.env"
        target.write_bytes(b"OTHER_KEY=preserved\n")
        alias = private_env(self.root)
        try:
            alias.symlink_to(target)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows has not granted symlink creation privilege")
            raise
        launcher = seeded_deepseek_launcher(self.root)
        before = launcher.read_bytes()
        with self.assertRaises(MigrationError):
            migrate_one(
                launcher, alias, passing_probe(), secret_name="DEEPSEEK_API_KEY"
            )
        self.assertTrue(alias.is_symlink())
        self.assertEqual(target.read_bytes(), b"OTHER_KEY=preserved\n")
        self.assertEqual(launcher.read_bytes(), before)
        self.assertEqual(list(self.root.glob("subscription-backup-*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction contract")
    def test_junction_parent_cannot_move_store_or_backups_into_git(self):
        repo = self.root / "checkout"
        subprocess.run(
            ["git", "init", str(repo)], check=True, capture_output=True, timeout=10
        )
        junction = repo / "alias"
        outside = self.root / "outside"
        outside.mkdir()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        launcher = seeded_deepseek_launcher(self.root)
        with self.assertRaises(MigrationError):
            migrate_one(
                launcher,
                junction / "store.env",
                passing_probe(),
                secret_name="DEEPSEEK_API_KEY",
            )
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(repo.glob("subscription-backup-*")), [])

    def test_keyboard_interrupt_immediately_after_each_publication_restores_launchers(
        self,
    ):
        for stop_at in ("subscription-router.env", "deepseek.cmd", "glm-code.ps1"):
            for interrupt in (KeyboardInterrupt, SystemExit):
                with self.subTest(target=stop_at, interrupt=interrupt.__name__):
                    glm, deepseek = seeded_launchers(self.root)
                    target = private_env(self.root)
                    target.unlink(missing_ok=True)
                    originals = {p: p.read_bytes() for p in (glm, deepseek)}
                    replace = module._publish_exclusive
                    interrupted = False

                    def after_replace(source, destination):
                        nonlocal interrupted
                        replace(source, destination)
                        if Path(destination).name == stop_at and not interrupted:
                            interrupted = True
                            raise interrupt()

                    with patch.object(
                        module, "_publish_exclusive", side_effect=after_replace
                    ):
                        with self.assertRaises(interrupt):
                            migrate_launcher_secrets(
                                glm, deepseek, target, passing_probe()
                            )
                    self.assertEqual({p: p.read_bytes() for p in originals}, originals)
                    self.assertFalse(
                        target.exists(),
                        "store may be removed only after every launcher is restored",
                    )

    def test_store_edit_during_preparation_is_preserved_before_first_write(self):
        launcher = seeded_deepseek_launcher(self.root)
        target = private_env(self.root)
        target.write_bytes(b"OTHER_KEY=old\n")
        changed = b"OTHER_KEY=concurrent edit\n"
        parse = module.parse_assignments

        def edit_during_prepare(text):
            values = parse(text)
            target.write_bytes(changed)
            return values

        with patch.object(module, "parse_assignments", side_effect=edit_during_prepare):
            with self.assertRaises(MigrationError):
                migrate_one(
                    launcher, target, passing_probe(), secret_name="DEEPSEEK_API_KEY"
                )
        self.assertEqual(target.read_bytes(), changed)
        self.assertIn(DEEPSEEK_SECRET, launcher.read_text())

    def test_edit_after_candidate_flush_is_preserved_before_replace(self):
        launcher = seeded_deepseek_launcher(self.root)
        target = private_env(self.root)
        target.write_bytes(b"OTHER_KEY=old\n")
        changed = b"OTHER_KEY=edited after candidate flush\n"
        actual_write = module._write_candidate
        actual_fsync = os.fsync

        def write_with_late_edit(path, values, **kwargs):
            def after_flush(descriptor):
                actual_fsync(descriptor)
                target.write_bytes(changed)

            with patch.object(module.os, "fsync", side_effect=after_flush):
                actual_write(path, values, **kwargs)

        with patch.object(module, "_write_candidate", side_effect=write_with_late_edit):
            with self.assertRaises(MigrationError):
                migrate_one(
                    launcher, target, passing_probe(), secret_name="DEEPSEEK_API_KEY"
                )
        self.assertEqual(target.read_bytes(), changed)
        self.assertIn(DEEPSEEK_SECRET, launcher.read_text())

    def test_recovery_interruption_preserves_store_until_every_launcher_is_certified(
        self,
    ):
        glm, deepseek = seeded_launchers(self.root)
        target = private_env(self.root)
        originals = {p: p.read_bytes() for p in (glm, deepseek)}
        actual_replace = module._publish_exclusive
        interrupted = set()

        def interrupt_publication_and_recovery(source, destination):
            actual_replace(source, destination)
            destination = Path(destination)
            if destination == glm and "publish" not in interrupted:
                interrupted.add("publish")
                raise KeyboardInterrupt()
            if (
                destination == deepseek
                and "publish" in interrupted
                and "recover" not in interrupted
            ):
                interrupted.add("recover")
                raise KeyboardInterrupt()

        with patch.object(
            module, "_publish_exclusive", side_effect=interrupt_publication_and_recovery
        ):
            with self.assertRaises(KeyboardInterrupt):
                migrate_launcher_secrets(glm, deepseek, target, passing_probe())
        self.assertTrue(
            target.exists(), "uncertain recovery must retain the proven store"
        )
        self.assertTrue(target.with_name(target.name + ".migration.json").exists())
        self.assertEqual(deepseek.read_bytes(), originals[deepseek])
        self.assertNotIn(GLM_SECRET.encode(), glm.read_bytes())
        observed = []

        def reject_fresh_proof(name, value):
            observed.append({p: p.read_bytes() for p in originals})
            return None

        with self.assertRaises(MigrationError):
            migrate_launcher_secrets(glm, deepseek, target, reject_fresh_proof)
        self.assertEqual(observed, [originals])
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(target.name + ".migration.json").exists())

    def test_process_death_immediately_after_replace_recovers_before_new_proof(self):
        code = """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_subscription_configuration import module, passing_probe
root, victim = Path(sys.argv[1]), sys.argv[2]
replace = module._publish_exclusive
def die_after_replace(source, destination):
    replace(source, destination)
    if Path(destination).name == victim:
        os._exit(73)
module._publish_exclusive = die_after_replace
module.migrate_launcher_secrets(root / "glm-code.ps1", root / "deepseek.cmd", root / "subscription-router.env", passing_probe())
"""
        for victim in ("subscription-router.env", "deepseek.cmd", "glm-code.ps1"):
            with self.subTest(victim=victim):
                glm, deepseek = seeded_launchers(self.root)
                target = private_env(self.root)
                old_store = b"OTHER_KEY=preexisting store\n"
                target.write_bytes(old_store)
                originals = {p: p.read_bytes() for p in (glm, deepseek)}
                result = subprocess.run(
                    [sys.executable, "-", str(self.root), victim],
                    input=code,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 73, result.stderr)
                self.assertTrue(target.exists())
                observed = []

                def reject_fresh_proof(name, value):
                    observed.append({p: p.read_bytes() for p in originals})
                    return None

                with self.assertRaises(MigrationError):
                    migrate_launcher_secrets(glm, deepseek, target, reject_fresh_proof)
                self.assertEqual(observed, [originals])
                self.assertEqual({p: p.read_bytes() for p in originals}, originals)
                self.assertEqual(target.read_bytes(), old_store)
                self.assertFalse(
                    target.with_name(target.name + ".migration.json").exists()
                )

    def test_unknown_launcher_during_rollback_keeps_store_and_journal(self):
        glm, deepseek = seeded_launchers(self.root)
        target = private_env(self.root)
        actual_replace = module._publish_exclusive
        user_edit = b"user changed launcher\n"

        def fail_second_publication(source, destination):
            if Path(destination) == glm:
                deepseek.write_bytes(user_edit)
                raise OSError("fixture")
            actual_replace(source, destination)

        with patch.object(
            module, "_publish_exclusive", side_effect=fail_second_publication
        ):
            with self.assertRaises(MigrationError):
                migrate_launcher_secrets(glm, deepseek, target, passing_probe())
        self.assertEqual(deepseek.read_bytes(), user_edit)
        self.assertTrue(target.exists())
        self.assertTrue(target.with_name(target.name + ".migration.json").exists())

    def test_equivalent_path_spelling_uses_the_same_migration_lock(self):
        glm, deepseek = seeded_launchers(self.root)
        (self.root / "path-part").mkdir()
        alias = self.root / "path-part" / ".." / "subscription-router.env"
        entered, release = threading.Event(), threading.Event()

        def probe(name, value):
            entered.set()
            self.assertTrue(release.wait(10))
            return passing_probe()(name, value)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                migrate_one, deepseek, alias, probe, secret_name="DEEPSEEK_API_KEY"
            )
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(
                    MigrationError, "migration already running"
                ):
                    migrate_one(
                        glm,
                        private_env(self.root),
                        passing_probe(),
                        secret_name="GLM_API_KEY",
                    )
            finally:
                release.set()
            result = future.result(timeout=10)
        self.assertEqual(result.private_env, private_env(self.root).resolve())

    def test_hard_link_alias_cannot_create_an_independent_store_lock(self):
        target = private_env(self.root)
        target.write_bytes(b"OTHER_KEY=preserved\n")
        alias = self.root / "hard-link.env"
        os.link(target, alias)
        launcher = seeded_deepseek_launcher(self.root)
        with self.assertRaises(MigrationError):
            migrate_one(
                launcher, alias, passing_probe(), secret_name="DEEPSEEK_API_KEY"
            )
        self.assertEqual(target.read_bytes(), b"OTHER_KEY=preserved\n")
        self.assertEqual(alias.read_bytes(), b"OTHER_KEY=preserved\n")

    def test_equal_candidate_written_externally_before_publish_is_not_rolled_back(self):
        launcher = seeded_deepseek_launcher(self.root)
        target = private_env(self.root)
        target.write_bytes(b"OTHER_KEY=old\n")
        concurrent = f"DEEPSEEK_API_KEY={DEEPSEEK_SECRET}\nOTHER_KEY=old\n".encode()
        parse = module.parse_assignments

        def external_edit(text):
            target.write_bytes(concurrent)
            return parse(text)

        with patch.object(module, "parse_assignments", side_effect=external_edit):
            with self.assertRaises(MigrationError):
                migrate_one(
                    launcher, target, passing_probe(), secret_name="DEEPSEEK_API_KEY"
                )
        self.assertEqual(target.read_bytes(), concurrent)
        self.assertIn(DEEPSEEK_SECRET, launcher.read_text())

    def test_equal_candidate_replaced_externally_during_cleanup_is_preserved(self):
        glm, deepseek = seeded_launchers(self.root)
        target = private_env(self.root)
        originals = {p: p.read_bytes() for p in (glm, deepseek)}
        externally_written = []

        def replace_store_before_cleanup(name, value):
            if name == "GLM_API_KEY":
                external = self.root / "external-edit.env"
                current = target.read_bytes()
                external.write_bytes(current)
                os.replace(external, target)
                externally_written.append(current)
            return passing_probe()(name, value)

        with self.assertRaises(MigrationError):
            migrate_launcher_secrets(
                glm, deepseek, target, replace_store_before_cleanup
            )
        self.assertTrue(
            target.exists(), "rollback must retain externally published store"
        )
        self.assertEqual(target.read_bytes(), externally_written[0])
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)


class LiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.db_path = Path(self.tmp.name) / "router.db"
        self.environment = patch.dict(
            os.environ, {"GONCLOUD_SUBSCRIPTION_DB": str(self.db_path)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_default_live_matrix_probes_exactly_the_adapters_the_table_binds(self):
        from subscriptions.catalog import compile_profile_routes, load_routing_source

        class ReadyAdapter:
            timeout_seconds = 300
            probe_timeout_seconds = 30

            def probe(self, route):
                return AdapterHealth(True, route.adapter)

            def invoke(self, request, route):
                return InvocationResult(
                    True,
                    "GONCLOUD_PROBE_OK",
                    route.adapter,
                    route.model,
                    route.billing_mode,
                )

            def live_probe(self, route):
                return self.invoke(None, route)

        routing = load_routing_source()
        bound = {
            route.adapter
            for profile in routing["chains"]
            for route in compile_profile_routes(profile, routing)
        }
        # The HTTP twins are probed with a table model of their family.
        expected = bound | {"zai", "deepseek"}
        adapters = {name: ReadyAdapter() for name in expected | {"opencode_zen"}}
        with (
            patch.object(module, "read_private_env", return_value={}),
            patch(
                "subscriptions.adapters.build_adapter_registry",
                return_value=adapters,
            ),
            redirect_stdout(io.StringIO()),
        ):
            summary = module.run_live_probe_all()

        probed = [result["adapter"] for result in summary["results"]]
        self.assertEqual(set(probed), expected)
        self.assertEqual(len(probed), len(expected))
        self.assertIn("opencode_zai", probed)
        self.assertEqual(summary["passed"], len(expected))

    def test_live_probe_commands_exclude_other_processes_and_release_after_crash(self):
        import sqlite3
        import time

        root = Path(self.tmp.name)
        counter = root / "counts.db"
        with closing(sqlite3.connect(counter)) as conn, conn:
            conn.execute("CREATE TABLE counts(active INTEGER, maximum INTEGER)")
            conn.execute("INSERT INTO counts VALUES(0, 0)")
        code = r"""
import importlib.util,sys,os,time,sqlite3
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
root,fixture,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
spec=importlib.util.spec_from_file_location('configure_subscriptions',root/'scripts/configure_subscriptions.py')
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
from subscriptions.types import AdapterHealth,InvocationResult
class Adapter:
    timeout_seconds=300
    probe_timeout_seconds=30
    def probe(self,route): return AdapterHealth(True,'subscription')
    def invoke(self,request,route):
        with closing(sqlite3.connect(fixture/'counts.db')) as conn,conn:
            conn.execute('UPDATE counts SET active=active+1,maximum=MAX(maximum,active+1)')
        (fixture/'entered').touch()
        if mode=='crash': os._exit(7)
        deadline=time.monotonic()+15
        while not (fixture/'release').exists():
            if time.monotonic()>deadline: raise RuntimeError('fixture release missing')
            time.sleep(.01)
        with closing(sqlite3.connect(fixture/'counts.db')) as conn,conn:
            conn.execute('UPDATE counts SET active=active-1')
        return InvocationResult(True,'GONCLOUD_PROBE_OK','codex','fixture','subscription')
with patch.object(module,'read_private_env',return_value={}),patch('subscriptions.adapters.build_adapter_registry',return_value={'codex':Adapter()}):
    sys.argv=['probe','--live-probe-all','--only','codex']
    sys.exit(module.main())
"""
        command = [sys.executable, "-c", code, str(ROOT), str(root)]
        env = {**os.environ, "PYTHONUTF8": "1"}
        first = subprocess.Popen(
            command + ["normal"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        second = None
        try:
            deadline = time.monotonic() + 10
            while not (root / "entered").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((root / "entered").exists())
            second = subprocess.Popen(
                command + ["normal"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            deadline = time.monotonic() + 10
            while second.poll() is None and time.monotonic() < deadline:
                with closing(sqlite3.connect(counter)) as conn:
                    maximum = conn.execute("SELECT maximum FROM counts").fetchone()[0]
                if maximum > 1:
                    break
                time.sleep(0.01)
            (root / "release").touch()
            first.communicate(timeout=15)
            second.communicate(timeout=15)
            with closing(sqlite3.connect(counter)) as conn:
                maximum = conn.execute("SELECT maximum FROM counts").fetchone()[0]
            self.assertEqual(maximum, 1)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 1)
        finally:
            (root / "release").touch()
            for process in (first, second):
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=10)
        crashed = subprocess.run(
            command + ["crash"], capture_output=True, env=env, timeout=10
        )
        self.assertEqual(crashed.returncode, 7)
        recovered = subprocess.run(
            command + ["normal"], capture_output=True, env=env, timeout=15
        )
        self.assertEqual(recovered.returncode, 0)

    def test_adapter_owner_blocks_live_effects_and_failed_proof_publishes_before_unlock(
        self,
    ):
        import time
        from subscriptions.router import SubscriptionRouter
        from subscriptions.state import SubscriptionStateStore, SubscriptionBusy
        from test_subscription_router import (
            SOURCE,
            ScriptedAdapter,
            completed,
            failed,
            request,
            primary_and_next,
        )

        root = Path(self.tmp.name)
        store = SubscriptionStateStore(self.db_path)
        store.save_health("opencode_go", ready=True, account_class="subscription")
        before = store.health("opencode_go")
        code = r"""
import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'plugins/goncloud-team'))
from subscriptions.state import SubscriptionStateStore,SubscriptionBusy
fixture,mode=Path(sys.argv[2]),sys.argv[3]
state=SubscriptionStateStore(fixture/'router.db')
deadline=time.monotonic()+20
while True:
    try:
        with state.dispatch_lock('opencode_go'):
            if mode=='hold':
                (fixture/'owner-entered').touch()
                while not (fixture/'owner-release').exists():
                    if time.monotonic()>deadline: raise RuntimeError('fixture deadline')
                    time.sleep(.01)
            else:
                health=state.health('opencode_go')
                (fixture/'observed.json').write_text(json.dumps({'quarantined':health['quarantined'],'failure':health['failure']}))
            break
    except SubscriptionBusy:
        (fixture/'observer-blocked').touch()
        if time.monotonic()>deadline: raise RuntimeError('fixture deadline')
        time.sleep(.01)
"""
        command = [sys.executable, "-c", code, str(ROOT), str(root)]
        env = {**os.environ, "PYTHONUTF8": "1"}
        processes = []

        def start(mode):
            process = subprocess.Popen(
                command + [mode],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            processes.append(process)
            return process

        def wait_for(name):
            deadline = time.monotonic() + 10
            while not (root / name).exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((root / name).exists(), name)

        go, second = ScriptedAdapter("opencode_go"), ScriptedAdapter("claude")
        for adapter in (go, second):
            adapter.timeout_seconds, adapter.probe_timeout_seconds = 300, 30
        go.result = failed(FailureKind.TASK_FAILURE, "opencode_go")
        second.result = completed(adapter="claude")
        try:
            owner = start("hold")
            wait_for("owner-entered")
            with (
                patch.object(module, "read_private_env", return_value={}),
                patch(
                    "subscriptions.adapters.build_adapter_registry",
                    return_value={"opencode_go": go},
                ),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SubscriptionBusy):
                    module.run_live_probe_all(only="opencode_go")
                self.assertEqual(go.probes, [])
                self.assertEqual(go.requests, [])
                self.assertEqual(store.health("opencode_go"), before)
                (root / "owner-release").touch()
                owner.communicate(timeout=10)
                self.assertEqual(owner.returncode, 0)

                def failed_proof(assignment, route):
                    start("observe")
                    wait_for("observer-blocked")
                    return failed(FailureKind.TASK_FAILURE, "opencode_go")

                go.result = failed_proof
                result = module.run_live_probe_all(only="opencode_go")
                self.assertEqual(result["passed"], 0)
            processes[-1].communicate(timeout=10)
            self.assertEqual(processes[-1].returncode, 0)
            self.assertEqual(
                json.loads((root / "observed.json").read_text()),
                {"quarantined": True, "failure": "task_failure"},
            )
            self.assertEqual(len(go.probes), 1)
            self.assertEqual(len(go.requests), 1)
            router = SubscriptionRouter(
                adapters={"opencode_go": go, "claude": second},
                store=store,
                routing_source=SOURCE,
            )
            self.assertTrue(router.run(request(root), primary_and_next()).success)
            self.assertEqual(len(go.probes), 1)
            self.assertEqual(len(go.requests), 1)
            self.assertEqual(len(second.requests), 1)
        finally:
            (root / "owner-release").touch()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)

    def test_live_probe_lock_also_excludes_threads_until_publication(self):
        from subscriptions.state import SubscriptionStateStore, SubscriptionBusy
        from test_subscription_router import ScriptedAdapter, completed

        entered, release = threading.Event(), threading.Event()
        adapter = ScriptedAdapter("codex")
        adapter.timeout_seconds, adapter.probe_timeout_seconds = 300, 30
        adapter.result = completed("GONCLOUD_PROBE_OK", "codex")
        original = SubscriptionStateStore._save_live_health_locked

        def publish(store, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(store, *args, **kwargs)

        with (
            patch.object(module, "read_private_env", return_value={}),
            patch(
                "subscriptions.adapters.build_adapter_registry",
                return_value={"codex": adapter},
            ),
            patch.object(
                SubscriptionStateStore, "_save_live_health_locked", new=publish
            ),
            redirect_stdout(io.StringIO()),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            first = pool.submit(module.run_live_probe_all, only="codex")
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(SubscriptionBusy):
                    module.run_live_probe_all(only="codex")
                self.assertEqual(len(adapter.requests), 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=15)["passed"], 1)

    def test_scoped_publisher_cannot_write_after_release_or_from_another_thread(self):
        from subscriptions.state import SubscriptionStateStore, SubscriptionBusy

        store = SubscriptionStateStore(self.db_path)
        health = {"ready": False, "account_class": "subscription", "failure": "auth"}
        with store.dispatch_lock("opencode_go") as publish:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(publish, **health)
                with self.assertRaises(SubscriptionBusy):
                    future.result(timeout=5)
            self.assertIsNone(store.health("opencode_go"))
        with self.assertRaises(SubscriptionBusy):
            publish(**health)
        self.assertIsNone(store.health("opencode_go"))

    def test_scoped_publisher_cannot_replace_failed_proof_with_success(self):
        from subscriptions.state import SubscriptionStateStore, SubscriptionBusy

        store = SubscriptionStateStore(self.db_path)
        with store.dispatch_lock("opencode_go") as publish:
            publish(ready=False, account_class="subscription", failure="auth")
            first = store.health("opencode_go")
            self.assertTrue(first["quarantined"])
            with self.assertRaises(SubscriptionBusy):
                publish(ready=True, account_class="subscription", failure=None)
            self.assertEqual(store.health("opencode_go"), first)

    def test_scoped_publisher_is_consumed_even_when_database_write_raises(self):
        from subscriptions.state import SubscriptionStateStore, SubscriptionBusy

        store = SubscriptionStateStore(self.db_path)
        original = store._save_live_health_locked
        for after_write in (False, True):
            with self.subTest(after_write=after_write):
                store.save_live_health(
                    "opencode_go",
                    ready=False,
                    account_class="subscription",
                    failure="auth",
                )

                def fail(*args, **kwargs):
                    if after_write:
                        original(*args, **kwargs)
                    raise RuntimeError("fixture database boundary failure")

                with store.dispatch_lock("opencode_go") as publish:
                    with patch.object(
                        store, "_save_live_health_locked", side_effect=fail
                    ):
                        with self.assertRaisesRegex(RuntimeError, "fixture database"):
                            publish(
                                ready=False,
                                account_class="subscription",
                                failure="timeout",
                            )
                    first = store.health("opencode_go")
                    self.assertTrue(first["quarantined"])
                    self.assertEqual(
                        first["failure"], "timeout" if after_write else "auth"
                    )
                    with self.assertRaises(SubscriptionBusy):
                        publish(ready=True, account_class="subscription", failure=None)
                    self.assertEqual(store.health("opencode_go"), first)

    def test_live_failure_survives_ttl_and_only_exact_proof_restores_route(self):
        from subscriptions.router import SubscriptionRouter
        from subscriptions.state import SubscriptionStateStore
        from test_subscription_router import (
            SOURCE,
            ScriptedAdapter,
            completed,
            failed,
            request,
            primary_and_next,
        )

        adapters = {name: ScriptedAdapter(name) for name in ("opencode_go", "claude")}
        for adapter in adapters.values():
            adapter.timeout_seconds = 300
            adapter.probe_timeout_seconds = 30
        adapters["opencode_go"].result = failed(FailureKind.TIMEOUT, "opencode_go")
        adapters["claude"].result = completed(adapter="claude")
        store = SubscriptionStateStore(self.db_path)
        store.save_health("claude", ready=True, account_class="subscription")
        before = store.health("claude")
        with (
            patch.object(module, "read_private_env", return_value={}),
            patch(
                "subscriptions.adapters.build_adapter_registry", return_value=adapters
            ),
            redirect_stdout(io.StringIO()),
        ):
            summary = module.run_live_probe_all(only="opencode_go")
            self.assertEqual(summary["passed"], 0)
            self.assertEqual(store.health("claude"), before)
            self.assertIsNotNone(store.health("opencode_go"))
            self.assertTrue(store.health("opencode_go")["quarantined"])
            # Ordinary health updates cannot erase a live-proof quarantine.
            store.save_health(
                "opencode_go",
                ready=False,
                account_class="subscription",
                failure="auth",
                detail="sk-private owner@example.test",
            )
            store.save_health("opencode_go", ready=True, account_class="subscription")
            router = SubscriptionRouter(
                adapters=adapters,
                store=store,
                routing_source=SOURCE,
                health_ttl_seconds=-1,
            )
            adapters["opencode_go"].requests.clear()
            adapters["opencode_go"].probes.clear()
            result = router.run(request(Path(self.tmp.name)), primary_and_next())
            self.assertTrue(result.success)
            self.assertEqual(result.adapter, "claude")
            self.assertEqual(adapters["opencode_go"].requests, [])
            self.assertEqual(adapters["opencode_go"].probes, [])
            attempts = store.stage_attempts("wf-6", "st-6")
            self.assertEqual([item["slot"] for item in attempts], [0, 2])
            self.assertNotIn("sk-private", repr(store.health("opencode_go")))
            self.assertNotIn("owner@", repr(store.health("opencode_go")))
            store.set_cooldown("opencode_go", hours=1)
            result = router.run(
                request(Path(self.tmp.name), stage_id="cooldown"), primary_and_next()
            )
            self.assertTrue(result.success)
            self.assertEqual(
                [item["slot"] for item in store.stage_attempts("wf-6", "cooldown")],
                [0, 2],
            )
            adapters["opencode_go"].result = completed(
                "GONCLOUD_PROBE_OK extra", adapter="opencode_go"
            )
            module.run_live_probe_all(only="opencode_go")
            self.assertTrue(store.health("opencode_go")["quarantined"])
            adapters["opencode_go"].result = completed(
                "GONCLOUD_PROBE_OK", adapter="opencode_go"
            )
            module.run_live_probe_all(only="opencode_go")
            self.assertFalse(store.health("opencode_go")["quarantined"])
            self.assertIsNone(store.cooldown_until("opencode_go"))
            result = router.run(
                request(Path(self.tmp.name), stage_id="restored"), primary_and_next()
            )
            self.assertTrue(result.success)
            self.assertEqual(result.adapter, "opencode_go")

    def test_existing_health_database_migrates_concurrently_without_losing_state(self):
        import sqlite3
        from subscriptions.state import SubscriptionStateStore

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE subscription_health (adapter TEXT PRIMARY KEY, "
                "ready INTEGER NOT NULL, account_class TEXT NOT NULL, failure TEXT, "
                "detail TEXT NOT NULL DEFAULT '', cooldown_until TEXT, "
                "updated_at TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO subscription_health(adapter, ready, account_class) "
                "VALUES ('opencode_zen', 0, 'subscription')"
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            stores = list(
                pool.map(SubscriptionStateStore, [self.db_path, self.db_path])
            )
        for store in stores:
            health = store.health("opencode_zen")
            self.assertEqual(health["ready"], False)
            self.assertEqual(health["account_class"], "subscription")
            self.assertEqual(health.get("quarantined"), False)

    def test_real_probe_uses_persisted_fixtures_and_table_models_over_local_http(self):
        from test_subscription_vendor_adapters import (
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
        from test_subscription_vendor_adapters import (
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

    def test_live_matrix_keeps_the_probe_window_an_adapter_declared(self):
        # Shrinking a declared window here reports a slow-launching but healthy
        # account as failed; the ceiling belongs to the adapter contract.
        from subscriptions.adapters import SubscriptionAdapter

        class ColdStartAdapter(SubscriptionAdapter):
            max_probe_timeout_seconds = 120
            probe_timeout_seconds = 120

            def probe(self, route):
                self.observed = self.probe_timeout_seconds
                return AdapterHealth(False, "cold", failure=FailureKind.TIMEOUT)

        adapter = ColdStartAdapter()
        with (
            patch.object(module, "read_private_env", return_value={}),
            patch(
                "subscriptions.adapters.build_adapter_registry",
                return_value={"muse": adapter},
            ),
            redirect_stdout(io.StringIO()),
        ):
            module.run_live_probe_all(only="muse")

        self.assertEqual(adapter.observed, 120)


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
            roles = json.loads((ROOT / "team.json").read_text())["profiles"]
            routing_source = (ROOT / "model-routing.source.json").read_bytes()
            for role in roles:
                profile = runtime / "profiles" / role
                deployed_routing = (
                    profile / "plugins/goncloud-team/model-routing.source.json"
                ).read_bytes()
                self.assertEqual(
                    hashlib.sha256(deployed_routing).digest(),
                    hashlib.sha256(routing_source).digest(),
                    role,
                )

            claw = runtime / "profiles" / "claw"
            self.assertFalse((claw / "plugins/model-providers").exists())
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
                ["Primary", "Primary Go 2", "F1", "F2", "F3", "F4", "F5", "F6"],
            )
            claw_config = yaml.safe_load(
                (claw / "config.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(claw_config["model"]["provider"], "opencode-go")

            secrets = claw / "subscription-router.env"
            second = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=180
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(secrets.exists())
            self.assertTrue(routing.is_file())


if __name__ == "__main__":
    unittest.main()
