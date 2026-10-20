#!/usr/bin/env python3
"""Encargo H-avisos-a-la-sesion: el aviso de una cadena llega a la sesión
que la lanzó, no al Bot Chat genérico.

(a) cadena lanzada con session_key -> al quedar blocked se encola un evento
    con esa session_key y el aviso queda .encolado (no .entregado: el
    evento solo vive en la cola en memoria hasta que el gateway lo drena);
(b) aviso sin session_key -> el vigilante del gateway lo deja para el
    despertar de hoy;
(c) el despertar ignora los avisos que traen session_key (no duplica);
(d) reinicio: un aviso pendiente se entrega una sola vez.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))
sys.path.insert(0, str(ROOT / "tests"))

import local_orchestrator as local_orchestrator_module  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402
from test_local_orchestrator import (  # noqa: E402
    DriverBootstrapTests,
    _OrchestratorHarness,
    _plugin_package,
    envelope,
    fenced,
)

avisos_sesion = importlib.import_module("avisos_sesion")
despertar = importlib.import_module("raices.despertar")
gateway_doble = importlib.import_module("gateway_watch_doble")

SESSION_KEY = "agent:claw:telegram:dm:6470689715"
ROUTING = {"user_id": "6470689715", "user_name": "David", "scope_id": "scope-1"}


class ColaFalsa:
    """El doble de process_registry.completion_queue."""

    def __init__(self):
        self.eventos = []

    def put(self, evento):
        self.eventos.append(evento)


class ColaQueFallaAlPrincipio(ColaFalsa):
    """put lanza hasta que se le quita la falla: el reclamo debe soltarse."""

    def __init__(self):
        super().__init__()
        self.falla = True

    def put(self, evento):
        if self.falla:
            raise RuntimeError("cola caida")
        super().put(evento)


class ColaQueAnotaElReclamo(ColaFalsa):
    """Anota si al encolar el .json ya fue reclamado a .encolado."""

    def __init__(self, ruta_json: Path):
        super().__init__()
        self._ruta_json = ruta_json
        self.json_presente_al_put = None
        self.encolado_presente_al_put = None

    def put(self, evento):
        self.json_presente_al_put = self._ruta_json.exists()
        self.encolado_presente_al_put = self._ruta_json.with_suffix(
            ".encolado"
        ).exists()
        super().put(evento)


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
        self.assertEqual(evento["type"], "watch_match")
        self.assertEqual(evento["pattern"], "cadena blocked")
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

    # B1: el evento SOBREVIVE el drain real del gateway (v0.22.0 e06df9c)
    # y el formato watch_match produce el texto con la línea del aviso.
    # Sobre bc34420 (type=completion) el drain devuelve []: el aviso se
    # sacaba de la cola y se tiraba, claw nunca despertaba.
    def test_b1_el_evento_pasa_el_drain_y_el_formato_del_gateway(self):
        evento = avisos_sesion.construir_evento(
            AVISO_CON_ORIGEN, nombre="t_abc-run-blocked.json"
        )
        self.assertEqual(evento["type"], "watch_match")
        self.assertEqual(evento["pattern"], "cadena blocked")
        cola = queue.Queue()
        cola.put(evento)
        drenados = gateway_doble._drain_gateway_watch_events(cola)
        self.assertEqual(drenados, [evento])
        texto = gateway_doble._format_gateway_process_notification(drenados[0])
        self.assertIn("se trabó en reviewer", texto)
        self.assertIn("revisa con goncloud_local_status y sigue", texto)
        self.assertIn(SESSION_KEY, evento["session_key"])
        self.assertEqual(evento["goncloud_aviso"], "t_abc-run-blocked.json")

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

    # M1: si construir/encolar falla tras el reclamo, el aviso vuelve a
    # .json y la pasada siguiente lo reintenta (no queda .encolado huerfano).
    def test_m1_put_que_lanza_devuelve_a_json_y_la_siguiente_pasada_encola(self):
        ruta = escribir_aviso(self.buzon(), "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        cola = ColaQueFallaAlPrincipio()
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon(), cola), 0)
        self.assertTrue(ruta.exists())
        self.assertFalse((self.buzon() / "t_abc-run-blocked.encolado").exists())
        self.assertEqual(cola.eventos, [])
        cola.falla = False
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon(), cola), 1)
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

    # B2: el gateway ya drenó el evento (no está en la cola) → la siguiente
    # pasada cierra el .encolado a .entregado; si sigue en cola, no se toca.
    def test_b2_drenado_pasa_a_entregado_y_en_cola_sigue_encolado(self):
        escribir_aviso(self.buzon(), "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        cola = queue.Queue()
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon(), cola), 1)
        encolado = self.buzon() / "t_abc-run-blocked.encolado"
        self.assertTrue(encolado.exists())
        # Aún en la cola: el gateway no lo drenó, sigue .encolado.
        self.assertEqual(avisos_sesion.cerrar_encolados_drenados(self.buzon(), cola), 0)
        self.assertTrue(encolado.exists())
        # El watcher del gateway lo drena (sale de la cola): se cierra.
        cola.get_nowait()
        self.assertEqual(avisos_sesion.cerrar_encolados_drenados(self.buzon(), cola), 1)
        self.assertFalse(encolado.exists())
        self.assertTrue((self.buzon() / "t_abc-run-blocked.entregado").exists())

    # B2: al reiniciar solo se re-encola el .encolado NO drenado; el que el
    # proceso anterior ya vio drenar quedó .entregado y no vuelve a claw.
    def test_b2_reinicio_reencola_solo_el_no_drenado(self):
        otro = dict(AVISO_CON_ORIGEN, workflow_id="t_xyz")
        escribir_aviso(self.buzon(), "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        escribir_aviso(self.buzon(), "t_xyz-run-blocked.json", otro)
        cola = queue.Queue()
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon(), cola), 2)
        # El gateway drena solo el primero antes del reinicio.
        cola.get_nowait()
        self.assertEqual(avisos_sesion.cerrar_encolados_drenados(self.buzon(), cola), 1)
        # Reinicio: proceso nuevo (sin memoria de encolados), cola vacía.
        avisos_sesion._encolados_propios.clear()
        cola2 = queue.Queue()
        self.assertEqual(avisos_sesion.reencolar_encolados(self.buzon(), cola2), 1)
        reencolado = cola2.get_nowait()
        self.assertEqual(reencolado["goncloud_aviso"], "t_xyz-run-blocked.json")
        self.assertTrue(cola2.empty())

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


def _cargar_copia(nombre: str):
    """Otra copia de avisos_sesion, como la que carga cada perfil."""
    ruta = ROOT / "plugins/goncloud-team/avisos_sesion.py"
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


class ColaConBarrera(ColaFalsa):
    """put sincroniza a los productores: si dos encolan, se ven."""

    def __init__(self, barrera):
        super().__init__()
        self._barrera = barrera
        self._candado = threading.Lock()

    def put(self, evento):
        try:
            self._barrera.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        with self._candado:
            self.eventos.append(evento)


class VigilanteUnicoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self._tmp)
        self.buzon = Path(self._tmp.name) / "avisos"
        self.buzon.mkdir(parents=True)

    def _hilos_vivos(self):
        return [
            hilo
            for hilo in threading.enumerate()
            if hilo.name == "goncloud-avisos-sesion" and hilo.is_alive()
        ]

    def test_dos_copias_un_solo_vigilante(self):
        escribir_aviso(self.buzon, "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        copia1 = _cargar_copia("avisos_copia_uno")
        copia2 = _cargar_copia("avisos_copia_dos")
        cola = ColaFalsa()
        alto1, alto2 = threading.Event(), threading.Event()
        hilo1 = copia1.iniciar_vigilante(
            self.buzon,
            cola=cola,
            intervalo=0.05,
            detener=alto1,
            exigir_gateway=False,
        )
        try:
            hilo2 = copia2.iniciar_vigilante(
                self.buzon,
                cola=cola,
                intervalo=0.05,
                detener=alto2,
                exigir_gateway=False,
            )
            self.assertIsNotNone(hilo1)
            self.assertIs(hilo1, hilo2)
            self.assertEqual(len(self._hilos_vivos()), 1)
        finally:
            alto1.set()
            alto2.set()
            for hilo in self._hilos_vivos():
                hilo.join(timeout=5)

    def test_un_segundo_buzon_no_adopta_el_vigilante_del_primero(self):
        # H62 F3: el fallback por nombre de hilo adoptaba cualquier
        # vigilante vivo; una copia con otro buzón/cola recibía el hilo del
        # primero y su buzón quedaba sin vigilar.
        otro_buzon = Path(self._tmp.name) / "otro" / "avisos"
        otro_buzon.mkdir(parents=True)
        escribir_aviso(otro_buzon, "t_xyz-run-blocked.json", AVISO_CON_ORIGEN)
        cola1, cola2 = ColaFalsa(), ColaFalsa()
        alto1, alto2 = threading.Event(), threading.Event()
        hilo1 = avisos_sesion.iniciar_vigilante(
            self.buzon,
            cola=cola1,
            intervalo=0.05,
            detener=alto1,
            exigir_gateway=False,
        )
        try:
            hilo2 = avisos_sesion.iniciar_vigilante(
                otro_buzon,
                cola=cola2,
                intervalo=0.05,
                detener=alto2,
                exigir_gateway=False,
            )
            self.assertIsNotNone(hilo1)
            self.assertIsNotNone(hilo2)
            self.assertIsNot(hilo1, hilo2)
            # El segundo buzón sí se vigila: su aviso llega a su cola.
            for _ in range(100):
                if cola2.eventos:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(len(cola2.eventos), 1)
            self.assertEqual(cola2.eventos[0]["session_key"], SESSION_KEY)
        finally:
            alto1.set()
            alto2.set()
            for hilo in self._hilos_vivos():
                hilo.join(timeout=5)

    def test_el_logger_cae_en_el_arbol_que_hermes_escribe(self):
        # H62 B3-3 / H70 L2 + T1 (#73): importado como módulo de primer
        # nivel, __name__ queda fuera de hermes_plugins.* y Hermes no
        # escribe esos .log. La prueba anterior esperaba el mismo literal
        # que el código traía fijo, así que no discriminaba: aquí el módulo
        # se carga como submódulo de un paquete con OTRO nombre
        # (hermes_plugins.plugin_prueba) y se exige ese nombre; con el
        # literal fijo de antes (hermes_plugins.goncloud_team.*) sale roja.
        plugin_dir = ROOT / "plugins/goncloud-team"
        nombres = (
            "hermes_plugins",
            "hermes_plugins.plugin_prueba",
            "hermes_plugins.plugin_prueba.avisos_sesion",
        )
        previos = {nombre: sys.modules.get(nombre) for nombre in nombres}
        paquete_raiz = types.ModuleType("hermes_plugins")
        paquete_raiz.__path__ = []
        paquete = types.ModuleType("hermes_plugins.plugin_prueba")
        paquete.__path__ = [str(plugin_dir)]
        sys.modules["hermes_plugins"] = paquete_raiz
        sys.modules["hermes_plugins.plugin_prueba"] = paquete
        sys.modules.pop("hermes_plugins.plugin_prueba.avisos_sesion", None)
        # Como el __init__ de Hermes: el logger del paquete ya existe.
        logging.getLogger("hermes_plugins.plugin_prueba")
        try:
            modulo = importlib.import_module(
                "hermes_plugins.plugin_prueba.avisos_sesion"
            )
            self.assertEqual(
                modulo.logger.name, "hermes_plugins.plugin_prueba.avisos_sesion"
            )
            self.assertIs(
                modulo.logger.parent,
                logging.getLogger("hermes_plugins.plugin_prueba"),
            )
        finally:
            for nombre, previo in previos.items():
                if previo is None:
                    sys.modules.pop(nombre, None)
                else:
                    sys.modules[nombre] = previo
        # Y el camino de siempre sigue en su árbol: hijo directo del logger
        # del paquete (__init__) bajo Hermes, no solo con el mismo prefijo.
        plugin = _plugin_package()
        avisos_pkg = importlib.import_module("goncloud_team.avisos_sesion")
        esperado = f"hermes_plugins.{plugin.__name__}.avisos_sesion"
        self.assertEqual(plugin.logger.name, plugin.__name__)
        self.assertEqual(avisos_sesion.logger.name, esperado)
        self.assertEqual(avisos_pkg.logger.name, esperado)
        logger_init_hermes = logging.getLogger(f"hermes_plugins.{plugin.__name__}")
        self.assertIs(avisos_sesion.logger.parent, logger_init_hermes)

    def test_el_log_del_aviso_no_expone_la_session_key_completa(self):
        # cr 4320005117: el chat id de la session_key no va al log; se
        # conserva plataforma y tipo para poder seguir el aviso.
        escribir_aviso(self.buzon, "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        cola = ColaFalsa()
        with self.assertLogs(avisos_sesion.logger, level="INFO") as capturado:
            self.assertEqual(avisos_sesion.entregar_avisos(self.buzon, cola), 1)
        texto = "\n".join(capturado.output)
        self.assertNotIn(SESSION_KEY, texto)
        self.assertNotIn("6470689715", texto)
        self.assertIn("telegram", texto)
        self.assertIn("dm", texto)

    def test_dos_hilos_mismo_buzon_exactamente_un_put(self):
        escribir_aviso(self.buzon, "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        copia1 = _cargar_copia("avisos_copia_tres")
        copia2 = _cargar_copia("avisos_copia_cuatro")
        cola = ColaConBarrera(threading.Barrier(2))
        salida = threading.Barrier(3)
        resultados = []

        def correr(modulo):
            salida.wait(timeout=5)
            resultados.append(modulo.entregar_avisos(self.buzon, cola))

        hilos = [
            threading.Thread(target=correr, args=(copia1,)),
            threading.Thread(target=correr, args=(copia2,)),
        ]
        for hilo in hilos:
            hilo.start()
        salida.wait(timeout=5)
        for hilo in hilos:
            hilo.join(timeout=10)
        self.assertEqual(len(cola.eventos), 1)
        self.assertEqual(sum(resultados), 1)
        self.assertTrue((self.buzon / "t_abc-run-blocked.encolado").exists())
        self.assertFalse((self.buzon / "t_abc-run-blocked.json").exists())

    # L1: el reclamo directo lo gana uno solo; el otro recibe None.
    def test_reclamo_directo_dos_sobre_la_misma_ruta_uno_gana(self):
        ruta = escribir_aviso(self.buzon, "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        primero = avisos_sesion._reclamar(ruta)
        segundo = avisos_sesion._reclamar(ruta)
        self.assertIsNotNone(primero)
        self.assertEqual(primero, self.buzon / "t_abc-run-blocked.encolado")
        self.assertTrue(primero.exists())
        self.assertIsNone(segundo)
        self.assertFalse(ruta.exists())

    # L1: al encolar, el .json ya no existe (reclamo ANTES del put). Con
    # el orden viejo (put y luego renombrar) esta prueba queda en rojo.
    def test_put_ve_el_aviso_ya_reclamado(self):
        ruta = escribir_aviso(self.buzon, "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        cola = ColaQueAnotaElReclamo(ruta)
        self.assertEqual(avisos_sesion.entregar_avisos(self.buzon, cola), 1)
        self.assertFalse(cola.json_presente_al_put)
        self.assertTrue(cola.encolado_presente_al_put)

    def test_reinicio_encolado_una_sola_reencolada_con_dos_copias(self):
        ruta = escribir_aviso(self.buzon, "t_abc-run-blocked.json", AVISO_CON_ORIGEN)
        ruta.replace(ruta.with_suffix(".encolado"))
        copia1 = _cargar_copia("avisos_copia_cinco")
        copia2 = _cargar_copia("avisos_copia_seis")
        # Simula proceso nuevo: sin memoria de encolados propios.
        copia1._encolados_propios.clear()
        cola = ColaFalsa()
        total = copia1.reencolar_encolados(self.buzon, cola)
        total += copia2.reencolar_encolados(self.buzon, cola)
        self.assertEqual(total, 1)
        self.assertEqual(len(cola.eventos), 1)
        self.assertEqual(cola.eventos[0]["session_key"], SESSION_KEY)


if __name__ == "__main__":
    unittest.main()
