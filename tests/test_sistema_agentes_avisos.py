#!/usr/bin/env python3
"""El emisor de excepciones de H (§3.5) y la pasada que comparte estado con la
proyección (I14): texto armado como en G, reaviso por renoticeAfterMs y
checkpoint que solo avanza con messageId positivo."""

import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

store = importlib.import_module("raices.store")
proyeccion = importlib.import_module("raices.proyeccion")
decidir = importlib.import_module("raices.decidir")

RAIZ = "root-aviso"
AHORA = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64
REF = {"ref": "artifact:x", "digest": DIGEST}


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
            "actions": ["read", "implement", "review"],
            "repos": ["owner-01/repo-a"],
            "accounts": [],
            "destinations": ["dest-a"],
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
                        {
                            "criterionId": "a1",
                            "text": "a1",
                            "verifier": "verifier",
                        }
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


class Emisor(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.instante_actual = "2026-10-17T12:00:00Z"
        self.tienda = store.Store(
            Path(self.directorio.name) / "db.sqlite",
            reloj=lambda: self.instante_actual,
        )
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de())
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "NECESITA_DAVID",
            {"kind": "rounds", "unitId": "a", "block": 0},
            ["seguir", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(self.tienda.aplicar(RAIZ, dec, "dec-exc"), store.Ok)
        self.exception_id = borrador["exceptionId"]

    def avisar(self, ahora=AHORA, mensaje_id=42, tienda=None):
        mensajes = []

        def enviar(texto):
            mensajes.append(texto)
            return mensaje_id

        informe = proyeccion.avisar_excepciones(
            tienda or self.tienda, ahora=ahora, enviar=enviar
        )
        return informe, mensajes

    def test_la_excepcion_duele_sale_con_el_texto_de_g(self):
        informe, mensajes = self.avisar()
        self.assertEqual(informe["errores"], [])
        self.assertEqual(informe["avisadas"], [self.exception_id])
        self.assertEqual(informe["messageId"], 42)
        self.assertEqual(len(mensajes), 1)
        fila = self.tienda._una_fila(
            "SELECT raised_at FROM excepciones WHERE exception_id = ?",
            (self.exception_id,),
        )
        self.assertEqual(fila["raised_at"], "2026-10-17T12:00:00Z")
        texto = mensajes[0]
        self.assertTrue(texto.startswith("⚠️ *Excepciones sin respuesta* (1)"))
        self.assertIn(f"*{RAIZ}/a · NECESITA_DAVID ({self.exception_id})", texto)
        self.assertIn("· desde 12:00Z", texto)
        self.assertIn(
            "responder: omitir=Omitirlo sin aceptarlo."
            " | cancelar=Cancelar. | seguir=Seguir..",
            texto,
        )

    def test_el_aviso_notado_no_vuelve_hasta_el_renotice(self):
        self.avisar()
        informe, mensajes = self.avisar(ahora=AHORA + timedelta(minutes=30))
        self.assertEqual(informe["avisadas"], [])
        self.assertEqual(mensajes, [])
        informe, mensajes = self.avisar(ahora=AHORA + timedelta(milliseconds=3600001))
        self.assertEqual(informe["avisadas"], [self.exception_id])
        self.assertEqual(informe["messageId"], 42)

    def test_sin_confirmacion_el_checkpoint_no_avanza(self):
        informe, mensajes = self.avisar(mensaje_id=None)
        self.assertEqual(len(mensajes), 1)
        self.assertIn("el aviso no se confirmó", informe["errores"][0])
        self.assertIsNone(informe["messageId"])
        fila = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (self.exception_id,),
        )
        self.assertIsNone(fila["noticed_at"])
        informe, _ = self.avisar(mensaje_id=7)
        self.assertEqual(informe["avisadas"], [self.exception_id])

    def test_una_malformada_se_salta_con_su_error_y_no_tumba_el_aviso(self):
        self.tienda._conexion.execute(
            "INSERT INTO excepciones (exception_id, root_id, unit_id, type,"
            " subject_json, options_json, recipient_json, raised_at,"
            " renotice_after_ms)"
            " VALUES ('exc-rota', ?, 'a', 'INESPERADO', '{}',"
            ' \'[{"optionId": "seguir", "text": "Seguir."}]\','
            ' \'{"channel": "telegram", "principal": ["david"]}\','
            " 'no-es-fecha', 3600000)",
            (RAIZ,),
        )
        informe, mensajes = self.avisar()
        self.assertEqual(informe["avisadas"], [self.exception_id])
        self.assertTrue(
            any("exc-rota" in error for error in informe["errores"]), informe
        )
        self.assertEqual(len(mensajes), 1)
        self.assertNotIn("exc-rota", mensajes[0])

    def test_pasada_comparte_estado_entre_aviso_y_proyeccion(self):
        mensajes = []

        def enviar(texto):
            mensajes.append(texto)
            return 11

        informe = proyeccion.pasada(
            self.tienda,
            ahora=AHORA,
            dir_destino=Path(self.directorio.name) / "raices",
            enviar=enviar,
            stale_after_ms=300000,
            intervalo_ms=300000,
            host_gateway="mac-local",
        )
        self.assertEqual(informe["aviso"]["avisadas"], [self.exception_id])
        doc = informe["proyecciones"][RAIZ]
        fila = doc["units"][0]
        self.assertEqual(fila["nextCheckAt"], "2026-10-17T12:00:00Z")
        segunda = proyeccion.pasada(
            self.tienda,
            ahora=AHORA + timedelta(minutes=1),
            dir_destino=Path(self.directorio.name) / "raices",
            enviar=enviar,
            stale_after_ms=300000,
            intervalo_ms=300000,
            host_gateway="mac-local",
        )
        self.assertEqual(segunda["aviso"]["avisadas"], [])
        fila = segunda["proyecciones"][RAIZ]["units"][0]
        self.assertEqual(fila["nextCheckAt"], "2026-10-17T13:00:00Z")
        self.assertEqual(len(mensajes), 1)


