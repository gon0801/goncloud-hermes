#!/usr/bin/env python3
"""Encargo H-avisos-a-la-sesion: el aviso de una cadena llega a la sesión
que la lanzó, no al Bot Chat genérico.

(a) cadena lanzada con session_key -> al quedar blocked se encola un evento
    con esa session_key y el aviso queda .entregado;
(b) aviso sin session_key -> el vigilante del gateway lo deja para el
    despertar de hoy;
(c) el despertar ignora los avisos que traen session_key (no duplica);
(d) reinicio: un aviso pendiente se entrega una sola vez.
"""

from __future__ import annotations

import importlib
import json
import os
import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))
sys.path.insert(0, str(ROOT / "tests"))

import local_orchestrator as local_orchestrator_module  # noqa: E402
from test_local_orchestrator import (  # noqa: E402
    DriverBootstrapTests,
    _OrchestratorHarness,
    _plugin_package,
    envelope,
    fenced,
)

avisos_sesion = importlib.import_module("avisos_sesion")
despertar = importlib.import_module("raices.despertar")

SESSION_KEY = "agent:claw:telegram:dm:6470689715"
ROUTING = {"user_id": "6470689715", "user_name": "David", "scope_id": "scope-1"}


class ColaFalsa:
    """El doble de process_registry.completion_queue."""

    def __init__(self):
        self.eventos = []

    def put(self, evento):
        self.eventos.append(evento)


def escribir_aviso(buzon: Path, nombre: str, aviso: dict) -> Path:
    buzon.mkdir(parents=True, exist_ok=True)
    ruta = buzon / nombre
    ruta.write_text(json.dumps(aviso), encoding="utf-8")
    return ruta


AVISO_CON_ORIGEN = {
    "workflow_id": "t_abc",
    "repo": "owner/repo",
    "outcome": "blocked",
    "etapa": "reviewer",
    "motivo": "el reviewer rechazó el cambio",
    "instruccion": "inspecciona",
    "fecha": "2026-10-19T00:00:00+00:00",
    "origin_session_key": SESSION_KEY,
    "origin_routing": ROUTING,
}

AVISO_SIN_ORIGEN = {
    "workflow_id": "t_vieja",
    "repo": "owner/repo",
    "outcome": "delivered",
    "etapa": None,
    "motivo": "",
    "instruccion": "inspecciona",
    "fecha": "2026-10-19T00:00:00+00:00",
}


