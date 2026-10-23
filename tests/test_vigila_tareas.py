import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vigila_tareas as module  # noqa: E402

DATOS = ROOT / "tests" / "datos" / "vigia"

CAMPOS_LAUNCHD = {
    "label",
    "periodico",
    "keepalive",
    "cargado",
    "state",
    "runs",
    "last_exit_code",
    "rutas_que_faltan",
}
PREFIJOS = ("ai.goncloud.", "cc.goncloud.", "com.goncloud.", "claw-", "ai.openclaw.")
CLAVES_PROHIBIDAS = {
    "message",
    "prompt",
    "env",
    "argv",
    "token",
    "toolsAllow",
    "origin",
}
SECRETO = re.compile(
    r"sk-[A-Za-z0-9]{8}|ghp_|xox[bp]-|Bearer |\d{9,}:[\w-]{30,}|[\w.+-]+@[\w-]+\.\w+|6470689715"
)


def carga(nombre):
    return json.loads((DATOS / nombre).read_text(encoding="utf-8"))


def claves(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from claves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from claves(v)


class TestDatosLimpios(unittest.TestCase):
    def test_launchd_de_mac_y_mini_traen_los_campos_y_solo_los_prefijos(self):
        for nombre in ("mac-launchd.json", "mini-launchd.json"):
            filas = carga(nombre)
            self.assertTrue(filas, nombre)
            for fila in filas:
                self.assertEqual(set(fila), CAMPOS_LAUNCHD, nombre)
                self.assertTrue(fila["label"].startswith(PREFIJOS), fila["label"])
                self.assertIsInstance(fila["rutas_que_faltan"], list)

    def test_el_cron_de_la_mini_trae_todas_las_paginas(self):
        datos = carga("mini-cron.json")
        self.assertEqual(len(datos["jobs"]), datos["total"])
        self.assertGreater(datos["total"], 25)

    def test_los_trabajos_de_la_pc_traen_su_ultima_salida(self):
        for job in carga("pc-hermes-claw-jobs.json")["jobs"]:
            self.assertTrue((DATOS / job["ultima_salida"]).is_file(), job["name"])

    def test_no_queda_ningun_prompt_ni_secreto(self):
        archivos = [p for p in DATOS.rglob("*") if p.is_file()]
        self.assertGreaterEqual(len(archivos), 6)
        for ruta in archivos:
            texto = ruta.read_text(encoding="utf-8")
            self.assertIsNone(SECRETO.search(texto), ruta.name)
            if ruta.suffix == ".json":
                self.assertFalse(
                    CLAVES_PROHIBIDAS & set(claves(json.loads(texto))), ruta.name
                )
            if ruta.suffix == ".md" and ruta.name != "LEEME.md":
                self.assertNotIn("## 0. ", texto, ruta.name)

    def test_el_leeme_nombra_cada_archivo(self):
        leeme = (DATOS / "LEEME.md").read_text(encoding="utf-8")
        self.assertIn("2026-10-22", leeme)
        for ruta in DATOS.glob("*.json"):
            self.assertIn(ruta.name, leeme)


class TestPolitica(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT / "tareas-programadas.json").read_text("utf-8"))

    def carga(self, datos):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "tareas-programadas.json"
            ruta.write_text(json.dumps(datos), encoding="utf-8")
            return module.Politica.carga(ruta)

    def test_la_politica_del_repo_carga(self):
        pol = module.Politica.carga(ROOT / "tareas-programadas.json")
        self.assertEqual(pol.ventana.zona, "America/Los_Angeles")
        self.assertEqual((pol.ventana.desde, pol.ventana.hasta), ("03:45", "05:10"))
        self.assertEqual((pol.claw_rss_mb, pol.chrome_perfil_mb), (3584, 3072))
        self.assertEqual(pol.marca_fallo, "FALLO:")
        self.assertIn("ai.goncloud.vigila-modelos", pol.salidas_ok)
        self.assertEqual(len(pol.modelo_propio), 4)

    def test_una_clave_de_mas_falla_y_nombra_la_clave(self):
        for ruta, clave in ((None, "intrusa"), ("limites", "limites.intrusa")):
            datos = json.loads(json.dumps(self.base))
            (datos if ruta is None else datos[ruta])["intrusa"] = 1
            with self.assertRaisesRegex(module.PoliticaError, clave):
                self.carga(datos)

    def test_un_tipo_equivocado_falla_y_nombra_la_clave(self):
        casos = (
            ("limites", "claw_rss_mb", "3584", "limites.claw_rss_mb"),
            ("limites", "chrome_perfil_mb", True, "limites.chrome_perfil_mb"),
            ("ventana_reinicio", "desde", "25:99", "ventana_reinicio.desde"),
            ("launchd", "prefijos", "ai.", "launchd.prefijos"),
            (None, "marca_fallo", 1, "marca_fallo"),
        )
        for padre, clave, valor, esperada in casos:
            datos = json.loads(json.dumps(self.base))
            (datos if padre is None else datos[padre])[clave] = valor
            with self.assertRaisesRegex(module.PoliticaError, esperada):
                self.carga(datos)

    def test_una_clave_que_falta_falla(self):
        del self.base["marca_fallo"]
        with self.assertRaisesRegex(module.PoliticaError, "marca_fallo"):
            self.carga(self.base)


if __name__ == "__main__":
    unittest.main()
