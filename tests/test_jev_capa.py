"""JEV (TypeSafe System One) como capa de decisión del router de suscripciones.

Dobles de TypeSafe: ningún test toca la red ni una llave real. La forma de la
petición/respuesta es la del cliente del plugin jev fijado en el repo
(POST https://api.typesafe.ai/v1/systemone, {state, model, questions},
answers noul/choice/score).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
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


class _AlcanceFake:
    """Instala un agent.secret_scope de mentira y lo saca al salir."""

    def __init__(self, get_secret=None, current_secret_scope=None):
        self.modulo_agente = types.ModuleType("agent")
        self.modulo_alcance = types.ModuleType("agent.secret_scope")
        if get_secret is not None:
            self.modulo_alcance.get_secret = get_secret
        if current_secret_scope is not None:
            self.modulo_alcance.current_secret_scope = current_secret_scope
        self.previos = None

    def __enter__(self):
        self.previos = {
            nombre: sys.modules.get(nombre)
            for nombre in ("agent", "agent.secret_scope")
        }
        sys.modules["agent"] = self.modulo_agente
        sys.modules["agent.secret_scope"] = self.modulo_alcance
        return self

    def __exit__(self, *exc):
        for nombre, previo in self.previos.items():
            if previo is None:
                sys.modules.pop(nombre, None)
            else:
                sys.modules[nombre] = previo
        return False


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

    def test_los_secretos_del_scope_y_la_llave_no_salen_a_typesafe(self):
        # F13: en el gateway multiplexado las credenciales del perfil viven
        # en agent.secret_scope, no en os.environ, y ni esas values() ni la
        # llave resuelta tienen por qué tener forma de llave: lo que sale a
        # TypeSafe no las contiene.
        secreto_scope = "mjs-privado-scope-9931"
        llave_plana = "llave-plana-sin-forma-4567"
        with _AlcanceFake(current_secret_scope=lambda: {"OTRA_LLAVE": secreto_scope}):
            transporte = TransporteFalso({"jev_check": {"type": "noul", "noul": 0.5}})
            cliente = jev_cliente.ClienteJev(
                transporte=transporte, obtener_llave=lambda: llave_plana
            )
            cliente.check(
                f"evidencia con {secreto_scope} y {llave_plana} dentro", "¿pasa?"
            )
        enviado = json.dumps(transporte.llamadas[0]["payload"])
        self.assertNotIn(secreto_scope, enviado)
        self.assertNotIn(llave_plana, enviado)
        self.assertIn("[redacted]", transporte.llamadas[0]["payload"]["state"])

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

    def test_falla_mutante_reclamada_por_jev_no_sale_del_bloqueo(self):
        # F12: la heurística dejó transport (bloquea tras mutación) con el
        # workspace ya cambiado; JEV reclama quota 0.99: la etapa se bloquea
        # igual (el siguiente slot no corre, no hay continue) y la salud de
        # la cuenta se evalúa con la clase heurística, no con la de JEV.
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)

        def muta_y_transport(request, route):
            (request.workspace / "parcial.py").write_text("medio\n", encoding="utf-8")
            return failed(
                FailureKind.TRANSPORT,
                adapter="opencode_go",
                text="connection reset before an answer",
            )

        self.adapters["opencode_go"].result = muta_y_transport
        self.adapters["claude"].result = completed()
        transporte = TransporteFalso(answers_choice("quota", 0.99))
        router = self.router(transporte)
        resultado = router.run(self.request(), self.rutas())
        self.assertFalse(resultado.success)
        self.assertEqual(self.adapters["claude"].requests, [])
        self.assertEqual(resultado.failure, FailureKind.QUOTA)
        salud = router.store.health("opencode_go")
        self.assertEqual(salud["failure"], "transport")
        self.assertFalse(salud["ready"])
        (decision,) = self.decisiones()
        self.assertEqual(decision["actuo"], "jev")
        self.assertEqual(decision["respuesta"], "quota")

    def test_reanudacion_tras_mutante_reclasificado_no_avanza_ni_cooldownea(self):
        # F14: la heurística dejó transport (bloquea tras mutación) con el
        # workspace ya cambiado; JEV reclama quota 0.99 y la corrida bloquea
        # (F12). Lo asentado guarda AMBAS clases: al reanudar, la guarda lee
        # la heurística y NO salta al siguiente slot con continue sobre la
        # mutación sin reportar. Y el cooldown de la cuenta va con la
        # heurística: transport no cooldownea, quota sí lo haría.
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)

        def muta_y_transport(request, route):
            (request.workspace / "parcial.py").write_text("medio\n", encoding="utf-8")
            return failed(
                FailureKind.TRANSPORT,
                adapter="opencode_go",
                text="connection reset before an answer",
            )

        self.adapters["opencode_go"].result = muta_y_transport
        self.adapters["claude"].result = completed()
        transporte = TransporteFalso(answers_choice("quota", 0.99))
        router = self.router(transporte)
        peticion = InvocationRequest(
            workflow_id="wf-jev",
            stage_id="st-jev",
            role="implementer",
            prompt="Do it once.",
            workspace=self.workspace,
            start_revision="",
            current_revision="",
            result_transform=lambda result: result,
        )
        resultado = router.run(peticion, self.rutas())
        self.assertFalse(resultado.success)
        self.assertEqual(resultado.failure, FailureKind.QUOTA)
        self.assertEqual(self.adapters["claude"].requests, [])
        (intento,) = router.store.stage_attempts("wf-jev", "st-jev")
        self.assertEqual(intento["failure"], "quota")
        self.assertEqual(intento.get("failure_heuristic"), "transport")

        reanudado = self.router(TransporteFalso(answers_choice("quota", 0.99)))
        resultado2 = reanudado.run(peticion, self.rutas())
        self.assertFalse(resultado2.success)
        self.assertEqual(resultado2.failure, FailureKind.QUOTA)
        self.assertEqual(self.adapters["claude"].requests, [])

        # Cooldown con la heurística: transport sin mutación que JEV lee
        # quota avanza al siguiente slot, pero la cuenta no queda bencheada.
        config = jev_cliente.JevConfig(registro_path=self.registro)
        cliente = jev_cliente.ClienteJev(
            transporte=TransporteFalso(answers_choice("quota", 0.99)),
            obtener_llave=lambda: LLAVE_FALSA,
            config=config,
        )
        router_bench = SubscriptionRouter(
            adapters=self.adapters,
            db_path=self.workspace / "bench.db",
            routing_source=SOURCE,
            sleep=self.sleeps.append,
            jev_cliente=cliente,
            jev_config=config,
        )
        self.adapters["opencode_go"].result = failed(
            FailureKind.TRANSPORT,
            adapter="opencode_go",
            text="connection reset before an answer",
        )
        # Workspace aparte y limpio, fuera del repo: en el principal viven
        # la db y el registro del propio router, que contarían como mutación.
        tmp_bench = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, tmp_bench)
        workspace_bench = Path(tmp_bench.name).resolve()
        peticion_bench = InvocationRequest(
            workflow_id="wf-jev",
            stage_id="st-bench",
            role="implementer",
            prompt="Do it once.",
            workspace=workspace_bench,
            start_revision="",
            current_revision="",
        )
        self.assertTrue(router_bench.run(peticion_bench, self.rutas()).success)
        self.assertIsNone(router_bench.store.cooldown_until("opencode_go"))
        (intento_bench,) = router_bench.store.stage_attempts("wf-jev", "st-bench")[:1]
        self.assertEqual(intento_bench["failure"], "quota")
        self.assertEqual(intento_bench.get("failure_heuristic"), "transport")

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


class ConfigJevTests(unittest.TestCase):
    """F4: config_jev con Mapping, con ctx (get_config) y con entorno."""

    def test_desde_mapping(self):
        config = jev_cliente.config_jev(
            {
                "jev_umbral": 0.7,
                "jev_timeout_seconds": 2,
                "jev_router_clasificacion": "false",
                "jev_muse_bridge_clasificacion": "0",
                "jev_registro": "/tmp/jev-x/decisiones.jsonl",
            },
            env={},
        )
        self.assertEqual(config.umbral, 0.7)
        self.assertEqual(config.timeout_seconds, 2.0)
        self.assertFalse(config.punto_activo("router_clasificacion"))
        self.assertFalse(config.punto_activo("muse_bridge_clasificacion"))
        self.assertTrue(config.punto_activo("ticker_incidencia"))
        self.assertEqual(config.registro_path, Path("/tmp/jev-x/decisiones.jsonl"))

    def test_desde_ctx_con_get_config(self):
        class Ctx:
            def __init__(self):
                self.valores = {"jev_umbral": 0.66, "jev_ticker_incidencia": False}

            def get_config(self, clave, default=None):
                return self.valores.get(clave, default)

        config = jev_cliente.config_jev(Ctx(), env={})
        self.assertEqual(config.umbral, 0.66)
        self.assertFalse(config.punto_activo("ticker_incidencia"))
        self.assertTrue(config.punto_activo("router_clasificacion"))

    def test_entorno_pisa_y_clamps(self):
        config = jev_cliente.config_jev(
            {"jev_umbral": 5, "jev_timeout_seconds": 999},
            env={"GONCLOUD_JEV_ROUTER_CLASIFICACION": "0"},
        )
        self.assertEqual(config.umbral, 1.0)
        self.assertEqual(config.timeout_seconds, 30.0)
        self.assertFalse(config.punto_activo("router_clasificacion"))
        config = jev_cliente.config_jev(
            env={"GONCLOUD_JEV_UMBRAL": "0.42", "GONCLOUD_JEV_REGISTRO": "/tmp/r.jsonl"}
        )
        self.assertEqual(config.umbral, 0.42)
        self.assertEqual(config.registro_path, Path("/tmp/r.jsonl"))

    def test_punto_desconocido_es_fail_closed(self):
        self.assertFalse(
            jev_cliente.JevConfig(puntos={}).punto_activo("router_clasificacion")
        )
        config = jev_cliente.JevConfig()
        for punto in (
            "router_clasificacion",
            "muse_bridge_clasificacion",
            "ticker_incidencia",
        ):
            self.assertTrue(config.punto_activo(punto), punto)

    def test_el_soporte_de_pruebas_apaga_jev_en_la_suite(self):
        # F2: goncloud_test_support ya se importó arriba; la config por
        # omisión de la suite no debe consultar ni registrar nada real.
        with patch.dict(os.environ, {}, clear=False):
            config = jev_cliente.config_jev()
        for punto in (
            "router_clasificacion",
            "muse_bridge_clasificacion",
            "ticker_incidencia",
        ):
            self.assertFalse(config.punto_activo(punto), punto)


class TransporteRedirectTests(unittest.TestCase):
    """CodeRabbit: un 3xx nunca reenvía la llave a otro host."""

    def test_redirect_no_se_sigue_y_es_sin_respuesta(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        destino = {"visitado": False, "authorization": None}

        class Manejador(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/destino")
                    self.end_headers()
                else:
                    destino["visitado"] = True
                    destino["authorization"] = self.headers.get("Authorization")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
        hilo.start()
        try:
            url = f"http://127.0.0.1:{servidor.server_port}/redir"
            with patch.object(jev_cliente, "API_URL", url):
                cliente = jev_cliente.ClienteJev(obtener_llave=lambda: LLAVE_FALSA)
                self.assertIsNone(cliente.check("estado", "¿pasa?"))
                self.assertEqual(cliente.ultimo_error, "http_302")
        finally:
            servidor.shutdown()
        self.assertFalse(destino["visitado"])
        self.assertIsNone(destino["authorization"])


class CtxHermesReal:
    """Doble con la firma real de Hermes 0.22 (hermes_cli/plugins.py:266).

    get_config(key, default=None) lee plugins.entries.<plugin>.settings.<key>
    y devuelve el default que pasa el código si no existe; aquí el settings
    ya viene extraído, como lo entrega Hermes al plugin.
    """

    def __init__(self, settings=None, profile_name="claw"):
        self._settings = dict(settings or {})
        self.profile_name = profile_name

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


def cargar_plugin():
    """El paquete goncloud_team cargado como en Hermes, con su fake de cli."""
    if "goncloud_team_r4" in sys.modules:
        return sys.modules["goncloud_team_r4"]
    fake = str(ROOT / "tests" / "fake_hermes_cli")
    if fake not in sys.path:
        sys.path.insert(0, fake)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "goncloud_team_r4",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["goncloud_team_r4"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


class ConfigCtxRealTests(unittest.TestCase):
    """F4: _valor_fuente contra la firma real; sin get_config hay aviso."""

    def test_ctx_real_lee_settings_y_default_del_codigo(self):
        ctx = CtxHermesReal({"jev_umbral": 0.42, "jev_ticker_incidencia": False})
        config = jev_cliente.config_jev(ctx, env={})
        self.assertEqual(config.umbral, 0.42)
        self.assertFalse(config.punto_activo("ticker_incidencia"))
        # Lo que el ctx no trae sale del default que pasa config_jev.
        self.assertTrue(config.punto_activo("router_clasificacion"))

    def test_ctx_sin_get_config_avisa_y_usa_el_default_documentado(self):
        class CtxSinConfig:
            profile_name = "claw"

        with self.assertLogs(level="WARNING") as logs:
            config = jev_cliente.config_jev(CtxSinConfig(), env={})
        self.assertEqual(config.umbral, jev_cliente.DEFAULT_UMBRAL)
        self.assertTrue(any("get_config" in linea for linea in logs.output))

    def test_ctx_con_get_config_roto_avisa_y_usa_el_default(self):
        class CtxRoto(CtxHermesReal):
            def get_config(self, key, default=None):
                raise RuntimeError("config ilegible")

        with self.assertLogs(level="WARNING"):
            config = jev_cliente.config_jev(CtxRoto(), env={})
        self.assertEqual(config.umbral, jev_cliente.DEFAULT_UMBRAL)


class CadenaGatewayTests(unittest.TestCase):
    """F8/F6a: _build_service con ctx real hasta el router."""

    def test_la_config_del_ctx_llega_al_router_y_al_orquestador(self):
        plugin = cargar_plugin()
        ctx = CtxHermesReal({"jev_umbral": 0.42, "jev_router_clasificacion": False})
        servicio = plugin._build_service(ctx)
        self.assertEqual(servicio.jev_config.umbral, 0.42)
        self.assertFalse(servicio.jev_config.punto_activo("router_clasificacion"))

        import importlib

        workflow = importlib.import_module("goncloud_team_r4.workflow")
        router = workflow._build_subscription_router(jev_config=servicio.jev_config)
        self.assertEqual(router._jev_config_inyectada.umbral, 0.42)
        self.assertFalse(
            router._jev_config_inyectada.punto_activo("router_clasificacion")
        )

    def test_muse_delegate_recibe_la_config_del_ctx(self):
        plugin = cargar_plugin()
        herramientas = {}

        class CtxRegistro(CtxHermesReal):
            def register_tool(self, name=None, handler=None, **kwargs):
                herramientas[name] = handler

            def register_hook(self, *args, **kwargs):
                pass

        registro = CtxRegistro({"jev_umbral": 0.42}, profile_name="claw")
        capturado = {}
        original = plugin.delegate_to_muse
        plugin.delegate_to_muse = lambda **kwargs: (
            capturado.update(kwargs) or {"text": "ok"}
        )
        try:
            plugin.register(registro)
            herramientas["goncloud_muse_delegate"](
                {"prompt": "hazlo", "workspace": r"C:\dev\repo"}
            )
        finally:
            plugin.delegate_to_muse = original
        self.assertEqual(capturado["jev_config"].umbral, 0.42)


CONFIG_YAML_05 = (
    "plugins:\n"
    "  entries:\n"
    "    goncloud-team:\n"
    "      settings:\n"
    "        jev_umbral: 0.5\n"
)


def env_de_home(home):
    """El entorno de un proceso sin ctx: sin GONCLOUD_JEV_*, con su home."""
    env = {
        clave: valor
        for clave, valor in os.environ.items()
        if not clave.startswith("GONCLOUD_JEV_")
    }
    env["HERMES_HOME"] = str(home)
    return env


class CausaComunSinCtxTests(unittest.TestCase):
    """Causa común F1/F6: sin ctx, config_jev lee el config.yaml de la raíz."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.home = Path(self.tmp.name).resolve()
        (self.home / "config.yaml").write_text(CONFIG_YAML_05, encoding="utf-8")

    def test_workflow_sin_ctx_ve_umbral_e_interruptor_del_yaml(self):
        (self.home / "config.yaml").write_text(
            CONFIG_YAML_05 + "        jev_router_clasificacion: false\n",
            encoding="utf-8",
        )
        cargar_plugin()
        import importlib

        workflow = importlib.import_module("goncloud_team_r4.workflow")
        with patch.dict(os.environ, env_de_home(self.home), clear=True):
            servicio = workflow.WorkflowService(actor_role="claw")
            router = workflow._build_subscription_router(jev_config=servicio.jev_config)
            resuelto = router._jev_resolver()
        self.assertIsNotNone(resuelto)
        _, _, config = resuelto
        self.assertEqual(config.umbral, 0.5)
        self.assertFalse(config.punto_activo("router_clasificacion"))

    def test_muse_por_el_router_una_consulta_y_una_linea(self):
        # F6+F6b: la falla de Muse por el router la clasifica solo el router.
        import muse_bridge
        from subscriptions.adapters.muse import MuseAdapter

        registro = self.home / "jev" / "decisiones.jsonl"

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="1.0", stderr="")

        adapter = MuseAdapter(runner=runner)
        adapters = {"muse": adapter}
        from subscriptions.types import RouteSpec

        route = RouteSpec(
            slot=0,
            label="muse",
            adapter="muse",
            model="muse-spark-1.3",
            billing_mode="subscription",
        )
        request = InvocationRequest(
            workflow_id="wf-muse",
            stage_id="st-muse",
            role="implementer",
            prompt="Do it.",
            workspace=self.home,
            start_revision="",
            current_revision="",
        )
        from types import SimpleNamespace

        evidencia = SimpleNamespace(
            returncode=1,
            stream="salida rara",
            stderr="429 Output token rate limit exceeded. Please retry after a brief wait.",
            stdout="",
        )
        llamadas = []

        def transporte(url, payload, headers, timeout):
            llamadas.append(payload)
            return 200, {"model": "jev-latest", "answers": answers_choice("quota", 0.6)}

        env = {**env_de_home(self.home), "TYPESAFE_API_KEY": LLAVE_FALSA}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(muse_bridge, "build_muse_command", lambda *a, **k: ["muse"]),
            patch.object(muse_bridge, "run_cli", lambda *a, **k: evidencia),
            patch.object(jev_cliente, "_transporte_urllib", transporte),
        ):
            router = SubscriptionRouter(
                adapters=adapters,
                db_path=self.home / "router.db",
                routing_source=SOURCE,
                sleep=lambda segundos: None,
            )
            resultado = router.run(request, [route])
        self.assertFalse(resultado.success)
        self.assertEqual(resultado.failure, FailureKind.QUOTA)
        self.assertEqual(len(llamadas), 1)
        lineas = registro.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lineas), 1)
        decision = json.loads(lineas[0])
        self.assertEqual(decision["punto"], "router_clasificacion")
        self.assertEqual(decision["umbral"], 0.5)
        # El adaptador recibió la config del router (aunque no clasifica).
        self.assertIsNotNone(adapter.jev_config)
        self.assertEqual(adapter.jev_config.umbral, 0.5)


