#!/usr/bin/env python3
"""El emisor de excepciones de H (§3.5) y la pasada que comparte estado con la
proyección (I14): texto armado como en G, reaviso por renoticeAfterMs y
checkpoint que solo avanza con messageId positivo."""

import importlib
import json
import sqlite3
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

    def test_un_conflicto_del_checkpoint_no_se_informa_como_avisada(self):
        class TiendaQueConflicta:
            def __init__(self, tienda):
                self.envuelta = tienda
                self._conexion = tienda._conexion

            def notificar(self, root_id, exception_ids):
                return store.Conflicto(
                    "exception-unknown", {"exceptionId": exception_ids[0]}
                )

        informe, mensajes = self.avisar(tienda=TiendaQueConflicta(self.tienda))
        self.assertEqual(len(mensajes), 1)
        self.assertIn("exception-unknown", informe["errores"][0])
        self.assertEqual(informe["avisadas"], [])
        self.assertIsNone(informe["messageId"])
        fila = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (self.exception_id,),
        )
        self.assertIsNone(fila["noticed_at"])

    def test_el_conflicto_de_una_raiz_no_descarta_a_las_demas(self):
        # F6 (r5): el checkpoint en conflicto de una raíz no deja sin anotar a
        # las demás, cuyo aviso ya salió en el mismo mensaje.
        otra = meta_de()
        otra["rootId"] = "root-otra"
        self.assertIsInstance(self.tienda.abrir_raiz(otra), store.Ok)
        borrador = decidir.borrador_excepcion(
            "root-otra",
            "NECESITA_DAVID",
            {"kind": "rounds", "unitId": "a", "block": 0},
            ["seguir", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(
            self.tienda.aplicar("root-otra", dec, "dec-exc-otra"), store.Ok
        )

        class TiendaQueConflictaUnaRaiz:
            def __init__(self, tienda, raiz_mala):
                self.envuelta = tienda
                self._conexion = tienda._conexion
                self.raiz_mala = raiz_mala

            def notificar(self, root_id, exception_ids):
                if root_id == self.raiz_mala:
                    return store.Conflicto(
                        "exception-unknown", {"exceptionId": exception_ids[0]}
                    )
                return self.envuelta.notificar(root_id, exception_ids)

        informe, mensajes = self.avisar(
            tienda=TiendaQueConflictaUnaRaiz(self.tienda, RAIZ)
        )
        self.assertEqual(len(mensajes), 1)
        self.assertIn(f"checkpoint {RAIZ}: exception-unknown", informe["errores"][0])
        self.assertEqual(informe["avisadas"], [borrador["exceptionId"]])
        self.assertEqual(informe["messageId"], 42)
        mala = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (self.exception_id,),
        )
        sana = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (borrador["exceptionId"],),
        )
        self.assertIsNone(mala["noticed_at"])
        self.assertEqual(sana["noticed_at"], "2026-10-17T12:00:00Z")
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


