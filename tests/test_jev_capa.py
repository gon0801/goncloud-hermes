"""JEV (TypeSafe System One) como capa de decisión del router de suscripciones.

Dobles de TypeSafe: ningún test toca la red ni una llave real. La forma de la
petición/respuesta es la del cliente del plugin jev fijado en el repo
(POST https://api.typesafe.ai/v1/systemone, {state, model, questions},
answers noul/choice/score).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

import jev_cliente  # noqa: E402
from subscriptions.catalog import compile_profile_routes  # noqa: E402
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.types import (  # noqa: E402
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
)

LLAVE_FALSA = "sk-jevtest-llave-falsa-1234567890"
SOURCE = {
    "profile_names": {"main": "claw"},
    "profile_inheritance": {"entrega": "main"},
    "chains": {
        "main": [
            "opencode-go/deepseek-v4.1-flash (off)",
            "anthropic/claude-sonnet-5",
            "meta/muse-spark-1.3 (max)",
            "xai/grok-4.6",
        ]
    },
}


class TransporteFalso:
    """Doble de TypeSafe: captura la petición y contesta answers tipadas."""

    def __init__(self, answers=None, error=None):
        self.answers = answers or {}
        self.error = error
        self.llamadas = []

    def __call__(self, url, payload, headers, timeout):
        self.llamadas.append(
            {
                "url": url,
                "payload": payload,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return 200, {"model": "jev-latest", "answers": self.answers}


def answers_choice(opcion, probabilidad):
    return {
        "jev_route": {
            "type": "choice",
            "choice": opcion,
            "probabilities": {opcion: probabilidad},
            "confidence": probabilidad,
        }
    }


class ScriptedAdapter:
    billing_mode = "subscription"

    def __init__(self, name):
        self.name = name
        self.result = None
        self.requests = []
        self.health = AdapterHealth(ready=True, account_class=f"{self.name}_test")

    def probe(self, route):
        return self.health

    def invoke(self, request, route):
        self.requests.append(request)
        if callable(self.result):
            return self.result(request, route)
        return self.result


def failed(kind, adapter="adapter", text=""):
    return InvocationResult(
        success=False,
        text=text or f"{adapter} invocation failed ({kind.value})",
        adapter=adapter,
        model="model-x",
        billing_mode="subscription",
        failure=kind,
    )


def completed(adapter="claude"):
    return InvocationResult(
        success=True,
        text="ok",
        adapter=adapter,
        model="model-x",
        billing_mode="subscription",
        session_id="s1",
    )


class ClienteTests(unittest.TestCase):
    def test_route_usa_la_api_real_de_typesafe(self):
        transporte = TransporteFalso(answers_choice("quota", 0.92))
        cliente = jev_cliente.ClienteJev(
            transporte=transporte, obtener_llave=lambda: LLAVE_FALSA
        )
        respuesta = cliente.route(
            "adaptador=kimi intento=1 evidencia: weekly limit",
            "¿Cómo se clasifica esta falla?",
            {"quota": "ventana del plan agotada", "rate_limit": "throttle corto"},
        )
        self.assertIsNotNone(respuesta)
        self.assertEqual(respuesta.opcion, "quota")
        self.assertAlmostEqual(respuesta.probabilidad, 0.92)
        llamada = transporte.llamadas[0]
        self.assertEqual(llamada["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(llamada["payload"]["model"], "jev-latest")
        pregunta = llamada["payload"]["questions"]["jev_route"]
        self.assertEqual(pregunta["type"], "choice")
        self.assertEqual(
            pregunta["criteria"],
            {"quota": "ventana del plan agotada", "rate_limit": "throttle corto"},
        )
        self.assertEqual(llamada["headers"]["Authorization"], f"Bearer {LLAVE_FALSA}")
        self.assertLessEqual(llamada["timeout"], 3.0)

    def test_check_y_score_formas_noul_y_score(self):
        transporte = TransporteFalso(
            {
                "jev_check": {"type": "noul", "noul": 0.87},
                "jev_score": {
                    "type": "score",
                    "score": 2,
                    "legend": ["leve", "medio", "grave"],
                    "confidence": 0.66,
                },
            }
        )
        cliente = jev_cliente.ClienteJev(
            transporte=transporte, obtener_llave=lambda: LLAVE_FALSA
        )
        self.assertAlmostEqual(cliente.check("estado", "¿es pasajera?"), 0.87)
        puntaje = cliente.score("estado", "¿severidad?", ["leve", "medio", "grave"])
        self.assertEqual(puntaje.valor, 2)
        self.assertAlmostEqual(puntaje.confianza, 0.66)
        tipos = [
            llamada["payload"]["questions"][nombre]["type"]
            for llamada in transporte.llamadas
            for nombre in llamada["payload"]["questions"]
        ]
        self.assertEqual(tipos, ["noul", "score"])

    def test_sin_llave_sin_respuesta_y_sin_llamada(self):
        transporte = TransporteFalso(answers_choice("quota", 0.99))

        def sin_llave():
            raise jev_cliente.JevError("sin llave")

        cliente = jev_cliente.ClienteJev(transporte=transporte, obtener_llave=sin_llave)
        self.assertIsNone(cliente.route("e", "p", {"a": "x", "b": "y"}))
        self.assertEqual(transporte.llamadas, [])

    def test_llave_desde_secret_scope_y_respaldo_entorno(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "llave-de-entorno"}):
            self.assertEqual(
                jev_cliente.obtener_llave(getter=lambda nombre: "llave-de-scope"),
                "llave-de-scope",
            )

            def getter_roto(nombre):
                raise RuntimeError("scope no disponible")

            self.assertEqual(
                jev_cliente.obtener_llave(getter=getter_roto), "llave-de-entorno"
            )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(jev_cliente.JevError):
                jev_cliente.obtener_llave(getter=lambda nombre: "")

    def test_error_o_tope_es_sin_respuesta(self):
        for error in (TimeoutError("tope"), RuntimeError("caido"), OSError("red")):
            transporte = TransporteFalso(error=error)
            cliente = jev_cliente.ClienteJev(
                transporte=transporte, obtener_llave=lambda: LLAVE_FALSA
            )
            self.assertIsNone(cliente.check("estado", "¿pasa?"), repr(error))

    def test_estado_con_secreto_no_llega_a_la_peticion(self):
        transporte = TransporteFalso({"jev_check": {"type": "noul", "noul": 0.5}})
        cliente = jev_cliente.ClienteJev(
            transporte=transporte, obtener_llave=lambda: LLAVE_FALSA
        )
        cliente.check(f"fallo con {LLAVE_FALSA} en el texto", "¿pasa?")
        enviado = json.dumps(transporte.llamadas[0]["payload"])
        self.assertNotIn(LLAVE_FALSA, enviado)
        self.assertIn("[redacted]", transporte.llamadas[0]["payload"]["state"])


class RouterJevTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.workspace = Path(self.tmp.name).resolve()
        self.registro = self.workspace / "jev" / "decisiones.jsonl"
        self.adapters = {
            name: ScriptedAdapter(name)
            for name in ("opencode_go", "claude", "muse", "grok")
        }
        self.sleeps = []

    def router(self, transporte, **config_extra):
        config = jev_cliente.JevConfig(registro_path=self.registro, **config_extra)
        cliente = jev_cliente.ClienteJev(
            transporte=transporte, obtener_llave=lambda: LLAVE_FALSA, config=config
        )
        return SubscriptionRouter(
            adapters=self.adapters,
            db_path=self.workspace / "router.db",
            routing_source=SOURCE,
            sleep=self.sleeps.append,
            jev_cliente=cliente,
            jev_config=config,
        )

    def request(self):
        return InvocationRequest(
            workflow_id="wf-jev",
            stage_id="st-jev",
            role="implementer",
            prompt="Do it once.",
            workspace=self.workspace,
            start_revision="",
            current_revision="",
        )

    def rutas(self):
        routes = compile_profile_routes("claw", SOURCE)
        return [routes[0], routes[1]]

    def decisiones(self):
        if not self.registro.is_file():
            return []
        return [
            json.loads(linea)
            for linea in self.registro.read_text(encoding="utf-8").splitlines()
            if linea.strip()
        ]

    def test_jev_sobre_umbral_cambia_clasificacion_y_el_router_avanza(self):
        # La heurística lee throttle (reintentaría la misma posición); JEV dice
        # cuota con 0.92: el router avanza sin dormir ni reintentar.
        self.adapters["opencode_go"].result = failed(
            FailureKind.RATE_LIMIT,
            adapter="opencode_go",
            text="429 rate limit exceeded for your plan",
        )
        self.adapters["claude"].result = completed()
        transporte = TransporteFalso(answers_choice("quota", 0.92))
        resultado = self.router(transporte).run(self.request(), self.rutas())
        self.assertTrue(resultado.success)
        self.assertEqual(resultado.adapter, "claude")
        self.assertEqual(len(self.adapters["opencode_go"].requests), 1)
        self.assertEqual(self.sleeps, [])
        (decision,) = self.decisiones()
        self.assertEqual(decision["punto"], "router_clasificacion")
        self.assertEqual(decision["pregunta"], "choice")
        self.assertEqual(decision["respuesta"], "quota")
        self.assertAlmostEqual(decision["probabilidad"], 0.92)
        self.assertEqual(decision["umbral"], 0.85)
        self.assertEqual(decision["actuo"], "jev")
        self.assertEqual(decision["resultado_regla"], "rate_limit")
        self.assertIsNone(decision["error"])

    def test_jev_en_el_borde_del_umbral_actua(self):
        self.adapters["opencode_go"].result = failed(
            FailureKind.TASK_FAILURE, adapter="opencode_go", text="salida rara"
        )
        self.adapters["claude"].result = completed()
        transporte = TransporteFalso(answers_choice("quota", 0.85))
        resultado = self.router(transporte).run(self.request(), self.rutas())
        self.assertTrue(resultado.success)
        (decision,) = self.decisiones()
        self.assertEqual(decision["actuo"], "jev")
        self.assertEqual(decision["respuesta"], "quota")

    def test_jev_bajo_umbral_gana_heuristica_y_reintenta_misma_posicion(self):
        intentos = []

        def falla_y_luego_ok(request, route):
            intentos.append(1)
            if len(intentos) == 1:
                return failed(
                    FailureKind.RATE_LIMIT,
                    adapter="opencode_go",
                    text="429 Output token rate limit exceeded. Please retry after a brief wait.",
                )
            return completed(adapter="opencode_go")

        self.adapters["opencode_go"].result = falla_y_luego_ok
        transporte = TransporteFalso(answers_choice("quota", 0.5))
        resultado = self.router(transporte).run(self.request(), self.rutas())
        self.assertTrue(resultado.success)
        self.assertEqual(resultado.adapter, "opencode_go")
        self.assertEqual(len(intentos), 2)
        self.assertEqual(self.sleeps, [20])
        (decision,) = self.decisiones()
        self.assertEqual(decision["actuo"], "regla")
        self.assertEqual(decision["resultado_regla"], "rate_limit")

    def test_jev_caido_o_lento_gana_heuristica(self):
        for error in (TimeoutError("tope de 3s"), RuntimeError("typesafe caido")):
            with self.subTest(error=repr(error)):
                transporte = TransporteFalso(error=error)
                router = self.router(transporte)
                resultado = router.run(self.request(), self.rutas())
                self.assertFalse(resultado.success)
                decisiones = self.decisiones()
                self.assertTrue(decisiones)
                self.assertEqual(decisiones[-1]["actuo"], "regla")
                self.assertIsNotNone(decisiones[-1]["error"])
                self.assertIsNone(decisiones[-1]["respuesta"])

    def test_secreto_nunca_llega_al_registro_ni_a_la_peticion(self):
        secreto_en_texto = "sk-jevtest-secreto-en-stderr-9876543210"
        self.adapters["opencode_go"].result = failed(
            FailureKind.TASK_FAILURE,
            adapter="opencode_go",
            text=f"vendor explotó con {secreto_en_texto} y {LLAVE_FALSA}",
        )
        self.adapters["claude"].result = completed()
        transporte = TransporteFalso(answers_choice("quota", 0.95))
        self.router(transporte).run(self.request(), self.rutas())
        peticion = json.dumps(transporte.llamadas[0]["payload"])
        self.assertNotIn(secreto_en_texto, peticion)
        self.assertNotIn(LLAVE_FALSA, peticion)
        registro = self.registro.read_text(encoding="utf-8")
        self.assertNotIn(secreto_en_texto, registro)
        self.assertNotIn(LLAVE_FALSA, registro)

    def test_interruptor_apagado_nunca_llama(self):
        self.adapters["opencode_go"].result = failed(
            FailureKind.TASK_FAILURE, adapter="opencode_go", text="salida rara"
        )
        self.adapters["claude"].result = completed()
        transporte = TransporteFalso(answers_choice("quota", 0.99))
        router = self.router(transporte, puntos={"router_clasificacion": False})
        resultado = router.run(self.request(), self.rutas())
        self.assertTrue(resultado.success)
        self.assertEqual(transporte.llamadas, [])


class MuseBridgeJevTests(unittest.TestCase):
    """Punto r2-1: la falla de la delegación a Muse pasa por JEV."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.registro = Path(self.tmp.name) / "jev" / "decisiones.jsonl"

    def delegar(self, transporte, stderr, **config_extra):
        import muse_bridge
        from types import SimpleNamespace

        config = jev_cliente.JevConfig(registro_path=self.registro, **config_extra)
        cliente = jev_cliente.ClienteJev(
            transporte=transporte, obtener_llave=lambda: LLAVE_FALSA, config=config
        )
        completado = SimpleNamespace(
            returncode=1, stream="salida rara del proceso", stderr=stderr
        )
        with (
            patch.object(muse_bridge, "build_muse_command", lambda *a, **k: ["muse"]),
            patch.object(muse_bridge, "run_cli", lambda *a, **k: completado),
            self.assertRaises(muse_bridge.MuseBridgeError) as ctx,
        ):
            muse_bridge.delegate_to_muse(
                "haz la tarea",
                Path(r"C:\dev\repo"),
                actor_role="implementer",
                jev_cliente=cliente,
                jev_config=config,
            )
        return ctx.exception

    def decisiones(self):
        if not self.registro.is_file():
            return []
        return [
            json.loads(linea)
            for linea in self.registro.read_text(encoding="utf-8").splitlines()
            if linea.strip()
        ]

    def test_jev_sobre_umbral_reclasifica_la_falla_de_muse(self):
        transporte = TransporteFalso(answers_choice("quota", 0.92))
        error = self.delegar(
            transporte,
            "429 Output token rate limit exceeded. Please retry after a brief wait.",
        )
        self.assertEqual(error.failure, FailureKind.QUOTA)
        (decision,) = self.decisiones()
        self.assertEqual(decision["punto"], "muse_bridge_clasificacion")
        self.assertEqual(decision["actuo"], "jev")
        self.assertEqual(decision["resultado_regla"], "rate_limit")

    def test_jev_bajo_umbral_o_caido_gana_classify_failure(self):
        stderr = (
            "429 Output token rate limit exceeded. Please retry after a brief wait."
        )
        transporte = TransporteFalso(answers_choice("quota", 0.4))
        self.assertEqual(
            self.delegar(transporte, stderr).failure, FailureKind.RATE_LIMIT
        )
        caido = TransporteFalso(error=TimeoutError("tope"))
        self.assertEqual(self.delegar(caido, stderr).failure, FailureKind.RATE_LIMIT)
        self.assertEqual(self.decisiones()[-1]["actuo"], "regla")

    def test_interruptor_apagado_nunca_llama_y_secreto_fuera(self):
        transporte = TransporteFalso(answers_choice("quota", 0.99))
        error = self.delegar(
            transporte,
            f"fallo con {LLAVE_FALSA} dentro",
            puntos={"muse_bridge_clasificacion": False},
        )
        self.assertEqual(error.failure, FailureKind.TASK_FAILURE)
        self.assertEqual(transporte.llamadas, [])

    def test_secreto_no_llega_a_la_peticion_ni_al_registro(self):
        transporte = TransporteFalso(answers_choice("quota", 0.95))
        self.delegar(transporte, f"vendor explotó con {LLAVE_FALSA}")
        self.assertNotIn(LLAVE_FALSA, json.dumps(transporte.llamadas[0]["payload"]))
        self.assertNotIn(LLAVE_FALSA, self.registro.read_text(encoding="utf-8"))


