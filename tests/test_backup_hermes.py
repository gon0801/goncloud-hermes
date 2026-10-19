import contextlib
import hashlib
import os
import sqlite3
import stat
import tarfile
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from backup_support import (
    ARCHIVE,
    backup_hermes,
    calls,
    make_cli_homes,
    make_install,
    make_receiver,
    manifest,
    members,
    run_main,
    settings,
    write,
)
from goncloud_test_support import cleanup_temp_directory


def clock_jumping_once(path):
    """A clock that leaps past any budget once path exists."""
    return lambda: time.monotonic() + (1_000_000 if path.exists() else 0)


def corrupt_index(path):
    """An index page whose key no longer matches its row: integrity_check fails."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute("create table t (v text)")
        db.execute("create index i on t (v)")
        db.executemany(
            "insert into t values (?)", [(f"fila-{n:04d}",) for n in range(200)]
        )
        db.commit()
        root = db.execute(
            "select rootpage from sqlite_master where name = 'i'"
        ).fetchone()[0]
        size = db.execute("pragma page_size").fetchone()[0]
    data = bytearray(path.read_bytes())
    at = data.rfind(b"fila-0100", (root - 1) * size, root * size)
    data[at : at + 9] = b"fila-9999"
    path.write_bytes(bytes(data))


class Punto12Tests(unittest.TestCase):
    """The PC's live backup script (2026-10-16 hand patch) the repo must own.

    Without these rules the nightly backup fails again on the vault lock, on
    the list-denied empty pending_messages folder and on cron output pruned
    between listing and reading; and configure_team refuses to deploy over the
    live copy because its bytes differ from the repo's.
    """

    def setUp(self):
        self.selection = backup_hermes.Selection()

    def test_the_vault_lock_is_a_live_file(self):
        self.assertEqual(
            backup_hermes._transient_reason(
                Path("vault/.vault.lock"), ("vault", ".vault.lock")
            ),
            backup_hermes._LIVE_FILE,
        )
        self.assertEqual(
            backup_hermes._transient_reason(
                Path("x"), ("profiles", "claw", "vault", ".vault.lock")
            ),
            backup_hermes._LIVE_FILE,
        )

    def test_the_empty_pending_messages_folder_is_skipped_not_lost(self):
        error = PermissionError(13, "Permiso denegado")

        self.selection.cannot_read("home/pending_messages", Path("x"), error)

        self.assertEqual(self.selection.unreadable, [])
        self.assertEqual(
            self.selection.skipped,
            [
                {
                    "path": "home/pending_messages",
                    "reason": "carpeta vacia sin permiso de listado (0 archivos)",
                }
            ],
        )

    def test_a_file_pruned_during_the_copy_is_skipped_not_lost(self):
        error = FileNotFoundError(2, "El sistema no puede encontrar el archivo")

        self.selection.cannot_read(
            "home/profiles/claw/cron/output/a1/fresh.md", Path("x"), error
        )

        self.assertEqual(self.selection.unreadable, [])
        self.assertEqual(
            self.selection.skipped,
            [
                {
                    "path": "home/profiles/claw/cron/output/a1/fresh.md",
                    "reason": "desaparecio durante la copia",
                }
            ],
        )

    def test_any_other_unreadable_file_still_fails_the_run(self):
        error = PermissionError(13, "Permiso denegado")

        self.selection.cannot_read("home/auth.json", Path("x"), error)

        self.assertEqual(self.selection.skipped, [])
        self.assertEqual(
            self.selection.unreadable,
            [{"path": "home/auth.json", "error": "Permiso denegado"}],
        )


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directory)
        self.base = Path(self.directory.name)
        self.home, self.user = make_install(self.base)
        self.out = self.base / "respaldo"

    def backup(self, receiver=None, **overrides):
        receiver = receiver or make_receiver(self.base)
        return run_main(
            settings(self.home, self.user, self.base, receiver, **overrides)
        )

    def test_the_archive_holds_what_reinstalling_cannot_recreate(self):
        code, line = self.backup()

        self.assertEqual(code, 0, line)
        self.assertEqual(
            set(members(self.out / ARCHIVE)),
            {
                "manifest.json",
                "home/.env",
                "home/auth.json",
                "home/backup-key/id_ed25519",
                "home/config.yaml",
                "home/cron/jobs.json",
                "home/kanban.db",
                "home/profiles/claw/.env",
                "home/profiles/claw/config.yaml",
                "home/profiles/claw/cron/jobs.json",
                "home/profiles/claw/memories/MEMORY.md",
                "home/profiles/claw/scripts/backup-claw.sh",
                "home/profiles/claw/state.db",
                "home/profiles/scout/config.yaml",
                "home/subscription-router.env",
                "user/.claude.json",
                "user/.claude/.credentials.json",
                "user/.codex/auth.json",
                "user/.config/opencode/opencode.json",
            },
        )
        skipped = {
            entry["path"]: entry["reason"]
            for entry in manifest(self.out / ARCHIVE)["skipped"]
        }
        self.assertEqual(
            skipped,
            {
                "home/auth.lock": "candado de un proceso vivo",
                "home/backups": "copias viejas; este archivo las reemplaza",
                "home/gateway.pid": "pid de un proceso vivo",
                "home/hermes-agent": "el instalador oficial lo recrea en el commit fijado",
                "home/logs": "bitacoras; no hacen falta para reinstalar",
                "home/profiles/claw/cache": "cache que Hermes vuelve a llenar",
                "home/profiles/claw/logs": "bitacoras; no hacen falta para reinstalar",
                "home/tools": "el instalador oficial lo recrea en el commit fijado",
                "user/.claude/projects": "historial de sesiones; puede pesar GB",
                "user/.config/opencode/node_modules": "dependencias que la CLI reinstala",
            },
        )

    def test_only_logins_settings_and_owner_files_of_each_cli_are_kept(self):
        kept = make_cli_homes(self.user)

        code, line = self.backup()

        self.assertEqual((code, line), (0, ""))
        user_members = {
            name for name in members(self.out / ARCHIVE) if name.startswith("user/")
        }
        self.assertEqual(
            user_members - kept,
            {
                "user/.claude.json",
                "user/.config/opencode/opencode.json",
            },
        )
        self.assertEqual(kept - user_members, set())
        skipped = {
            entry["path"]: entry["reason"]
            for entry in manifest(self.out / ARCHIVE)["skipped"]
        }
        self.assertEqual(
            skipped["user/.codex/packages"],
            "binarios de la CLI; su instalador los recrea",
        )
        self.assertEqual(
            skipped["user/.claude/plugins/cache"],
            "plugins; se reinstalan desde la configuracion",
        )
        self.assertEqual(
            skipped["user/.grok/models.json"],
            "Grok lo recrea al reinstalarse; solo entran su login, sus ajustes "
            "(archivos sueltos de hasta 1 MB) y lo que escribio el dueno",
        )

    def test_the_jev_plugins_git_repo_is_dropped_but_its_plugin_yaml_is_kept(self):
        write(self.home / "profiles/claw/plugins/jev/.git/index", "index")
        write(self.home / "profiles/claw/plugins/jev/plugin.yaml", "plugin")

        code, line = self.backup()

        self.assertEqual((code, line), (0, ""))
        names = set(members(self.out / ARCHIVE))
        self.assertIn("home/profiles/claw/plugins/jev/plugin.yaml", names)
        self.assertNotIn("home/profiles/claw/plugins/jev/.git/index", names)
        skipped = {
            entry["path"]: entry["reason"]
            for entry in manifest(self.out / ARCHIVE)["skipped"]
        }
        self.assertEqual(
            skipped["home/profiles/claw/plugins/jev/.git"],
            "repo git del plugin jev; se reclona desde su remoto",
        )

    def test_profile_dirs_drop_what_the_home_drops(self):
        for name in ("backups/a.zip", "lsp/index", "state-snapshots/s.db", "auth.lock"):
            write(self.home / "profiles/claw" / name, "x")
        write(self.home / "profiles/claw/skills/tool/uv.lock", "lock")

        code, line = self.backup()

        self.assertEqual((code, line), (0, ""))
        names = set(members(self.out / ARCHIVE))
        self.assertIn("home/profiles/claw/skills/tool/uv.lock", names)
        skipped = {entry["path"] for entry in manifest(self.out / ARCHIVE)["skipped"]}
        self.assertLessEqual(
            {
                "home/profiles/claw/backups",
                "home/profiles/claw/lsp",
                "home/profiles/claw/state-snapshots",
                "home/profiles/claw/auth.lock",
            },
            skipped,
        )

    def test_an_oversized_user_file_is_left_out_and_fails_the_run(self):
        write(self.user / ".codex/skills/big/model.bin", "x" * 2000)
        receiver = make_receiver(self.base)

        with mock.patch.object(backup_hermes, "USER_FILE_LIMIT", 1000):
            code, line = self.backup(receiver)

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            "ERROR respaldo Hermes: subido, pero user/.codex/skills/big/model.bin "
            "pesa 2 KB (tope 1 KB por archivo del perfil de usuario) y quedo fuera; "
            "si es una cache nueva agrega una regla\n",
        )
        self.assertEqual(len(calls(receiver)), 1)
        self.assertNotIn(
            "user/.codex/skills/big/model.bin", members(self.out / ARCHIVE)
        )

    def test_a_user_side_over_the_total_limit_fails_the_run(self):
        write(self.user / ".codex/skills/big/a.md", "x" * 2000)
        write(self.user / ".codex/skills/big/b.md", "x" * 1000)

        with mock.patch.object(backup_hermes, "USER_TOTAL_LIMIT", 2000):
            code, line = self.backup()

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            "ERROR respaldo Hermes: subido, pero el perfil de usuario suma 3 KB "
            "(tope 2 KB); el archivo mas grande es user/.codex/skills/big/a.md "
            "(2 KB)\n",
        )
        self.assertIn("user/.codex/skills/big/b.md", members(self.out / ARCHIVE))

    def test_every_manifest_entry_matches_the_bytes_archived(self):
        self.backup()

        archive = self.out / ARCHIVE
        files = {entry["path"]: entry for entry in manifest(archive)["files"]}
        with tarfile.open(archive) as tar:
            data = tar.extractfile("home/.env").read()
        self.assertEqual(data, b"TELEGRAM_BOT_TOKEN=secreto\n")
        self.assertEqual(
            files["home/.env"],
            {
                "path": "home/.env",
                "size": 27,
                "sha256": hashlib.sha256(data).hexdigest(),
                "sqlite": False,
            },
        )
        self.assertIs(files["home/kanban.db"]["sqlite"], True)

    def test_a_database_is_found_by_its_header_not_its_name(self):
        db = self.home / "profiles/claw/memory_store"
        with contextlib.closing(sqlite3.connect(db)) as handle:
            handle.execute("create table m (v)")
            handle.commit()

        self.backup()

        files = {
            entry["path"]: entry for entry in manifest(self.out / ARCHIVE)["files"]
        }
        self.assertIs(files["home/profiles/claw/memory_store"]["sqlite"], True)
        self.assertIs(files["home/profiles/claw/config.yaml"]["sqlite"], False)

    def test_a_row_still_in_the_live_wal_is_in_the_copy(self):
        db_path = self.home / "profiles/claw/state.db"
        live = sqlite3.connect(db_path)
        self.addCleanup(live.close)
        live.execute("pragma journal_mode=wal")
        live.execute("pragma wal_autocheckpoint=0")
        live.execute("insert into t values ('solo en el wal')")
        live.commit()
        self.assertGreater(db_path.with_name("state.db-wal").stat().st_size, 0)

        code, line = self.backup()

        self.assertEqual(code, 0, line)
        names = set(members(self.out / ARCHIVE))
        self.assertNotIn("home/profiles/claw/state.db-wal", names)
        self.assertNotIn("home/profiles/claw/state.db-shm", names)
        restored = self.base / "restored.db"
        with tarfile.open(self.out / ARCHIVE) as tar:
            restored.write_bytes(tar.extractfile("home/profiles/claw/state.db").read())
        with contextlib.closing(sqlite3.connect(restored)) as copy:
            rows = copy.execute("select v from t order by rowid").fetchall()
        self.assertEqual(rows, [("uno",), ("solo en el wal",)])
        skipped = {entry["path"] for entry in manifest(self.out / ARCHIVE)["skipped"]}
        self.assertIn("home/profiles/claw/state.db-wal", skipped)

    def test_a_corrupt_database_aborts_without_archive_or_upload(self):
        corrupt_index(self.home / "profiles/claw/broken.db")
        receiver = make_receiver(self.base)

        code, line = self.backup(receiver)

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            "ERROR respaldo Hermes: la base home/profiles/claw/broken.db no pasa "
            "integrity_check: row 101 missing from index i\n",
        )
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [])
        self.assertEqual(calls(receiver), [])

    def test_success_uploads_the_same_bytes_with_the_backup_key(self):
        receiver = make_receiver(self.base)

        code, line = self.backup(receiver)

        archive = self.out / ARCHIVE
        self.assertEqual((code, line), (0, ""))
        self.assertEqual(
            (receiver / "received" / ARCHIVE).read_bytes(), archive.read_bytes()
        )
        key = self.home / "backup-key"
        self.assertEqual(
            [call["argv"] for call in calls(receiver)],
            [
                [
                    "-i",
                    str(key / "id_ed25519"),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    f"UserKnownHostsFile={key / 'known_hosts'}",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "clawbackup@10.13.13.1",
                    ARCHIVE,
                ]
            ],
        )

    def test_a_failed_upload_is_retried_then_reported_and_the_copy_kept(self):
        receiver = make_receiver(self.base, mode="fail")

        code, line = self.backup(receiver)

        archive = self.out / ARCHIVE
        failure = (
            "salida 255 ssh: connect to host 10.13.13.1 port 22: Connection timed out"
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            "ERROR respaldo Hermes: no se pudo subir a clawbackup@10.13.13.1: "
            f"intento 1: {failure}; intento 2: {failure}; intento 3: {failure}; "
            f"copia local en {archive}\n",
        )
        self.assertEqual(len(calls(receiver)), 3)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [ARCHIVE])
        self.assertEqual(manifest(archive)["format"], 1)

    @unittest.skipIf(os.name == "nt", "chmod 0 does not deny reads on Windows")
    def test_an_unreadable_file_is_uploaded_around_and_fails_the_run(self):
        login = self.home / "auth.json"
        login.chmod(0)
        self.addCleanup(login.chmod, 0o600)
        receiver = make_receiver(self.base)

        code, line = self.backup(receiver)

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            "ERROR respaldo Hermes: subido, pero 1 sin leer, p. ej. home/auth.json\n",
        )
        self.assertEqual(len(calls(receiver)), 1)
        self.assertEqual(
            manifest(self.out / ARCHIVE)["unreadable"],
            [{"path": "home/auth.json", "error": "Permission denied"}],
        )

    @unittest.skipIf(os.name == "nt", "chmod 0 does not deny reads on Windows")
    def test_an_unreadable_directory_fails_the_run(self):
        memories = self.home / "profiles/claw/memories"
        memories.chmod(0)
        self.addCleanup(memories.chmod, 0o700)

        code, line = self.backup()

        self.assertEqual(
            (code, line),
            (
                1,
                (
                    "ERROR respaldo Hermes: subido, pero 1 sin leer, "
                    "p. ej. home/profiles/claw/memories\n"
                ),
            ),
        )

    @unittest.skipIf(os.name == "nt", "chmod 0 does not deny reads on Windows")
    def test_files_a_live_process_holds_are_skipped_without_failing(self):
        held = [
            write(self.home / "profiles/claw/gateway.sock"),
            write(self.user / ".codex/app.guard"),
            write(self.home / "profiles/claw/cron/output/a1/20261015-0300.md", "x"),
            write(self.home / "gateway.lock"),
            write(self.home / "kanban/.dispatcher.lock"),
        ]
        for path in held:
            path.chmod(0)
            self.addCleanup(path.chmod, 0o600)

        code, line = self.backup()

        self.assertEqual((code, line), (0, ""))
        skipped = {
            entry["path"]: entry["reason"]
            for entry in manifest(self.out / ARCHIVE)["skipped"]
        }
        self.assertEqual(
            [
                skipped["home/profiles/claw/gateway.sock"],
                skipped["user/.codex/app.guard"],
                skipped["home/profiles/claw/cron/output/a1/20261015-0300.md"],
                skipped["home/gateway.lock"],
                skipped["home/kanban/.dispatcher.lock"],
            ],
            [
                "archivo de un proceso vivo",
                "archivo de un proceso vivo",
                "salida de un cron que se estaba escribiendo",
                "archivo de un proceso vivo",
                "archivo de un proceso vivo",
            ],
        )
        self.assertEqual(manifest(self.out / ARCHIVE)["unreadable"], [])

    @unittest.skipIf(os.name == "nt", "chmod 0 does not deny reads on Windows")
    def test_an_old_unreadable_cron_output_fails_the_run(self):
        output = write(self.home / "profiles/claw/cron/output/a1/old.md", "x")
        os.utime(output, (1_000_000_000, 1_000_000_000))
        output.chmod(0)
        self.addCleanup(output.chmod, 0o600)

        code, line = self.backup()

        self.assertEqual(code, 1)
        self.assertIn("home/profiles/claw/cron/output/a1/old.md", line)

    def test_a_wrong_remote_checksum_counts_as_a_failed_upload(self):
        code, line = self.backup(make_receiver(self.base, mode="badsum"))

        self.assertEqual(code, 1)
        self.assertIn("intento 3: sha256 remoto distinto (000000000000)", line)

    def test_one_transient_failure_still_ends_ok(self):
        receiver = make_receiver(self.base, fail_first=1)

        code, line = self.backup(receiver)

        self.assertEqual(code, 0, line)
        self.assertEqual(len(calls(receiver)), 2)

    def test_no_upload_starts_once_the_cron_budget_is_spent(self):
        receiver = make_receiver(self.base)

        code, line = self.backup(
            receiver, budget=100, clock=clock_jumping_once(self.out / ARCHIVE)
        )

        self.assertEqual(code, 1)
        self.assertIn("intento 1: sin tiempo antes del corte del cron", line)
        self.assertEqual(calls(receiver), [])

    def test_a_spent_budget_stops_collecting_before_anything_is_written(self):
        receiver = make_receiver(self.base)

        code, line = self.backup(receiver, budget=0)

        self.assertEqual(
            (code, line),
            (
                1,
                (
                    "ERROR respaldo Hermes: se acabo el tiempo (--budget 0 s) "
                    "juntando home; no se subio nada\n"
                ),
            ),
        )
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual(calls(receiver), [])

    def test_a_budget_spent_while_writing_aborts_without_a_partial_file(self):
        receiver = make_receiver(self.base)
        partial = self.out / f"{ARCHIVE}.tmp"

        code, line = self.backup(
            receiver, budget=100, clock=clock_jumping_once(partial)
        )

        self.assertEqual(
            (code, line),
            (
                1,
                (
                    "ERROR respaldo Hermes: se acabo el tiempo (--budget 100 s) "
                    "escribiendo home/.env; no se subio nada\n"
                ),
            ),
        )
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual(calls(receiver), [])

    def test_a_write_error_aborts_instead_of_passing_as_unreadable(self):
        real, added = backup_hermes._add, []

        def disk_full_on_second(*args, **kwargs):
            added.append(args[1])
            if len(added) == 2:
                raise OSError(28, "No space left on device")
            return real(*args, **kwargs)

        receiver = make_receiver(self.base)
        with mock.patch.object(backup_hermes, "_add", disk_full_on_second):
            code, line = self.backup(receiver)

        self.assertEqual(code, 1)
        self.assertIn("No space left on device", line)
        self.assertNotIn("sin leer", line)
        self.assertEqual(sorted(path.name for path in self.out.glob("*.tar.gz*")), [])
        self.assertEqual(calls(receiver), [])

    def test_leftovers_of_a_killed_run_are_removed(self):
        self.out.mkdir()
        (self.out / "hermes-backup-20261014.tar.gz.tmp").write_bytes(b"partial")
        write(self.out / ".trabajo-k1ll3d/db-0.sqlite", "partial")

        code, line = self.backup()

        self.assertEqual((code, line), (0, ""))
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), [ARCHIVE])

    def test_only_the_two_newest_local_archives_stay(self):
        self.out.mkdir()
        for name in (
            "hermes-backup-20261001.tar.gz",
            "hermes-backup-20261014.tar.gz",
            "hermes-backup-20260930.tar.gz",
            "notas.txt",
        ):
            (self.out / name).write_text("viejo", encoding="utf-8")

        code, line = self.backup()

        self.assertEqual(code, 0, line)
        self.assertEqual(
            sorted(path.name for path in self.out.iterdir()),
            ["hermes-backup-20261014.tar.gz", ARCHIVE, "notas.txt"],
        )

    def test_a_long_windows_path_gets_the_extended_length_prefix(self):
        deep = "C:\\Users\\ehven\\.codex\\skills\\" + "carpeta\\" * 40 + "SKILL.md"
        self.assertGreater(len(deep), 260)

        self.assertEqual(
            [
                backup_hermes.long_path(deep),
                backup_hermes.long_path("C:/Users/ehven/.claude/settings.json"),
                backup_hermes.long_path("\\\\gonserver\\share\\a.txt"),
                backup_hermes.long_path("\\\\?\\C:\\ya\\tiene"),
                backup_hermes.long_path("relativa\\a.txt"),
            ],
            [
                "\\\\?\\" + deep,
                "\\\\?\\C:\\Users\\ehven\\.claude\\settings.json",
                "\\\\?\\UNC\\gonserver\\share\\a.txt",
                "\\\\?\\C:\\ya\\tiene",
                "relativa\\a.txt",
            ],
        )

    def test_a_junction_is_recognized_without_is_junction(self):
        def entry(attributes):
            status = types.SimpleNamespace(st_file_attributes=attributes)
            return types.SimpleNamespace(
                is_symlink=lambda: False, stat=lambda follow_symlinks: status
            )

        self.assertEqual(
            [
                backup_hermes._is_link(entry(stat.FILE_ATTRIBUTE_REPARSE_POINT)),
                backup_hermes._is_link(entry(stat.FILE_ATTRIBUTE_DIRECTORY)),
            ],
            [True, False],
        )

    def test_the_installed_script_backs_up_the_whole_home_not_its_profile(self):
        script = self.home / "profiles/claw/scripts/backup_hermes.py"
        script.write_text("", encoding="utf-8")

        self.assertEqual(backup_hermes.default_home(script), self.home.resolve())


if __name__ == "__main__":
    unittest.main()