class TopeDeDetalle(unittest.TestCase):
    """CodeRabbit (r6d): cuando hay más excepciones debidas que las que el
    mensaje detalla, el checkpoint (noticed_at / avisadas) cubre SOLO las
    mostradas; las demás quedan sin checkpoint y salen en la pasada
    siguiente. El dueño no puede haber visto lo que no se le mostró."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.tienda = store.Store(
            Path(self.directorio.name) / "db.sqlite",
            reloj=lambda: "2026-10-17T12:00:00Z",
        )
        self.addCleanup(self.tienda.cerrar)
        # 5 excepciones por raíz, en dos raíces: 10 debidas.
        for raiz in ("root-a", "root-b"):
            meta = meta_de()
            meta["rootId"] = raiz
            self.assertIsInstance(self.tienda.abrir_raiz(meta), store.Ok)
            dec = decidir._decision_vacia(1, 1)
            dec["excepciones"] = [
                decidir.borrador_excepcion(
                    raiz,
                    "NECESITA_DAVID",
                    {"kind": "rounds", "unitId": "a", "block": bloque},
                    ["seguir", "omitir", "cancelar"],
                )
                for bloque in range(5)
            ]
            self.assertIsInstance(
                self.tienda.aplicar(raiz, dec, f"dec-{raiz}"), store.Ok
            )

    def avisar(self, mensaje_id=42):
        mensajes = []

        def enviar(texto):
            mensajes.append(texto)
            return mensaje_id

        informe = proyeccion.avisar_excepciones(self.tienda, ahora=AHORA, enviar=enviar)
        return informe, mensajes

    def todas(self):
        return {
            f["exception_id"]: f["noticed_at"]
            for f in self.tienda._conexion.execute(
                "SELECT exception_id, noticed_at FROM excepciones"
            ).fetchall()
        }

    def test_el_checkpoint_cubre_solo_las_mostradas(self):
        self.assertEqual(len(self.todas()), 10)
        informe, mensajes = self.avisar()
        self.assertEqual(len(mensajes), 1)
        self.assertIn("(10)", mensajes[0])
        self.assertIn("… y 2 más", mensajes[0])
        self.assertEqual(len(informe["avisadas"]), 8)
        con_checkpoint = {
            eid for eid, notado in self.todas().items() if notado is not None
        }
        self.assertEqual(con_checkpoint, set(informe["avisadas"]))

        segunda, mensajes2 = self.avisar()
        self.assertEqual(len(mensajes2), 1, "las 2 no mostradas salen solas")
        self.assertIn("(2)", mensajes2[0])
        self.assertEqual(len(segunda["avisadas"]), 2)
        self.assertEqual(
            set(segunda["avisadas"]) | set(informe["avisadas"]),
            set(self.todas()),
        )
        tercera, mensajes3 = self.avisar()
        self.assertEqual(mensajes3, [])
        self.assertEqual(tercera["avisadas"], [])


class RecortePorLargo(unittest.TestCase):
    """El SEGUNDO recorte (el tope de texto del mensaje): cuando un bloque
    completo no cabe, la excepción no cuenta como mostrada — el checkpoint
    cubre exactamente lo que quedó en el mensaje final, contando los dos
    recortes (cantidad y largo)."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.tienda = store.Store(
            Path(self.directorio.name) / "db.sqlite",
            reloj=lambda: "2026-10-17T12:00:00Z",
        )
        self.addCleanup(self.tienda.cerrar)
        # Tres excepciones con opciones larguísimas: caben dos bloques
        # completos bajo el tope de texto, el tercero no.
        for letra in "abc":
            self.tienda._conexion.execute(
                "INSERT INTO excepciones (exception_id, root_id, unit_id, type,"
                " subject_json, options_json, recipient_json, raised_at,"
                " renotice_after_ms)"
                " VALUES (?,?,NULL,'INESPERADO','{}',?,"
                ' \'{"channel": "telegram", "principal": ["david"]}\','
                " '2026-10-17T12:00:00Z', 3600000)",
                (
                    f"exc-larga-{letra}",
                    "root-largo",
                    json.dumps([{"optionId": "seguir", "text": "x" * 1600}]),
                ),
            )
        self.tienda._conexion.commit()

    def avisar(self):
        mensajes = []

        def enviar(texto):
            mensajes.append(texto)
            return 42

        informe = proyeccion.avisar_excepciones(self.tienda, ahora=AHORA, enviar=enviar)
        return informe, mensajes

    def notadas(self):
        return {
            eid
            for eid, notado in {
                f["exception_id"]: f["noticed_at"]
                for f in self.tienda._conexion.execute(
                    "SELECT exception_id, noticed_at FROM excepciones"
                ).fetchall()
            }.items()
            if notado is not None
        }

    def test_el_bloque_que_no_cabe_no_recibe_checkpoint(self):
        informe, mensajes = self.avisar()
        self.assertEqual(len(mensajes), 1)
        self.assertIn("(3)", mensajes[0])
        self.assertIn("… y 1 más", mensajes[0])
        self.assertEqual(informe["avisadas"], ["exc-larga-a", "exc-larga-b"])
        self.assertEqual(self.notadas(), {"exc-larga-a", "exc-larga-b"})
        self.assertLessEqual(len(mensajes[0]), 3500)

        segunda, mensajes2 = self.avisar()
        self.assertEqual(len(mensajes2), 1)
        self.assertEqual(segunda["avisadas"], ["exc-larga-c"])
        self.assertEqual(self.notadas(), {"exc-larga-a", "exc-larga-b", "exc-larga-c"})


class OptionsInvalidos(Emisor):
    """C3 (r6e): un options_json que no parsea se salta con su error, como
    un raisedAt malformado: no sale un aviso SIN opciones ni checkpoint."""

    def test_un_options_json_roto_se_salta_sin_checkpoint(self):
        self.tienda._conexion.execute(
            "INSERT INTO excepciones (exception_id, root_id, unit_id, type,"
            " subject_json, options_json, recipient_json, raised_at,"
            " renotice_after_ms)"
            " VALUES ('exc-options-roto', ?, 'a', 'INESPERADO', '{}',"
            " '{roto',"
            ' \'{"channel": "telegram", "principal": ["david"]}\','
            " '2026-10-17T12:00:00Z', 3600000)",
            (RAIZ,),
        )
        self.tienda._conexion.commit()
        informe, mensajes = self.avisar()
        self.assertEqual(informe["avisadas"], [self.exception_id])
        self.assertTrue(
            any("exc-options-roto" in error for error in informe["errores"]), informe
        )
        self.assertEqual(len(mensajes), 1)
        self.assertNotIn("exc-options-roto", mensajes[0])
        fila = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            ("exc-options-roto",),
        )
        self.assertIsNone(fila["noticed_at"])

    def test_un_options_json_null_tampoco_pasa(self):
        # JSON válido pero null: la excepción del contrato siempre trae
        # opciones; un aviso sin cómo responder no sale.
        self.tienda._conexion.execute(
            "INSERT INTO excepciones (exception_id, root_id, unit_id, type,"
            " subject_json, options_json, recipient_json, raised_at,"
            " renotice_after_ms)"
            " VALUES ('exc-options-null', ?, 'a', 'INESPERADO', '{}',"
            " 'null',"
            ' \'{"channel": "telegram", "principal": ["david"]}\','
            " '2026-10-17T12:00:00Z', 3600000)",
            (RAIZ,),
        )
        self.tienda._conexion.commit()
        informe, mensajes = self.avisar()
        self.assertEqual(informe["avisadas"], [self.exception_id])
        self.assertTrue(
            any("exc-options-null" in error for error in informe["errores"]), informe
        )
        self.assertNotIn("exc-options-null", mensajes[0])