class TiendaFalsa:
    def __init__(self):
        self.incidencias = []

    def registrar_incidencia(self, root_id, unit_id, attempt, clave, evidencia):
        self.incidencias.append((clave, evidencia))


class TarjetaFalsa:
    def __init__(self, estado, resultado=None):
        self.id = "tarjeta-1"
        self.estado = estado
        self.resultado = resultado


class TickerJevTests(unittest.TestCase):
    """Punto r2-4: transporte caído vs resultado inválido con evidencia ambigua."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.registro = Path(self.tmp.name) / "jev" / "decisiones.jsonl"
        import importlib

        self.ticker = importlib.import_module("raices.ticker")
        self.intento = {
            "rootId": "root-1",
            "unitId": "unit-1",
            "attempt": 1,
            "state": "running",
        }

    def con_jev(self, transporte, **config_extra):
        config = jev_cliente.JevConfig(registro_path=self.registro, **config_extra)
        cliente = jev_cliente.ClienteJev(
            transporte=transporte, obtener_llave=lambda: LLAVE_FALSA, config=config
        )
        return patch.object(
            self.ticker, "_jev_ticker", lambda: (jev_cliente, cliente, config)
        )

    def decisiones(self):
        if not self.registro.is_file():
            return []
        return [
            json.loads(linea)
            for linea in self.registro.read_text(encoding="utf-8").splitlines()
            if linea.strip()
        ]

    @staticmethod
    def check_answers(p):
        return {"jev_check": {"type": "noul", "noul": p}}

    def test_transporte_caido_que_jev_desmiente_es_incidencia_invalida(self):
        tienda = TiendaFalsa()
        tarjeta = TarjetaFalsa("bloqueada", None)
        with self.con_jev(TransporteFalso(self.check_answers(0.03))):
            self.ticker._transporte_caido(tienda, self.intento, tarjeta)
        ((clave, evidencia),) = tienda.incidencias
        self.assertEqual(evidencia["kind"], "result-missing")
        (decision,) = self.decisiones()
        self.assertEqual(decision["punto"], "ticker_incidencia")
        self.assertEqual(decision["pregunta"], "noul")
        self.assertEqual(decision["actuo"], "jev")

    def test_invalida_que_jev_confirma_transporte_es_reintentable(self):
        tienda = TiendaFalsa()
        tarjeta = TarjetaFalsa("completada", {"resultContract": 123})
        with self.con_jev(TransporteFalso(self.check_answers(0.95))):
            self.ticker._incidencia_invalida(
                tienda, self.intento, tarjeta, tarjeta.resultado
            )
        ((clave, evidencia),) = tienda.incidencias
        self.assertEqual(evidencia["kind"], "recovery-transport-failure")

    def test_ambiguo_caido_o_apagado_gana_la_regla_de_hoy(self):
        # Ambiguo (0.5): la regla de hoy manda en ambos sitios.
        tienda = TiendaFalsa()
        with self.con_jev(TransporteFalso(self.check_answers(0.5))):
            self.ticker._transporte_caido(
                tienda, self.intento, TarjetaFalsa("archivada", None)
            )
            self.ticker._incidencia_invalida(
                tienda, self.intento, TarjetaFalsa("completada", {"x": 1}), {"x": 1}
            )
        kinds = [evidencia["kind"] for _, evidencia in tienda.incidencias]
        self.assertEqual(kinds, ["recovery-transport-failure", "invalid-result"])
        # Caído: también la regla.
        tienda = TiendaFalsa()
        with self.con_jev(TransporteFalso(error=RuntimeError("caido"))):
            self.ticker._transporte_caido(
                tienda, self.intento, TarjetaFalsa("bloqueada", None)
            )
        self.assertEqual(tienda.incidencias[0][1]["kind"], "recovery-transport-failure")
        # Apagado: nunca llama.
        transporte = TransporteFalso(self.check_answers(0.99))
        tienda = TiendaFalsa()
        with self.con_jev(transporte, puntos={"ticker_incidencia": False}):
            self.ticker._incidencia_invalida(
                tienda, self.intento, TarjetaFalsa("completada", {"x": 1}), {"x": 1}
            )
        self.assertEqual(transporte.llamadas, [])
        self.assertEqual(tienda.incidencias[0][1]["kind"], "invalid-result")

    def test_secreto_de_la_evidencia_no_llega_ni_a_peticion_ni_a_registro(self):
        tienda = TiendaFalsa()
        carga = {"detalle": f"murió con {LLAVE_FALSA}"}
        transporte = TransporteFalso(self.check_answers(0.5))
        with self.con_jev(transporte):
            self.ticker._incidencia_invalida(
                tienda, self.intento, TarjetaFalsa("completada", carga), carga
            )
        self.assertNotIn(LLAVE_FALSA, json.dumps(transporte.llamadas[0]["payload"]))
        self.assertNotIn(LLAVE_FALSA, self.registro.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
