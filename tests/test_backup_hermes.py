import contextlib
import hashlib
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from backup_support import (
    ARCHIVE,
    backup_hermes,
    calls,
    make_install,
    make_receiver,
    manifest,
    members,
    run_main,
    settings,
)
from goncloud_test_support import cleanup_temp_directory


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
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(code, 0)
        size = f"{archive.stat().st_size / 1_000_000:.0f} MB"
        self.assertEqual(
            line,
            f"OK respaldo Hermes {ARCHIVE}: {size}, 18 archivos, 2 bases, "
            f"subido a clawbackup@10.13.13.1 (sha256 {digest[:12]})\n",
        )
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

        code, line = self.backup(receiver, budget=0)

        self.assertEqual(code, 1)
        self.assertIn("intento 1: sin tiempo antes del corte del cron", line)
        self.assertEqual(calls(receiver), [])

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

    def test_the_installed_script_backs_up_the_whole_home_not_its_profile(self):
        script = self.home / "profiles/claw/scripts/backup_hermes.py"
        script.write_text("", encoding="utf-8")

        self.assertEqual(backup_hermes.default_home(script), self.home.resolve())


if __name__ == "__main__":
    unittest.main()