class EmisorQueLanza(Emisor):
    """C4 (r6e): si enviar lanza, no sube: error "el aviso no se confirmó:
    <tipo>", ningún checkpoint avanza y la pasada no pierde nada."""

    @staticmethod
    def enviar_que_lanza(_texto):
        raise ValueError("boom")

    def test_un_emisor_que_lanza_deja_error_y_no_avanza_nada(self):
        informe = proyeccion.avisar_excepciones(
            self.tienda, ahora=AHORA, enviar=self.enviar_que_lanza
        )
        self.assertIn("el aviso no se confirmó: ValueError", informe["errores"])
        self.assertEqual(informe["avisadas"], [])
        self.assertIsNone(informe["messageId"])
        fila = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (self.exception_id,),
        )
        self.assertIsNone(fila["noticed_at"])

    def test_la_pasada_sobrevive_al_emisor_que_lanza(self):
        informe = proyeccion.pasada(
            self.tienda,
            ahora=AHORA,
            dir_destino=Path(self.directorio.name) / "raices",
            enviar=self.enviar_que_lanza,
            stale_after_ms=300000,
            intervalo_ms=300000,
            host_gateway="mac-local",
        )
        self.assertIn(RAIZ, informe["proyecciones"])
        self.assertTrue(
            any("el aviso no se confirmó" in e for e in informe["errores"]), informe
        )


class CheckpointQueLanza(Emisor):
    """C5 (r6e): un notificar que LANZA (p. ej. SQLite locked) no corta el
    ciclo de raíces: la suya queda con error y las demás avanzan."""

    def test_un_notificar_que_lanza_no_corta_a_las_demas(self):
        otra = meta_de()
        otra["rootId"] = "root-otra"
        self.assertIsInstance(self.tienda.abrir_raiz(otra), store.Ok)
        borrador = decidir.borrador_excepcion(
            "root-otra",
            "NECESITA_DAVID",
            {"kind": "rounds", "unitId": "a", "block": 0},
            ["seguir", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(
            self.tienda.aplicar("root-otra", dec, "dec-exc-otra"), store.Ok
        )

        class TiendaQueLanzaUna:
            def __init__(self, tienda, raiz_mala):
                self.envuelta = tienda
                self._conexion = tienda._conexion
                self.raiz_mala = raiz_mala

            def notificar(self, root_id, exception_ids):
                if root_id == self.raiz_mala:
                    raise sqlite3.OperationalError("database is locked")
                return self.envuelta.notificar(root_id, exception_ids)

        informe, mensajes = self.avisar(tienda=TiendaQueLanzaUna(self.tienda, RAIZ))
        self.assertEqual(len(mensajes), 1)
        self.assertIn(f"checkpoint {RAIZ}: OperationalError", informe["errores"][0])
        self.assertEqual(informe["avisadas"], [borrador["exceptionId"]])
        self.assertEqual(informe["messageId"], 42)
        mala = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (self.exception_id,),
        )
        sana = self.tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (borrador["exceptionId"],),
        )
        self.assertIsNone(mala["noticed_at"])
        self.assertIsNotNone(sana["noticed_at"])


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

    def test_crea_el_directorio_del_checkpoint_y_avisa_una_sola_vez(self):
        ruta = Path(self.directorio.name) / "no-existe" / "capacidad-incierta.json"
        informe = self.avisar(ruta=ruta)
        self.assertEqual(informe, {"avisada": True, "messageId": 7})
        self.assertTrue(ruta.parent.is_dir())
        estado = json.loads(ruta.read_text(encoding="utf-8"))
        self.assertEqual(estado["causa"], "version-no-certificada")
        informe = self.avisar(ruta=ruta)
        self.assertEqual(informe, {"avisada": False})
        self.assertEqual(len(self.textos), 1)


if __name__ == "__main__":
    unittest.main()
