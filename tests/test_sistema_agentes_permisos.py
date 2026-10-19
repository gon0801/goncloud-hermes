#!/usr/bin/env python3
"""El lector real de permisos de H (H4b3): toolsets efectivos = lo declarado
en permisos.json INTERSECTADO con lo que el config real del perfil le da.

Ninguna entrada amplía la autoridad: declarado sin respaldo del config no
otorga; sin config legible no hay toolsets; sin permisos.json no hay nada
(falla cerrada). La entrada unica es hogar.leer_permisos (lee, valida la
forma F5 y despues intersecta via raices.permisos); el ticker y las
herramientas la llaman a ella."""

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

from yaml_doble import YamlDoble, yaml_real  # noqa: E402

hogar = importlib.import_module("raices.hogar")
autoridad = importlib.import_module("raices.autoridad")

# El PyYAML real donde esté (la Gon-pc); si no, el doble del subset. Con
# cualquiera de los dos el lector corre el mismo código.
YAML_DE_PRUEBA = yaml_real or YamlDoble


def config_con_toolsets(clis):
    lineas = ["platform_toolsets:", "  cli:"]
    for cli in clis:
        lineas.append(f"    - {cli}")
    return "\n".join(lineas) + "\n"


class ToolsetsEfectivos(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directorio)
        self.home = Path(self.directorio.name)

    def instalar(self, datos, configs=None):
        (self.home / "raices").mkdir(exist_ok=True)
        (self.home / "raices" / "permisos.json").write_text(
            json.dumps(datos), encoding="utf-8"
        )
        for perfil, config in (configs or {}).items():
            carpeta = self.home / "profiles" / perfil
            carpeta.mkdir(parents=True)
            (carpeta / "config.yaml").write_text(config, encoding="utf-8")

    def leer(self):
        # Importar en caliente: otras pruebas purgan raices.* de sys.modules y
        # una referencia del arranque del archivo quedaria vieja.
        permisos = importlib.import_module("raices.permisos")
        with mock.patch.object(permisos, "yaml", YAML_DE_PRUEBA):
            return importlib.import_module("raices.hogar").leer_permisos(str(self.home))

    def gate(self, leido):
        self.assertIsNotNone(leido, "con permisos.json el lector no da None")
        return autoridad.PermisosH(leido)

    def test_el_write_declarado_sin_coding_real_queda_sin_write(self):
        self.instalar(
            {
                "acciones": ["read", "implement", "review"],
                "toolsets": {"implementer": ["read", "write"]},
                "comandos": ["git"],
            },
            configs={"implementer": config_con_toolsets(["connections", "kanban"])},
        )
        gate = self.gate(self.leer())
        self.assertEqual(
            gate.niega_perfil("implementer", "implement"),
            "toolset-faltante",
            "el config real no respalda el write declarado",
        )

    def test_el_write_declarado_con_coding_real_pasa(self):
        self.instalar(
            {"acciones": [], "toolsets": {"implementer": ["write"]}, "comandos": []},
            configs={
                "implementer": config_con_toolsets(
                    ["coding", "connections", "goncloud_team", "kanban"]
                )
            },
        )
        gate = self.gate(self.leer())
        self.assertIsNone(gate.niega_perfil("implementer", "implement"))

    def test_un_perfil_sin_config_queda_sin_toolsets(self):
        self.instalar({"acciones": [], "toolsets": {"implementer": ["read", "write"]}})
        gate = self.gate(self.leer())
        self.assertEqual(
            gate.niega_perfil("implementer", "read"),
            "perfil-sin-toolset",
        )

    def test_un_config_yaml_roto_no_revienta_y_queda_sin_toolsets(self):
        self.instalar(
            {"acciones": [], "toolsets": {"implementer": ["read"]}},
            configs={"implementer": "platform_toolsets: [sin cerrar"},
        )
        leido = self.leer()
        gate = self.gate(leido)
        self.assertEqual(
            gate.niega_perfil("implementer", "read"),
            "perfil-sin-toolset",
        )

    def test_sin_permisos_json_da_none(self):
        self.assertIsNone(self.leer())

    def test_la_tabla_file_da_write_y_read_y_write_solo_read(self):
        self.instalar(
            {
                "acciones": [],
                "toolsets": {"a": ["read", "write"], "b": ["read"]},
                "comandos": [],
            },
            configs={
                "a": config_con_toolsets(["file"]),
                "b": config_con_toolsets(["write", "web"]),
            },
        )
        gate = self.gate(self.leer())
        self.assertIsNone(gate.niega_perfil("a", "implement"))
        self.assertIsNone(gate.niega_perfil("a", "read"))
        self.assertIsNone(gate.niega_perfil("b", "read"))
        self.assertEqual(gate.niega_perfil("b", "implement"), "toolset-faltante")


class UnaSolaFuente(ToolsetsEfectivos):
    def test_las_herramientas_leyan_por_hogar(self):
        herramientas = importlib.import_module("raices.herramientas")
        hogar_actual = importlib.import_module("raices.hogar")
        with mock.patch.object(
            hogar_actual, "leer_permisos", return_value=None
        ) as lector:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    tienda = herramientas.abrir_tienda(tmp)
                except Exception:
                    tienda = None
                lector.assert_called_once()
                if tienda is not None:
                    tienda.cerrar()

    def test_la_forma_rota_no_llega_a_la_interseccion(self):
        # F5 primero: un documento con toolsets lista (forma que PermisosH no
        # lee) queda en None aunque hubiera configs reales que intersectar.
        self.instalar({"acciones": [], "toolsets": ["implementer"], "comandos": []})
        permisos = importlib.import_module("raices.permisos")
        with mock.patch.object(permisos, "toolsets_efectivos") as cruce:
            self.assertIsNone(self.leer())
        cruce.assert_not_called()


if __name__ == "__main__":
    unittest.main()