class RegistroRaizTests(unittest.TestCase):
    """F10: el registro deriva de la raíz de Hermes, no de HERMES_HOME."""

    def test_sin_hermes_home_sale_del_override_de_contexto(self):
        fake = str(ROOT / "tests" / "fake_hermes_cli")
        if fake not in sys.path:
            sys.path.insert(0, fake)
        import hermes_constants

        with tempfile.TemporaryDirectory() as directorio:
            raiz = Path(directorio).resolve()
            hermes_constants.set_override(str(raiz / "profiles" / "claw"))
            try:
                env = {
                    clave: valor
                    for clave, valor in os.environ.items()
                    if clave != "HERMES_HOME"
                }
                with patch.dict(os.environ, env, clear=True):
                    ruta = jev_cliente.default_registro_path()
            finally:
                hermes_constants.set_override(None)
        self.assertEqual(ruta, raiz / "jev" / "decisiones.jsonl")


class MusePaqueteTests(unittest.TestCase):
    """F9: _jev_muse funciona importando el plugin como paquete, sin sys.path."""

    def test_paquete_sin_directorio_en_syspath_consulta(self):
        codigo = (
            "import sys, json, importlib.util\n"
            f"sys.path.insert(0, {str(ROOT / 'tests' / 'fake_hermes_cli')!r})\n"
            f"_spec = importlib.util.spec_from_file_location("
            f"'goncloud_team', {str(PLUGIN_DIR / '__init__.py')!r},"
            f" submodule_search_locations=[{str(PLUGIN_DIR)!r}])\n"
            "_pkg = importlib.util.module_from_spec(_spec)\n"
            "sys.modules['goncloud_team'] = _pkg\n"
            "_spec.loader.exec_module(_pkg)\n"
            "import goncloud_team.jev_cliente as jc\n"
            "import goncloud_team.muse_bridge as mb\n"
            "def transporte(url, payload, headers, timeout):\n"
            "    return 200, {'answers': {'jev_route': {'type': 'choice',"
            " 'choice': 'quota', 'probabilities': {'quota': 0.9, 'auth': 0.1},"
            " 'confidence': 0.9}}}\n"
            "cfg = jc.JevConfig()\n"
            "cliente = jc.ClienteJev(transporte=transporte,"
            " obtener_llave=lambda: 'sk-falsa-de-prueba', config=cfg)\n"
            "resuelto = mb._jev_muse(cliente, cfg)\n"
            "salida = None\n"
            "if resuelto is not None:\n"
            "    modulo, cli, config = resuelto\n"
            "    salida = modulo.reclasificar_falla(cli, config,"
            " punto=modulo.PUNTO_MUSE_BRIDGE_CLASIFICACION, regla='auth',"
            " estado='adapter: muse', contexto={})\n"
            "print(json.dumps({'resuelto': resuelto is not None, 'salida': salida}))\n"
        )
        env = {
            clave: valor
            for clave, valor in os.environ.items()
            if clave not in ("PYTHONPATH",)
        }
        completado = subprocess.run(
            [sys.executable, "-c", codigo],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(completado.returncode, 0, completado.stderr[-2000:])
        resultado = json.loads(completado.stdout.strip().splitlines()[-1])
        self.assertTrue(resultado["resuelto"])
        self.assertEqual(resultado["salida"], "quota")


if __name__ == "__main__":
    unittest.main()
