#!/usr/bin/env python3
"""El documento flota.v1 de cada intento admitido (I09, parte Mac) y la
cobertura del advisor (I18). La copia fijada del contrato de G valida todo
documento emitido; sus fixtures inválidas se rechazan."""

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

from conexiones import rastrear_conexiones  # noqa: E402

compartido = importlib.import_module("raices.compartido")
contrato = compartido.contrato
flota = importlib.import_module("raices.flota")
store = importlib.import_module("raices.store")
ticker = importlib.import_module("raices.ticker")
kanban_port = importlib.import_module("raices.kanban_port")
herramientas = importlib.import_module("raices.herramientas")

DobleKanban = kanban_port.DobleKanban
clave_de_tarjeta = kanban_port.clave_de_tarjeta

RAIZ = "root-flota"
DIGEST = "sha256:" + "a" * 64
REF = {"ref": "artifact:x", "digest": DIGEST}
FLOTA_DIR = compartido.G_DIR / "scripts/tests/fixtures/sistema-agentes/flota"


class YamlDePrueba:
    """El PyYAML del runtime de Hermes, representado en la Mac: el subset que
    configure_team escribe (bloques anidados de dos niveles, mapa en flujo)
    y YAML roto."""

    @staticmethod
    def safe_load(texto):
        if "[" in texto and "]" not in texto:
            raise ValueError("yaml roto")
        datos: dict = {}
        bloque = None
        subbloque = None
        for linea in texto.splitlines():
            if not linea.strip() or linea.strip().startswith("#"):
                continue
            sangria = len(linea) - len(linea.lstrip())
            limpia = linea.strip()
            clave, _, valor = limpia.partition(":")
            clave, valor = clave.strip(), valor.strip()
            if sangria == 0:
                if valor == "":
                    datos[clave] = {}
                    bloque, subbloque = clave, None
                elif valor.startswith("{") and valor.endswith("}"):
                    datos[clave] = YamlDePrueba._flujo(valor[1:-1])
                    bloque, subbloque = None, None
                else:
                    datos[clave] = valor
                    bloque, subbloque = None, None
            elif sangria <= 2:
                if bloque is None:
                    continue
                if valor == "":
                    datos[bloque][clave] = {}
                    subbloque = clave
                else:
                    datos[bloque][clave] = valor
            else:
                if subbloque is None:
                    continue
                datos[bloque][subbloque][clave] = valor
        return datos

    @staticmethod
    def _flujo(interior):
        mapa = {}
        for par in interior.split(","):
            clave, _, valor = par.partition(":")
            mapa[clave.strip()] = valor.strip()
        return mapa


def permisos_de_prueba():
    return {
        "acciones": ["read", "implement", "review", "push"],
        "toolsets": {"codex": ["write", "read"], "revisor": ["read"]},
        "comandos": ["git", "python3"],
    }


