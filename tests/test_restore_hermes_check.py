import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from backup_support import (
    ARCHIVE,
    ROOT,
    make_install,
    make_receiver,
    run_main,
    settings,
    write,
)
from goncloud_test_support import cleanup_temp_directory

sys.path.insert(0, str(ROOT / "scripts"))

import restore_hermes_check  # noqa: E402


def rewrite(archive, changes):
    """Replace or drop members; changed members get a matching manifest entry."""
    with tarfile.open(archive) as tar:
        contents = {member.name: tar.extractfile(member).read() for member in tar}
    manifest = json.loads(contents.pop("manifest.json"))
    entries = {entry["path"]: entry for entry in manifest["files"]}
    for name, (data, fix_manifest) in changes.items():
        if data is None:
            contents.pop(name)
            continue
        contents[name] = data
        if fix_manifest:
            entries[name].update(
                size=len(data), sha256=hashlib.sha256(data).hexdigest()
            )
    contents["manifest.json"] = json.dumps(manifest).encode()
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


class RestoreDrillTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directory)
        self.base = Path(self.directory.name)
        self.home, self.user = make_install(self.base)
        self.archive = self.base / "respaldo" / ARCHIVE
        self.workdir = self.base / "drill"
        self.workdir.mkdir()

    def backup(self):
        receiver = make_receiver(self.base)
        code, line = run_main(settings(self.home, self.user, self.base, receiver))
        self.assertEqual(code, 0, line)

    def drill(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore_hermes_check.main(
                [str(self.archive), "--workdir", str(self.workdir)]
            )
        return code, out.getvalue()

    def test_a_fresh_backup_restores_and_is_counted(self):
        self.backup()

        code, line = self.drill()

        self.assertEqual(
            line,
            f"OK restauracion de prueba {ARCHIVE}: 2 perfiles, 3 crons, 2 bases, 18 archivos\n",
        )
        self.assertEqual(code, 0)
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_a_file_that_differs_from_the_manifest_is_named(self):
        self.backup()
        rewrite(
            self.archive, {"home/profiles/claw/memories/MEMORY.md": (b"MEMORIA", False)}
        )

        code, line = self.drill()

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            f"ERROR restauracion de prueba {ARCHIVE}: 1 problemas: "
            "home/profiles/claw/memories/MEMORY.md: no coincide con el manifiesto\n",
        )

    def test_a_missing_file_is_named(self):
        self.backup()
        rewrite(self.archive, {"home/auth.json": (None, False)})

        code, line = self.drill()

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            f"ERROR restauracion de prueba {ARCHIVE}: 1 problemas: home/auth.json: falta\n",
        )

    def test_a_corrupt_database_is_named(self):
        self.backup()
        broken = self.base / "broken.db"
        with contextlib.closing(sqlite3.connect(broken)) as db:
            db.execute("create table t (v text)")
            db.execute("create index i on t (v)")
            db.executemany(
                "insert into t values (?)", [(f"fila-{n:04d}",) for n in range(200)]
            )
            db.commit()
        data = bytearray(broken.read_bytes())
        at = data.rfind(b"fila-0100", 2 * 4096, 3 * 4096)
        data[at : at + 9] = b"fila-9999"
        rewrite(self.archive, {"home/kanban.db": (bytes(data), True)})

        code, line = self.drill()

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            f"ERROR restauracion de prueba {ARCHIVE}: 1 problemas: "
            "home/kanban.db: integrity_check row 101 missing from index i\n",
        )

    def test_unreadable_config_and_env_are_named_without_their_values(self):
        write(self.home / "profiles/scout/config.yaml", "model: [sin cerrar\n")
        write(self.home / "profiles/claw/.env", "A=1\nTOKEN secreto-sin-igual\n")
        self.backup()

        code, line = self.drill()

        self.assertEqual(code, 1)
        self.assertIn(
            f"ERROR restauracion de prueba {ARCHIVE}: 2 problemas: "
            "home/profiles/claw/.env: linea 2 no es CLAVE=valor; "
            "home/profiles/scout/config.yaml: no se puede leer: ",
            line,
        )
        self.assertNotIn("secreto", line)

    @unittest.skipIf(os.name == "nt", "chmod 0 does not deny reads on Windows")
    def test_a_file_the_backup_could_not_read_fails_the_drill(self):
        memory = self.home / "profiles/claw/memories/MEMORY.md"
        memory.chmod(0)
        self.addCleanup(memory.chmod, 0o600)
        receiver = make_receiver(self.base)
        code, _ = run_main(settings(self.home, self.user, self.base, receiver))
        self.assertEqual(code, 1)

        code, line = self.drill()

        self.assertEqual(
            (code, line),
            (
                1,
                f"ERROR restauracion de prueba {ARCHIVE}: 1 problemas: "
                "home/profiles/claw/memories/MEMORY.md: no se pudo leer al "
                "respaldar\n",
            ),
        )

    def test_an_entry_that_escapes_the_temp_dir_stops_the_drill(self):
        self.archive.parent.mkdir(parents=True)
        with tarfile.open(self.archive, "w:gz") as tar:
            info = tarfile.TarInfo("../fuera.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))

        code, line = self.drill()

        self.assertEqual(code, 1)
        self.assertEqual(
            line,
            f"ERROR restauracion de prueba {ARCHIVE}: entrada insegura en el archivo: ../fuera.txt\n",
        )
        self.assertFalse((self.base / "fuera.txt").exists())
        self.assertEqual(list(self.workdir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
