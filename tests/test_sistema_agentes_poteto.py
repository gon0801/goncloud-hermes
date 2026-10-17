#!/usr/bin/env python3
"""Poteto fijado en H (I08, parte Mac): la copia contra el manifiesto de G, la
adaptación Hermes, el bloque de SOUL, la generación de managed.json, la
atestación de flota y la puerta de despacho sin poteto."""

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

from conexiones import rastrear_conexiones  # noqa: E402

compartido = importlib.import_module("raices.compartido")
poteto = importlib.import_module("raices.poteto")

POTETO = compartido.REPO / "shared/sistema-agentes/poteto"


def canonico(documento):
    return json.dumps(
        documento, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class CopiaFijada(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.manifiesto = json.loads(
            (POTETO / "poteto.v1.json").read_text(encoding="utf-8")
        )
        self.registro = json.loads((POTETO / "hermes.json").read_text(encoding="utf-8"))

    def test_cada_archivo_del_manifiesto_esta_en_la_copia_con_su_sha(self):
        excluidos = set(self.registro["excluidos"])
        for fila in self.manifiesto["files"]:
            if fila["path"] in excluidos:
                continue
            with self.subTest(path=fila["path"]):
                contenido = (POTETO / fila["path"]).read_bytes()
                self.assertEqual(hashlib_de(contenido), fila["sha256"])

    def test_los_excluidos_estan_documentados_y_no_publicables(self):
        self.assertEqual(
            set(self.registro["excluidos"]),
            {"skills/poteto-openclaw/SKILL.md"},
        )

    def test_el_manifiesto_tiene_el_digest_que_hermes_json_registra(self):
        recomputado = "sha256:" + hashlib_de(
            canonico({k: v for k, v in self.manifiesto.items() if k != "digest"})
        )
        self.assertEqual(self.manifiesto["digest"], recomputado)
        self.assertEqual(self.registro["manifest"]["digest"], recomputado)
        self.assertEqual(self.registro["generation"], self.manifiesto["generation"])

    def test_un_byte_cambiado_en_la_copia_se_detecta(self):
        with tempfile.TemporaryDirectory() as tmp:
            copia = Path(tmp) / "poteto"
            shutil.copytree(POTETO, copia)
            poteto.comprobar_copia(copia)
            ruta = copia / "skills/poteto-mode/SKILL.md"
            contenido = ruta.read_text(encoding="utf-8")
            victima = contenido.split()[0]
            ruta.write_text(
                contenido.replace(victima, victima + "x", 1), encoding="utf-8"
            )
            with self.assertRaises(poteto.CopiaRota):
                poteto.comprobar_copia(copia)

    def test_un_manifiesto_con_otro_digest_se_detecta(self):
        with tempfile.TemporaryDirectory() as tmp:
            copia = Path(tmp) / "poteto"
            shutil.copytree(POTETO, copia)
            manifiesto = json.loads(
                (copia / "poteto.v1.json").read_text(encoding="utf-8")
            )
            manifiesto["files"].append(
                {"path": "skills/falso/SKILL.md", "sha256": "0" * 64}
            )
            manifiesto.pop("digest")
            manifiesto["digest"] = "sha256:" + hashlib_de(canonico(manifiesto))
            (copia / "poteto.v1.json").write_text(
                json.dumps(manifiesto), encoding="utf-8"
            )
            with self.assertRaises(poteto.CopiaRota):
                poteto.comprobar_copia(copia)

    def test_el_bloque_cita_generacion_y_digest_y_cabe(self):
        bloque = (POTETO / "bloque-soul-hermes.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(bloque), 1500)
        self.assertIn(f"generacion={self.manifiesto['generation']}", bloque)
        self.assertIn(self.manifiesto["digest"], bloque)
        self.assertIn("poteto-mode", bloque)

    def test_la_adaptacion_hermes_esta_registrada_y_cubre_el_metodo(self):
        adaptacion = (POTETO / "adaptacion-hermes.md").read_text(encoding="utf-8")
        self.assertEqual(
            self.registro["adaptacion"],
            "sha256:" + hashlib_de(adaptacion.encode("utf-8")),
        )
        for obligatoria in ("poteto-mode", "playbook", "permisos", "read"):
            self.assertIn(obligatoria, adaptacion)

    def test_las_skills_publicadas_son_las_del_perfil_segun_g(self):
        publicadas = poteto.skills_a_publicar("ingenieria")
        self.assertIn("skills/poteto-mode/SKILL.md", publicadas)
        self.assertTrue(
            any("/principle-" in r for r in publicadas),
            "las principles viajan con el paquete",
        )
        for skill in ("architect", "how", "tdd"):
            self.assertTrue(
                any(r.startswith(f"skills/{skill}/") for r in publicadas),
                skill,
            )
        self.assertFalse(
            any(r.startswith("skills/why/") for r in publicadas),
            "una skill que el perfil no requiere no se publica",
        )
        propias = poteto.skills_a_publicar("reviewer")
        self.assertTrue(any(r.startswith("skills/blast-radius/") for r in propias))


def hashlib_de(datos: bytes) -> str:
    import hashlib

    return hashlib.sha256(datos).hexdigest()


class GeneracionDelDespliegue(unittest.TestCase):
    def test_un_despliegue_que_cambia_archivos_sube_generation(self):
        self.assertEqual(poteto.siguiente_generation(None, {"a": "1"}), 1)
        self.assertEqual(poteto.siguiente_generation({"generation": 3}, {"a": "2"}), 4)
        self.assertEqual(
            poteto.siguiente_generation(
                {"generation": 3, "files": {"a": "1"}}, {"a": "1"}
            ),
            3,
            "sin cambios no sube",
        )

    def test_flota_usa_la_generation_del_despliegue(self):
        with tempfile.TemporaryDirectory() as tmp:
            perfil = Path(tmp) / "profiles" / "codex"
            perfil.mkdir(parents=True)
            (perfil / "config.yaml").write_text(
                "model:\n  provider: p\n  default: m\n", encoding="utf-8"
            )
            (perfil / "goncloud-managed.json").write_text(
                json.dumps(
                    {
                        "source_revision": "abc",
                        "generation": 7,
                        "poteto": {"digest": "sha256:" + "a" * 64},
                    }
                ),
                encoding="utf-8",
            )
            doc = flota_construir(Path(tmp), "codex")
        self.assertEqual(doc["generation"], 7)
        self.assertEqual(
            doc["attestations"],
            [{"what": "poteto", "digest": "sha256:" + "a" * 64}],
        )

    def test_sin_paquete_no_hay_atestacion(self):
        with tempfile.TemporaryDirectory() as tmp:
            perfil = Path(tmp) / "profiles" / "codex"
            perfil.mkdir(parents=True)
            (perfil / "config.yaml").write_text(
                "model:\n  provider: p\n  default: m\n", encoding="utf-8"
            )
            (perfil / "goncloud-managed.json").write_text(
                json.dumps({"source_revision": "abc"}), encoding="utf-8"
            )
            doc = flota_construir(Path(tmp), "codex")
        self.assertEqual(doc["attestations"], [])


def flota_construir(home, specialist):
    modulo_flota = importlib.import_module("raices.flota")
    return modulo_flota.construir(
        "root-flota", "a", 1, specialist, home, permisos_h=None
    )


class PuertaDePoteto(unittest.TestCase):
    """I08: sin poteto, la ruta no se admite; el ticker no despacha y la
    causa queda visible en read."""

    def setUp(self):
        rastrear_conexiones(self)
        modulo_store = importlib.import_module("raices.store")
        modulo_ticker = importlib.import_module("raices.ticker")
        modulo_kanban = importlib.import_module("raices.kanban_port")
        self.store = modulo_store
        self.ticker = modulo_ticker
        self.kanban_port = modulo_kanban
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.home = Path(self.directorio.name)
        self.tienda = modulo_store.Store(self.home / "sistema-agentes.db")
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de_raiz())
        self.kanban = modulo_kanban.DobleKanban()

    def ligar(self):
        decision = {
            "expected_plan_revision": 1,
            "expected_control_epoch": 1,
            "consumidos": [],
            "claims": [],
            "outcomes": [],
            "bindings": [
                {
                    "unitId": "a",
                    "assignment": {
                        "target": {
                            "kind": "cli",
                            "hostId": "host-01",
                            "adapterId": "codex",
                        },
                        "instructionRef": {
                            "ref": "artifact:inst",
                            "digest": "sha256:" + "a" * 64,
                        },
                        "resultContract": "review.v1",
                        "inputRevision": {
                            "kind": "code",
                            "repository": "owner-01/repo-a",
                            "sha": "0" * 40,
                        },
                        "continuation": {"kind": "requester"},
                    },
                }
            ],
            "efectos": [],
            "contadores": [],
            "excepciones": [],
            "respuestas_aplicadas": [],
        }
        self.assertIsInstance(
            self.tienda.aplicar("root-poteto", decision, "dec-1"), self.store.Ok
        )

    def desplegar_poteto(self):
        perfil = self.home / "profiles" / "codex"
        perfil.mkdir(parents=True, exist_ok=True)
        (perfil / "goncloud-managed.json").write_text(
            json.dumps(
                {
                    "source_revision": "abc",
                    "generation": 2,
                    "poteto": {"digest": "sha256:" + "b" * 64},
                }
            ),
            encoding="utf-8",
        )

    def vuelta(self):
        permisos = {
            "acciones": ["read", "implement", "review", "push"],
            "toolsets": {"codex": ["read", "write"]},
            "comandos": ["git", "python3"],
        }
        return self.ticker.vuelta(
            self.tienda, self.kanban, {}, permisos_h=permisos, home=self.home
        )

    def test_sin_poteto_el_ticker_no_despacha_y_la_causa_sale_en_read(self):
        self.ligar()
        informe = self.vuelta()
        self.assertEqual(informe.tarjetas_creadas, 0)
        vista = self.tienda.leer_vista("root-poteto", {})
        causas = [
            e["subject"].get("cause", "")
            for e in vista["excepciones"]
            if isinstance(e.get("subject"), dict)
        ]
        self.assertTrue(any(c.startswith("poteto:") for c in causas), causas)

    def test_con_poteto_despacha_y_la_flota_trae_la_atestacion(self):
        self.desplegar_poteto()
        self.ligar()
        informe = self.vuelta()
        self.assertEqual(informe.tarjetas_creadas, 1)
        vista = self.tienda.leer_vista("root-poteto", {})
        flota_a = [u["flota"] for u in vista["unidades"] if u["unitId"] == "a"][0]
        self.assertEqual(
            flota_a["attestations"],
            [{"what": "poteto", "digest": "sha256:" + "b" * 64}],
        )


def meta_de_raiz():
    return {
        "schema": "sistema-agentes.meta.v1",
        "rootId": "root-poteto",
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
            "planDigest": "sha256:" + "a" * 64,
            "requestDigest": "sha256:" + "a" * 64,
            "units": [
                {
                    "unitId": "a",
                    "title": "Unidad a",
                    "dependsOn": [],
                    "criteria": [
                        {"criterionId": "a1", "text": "a1", "verifier": "verifier"}
                    ],
                    "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
                }
            ],
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


class Sonda(unittest.TestCase):
    """La sonda de poteto por perfil: estructura verificada aquí; la tarea de
    comportamiento representativa la corre el coordinador en la PC."""

    def perfil_desplegado(self, tmp):
        home = Path(tmp)
        copia = home / "shared/sistema-agentes/poteto"
        shutil.copytree(POTETO, copia)
        bloque = (copia / "bloque-soul-hermes.md").read_text(encoding="utf-8")
        (home / "SOUL.md").write_text("alma local\n\n" + bloque, encoding="utf-8")
        for ruta in poteto.skills_a_publicar("codex"):
            origen = POTETO / ruta
            final = home / ruta
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(origen, final)
        (home / "goncloud-managed.json").write_text(
            json.dumps(
                {
                    "source_revision": "abc",
                    "generation": 2,
                    "poteto": {"digest": poteto.digest_de_manifiesto()},
                }
            ),
            encoding="utf-8",
        )
        return home

    def test_un_perfil_bien_desplegado_no_da_fallas(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            self.assertEqual(poteto.sonar(Path(tmp), "codex"), [])

    def test_un_soul_sin_el_bloque_da_falla(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            (Path(tmp) / "SOUL.md").write_text("alma local\n", encoding="utf-8")
            fallas = poteto.sonar(Path(tmp), "codex")
        self.assertTrue(any("SOUL" in f for f in fallas), fallas)

    def test_una_skill_que_no_coincide_da_falla(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            ruta = Path(tmp) / "skills/poteto-mode/SKILL.md"
            ruta.write_text(ruta.read_text(encoding="utf-8") + "x", encoding="utf-8")
            fallas = poteto.sonar(Path(tmp), "codex")
        self.assertTrue(any("skills" in f for f in fallas), fallas)


if __name__ == "__main__":
    unittest.main()