def meta_de():
    return {
        "schema": "sistema-agentes.meta.v1",
        "rootId": RAIZ,
        "requester": {"principal": ["david"], "agentId": "main"},
        "authority": {
            "schema": "sistema-agentes.autoridad.v1",
            "authorityRef": "run:turn-one",
            "grantedBy": "david",
            "grantedAt": "2026-10-17T00:00:00Z",
            "actions": ["read", "implement", "review", "push"],
            "repos": ["owner-01/repo-a"],
            "accounts": [],
            "destinations": ["github.com/owner-01/repo-a"],
            "deadline": None,
            "mode": "implement",
        },
        "budget": {
            "maxConcurrentTasks": 8,
            "maxDepth": 8,
            "maxChildren": 32,
            "maxModelCalls": 320,
            "maxInputTokens": 1000000,
            "maxOutputTokens": 100000,
            "maxCacheReadTokens": 1000000,
            "maxContextTokens": 100000,
            "maxTreeTokens": 1100000,
            "maxAutomaticRecoveryCalls": 8,
        },
        "plan": {
            "revision": 1,
            "planDigest": DIGEST,
            "requestDigest": DIGEST,
            "units": [
                {
                    "unitId": "a",
                    "title": "Unidad a",
                    "dependsOn": [],
                    "criteria": [
                        {"criterionId": "a1", "text": "a1", "verifier": "verifier"}
                    ],
                    "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
                },
                {
                    "unitId": "b",
                    "title": "Unidad b",
                    "dependsOn": ["a"],
                    "criteria": [
                        {"criterionId": "b1", "text": "b1", "verifier": "verifier"}
                    ],
                    "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
                },
            ],
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


def asignacion(adapter="codex", contract="review.v1"):
    return {
        "target": {"kind": "cli", "hostId": "host-01", "adapterId": adapter},
        "instructionRef": {"ref": f"artifact:inst-{adapter}", "digest": DIGEST},
        "resultContract": contract,
        "inputRevision": {
            "kind": "code",
            "repository": "owner-01/repo-a",
            "sha": "0" * 40,
        },
        "continuation": {"kind": "requester"},
    }


def decision(bindings=()):
    return {
        "expected_plan_revision": 1,
        "expected_control_epoch": 1,
        "consumidos": [],
        "claims": [],
        "outcomes": [],
        "bindings": list(bindings),
        "efectos": [],
        "contadores": [],
        "excepciones": [],
        "respuestas_aplicadas": [],
    }


def hechos_de_raiz():
    claims = [
        {
            "rootId": RAIZ,
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "planRevision": 1,
            "artifactRef": REF,
            "environment": {"host": "mac-local", "versions": {}},
            "role": "verifier",
        }
    ]
    return {"principal": ["david"], "asignaciones": [], "claims": claims, "efectos": []}


def escribir_perfil(home, perfil, config=None, gestionado=None, bytes_config=None):
    base = Path(home) / "profiles" / perfil
    base.mkdir(parents=True, exist_ok=True)
    if bytes_config is not None:
        (base / "config.yaml").write_bytes(bytes_config)
    elif config is not None:
        lineas = "model:\n"
        if "provider" in config:
            lineas += f"  provider: {config['provider']}\n"
        if "default" in config:
            lineas += f"  default: {config['default']}\n"
        (base / "config.yaml").write_text(lineas, encoding="utf-8")
    if gestionado is not None:
        (base / "goncloud-managed.json").write_text(
            json.dumps(gestionado), encoding="utf-8"
        )
    return base


class EscenarioBase(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.home = Path(self.directorio.name)
        self.tienda = store.Store(self.home / "sistema-agentes.db")
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de())
        self.kanban = DobleKanban()
        parche = mock.patch.object(flota, "yaml", YamlDePrueba, create=True)
        parche.start()
        self.addCleanup(parche.stop)

    def ligar_a(self, adapter="codex", contract="review.v1"):
        resultado = self.tienda.aplicar(
            RAIZ,
            decision([{"unitId": "a", "assignment": asignacion(adapter, contract)}]),
            "dec-1",
        )
        self.assertIsInstance(resultado, store.Ok)

    def vuelta(self):
        return ticker.vuelta(
            self.tienda,
            self.kanban,
            {RAIZ: hechos_de_raiz()},
            permisos_h=permisos_de_prueba(),
            home=self.home,
        )

    def flota_de_vista(self, attempt=1):
        vista = self.tienda.leer_vista(RAIZ, hechos_de_raiz())
        unidad = next(u for u in vista["unidades"] if u["attempt"] == attempt)
        return unidad.get("flota")


class ModeloDelPerfil(EscenarioBase):
    def test_modelo_exacto_y_documento_valido(self):
        escribir_perfil(
            self.home,
            "codex",
            config={"provider": "proveedor-01", "default": "modelo-a"},
            gestionado={"source_revision": "abc123", "generation": 3},
        )
        self.ligar_a()
        self.vuelta()
        doc = self.flota_de_vista()
        self.assertEqual(
            doc,
            {
                "schema": contrato.FLOTA,
                "rootId": RAIZ,
                "unitId": "a",
                "attempt": 1,
                "specialist": "codex",
                "host": "gon-pc",
                "executor": "cli",
                "accountRef": None,
                "model": {"id": "proveedor-01/modelo-a"},
                "generation": 3,
                "permissions": ["read", "write"],
                "attestations": [],
                "sourceRevision": "abc123",
            },
        )
        self.assertEqual(contrato.validar(doc), [])

    def test_modelo_incompleto_da_null_con_razon(self):
        escribir_perfil(
            self.home,
            "codex",
            config={"provider": "proveedor-01"},
            gestionado={"source_revision": "abc123"},
        )
        self.ligar_a()
        self.vuelta()
        doc = self.flota_de_vista()
        self.assertIsNone(doc["model"]["id"])
        self.assertIsInstance(doc["model"]["reason"], str)
        self.assertTrue(doc["model"]["reason"])
        self.assertEqual(contrato.validar(doc), [])

    def test_config_ilegible_da_null_con_razon(self):
        escribir_perfil(
            self.home,
            "codex",
            bytes_config=b"\xff\xfe\xff\x00model:",
            gestionado={"source_revision": "abc123"},
        )
        self.ligar_a()
        self.vuelta()
        doc = self.flota_de_vista()
        self.assertIsNone(doc["model"]["id"])
        self.assertTrue(doc["model"]["reason"])
        self.assertEqual(contrato.validar(doc), [])

    def test_snapshot_congelado_tras_cambiar_config(self):
        escribir_perfil(
            self.home,
            "codex",
            config={"provider": "proveedor-01", "default": "modelo-a"},
            gestionado={"source_revision": "abc123", "generation": 3},
        )
        self.ligar_a()
        self.vuelta()
        congelada = self.flota_de_vista()
        escribir_perfil(
            self.home,
            "codex",
            config={"provider": "proveedor-02", "default": "modelo-b"},
            gestionado={"source_revision": "nueva", "generation": 9},
        )
        self.vuelta()
        self.vuelta()
        self.assertEqual(self.flota_de_vista(), congelada)
        self.assertEqual(self.flota_de_vista()["model"]["id"], "proveedor-01/modelo-a")

    def test_sin_perfil_en_el_home_no_impide_la_admision(self):
        self.ligar_a()
        informe = self.vuelta()
        self.assertEqual(informe.tarjetas_creadas, 1)
        doc = self.flota_de_vista()
        self.assertIsNone(doc["model"]["id"])
        self.assertTrue(doc["model"]["reason"])
        self.assertEqual(contrato.validar(doc), [])

    def test_todo_documento_emitido_pasa_contrato_y_las_invalidas_de_g_se_rechazan(
        self,
    ):
        escribir_perfil(
            self.home,
            "codex",
            config={"provider": "proveedor-01", "default": "modelo-a"},
            gestionado={"source_revision": "abc123", "generation": 3},
        )
        self.ligar_a()
        self.vuelta()
        emitido = self.flota_de_vista()
        self.assertIsNotNone(emitido)
        self.assertEqual(contrato.validar(emitido), [])
        for fixture in sorted(FLOTA_DIR.glob("*.json")):
            documento = json.loads(fixture.read_text(encoding="utf-8"))
            with self.subTest(fixture=fixture.name):
                valido = contrato.validar(documento) == []
                self.assertEqual(valido, "invalida" not in fixture.name)

    def test_clave_default_anidada_mas_adentro_no_se_confunde(self):
        """Una `default` bajo otra clave del config no es el modelo del perfil."""
        escribir_perfil(
            self.home,
            "codex",
            gestionado={"source_revision": "abc123"},
        )
        perfil = self.home / "profiles" / "codex"
        (perfil / "config.yaml").write_text(
            "model:\n  provider: proveedor-01\n  extras:\n    default: z\n",
            encoding="utf-8",
        )
        self.ligar_a()
        self.vuelta()
        doc = self.flota_de_vista()
        self.assertIsNone(doc["model"]["id"], "el default ajeno no es el modelo")
        self.assertEqual(contrato.validar(doc), [])

    def test_la_forma_en_flujo_se_lee(self):
        escribir_perfil(
            self.home,
            "codex",
            gestionado={"source_revision": "abc123"},
        )
        perfil = self.home / "profiles" / "codex"
        (perfil / "config.yaml").write_text(
            "model: {provider: proveedor-01, default: modelo-a}\n",
            encoding="utf-8",
        )
        self.ligar_a()
        self.vuelta()
        doc = self.flota_de_vista()
        self.assertEqual(doc["model"], {"id": "proveedor-01/modelo-a"})

    def test_yaml_roto_da_razon(self):
        escribir_perfil(
            self.home,
            "codex",
            gestionado={"source_revision": "abc123"},
        )
        perfil = self.home / "profiles" / "codex"
        (perfil / "config.yaml").write_text(
            "model: [proveedor-01, modelo-a\n", encoding="utf-8"
        )
        self.ligar_a()
        self.vuelta()
        doc = self.flota_de_vista()
        self.assertIsNone(doc["model"]["id"])
        self.assertEqual(doc["model"]["reason"], "config-ilegible")

    def test_sin_yaml_disponible_da_razon(self):
        escribir_perfil(
            self.home,
            "codex",
            config={"provider": "proveedor-01", "default": "modelo-a"},
            gestionado={"source_revision": "abc123"},
        )
        with mock.patch.object(flota, "yaml", None):
            self.ligar_a()
            self.vuelta()
        doc = self.flota_de_vista()
        self.assertIsNone(doc["model"]["id"])
        self.assertTrue(doc["model"]["reason"])


class Advisor(EscenarioBase):
    def test_perfil_sin_eventos_declara_cobertura_y_read_la_muestra(self):
        self.ligar_a()
        self.vuelta()
        respuesta = herramientas.leer_raiz({"rootId": RAIZ}, "claw", self.home)
        unidades = respuesta["unidades"]
        self.assertEqual(
            [u.get("cobertura") for u in unidades if u["unitId"] == "a"],
            ["unknown"],
        )

    def test_la_cobertura_declarada_en_permisos_se_muestra_en_read(self):
        self.ligar_a()
        self.vuelta()
        raices = self.home / "raices"
        raices.mkdir(parents=True, exist_ok=True)
        (raices / "permisos.json").write_text(
            json.dumps(
                {
                    "acciones": ["read", "implement", "review", "push"],
                    "toolsets": {"codex": ["read", "write"]},
                    "comandos": ["git", "python3"],
                    "cobertura": {"codex": "checkpoint-only"},
                }
            ),
            encoding="utf-8",
        )
        respuesta = herramientas.leer_raiz({"rootId": RAIZ}, "claw", self.home)
        self.assertEqual(
            [u.get("cobertura") for u in respuesta["unidades"] if u["unitId"] == "a"],
            ["checkpoint-only"],
        )

    def test_revision_muerta_no_aprueba_ni_despacha(self):
        self.ligar_a()
        informe = self.vuelta()
        self.assertEqual(informe.tarjetas_creadas, 1)
        tarjeta = self.kanban.por_clave(clave_de_tarjeta(RAIZ, "a", 1))
        self.kanban.crear_hecha(tarjeta.clave, {})
        self.vuelta()
        vista = self.tienda.leer_vista(RAIZ, hechos_de_raiz())
        estado_a = [u["state"] for u in vista["unidades"] if u["unitId"] == "a"]
        tarjeta_b = self.kanban.por_clave(clave_de_tarjeta(RAIZ, "b", 1))
        self.assertEqual(vista["resultados"], [], "sin resultado no hay veredicto")
        self.assertNotIn("accepted", estado_a)
        self.assertIsNone(tarjeta_b, "la unidad dependiente no se despacha")


class MuerteSinResultado(EscenarioBase):
    def test_una_implementacion_muerta_sin_resultado_se_reintenta(self):
        """La muerte sin resultado reintenta el mismo intento como cualquier
        incidencia de transporte (mismo tope que invalid-result): el kind
        result-missing distingue en la evidencia, no cambia la política."""
        self.ligar_a(adapter="codex", contract="implement.v1")
        informe = self.vuelta()
        self.assertEqual(informe.tarjetas_creadas, 1)
        tarjeta = self.kanban.por_clave(clave_de_tarjeta(RAIZ, "a", 1))
        self.kanban.crear_hecha(tarjeta.clave, {})
        informe2 = self.vuelta()
        self.assertEqual(
            informe2.tarjetas_creadas,
            1,
            "la muerte sin resultado se reintenta como antes",
        )
        tarjeta_2 = self.kanban.por_clave(clave_de_tarjeta(RAIZ, "a", 2))
        self.assertIsNotNone(tarjeta_2)


if __name__ == "__main__":
    unittest.main()
