import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
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


if __name__ == "__main__":
    unittest.main()
