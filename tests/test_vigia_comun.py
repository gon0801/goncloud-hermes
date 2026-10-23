from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vigia_comun  # noqa: E402


class TestEncola(unittest.TestCase):
    def test_deja_un_solo_archivo_listo_con_el_nombre_del_motor(self):
        with tempfile.TemporaryDirectory() as tmp:
            pend = Path(tmp) / "pendientes"
            vigia_comun.encola(pend, "hola", "vigila-tareas")
            archivos = list(pend.iterdir())
            self.assertEqual(len(archivos), 1)
            self.assertRegex(archivos[0].name, r"^\d+-vigila-tareas\.txt$")
            self.assertEqual(archivos[0].read_text(encoding="utf-8"), "hola")

    def test_nunca_deja_a_la_vista_un_archivo_a_medias(self):
        with tempfile.TemporaryDirectory() as tmp:
            pend = Path(tmp)
            vigia_comun.encola(pend, "a", "x")
            self.assertEqual([p for p in pend.iterdir() if p.name.startswith(".")], [])

    def test_si_el_renombre_falla_propaga_el_error_y_no_deja_nada_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            pend = Path(tmp)
            with mock.patch.object(vigia_comun.os, "replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    vigia_comun.encola(pend, "a", "x")
            self.assertEqual(
                [p for p in pend.iterdir() if not p.name.startswith(".")], []
            )


if __name__ == "__main__":
    unittest.main()