class CapacidadIncierta(unittest.TestCase):
    """El aviso de ruta cerrada (H4a): una vez por cambio de causa, con el
    checkpoint en disco y sin confirmación no avanza."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.ruta = Path(self.directorio.name) / "capacidad-incierta.json"
        self.textos: list = []

    def enviar(self, mensaje_id=7):
        def enviar(texto):
            self.textos.append(texto)
            return mensaje_id

        return enviar

    def avisar(self, causa="version-no-certificada", mensaje_id=7, ruta=None):
        return proyeccion.avisar_capacidad_incierta(
            ruta or self.ruta, causa, AHORA, self.enviar(mensaje_id)
        )

    def test_la_primera_vez_avisa_y_deja_estado(self):
        informe = self.avisar()
        self.assertEqual(informe, {"avisada": True, "messageId": 7})
        self.assertEqual(len(self.textos), 1)
        self.assertIn("CAPACIDAD_INCIERTA", self.textos[0])
        self.assertIn("version-no-certificada", self.textos[0])
        estado = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(estado["causa"], "version-no-certificada")
        self.assertEqual(estado["messageId"], 7)
        self.assertEqual(estado["avisadaEn"], "2026-10-17T12:00:00Z")

    def test_la_misma_causa_no_vuelve_a_avisar(self):
        self.avisar(causa="par-incierto")
        informe = self.avisar(causa="par-incierto")
        self.assertEqual(informe, {"avisada": False})
        self.assertEqual(len(self.textos), 1)

    def test_una_causa_distinta_avisa_otra_vez(self):
        self.avisar(causa="par-incierto")
        informe = self.avisar(causa="capacidad-faltante:write_txn")
        self.assertEqual(informe, {"avisada": True, "messageId": 7})
        estado = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(estado["causa"], "capacidad-faltante:write_txn")

    def test_sin_confirmacion_no_avanza_el_estado(self):
        for mensaje_id in (None, 0, "x"):
            with self.subTest(mensaje_id=mensaje_id):
                informe = self.avisar(mensaje_id=mensaje_id)
                self.assertEqual(informe, {"avisada": False})
                self.assertFalse(self.ruta.exists())

    def test_el_estado_ilegible_reintenta_el_aviso(self):
        self.ruta.write_text("{ roto", encoding="utf-8")
        informe = self.avisar()
        self.assertEqual(informe, {"avisada": True, "messageId": 7})

    def test_nunca_lanza_cuando_el_directorio_no_existe(self):
        ruta = Path(self.directorio.name) / "no-existe" / "capacidad-incierta.json"
        informe = self.avisar(ruta=ruta)
        self.assertEqual(informe, {"avisada": False})


if __name__ == "__main__":
    unittest.main()
