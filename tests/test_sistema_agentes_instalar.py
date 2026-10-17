#!/usr/bin/env python3
"""El instalador y su reversa de la ruta de raíces (H4b3, para SUA-09H).

--aplicar deja el envoltorio del ticker en el perfil DEFAULT y el cron
ticker-raices (default: el scheduler le pone HERMES_HOME del perfil y
gateway_state.json vive en la raíz; en otro perfil la puerta cierra por
par-incierto). --revertir quita solo lo suyo: jamás borra datos. --check
no escribe nada."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALADOR = REPO / "scripts" / "instalar_raices.py"


def cargar_instalador():
    especificacion = importlib.util.spec_from_file_location(
        "instalar_raices", INSTALADOR
    )
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


class CorredorFalso:
    """El CLI hermes representado: cron list --json, cron create, cron
    remove. Registra cada llamada para que los tests afirmen los argv."""

    def __init__(self, crons=None):
        self.llamadas = []
        self.crons = list(crons or [])

    def __call__(self, argv):
        self.llamadas.append(list(argv))
        if argv[:3] == ["cron", "list", "--json"]:
            return 0, json.dumps([{"name": nombre} for nombre in self.crons])
        if argv[:2] == ["cron", "create"]:
            nombre = argv[argv.index("--name") + 1]
            self.crons.append(nombre)
            return 0, f"created {nombre}"
        if argv[:2] == ["cron", "remove"]:
            nombre = argv[2]
            if nombre in self.crons:
                self.crons.remove(nombre)
                return 0, f"removed {nombre}"
            return 1, f"no such cron: {nombre}"
        return 1, f"subcomando desconocido: {argv}"


class InstaladorDeRaices(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.home = Path(self.directorio.name) / "hermes"
        self.home.mkdir(parents=True)
        self.modulo = cargar_instalador()
        self.corredor = CorredorFalso()

    def aplicar(self):
        return self.modulo.main(
            ["--aplicar", "--home", str(self.home)], corredor=self.corredor
        )

    def revertir(self):
        return self.modulo.main(
            ["--revertir", "--home", str(self.home)], corredor=self.corredor
        )

    def crear_datos_del_operador(self):
        (self.home / "raices").mkdir(exist_ok=True)
        (self.home / "raices" / "permisos.json").write_text(
            json.dumps({"acciones": ["read"], "toolsets": {}, "comandos": []}),
            encoding="utf-8",
        )
        (self.home / "sistema-agentes.db").write_bytes(b"db de datos")
        (self.home / "kanban.db").write_bytes(b"kanban de datos")
        (self.home / "raices" / "proyecciones").mkdir()
        (self.home / "raices" / "proyecciones" / "root.json").write_text("{}", "utf-8")
        (self.home / "scripts").mkdir(exist_ok=True)
        (self.home / "scripts" / "backup-claw.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )

    def test_el_check_no_escribe_nada(self):
        self.crear_datos_del_operador()
        codigo = self.modulo.main(
            ["--check", "--home", str(self.home)], corredor=self.corredor
        )
        self.assertEqual(codigo, 0)
        self.assertEqual(self.corredor.llamadas, [["cron", "list", "--json"]])
        self.assertFalse((self.home / "profiles").exists(), "check no crea perfiles")
        self.assertEqual(self.corredor.crons, [])
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())
        self.assertEqual(
            json.loads((self.home / "raices" / "permisos.json").read_text("utf-8")),
            {"acciones": ["read"], "toolsets": {}, "comandos": []},
        )

    def test_aplicar_crea_el_cron_en_default_y_el_envoltorio(self):
        self.assertEqual(self.aplicar(), 0)
        create = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "create"]
        ]
        self.assertEqual(len(create), 1, "un solo create")
        self.assertNotIn("-p", create[0], "el cron es del perfil default")
        self.assertEqual(
            create[0],
            [
                "cron",
                "create",
                "every 1m",
                "--name",
                "ticker-raices",
                "--script",
                "scripts/ticker-raices.py",
                "--no-agent",
                "--deliver",
                "local",
            ],
        )
        envoltorio = self.home / "scripts" / "ticker-raices.py"
        self.assertTrue(envoltorio.exists())
        self.assertIn("cron_ticker", envoltorio.read_text(encoding="utf-8"))
        self.assertFalse(
            (self.home / "profiles").exists(),
            "default es la raíz del home: nunca se crea profiles/default",
        )
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(estado["cron"], "ticker-raices")
        self.assertEqual(estado["envoltorio"], "scripts/ticker-raices.py")

    def test_doble_aplicar_no_duplica_el_cron(self):
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(self.aplicar(), 0)
        creates = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "create"]
        ]
        self.assertEqual(len(creates), 1, "el segundo aplicar no crea otro cron")
        self.assertEqual(self.corredor.crons.count("ticker-raices"), 1)

    def test_aplicar_no_pisa_el_permisos_del_operador_y_sin_el_crea_la_plantilla(self):
        self.crear_datos_del_operador()
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(
            json.loads((self.home / "raices" / "permisos.json").read_text("utf-8")),
            {"acciones": ["read"], "toolsets": {}, "comandos": []},
            "los datos del operador son sagrados",
        )
        otro = tempfile.TemporaryDirectory()
        self.addCleanup(otro.cleanup)
        home2 = Path(otro.name) / "hermes"
        home2.mkdir()
        self.modulo.main(["--aplicar", "--home", str(home2)], corredor=self.corredor)
        plantilla = json.loads(
            (home2 / "raices" / "permisos.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            plantilla,
            {"acciones": [], "toolsets": {}, "comandos": [], "cobertura": {}},
            "la plantilla niega todo: el operador la abre a mano",
        )

    def test_revertir_quita_solo_lo_suyo_y_conserva_los_datos(self):
        self.crear_datos_del_operador()
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(self.revertir(), 0)
        self.assertEqual(self.corredor.crons, [])
        removes = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "remove"]
        ]
        self.assertEqual(removes, [["cron", "remove", "ticker-raices"]])
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertEqual(
            (self.home / "scripts" / "backup-claw.sh").read_text(encoding="utf-8"),
            "#!/bin/sh\nexit 0\n",
            "un script ajeno de scripts/ nunca se toca",
        )
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())
        self.assertTrue((self.home / "sistema-agentes.db").exists())
        self.assertTrue((self.home / "kanban.db").exists())
        self.assertTrue((self.home / "raices" / "permisos.json").exists())
        self.assertTrue((self.home / "raices" / "proyecciones" / "root.json").exists())
        self.assertEqual(
            json.loads((self.home / "raices" / "permisos.json").read_text("utf-8")),
            {"acciones": ["read"], "toolsets": {}, "comandos": []},
        )

    def test_revertir_sin_instalacion_no_toca_nada(self):
        self.crear_datos_del_operador()
        self.assertEqual(self.revertir(), 0)
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] != ["cron", "list"]
            ],
            [],
        )
        self.assertTrue((self.home / "raices" / "permisos.json").exists())


if __name__ == "__main__":
    unittest.main()