class AvisosALaSesionTests(_OrchestratorHarness):
    def buzon(self):
        return self.root / "goncloud-local" / "avisos"

    # (a) la cadena guarda el origen, el driver lo copia al aviso y el
    # vigilante lo encola a ESA sesión.
    def test_a_cadena_con_session_key_encola_en_su_sesion(self):
        self.responses["implementer"] = fenced(
            envelope(status="blocked", summary="Missing credentials.")
        )
        resultado = self.start(
            origin_session_key=SESSION_KEY, origin_routing=dict(ROUTING)
        )
        self.assertEqual(resultado["outcome"], "blocked")
        nombre = f"{resultado['workflow_id']}-{resultado['run_id']}-blocked.json"
        aviso = json.loads((self.buzon() / nombre).read_text(encoding="utf-8"))
        self.assertEqual(aviso["origin_session_key"], SESSION_KEY)
        self.assertEqual(aviso["origin_routing"], ROUTING)

        cola = ColaFalsa()
        entregados = avisos_sesion.entregar_avisos(self.buzon(), cola)

        self.assertEqual(entregados, 1)
        self.assertEqual(len(cola.eventos), 1)
        evento = cola.eventos[0]
        self.assertEqual(evento["type"], "completion")
        self.assertEqual(evento["session_key"], SESSION_KEY)
        self.assertEqual(evento["platform"], "telegram")
        self.assertEqual(evento["chat_type"], "dm")
        self.assertEqual(evento["chat_id"], "6470689715")
        self.assertEqual(evento["user_id"], "6470689715")
        self.assertIn("se trabó en implementer", evento["output"])
        self.assertIn("revisa con goncloud_local_status y sigue", evento["output"])
        self.assertFalse((self.buzon() / nombre).exists())
        # F2: al encolar queda .encolado, NO .entregado — el evento solo
        # vive en la cola en memoria y el gateway aún no lo inyectó.
        self.assertTrue((self.buzon() / nombre.replace(".json", ".encolado")).exists())
        self.assertFalse(
            (self.buzon() / nombre.replace(".json", ".entregado")).exists()
        )

    # (a, handler) goncloud_local_run captura la sesión que lo llama.
    # F4: register() se llama con el vigilante DOBLADO — arrancar el real
    # aquí contaminaba la prueba del hilo con un vigilante de otro buzón.
    def test_el_handler_captura_la_sesion_llamante(self):
        plugin = _plugin_package()
        registrado = {}

        class Ctx:
            profile_name = "claw"

            def register_tool(self, **kwargs):
                registrado[kwargs["name"]] = kwargs

            def register_hook(self, name, hook):
                pass

        avisos_pkg = importlib.import_module("goncloud_team.avisos_sesion")
        with patch.object(avisos_pkg, "iniciar_vigilante") as vigilante:
            plugin.register(Ctx())
        vigilante.assert_called_once_with()
        handler = registrado["goncloud_local_run"]["handler"]
        llamadas = {}

        class OrquestadorFalso:
            def create(self, **kwargs):
                llamadas.update(kwargs)
                return {"workflow_id": "t_fake", "stages": {}}

            def launch_detached(self, workflow_id, stop_after=None):
                return {"workflow_id": workflow_id, "driver_pid": 1}

        entorno = {
            "HERMES_SESSION_KEY": SESSION_KEY,
            "HERMES_SESSION_USER_ID": "6470689715",
            "HERMES_SESSION_USER_NAME": "David",
        }
        with patch.object(
            plugin, "_build_orchestrator", lambda ctx: OrquestadorFalso()
        ):
            with patch.dict(os.environ, entorno, clear=False):
                salida = json.loads(
                    handler(
                        {
                            "request_key": "con-sesion",
                            "repository": str(self.repo),
                            "instruction": "Inspecciona.",
                        }
                    )
                )
        self.assertNotIn("error", salida)
        self.assertEqual(llamadas["origin_session_key"], SESSION_KEY)
        self.assertEqual(llamadas["origin_routing"]["user_id"], "6470689715")

    def test_capturar_origen_sin_sesion_devuelve_vacio(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(local_orchestrator_module.capturar_origen_sesion(), {})

    # (b) sin session_key: el vigilante no lo toca; queda para el despertar.
    def test_b_aviso_sin_session_key_queda_para_el_despertar(self):
        ruta = escribir_aviso(
            self.buzon(), "t_vieja-run-delivered.json", AVISO_SIN_ORIGEN
        )
        cola = ColaFalsa()
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon(), cola), 0)
        self.assertEqual(cola.eventos, [])
        self.assertTrue(ruta.exists(), "el despertar de hoy lo sigue drenando")

    # (c) el despertar ignora los que traen session_key: ni imprime ni marca.
    def test_c_despertar_ignora_avisos_con_session_key(self):
        ruta = escribir_aviso(self.buzon(), "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        impresos = []
        despertar.correr(
            home=self.root, escribir=impresos.append, reloj=lambda: "2026-10-19"
        )
        self.assertFalse(any("t_abc" in linea for linea in impresos))
        self.assertTrue(ruta.exists(), "no lo marca: lo entrega el gateway")

    # (c, par) el despertar SÍ sigue entregando los que no traen origen.
    def test_c_despertar_si_entrega_avisos_sin_session_key(self):
        escribir_aviso(self.buzon(), "t_vieja-run-delivered.json", AVISO_SIN_ORIGEN)
        impresos = []
        despertar.correr(
            home=self.root, escribir=impresos.append, reloj=lambda: "2026-10-19"
        )
        self.assertTrue(any("t_vieja" in linea for linea in impresos))

    # (d) reinicio del gateway: el pendiente se encola una sola vez por vida.
    def test_d_reinicio_entrega_el_pendiente_una_sola_vez(self):
        escribir_aviso(self.buzon(), "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        cola = ColaFalsa()
        # Primera vida del gateway: encola el pendiente y queda .encolado.
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon(), cola), 1)
        # Otra pasada en la MISMA vida no lo duplica (fuera del glob *.json).
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon(), cola), 0)
        self.assertEqual(len(cola.eventos), 1)
        self.assertTrue((self.buzon() / "t_abc-run-blocked.encolado").exists())

    # (d/F2) reinicio con un .encolado: el arranque lo re-encola una vez,
    # porque la cola en memoria se perdió con el proceso anterior.
    def test_d_reinicio_reencola_el_encolado_una_vez(self):
        ruta = escribir_aviso(self.buzon(), "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        ruta.replace(ruta.with_suffix(".encolado"))
        cola = ColaFalsa()
        self.assertEqual(avisos_sesion.reencolar_encolados(self.buzon(), cola), 1)
        self.assertEqual(len(cola.eventos), 1)
        self.assertEqual(cola.eventos[0]["session_key"], SESSION_KEY)
        # Sigue .encolado: su .entregado llega recién a las 24 h.
        self.assertTrue((self.buzon() / "t_abc-run-blocked.encolado").exists())
        import time as _time

        viejos = avisos_sesion.purgar_encolados(
            self.buzon(), ahora=_time.time() + 25 * 3600
        )
        self.assertEqual(viejos, 1)
        self.assertTrue((self.buzon() / "t_abc-run-blocked.entregado").exists())

    # F1: register() fuera del gateway NO arranca ningún vigilante.
    def test_register_fuera_del_gateway_no_arranca_vigilante(self):
        import threading

        plugin = _plugin_package()

        class Ctx:
            profile_name = "claw"

            def register_tool(self, **kwargs):
                pass

            def register_hook(self, name, hook):
                pass

        avisos_pkg = importlib.import_module("goncloud_team.avisos_sesion")
        self.assertFalse(avisos_sesion.es_proceso_gateway())
        plugin.register(Ctx())
        self.assertIsNone(avisos_pkg._hilo)
        self.assertFalse(
            any(
                hilo.name == "goncloud-avisos-sesion"
                for hilo in threading.enumerate()
                if hilo.is_alive()
            )
        )

    # El vigilante en hilo entrega sin modelo y se detiene limpio
    # (seam explícito: exigir_gateway=False con la cola del doble).
    def test_el_vigilante_en_hilo_entrega_y_para(self):
        escribir_aviso(self.buzon(), "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        cola_real = queue.Queue()
        import threading

        detener = threading.Event()
        hilo = avisos_sesion.iniciar_vigilante(
            self.buzon(),
            cola=cola_real,
            intervalo=0.05,
            detener=detener,
            exigir_gateway=False,
        )
        self.assertIsNotNone(hilo)
        evento = cola_real.get(timeout=5)
        self.assertEqual(evento["session_key"], SESSION_KEY)
        detener.set()
        hilo.join(timeout=5)
        self.assertFalse(hilo.is_alive())
        self.assertTrue(cola_real.empty())


class BootErrorOrigenTests(DriverBootstrapTests):
    """F3: el aviso de boot-error hereda el origen de la marca running."""

    def test_el_aviso_de_boot_error_lleva_el_origen_de_la_marca(self):
        self.out = self.root / "goncloud-local" / "t_boot.json"
        self.out.parent.mkdir()
        # La marca que launch_detached/resume dejan en el archivo de
        # resultado, con el origen de la sesión que lanzó la cadena.
        self.out.write_text(
            json.dumps(
                {
                    "workflow_id": "t_boot",
                    "outcome": "running",
                    "run_id": "marca-1",
                    "origin_session_key": SESSION_KEY,
                    "origin_routing": ROUTING,
                }
            ),
            encoding="utf-8",
        )
        self.run_driver()
        avisos = self.root / "goncloud-local" / "avisos"
        evento = json.loads(
            (avisos / "t_boot-boot-error.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evento["outcome"], "error")
        self.assertEqual(evento["origin_session_key"], SESSION_KEY)
        self.assertEqual(evento["origin_routing"], ROUTING)
        # Y el vigilante lo puede encolar a esa sesión, como cualquier aviso.
        cola = ColaFalsa()
        self.assertEqual(avisos_sesion.entregar_avisos(avisos, cola), 1)
        self.assertEqual(cola.eventos[0]["session_key"], SESSION_KEY)


if __name__ == "__main__":
    unittest.main()
