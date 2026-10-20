"""La consulta automatica a JEV antes de cargar skill (encargo H-jev-auto r1).

Dobles del router (corredor falso y Popen falso) y del propio hook: los
turnos de claw en telegram y cli consultan y devuelven contexto; cron,
bot-chat, kanban, otro perfil o un turno sin user_message no consultan nada;
router lento o roto es fail-open con motivo acotado; el registro JSONL deja
la consulta sin el texto del mensaje (solo su largo y un sha256 corto) y sin
la llave; el pie "JEV: ..." sale una sola vez por turno, solo donde hubo
consulta, y se apaga con la llave de config del plugin; la llave
JEV_TYPESAFE_API_KEY solo viaja al entorno del hijo, jamas a argv, logs ni
registro.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

RAIZ = Path(__file__).resolve().parents[1]
PLUGIN = RAIZ / "plugins/goncloud-team"
sys.path.insert(0, str(RAIZ / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

import hermes_falso  # noqa: E402

_MODULO = None


def cargar_jev_auto():
    global _MODULO
    if _MODULO is None:
        spec = importlib.util.spec_from_file_location(
            "jev_auto_prueba", PLUGIN / "jev_auto.py"
        )
        modulo = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = modulo
        spec.loader.exec_module(modulo)
        _MODULO = modulo
    return _MODULO


MENSAJE = "abre el panel de suscripciones de amazon"
LLAVE_ENV = "sk-entorno-nunca-en-logs"
LLAVE_SCOPE = "sk-scope-tampoco"


def turno(**overrides):
    base = {
        "session_id": "s1",
        "turn_id": "t1",
        "platform": "telegram",
        "user_message": MENSAJE,
        "model": "m",
        "is_first_turn": True,
    }
    base.update(overrides)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.modulo = cargar_jev_auto()
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.temporal)
        self.hogar = Path(self.temporal.name)
        self.llamadas = []
        entorno = mock.patch.dict(
            os.environ, {"JEV_TYPESAFE_API_KEY": LLAVE_ENV}, clear=False
        )
        entorno.start()
        self.addCleanup(entorno.stop)

    def corredor_ok(self, texto, hogar):
        self.llamadas.append((texto, hogar))
        return {
            "estado": "ok",
            "skill": "web-navigation",
            "motivo": None,
            "ms": 120,
            "gate": 0.9,
            "fit": 0.9,
        }

    def corredor_vacio(self, texto, hogar):
        self.llamadas.append((texto, hogar))
        return {"estado": "vacio", "skill": None, "motivo": None, "ms": 90}

    def corredor_fallo(self, motivo):
        def corredor(texto, hogar):
            self.llamadas.append((texto, hogar))
            return {"estado": "fallo", "skill": None, "motivo": motivo, "ms": 8000}

        return corredor

    def auto(self, perfil="claw", corredor=None, **kwargs):
        kwargs.setdefault("hogar", self.hogar)
        kwargs.setdefault("corredor", corredor or self.corredor_ok)
        return self.modulo.JevAuto(perfil, **kwargs)

    def registro(self):
        ruta = self.hogar / "jev" / "consultas.jsonl"
        if not ruta.is_file():
            return []
        return [json.loads(linea) for linea in ruta.read_text().splitlines()]


class PuertaDelTurnoTests(Base):
    """Solo claw en telegram/cli con mensaje consulta; lo demas, nada."""

    def test_claw_en_telegram_consulta_con_el_texto_del_mensaje(self):
        auto = self.auto()
        respuesta = auto.pre_llm_call(**turno())
        self.assertEqual(
            respuesta, {"context": "JEV skill_select: web-navigation (cárgala)"}
        )
        self.assertEqual(self.llamadas, [(MENSAJE, self.hogar)])

    def test_claw_en_cli_tambien_consulta(self):
        auto = self.auto()
        respuesta = auto.pre_llm_call(**turno(platform="cli"))
        self.assertEqual(
            respuesta, {"context": "JEV skill_select: web-navigation (cárgala)"}
        )

    def test_otro_perfil_no_consulta_nada(self):
        auto = self.auto(perfil="scout")
        self.assertIsNone(auto.pre_llm_call(**turno()))
        self.assertIsNone(auto.transform_llm_output(**turno(response_text="hola")))
        self.assertEqual(self.llamadas, [])
        self.assertEqual(self.registro(), [])

    def test_plataformas_sin_persona_no_consultan(self):
        auto = self.auto()
        for plataforma in ("cron", "bot-chat", "kanban", "despertar", ""):
            with self.subTest(plataforma=plataforma):
                self.assertIsNone(auto.pre_llm_call(**turno(platform=plataforma)))
        self.assertEqual(self.llamadas, [])
        self.assertEqual(self.registro(), [])

    def test_turno_sin_user_message_no_consulta(self):
        auto = self.auto()
        for mensaje in (None, "", "   ", b"texto", ["parte"]):
            with self.subTest(mensaje=mensaje):
                self.assertIsNone(auto.pre_llm_call(**turno(user_message=mensaje)))
        self.assertEqual(self.llamadas, [])
        self.assertEqual(self.registro(), [])

    def test_el_nombre_de_plataforma_se_normaliza(self):
        auto = self.auto()
        self.assertIsNone(auto.pre_llm_call(**turno(platform="CRON")))
        self.assertIsNotNone(auto.pre_llm_call(**turno(platform=" Telegram ")))


class UnaConsultaPorTurnoTests(Base):
    """F3: si el turno ya tiene veredicto, no se vuelve a consultar."""

    def test_dos_invocaciones_del_gancho_en_el_mismo_turno_consultan_una(self):
        auto = self.auto()
        primera = auto.pre_llm_call(**turno())
        segunda = auto.pre_llm_call(**turno())
        self.assertEqual(primera, segunda)
        self.assertEqual(len(self.llamadas), 1)
        self.assertEqual(len(self.registro()), 1)

    def test_tambien_con_el_pie_ya_entregado(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        auto.transform_llm_output(
            session_id="s1",
            turn_id="t1",
            platform="telegram",
            response_text="listo",
        )
        de_nuevo = auto.pre_llm_call(**turno())
        self.assertIn("JEV skill_select: web-navigation", de_nuevo["context"])
        self.assertEqual(len(self.llamadas), 1)
        self.assertEqual(len(self.registro()), 1)

    def test_otro_turno_si_consulta_de_nuevo(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        auto.pre_llm_call(**turno(turn_id="t2"))
        self.assertEqual(len(self.llamadas), 2)

    def test_un_turno_fallido_tambien_cuenta_como_consultado(self):
        # El fail-open no convierte el turno en reintentos: el mismo motivo
        # vuelve sin otro subproceso ni otra linea de registro.
        auto = self.auto(corredor=self.corredor_fallo("vencio el tope de 8 s"))
        primera = auto.pre_llm_call(**turno())
        segunda = auto.pre_llm_call(**turno())
        self.assertEqual(primera, segunda)
        self.assertEqual(len(self.llamadas), 1)
        self.assertEqual(len(self.registro()), 1)


class VeredictosTests(Base):
    """Las tres lineas de contexto y el fail-open."""

    def test_router_sin_sugerencia_dice_ninguna(self):
        auto = self.auto(corredor=self.corredor_vacio)
        self.assertEqual(
            auto.pre_llm_call(**turno()),
            {"context": "JEV skill_select: ninguna"},
        )

    def test_router_lento_es_fail_open_en_el_tope_con_motivo(self):
        auto = self.auto(corredor=self.corredor_fallo("vencio el tope de 8 s"))
        self.assertEqual(
            auto.pre_llm_call(**turno()),
            {
                "context": (
                    "JEV skill_select no respondió (vencio el tope de 8 s): decide tú"
                )
            },
        )
        fila = self.registro()[0]
        self.assertFalse(fila["consultado"])
        self.assertEqual(fila["error"], "vencio el tope de 8 s")

    def test_router_con_error_es_fail_open_con_motivo_acotado(self):
        motivo = "backend HTTP 422: " + "x" * 300
        auto = self.auto(corredor=self.corredor_fallo(motivo))
        respuesta = auto.pre_llm_call(**turno())
        self.assertIn("no respondió", respuesta["context"])
        self.assertIn("422", respuesta["context"])
        self.assertLessEqual(len(respuesta["context"]), 160)

    def test_un_corredor_que_explota_no_rompe_el_turno(self):
        def corredor(texto, hogar):
            raise RuntimeError("kaboom interno")

        auto = self.auto(corredor=corredor)
        respuesta = auto.pre_llm_call(**turno())
        self.assertIn("JEV skill_select no respondió", respuesta["context"])
        self.assertIn("kaboom interno", respuesta["context"])

    def test_un_corredor_sin_veredicto_es_fallo(self):
        auto = self.auto(corredor=lambda texto, hogar: "basura")
        respuesta = auto.pre_llm_call(**turno())
        self.assertIn("no respondió", respuesta["context"])


class UmbralSkillFitTests(Base):
    """S2: fit >= UMBRAL_SKILL_FIT carga; debajo o sin fit, decide claw."""

    def veredicto(self, fit, con_fit=True):
        datos = {"estado": "ok", "skill": "apple-reminders", "motivo": None, "ms": 5}
        if con_fit:
            datos["fit"] = fit
        return datos

    def test_el_umbral_es_06(self):
        self.assertEqual(self.modulo.UMBRAL_SKILL_FIT, 0.6)

    def test_fit_en_y_sobre_el_umbral_dice_cargala_y_pie_limpio(self):
        for fit in (0.6, 0.87, 1.0):
            with self.subTest(fit=fit):
                self.assertEqual(
                    self.modulo._linea_contexto(self.veredicto(fit)),
                    "JEV skill_select: apple-reminders (cárgala)",
                )
                self.assertEqual(
                    self.modulo._pie_visible(self.veredicto(fit)),
                    "JEV: apple-reminders",
                )

    def test_fit_bajo_el_umbral_dice_posible_y_pie_dudosa(self):
        self.assertEqual(
            self.modulo._linea_contexto(self.veredicto(0.42)),
            "JEV skill_select: posible apple-reminders (fit 0.42): decide tú",
        )
        self.assertEqual(
            self.modulo._linea_contexto(self.veredicto(0.59)),
            "JEV skill_select: posible apple-reminders (fit 0.59): decide tú",
        )
        self.assertEqual(
            self.modulo._pie_visible(self.veredicto(0.42)),
            "JEV: apple-reminders? (dudosa)",
        )

    def test_sin_fit_dice_posible_y_pie_dudosa(self):
        for veredicto in (self.veredicto(None), self.veredicto(None, con_fit=False)):
            self.assertEqual(
                self.modulo._linea_contexto(veredicto),
                "JEV skill_select: posible apple-reminders (fit ?): decide tú",
            )
            self.assertEqual(
                self.modulo._pie_visible(veredicto),
                "JEV: apple-reminders? (dudosa)",
            )

    def test_vacio_y_fallo_no_cambian_con_el_umbral(self):
        vacio = {"estado": "vacio", "skill": None, "fit": None}
        fallo = {"estado": "fallo", "skill": None, "motivo": "caido", "fit": None}
        self.assertEqual(
            self.modulo._linea_contexto(vacio), "JEV skill_select: ninguna"
        )
        self.assertEqual(self.modulo._pie_visible(vacio), "JEV: ninguna")
        self.assertIn("no respondió", self.modulo._linea_contexto(fallo))
        self.assertEqual(self.modulo._pie_visible(fallo), "JEV: no respondió")

    def test_el_turno_con_fit_bajo_no_ordena_cargar_y_el_pie_es_dudosa(self):
        def corredor(texto, hogar):
            return {
                "estado": "ok",
                "skill": "apple-reminders",
                "motivo": None,
                "ms": 5,
                "gate": 0.8,
                "fit": 0.42,
            }

        auto = self.auto(corredor=corredor)
        self.assertEqual(
            auto.pre_llm_call(**turno()),
            {
                "context": (
                    "JEV skill_select: posible apple-reminders (fit 0.42): decide tú"
                )
            },
        )
        pie = auto.transform_llm_output(
            session_id="s1",
            turn_id="t1",
            platform="telegram",
            response_text="respuesta",
        )
        self.assertEqual(pie, "respuesta\n\nJEV: apple-reminders? (dudosa)")
        fila = self.registro()[0]
        self.assertEqual((fila["gate"], fila["fit"]), (0.8, 0.42))


class RegistroTests(Base):
    """Una linea JSON por consulta, sin texto del mensaje y sin llave."""

    def test_la_consulta_queda_registrada_con_su_forma_exacta(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        filas = self.registro()
        self.assertEqual(len(filas), 1)
        fila = filas[0]
        self.assertEqual(
            set(fila),
            {
                "at",
                "session_id",
                "turn_id",
                "platform",
                "consultado",
                "resultado",
                "gate",
                "fit",
                "error",
                "ms",
                "msg_len",
                "msg_sha256",
            },
        )
        self.assertEqual(fila["platform"], "telegram")
        self.assertTrue(fila["consultado"])
        self.assertEqual(fila["resultado"], "web-navigation")
        self.assertEqual(fila["gate"], 0.9)
        self.assertEqual(fila["fit"], 0.9)
        self.assertIsNone(fila["error"])
        self.assertEqual(fila["msg_len"], len(MENSAJE))
        self.assertIsInstance(fila["ms"], int)
        self.assertEqual(
            fila["msg_sha256"],
            hashlib.sha256(MENSAJE.encode("utf-8")).hexdigest()[:12],
        )
        self.assertEqual(len(fila["at"]), 25)  # ISO con fecha, hora y zona

    def test_el_registro_no_guarda_ni_el_mensaje_ni_la_llave(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        bruto = (self.hogar / "jev" / "consultas.jsonl").read_text()
        self.assertNotIn(MENSAJE, bruto)
        self.assertNotIn(LLAVE_ENV, bruto)
        self.assertNotIn(LLAVE_SCOPE, bruto)

    def test_ninguna_y_fallo_tambien_quedan(self):
        self.auto(corredor=self.corredor_vacio).pre_llm_call(**turno())
        self.auto(
            corredor=self.corredor_fallo("sin JEV_TYPESAFE_API_KEY")
        ).pre_llm_call(**turno(turn_id="t2"))
        filas = self.registro()
        self.assertEqual(filas[0]["resultado"], "ninguna")
        self.assertTrue(filas[0]["consultado"])
        self.assertIsNone(filas[1]["resultado"])
        self.assertEqual(filas[1]["error"], "sin JEV_TYPESAFE_API_KEY")

    def test_el_registro_crea_la_carpeta_jev(self):
        auto = self.auto()
        self.assertFalse((self.hogar / "jev").exists())
        auto.pre_llm_call(**turno())
        self.assertTrue((self.hogar / "jev" / "consultas.jsonl").is_file())

    def test_turnos_fuera_de_la_puerta_no_dejan_registro(self):
        auto = self.auto()
        auto.pre_llm_call(**turno(platform="cron"))
        auto.pre_llm_call(**turno(user_message=None))
        self.assertEqual(self.registro(), [])


class PieVisibleTests(Base):
    """El pie sale una sola vez por turno, solo donde hubo consulta."""

    def transformar(self, auto, respuesta="respuesta de claw", **overrides):
        datos = turno(**overrides)
        return auto.transform_llm_output(
            session_id=datos["session_id"],
            turn_id=datos["turn_id"],
            platform=datos["platform"],
            response_text=respuesta,
        )

    def test_el_pie_va_al_final_de_la_respuesta_de_ese_turno(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        self.assertEqual(
            self.transformar(auto),
            "respuesta de claw\n\nJEV: web-navigation",
        )

    def test_el_pie_sale_una_sola_vez_por_turno(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        self.assertIsNotNone(self.transformar(auto))
        self.assertIsNone(self.transformar(auto))
        self.assertIsNone(self.transformar(auto))

    def test_otro_turno_o_otra_sesion_no_llevan_pie(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        self.assertIsNone(self.transformar(auto, turn_id="otro"))
        auto.pre_llm_call(**turno(turn_id="t2"))
        self.assertIsNone(self.transformar(auto, turn_id="t2", session_id="s2"))

    def test_pie_para_ninguna_y_para_no_respondio(self):
        vacio = self.auto(corredor=self.corredor_vacio)
        vacio.pre_llm_call(**turno())
        self.assertEqual(self.transformar(vacio), "respuesta de claw\n\nJEV: ninguna")
        lento = self.auto(corredor=self.corredor_fallo("vencio el tope de 8 s"))
        lento.pre_llm_call(**turno(turn_id="t3"))
        self.assertEqual(
            self.transformar(lento, turn_id="t3"),
            "respuesta de claw\n\nJEV: no respondió",
        )

    def test_sin_consulta_previa_no_hay_pie(self):
        auto = self.auto()
        self.assertIsNone(self.transformar(auto))

    def test_respuesta_vacia_no_se_toca(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        self.assertIsNone(self.transformar(auto, respuesta=""))
        self.assertIsNone(self.transformar(auto, respuesta=None))

    def test_el_pie_no_se_duplica_en_una_respuesta_ya_pieada(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        pieada = "respuesta de claw\n\nJEV: web-navigation"
        self.assertIsNone(self.transformar(auto, respuesta=pieada))

    def test_el_pie_se_apaga_con_la_variable_y_la_consulta_sigue(self):
        auto = self.auto()
        with mock.patch.dict(os.environ, {"GONCLOUD_JEV_AUTO_VISIBLE": "0"}):
            self.assertIsNotNone(auto.pre_llm_call(**turno()))
            self.assertIsNone(self.transformar(auto))
        for valor in ("false", "no", "off", " 0 "):
            with mock.patch.dict(os.environ, {"GONCLOUD_JEV_AUTO_VISIBLE": valor}):
                otro = self.auto()
                otro.pre_llm_call(**turno(turn_id="t9"))
                self.assertIsNone(self.transformar(otro, turn_id="t9"), valor)

    def test_por_defecto_el_pie_esta_encendido(self):
        auto = self.auto()
        auto.pre_llm_call(**turno())
        self.assertIsNotNone(self.transformar(auto))

    def test_una_llave_de_config_del_ctx_gana_sobre_el_entorno(self):
        os.environ.pop("GONCLOUD_JEV_AUTO_VISIBLE", None)
        apagado = self.modulo.registrar(_CtxFalso(config={"jev_auto_visible": False}))
        apagado.decisor = None  # el decisor real jamas corre en pruebas
        apagado.hogar = self.hogar
        apagado.corredor = self.corredor_ok
        self.assertIsNotNone(apagado.pre_llm_call(**turno()))
        self.assertIsNone(self.transformar(apagado))
        encendido = self.modulo.registrar(_CtxFalso(config={"jev_auto_visible": True}))
        encendido.decisor = None  # el decisor real jamas corre en pruebas
        encendido.hogar = self.hogar
        encendido.corredor = self.corredor_ok
        with mock.patch.dict(os.environ, {"GONCLOUD_JEV_AUTO_VISIBLE": "0"}):
            encendido.pre_llm_call(**turno(turn_id="t5"))
            self.assertIsNotNone(self.transformar(encendido, turn_id="t5"))

    def test_la_llave_de_config_acepta_texto_apagador(self):
        # F4: "false"/"0"/"no"/"off" apagan igual que el parseo de la variable.
        for valor in ("false", "0", "no", "off", " Off ", False):
            with self.subTest(valor=valor):
                auto = self.modulo.registrar(
                    _CtxFalso(config={"jev_auto_visible": valor})
                )
                auto.decisor = None  # el decisor real jamas corre en pruebas
                auto.hogar = self.hogar
                auto.corredor = self.corredor_ok
                os.environ.pop("GONCLOUD_JEV_AUTO_VISIBLE", None)
                self.assertIsNotNone(auto.pre_llm_call(**turno(turn_id="t6")))
                self.assertIsNone(self.transformar(auto, turn_id="t6"), valor)

    def test_la_llave_de_config_acepta_texto_encendedor(self):
        for valor in ("true", "1", "si", "ON", True):
            with self.subTest(valor=valor):
                auto = self.modulo.registrar(
                    _CtxFalso(config={"jev_auto_visible": valor})
                )
                auto.decisor = None  # el decisor real jamas corre en pruebas
                auto.hogar = self.hogar
                auto.corredor = self.corredor_ok
                with mock.patch.dict(os.environ, {"GONCLOUD_JEV_AUTO_VISIBLE": "0"}):
                    auto.pre_llm_call(**turno(turn_id="t7"))
                    self.assertIsNotNone(self.transformar(auto, turn_id="t7"), valor)


class _CtxFalso:
    def __init__(self, perfil="claw", config=None):
        self.profile_name = perfil
        self.plugin_config = config or {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        pass

    def register_hook(self, nombre, funcion):
        self.hooks[nombre] = funcion


class CorredorRealTests(Base):
    """La via del router: su funcion suggest en un subproceso via uv."""

    def setUp(self):
        super().setUp()
        self.raiz_router = self.hogar / "plugins" / "jev-skill-router-mcp"
        self.raiz_router.mkdir(parents=True)
        (self.raiz_router / "pyproject.toml").write_text(
            "[project]\n", encoding="utf-8"
        )

    class PopenFalso:
        def __init__(self, args=None, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.pid = 424242
            self.returncode = 0
            self.salida = (
                json.dumps(
                    {"suggested": "web-navigation", "decision": "suggest"}
                ).encode("utf-8"),
                b"",
            )
            self.timeout_recibido = None
            self.entrada = None
            self.muerto = False

        def communicate(self, input=None, timeout=None):
            self.entrada = input
            self.timeout_recibido = timeout
            return self.salida

        def kill(self):
            self.muerto = True

        def wait(self, timeout=None):
            return 0

    def correr(self, texto=MENSAJE, popen=None, **kwargs):
        clase = popen or self.PopenFalso
        hechos = []

        def fabrica(*a, **k):
            proceso = clase(*a, **k)
            hechos.append(proceso)
            return proceso

        with mock.patch.object(self.modulo, "_popen", fabrica):
            veredicto = self.modulo._ejecutar_router(texto, self.hogar, **kwargs)
        self._ultimo = hechos[-1] if hechos else None
        return veredicto

    def ultimo_proceso(self):
        return self._ultimo

    def test_llama_a_suggest_por_uv_con_el_plugin_instalado(self):
        with mock.patch("shutil.which", return_value="/falso/uv"):
            veredicto = self.correr()
        self.assertEqual(veredicto["estado"], "ok")
        self.assertEqual(veredicto["skill"], "web-navigation")
        proceso = self.ultimo_proceso()
        comando = proceso.args
        self.assertEqual(comando[0], "/falso/uv")
        self.assertIn("--project", comando)
        self.assertEqual(comando[comando.index("--project") + 1], str(self.raiz_router))
        self.assertIn("-c", comando)
        env = proceso.kwargs["env"]
        self.assertEqual(env["JEV_TYPESAFE_API_KEY"], LLAVE_ENV)
        self.assertEqual(env["HERMES_HOME"], str(self.hogar))
        self.assertEqual(env["JEV_SKILLS_ROOT"], str(self.hogar / "skills"))
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(proceso.kwargs["cwd"], str(self.raiz_router))
        self.assertEqual(proceso.timeout_recibido, 8.0)

    def test_el_texto_viaja_por_stdin_y_jamas_por_argv(self):
        secreto = "pedido privado con dato reservado"
        self.correr(secreto)
        proceso = self.ultimo_proceso()
        self.assertEqual(proceso.entrada, secreto.encode("utf-8"))
        self.assertNotIn(secreto, " ".join(proceso.args))
        self.assertNotIn("reservado", " ".join(proceso.args))

    def test_sin_uv_cae_al_python_de_proceso_con_src_en_path(self):
        with mock.patch("shutil.which", return_value=None):
            veredicto = self.correr()
        self.assertEqual(veredicto["estado"], "ok")
        proceso = self.ultimo_proceso()
        self.assertEqual(proceso.args[0], sys.executable)
        self.assertIn(
            str(self.raiz_router / "src"), proceso.kwargs["env"]["PYTHONPATH"]
        )

    def test_router_no_instalado_es_fallo_sin_subproceso(self):
        shutil.rmtree(self.raiz_router)
        construidos = []

        class PopenQueNoDebeArrancar(self.PopenFalso):
            def __init__(self, args, **kwargs):
                construidos.append(args)
                super().__init__(args, **kwargs)

        veredicto = self.correr(popen=PopenQueNoDebeArrancar)
        self.assertEqual(veredicto["estado"], "fallo")
        self.assertEqual(veredicto["motivo"], "router no instalado")
        self.assertEqual(construidos, [])

    def test_la_llave_sale_del_entorno_del_padre_y_no_lo_muta(self):
        antes = dict(os.environ)
        self.correr()
        self.assertEqual(dict(os.environ), antes)
        env = self.ultimo_proceso().kwargs["env"]
        self.assertEqual(env["JEV_TYPESAFE_API_KEY"], LLAVE_ENV)

    def test_sin_llave_y_sin_adapter_es_fallo_sin_subproceso(self):
        class PopenQueNoDebeArrancar(self.PopenFalso):
            def __init__(self, args, **kwargs):
                raise AssertionError("no debe haber subproceso sin llave")

        limpio = {"JEV_TYPESAFE_API_KEY": "", "JEV_SKILL_ADAPTER_URL": ""}
        with mock.patch.dict(os.environ, limpio):
            os.environ.pop("JEV_SKILL_ADAPTER_URL", None)
            veredicto = self.correr(popen=PopenQueNoDebeArrancar)
        self.assertEqual(veredicto["estado"], "fallo")
        self.assertEqual(veredicto["motivo"], "sin JEV_TYPESAFE_API_KEY")

    def test_en_modo_adapter_la_llave_no_es_obligatoria(self):
        limpio = {
            "JEV_TYPESAFE_API_KEY": "",
            "JEV_SKILL_ADAPTER_URL": "http://lan",
            "JEV_SKILL_ADAPTER_TOKEN": "tok-adapter",
        }
        with mock.patch.dict(os.environ, limpio):
            veredicto = self.correr()
        self.assertEqual(veredicto["estado"], "ok")
        env = self.ultimo_proceso().kwargs["env"]
        self.assertNotIn("JEV_TYPESAFE_API_KEY", env)

    def test_adapter_sin_token_es_fallo_sin_subproceso(self):
        class PopenQueNoDebeArrancar(self.PopenFalso):
            def __init__(self, args, **kwargs):
                raise AssertionError("adapter sin token no debe arrancar")

        medio = {
            "JEV_TYPESAFE_API_KEY": "",
            "JEV_SKILL_ADAPTER_URL": "http://lan",
            "JEV_SKILL_ADAPTER_TOKEN": "",
        }
        with mock.patch.dict(os.environ, medio):
            os.environ.pop("JEV_SKILL_ADAPTER_TOKEN", None)
            veredicto = self.correr(popen=PopenQueNoDebeArrancar)
        self.assertEqual(veredicto["estado"], "fallo")
        self.assertEqual(veredicto["motivo"], "sin JEV_SKILL_ADAPTER_TOKEN")

    def test_produccion_sin_hogar_resuelve_el_home_al_hijo(self):
        # La via viva de registrar(): JevAuto sin hogar y corredor real; el
        # home RESUELTO (no el crudo) debe llegar como HERMES_HOME y
        # JEV_SKILLS_ROOT al entorno del hijo, aunque el gateway no lo pase.
        hechos = []

        def fabrica(*a, **k):
            proceso = self.PopenFalso(*a, **k)
            hechos.append(proceso)
            return proceso

        with mock.patch.dict(sys.modules, {"hermes_constants": None}):
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hogar)}):
                auto = self.modulo.registrar(_CtxFalso())
                with mock.patch.object(self.modulo, "_popen", fabrica):
                    veredicto = auto.consultar(MENSAJE)
        self.assertEqual(veredicto["estado"], "ok")
        self.assertTrue(hechos)
        self._ultimo = hechos[-1]
        proceso = self.ultimo_proceso()
        env = proceso.kwargs["env"]
        self.assertEqual(env["HERMES_HOME"], str(self.hogar))
        self.assertEqual(env["JEV_SKILLS_ROOT"], str(self.hogar / "skills"))
        self.assertEqual(proceso.kwargs["cwd"], str(self.raiz_router))

    def test_el_mensaje_sale_redactado_al_hijo(self):
        secreto = "sk-live-abcdefghijklmnop1234567890"
        texto = f"configura el panel con la llave {secreto} y sigue"
        self.correr(texto)
        proceso = self.ultimo_proceso()
        entrada = proceso.entrada.decode("utf-8")
        self.assertNotIn(secreto, entrada)
        self.assertIn("[redacted]", entrada)
        self.assertNotIn(secreto, " ".join(proceso.args))

    def test_la_llave_del_entorno_tampoco_sale_en_el_stdin(self):
        texto = f"mi llave es {LLAVE_ENV}, sigue con el panel"
        self.correr(texto)
        self.assertNotIn(LLAVE_ENV, self.ultimo_proceso().entrada.decode("utf-8"))

    def test_el_mensaje_largo_queda_acotado(self):
        self.correr("x" * 6000)
        self.assertLessEqual(len(self.ultimo_proceso().entrada), 4000)
        self.correr("x" * 3999)
        self.assertEqual(len(self.ultimo_proceso().entrada), 3999)

    def test_el_secreto_del_scope_sale_redactado_sin_forma_de_llave(self):
        # F7: en el gateway multiplexado la llave vive en agent.secret_scope,
        # no en os.environ; redact_text necesita el valor resuelto explicito.
        # El secreto no tiene forma de llave conocida: sin el valor, el filtro
        # de formas no lo ve.
        secreto = "mjs-privado-scope-9931"
        with _agente_fake(get_secret=lambda nombre, defecto="": secreto):
            with mock.patch.dict(os.environ, {"JEV_TYPESAFE_API_KEY": ""}):
                self.correr(f"usa el token interno {secreto} del panel")
        entrada = self.ultimo_proceso().entrada.decode("utf-8")
        self.assertNotIn(secreto, entrada)
        self.assertIn("[redacted]", entrada)

    def test_todos_los_secretos_del_scope_del_perfil_se_tachan(self):
        # F7 completo (Hermes 0.22): el perfil entero guarda sus llaves en
        # current_secret_scope(), no solo la de TypeSafe; una credencial de
        # otra llave pegada en el chat tampoco sale a TypeSafe.
        secreto = "valor-sin-forma-123"
        with _agente_fake(
            get_secret=lambda nombre, defecto="": LLAVE_SCOPE,
            current_secret_scope=lambda: {"OTRA_LLAVE": secreto},
        ):
            with mock.patch.dict(os.environ, {"JEV_TYPESAFE_API_KEY": ""}):
                self.correr(f"la entrada del panel va con {secreto} y sigue")
        entrada = self.ultimo_proceso().entrada.decode("utf-8")
        self.assertNotIn(secreto, entrada)
        self.assertIn("[redacted]", entrada)
        self.assertNotIn(LLAVE_SCOPE, entrada)

    def test_el_tope_mata_el_arbol_y_da_el_motivo(self):
        class PopenLento(self.PopenFalso):
            def communicate(self, input=None, timeout=None):
                self.timeout_recibido = timeout
                raise subprocess.TimeoutExpired(cmd="uv", timeout=timeout)

        veredicto = self.correr(popen=PopenLento)
        self.assertEqual(veredicto["estado"], "fallo")
        self.assertEqual(veredicto["motivo"], "vencio el tope de 8 s")
        self.assertIsInstance(veredicto["ms"], int)
        self.assertTrue(self.ultimo_proceso().muerto)

    def test_salida_con_error_va_acotada_al_motivo(self):
        roto = self.PopenFalso()
        roto.returncode = 1
        roto.salida = (b"", ("backend HTTP 422: " + "y" * 300).encode("utf-8"))
        veredicto = self.correr(popen=lambda *a, **k: roto)
        self.assertEqual(veredicto["estado"], "fallo")
        self.assertIn("422", veredicto["motivo"])
        self.assertLessEqual(len(veredicto["motivo"]), 120)

    def test_respuesta_no_json_es_fallo(self):
        raro = self.PopenFalso()
        raro.salida = (b"no-soy-json", b"")
        veredicto = self.correr(popen=lambda *a, **k: raro)
        self.assertEqual(veredicto["motivo"], "respuesta no JSON")

    def test_el_error_del_hijo_es_el_motivo(self):
        hijo = self.PopenFalso()
        hijo.salida = (json.dumps({"error": "sin roster"}).encode("utf-8"), b"")
        veredicto = self.correr(popen=lambda *a, **k: hijo)
        self.assertEqual(veredicto["motivo"], "sin roster")

    def test_abstencion_del_router_es_vacio(self):
        hijo = self.PopenFalso()
        hijo.salida = (
            json.dumps({"suggested": None, "decision": "abstain"}).encode("utf-8"),
            b"",
        )
        veredicto = self.correr(popen=lambda *a, **k: hijo)
        self.assertEqual(veredicto["estado"], "vacio")

    def test_gate_y_fit_del_hijo_viajan_en_el_veredicto(self):
        hijo = self.PopenFalso()
        hijo.salida = (
            json.dumps(
                {
                    "suggested": "web-navigation",
                    "decision": "suggest",
                    "gate": 0.91,
                    "fit": 0.42,
                }
            ).encode("utf-8"),
            b"",
        )
        veredicto = self.correr(popen=lambda *a, **k: hijo)
        self.assertEqual(veredicto["estado"], "ok")
        self.assertEqual((veredicto["gate"], veredicto["fit"]), (0.91, 0.42))

    def test_abstencion_conserva_gate_y_fit_queda_none(self):
        hijo = self.PopenFalso()
        hijo.salida = (
            json.dumps(
                {"suggested": None, "decision": "abstain", "gate": 0.12, "fit": None}
            ).encode("utf-8"),
            b"",
        )
        veredicto = self.correr(popen=lambda *a, **k: hijo)
        self.assertEqual(veredicto["estado"], "vacio")
        self.assertEqual((veredicto["gate"], veredicto["fit"]), (0.12, None))

    def test_gate_y_fit_no_numericos_quedan_none(self):
        hijo = self.PopenFalso()
        hijo.salida = (
            json.dumps(
                {
                    "suggested": "web-navigation",
                    "decision": "suggest",
                    "gate": "alto",
                    "fit": True,
                }
            ).encode("utf-8"),
            b"",
        )
        veredicto = self.correr(popen=lambda *a, **k: hijo)
        self.assertEqual((veredicto["gate"], veredicto["fit"]), (None, None))

    def test_suggest_sin_nombre_es_vacio(self):
        hijo = self.PopenFalso()
        hijo.salida = (
            json.dumps({"suggested": None, "decision": "suggest"}).encode("utf-8"),
            b"",
        )
        veredicto = self.correr(popen=lambda *a, **k: hijo)
        self.assertEqual(veredicto["estado"], "vacio")


class HijoRealTests(Base):
    """F6: el programa _HIJO de verdad, sin dobles en el subproceso."""

    ROUTER_OK = (
        "def load_roster():\n"
        "    return [{'name': 'web-navigation', 'description': 'd',"
        " 'path': 'x', 'body': 'b'}]\n"
        "\n"
        "def suggest(request, roster):\n"
        "    return {'suggested': roster[0]['name'], 'decision': 'suggest',"
        " 'gate': 0.9, 'fits': {'web-navigation': 0.87, 'otra': 0.1}}\n"
    )

    def setUp(self):
        super().setUp()
        paquete = self.hogar / "paquete" / "jev_skill_router"
        paquete.mkdir(parents=True)
        (paquete / "__init__.py").write_text("", encoding="utf-8")
        self.paquete = paquete
        self.raiz_router = self.hogar / "plugins" / "jev-skill-router-mcp"
        self.raiz_router.mkdir(parents=True, exist_ok=True)
        (self.raiz_router / "pyproject.toml").write_text(
            "[project]\n", encoding="utf-8"
        )

    def escribir_router(self, cuerpo):
        (self.paquete / "mcp_server.py").write_text(cuerpo, encoding="utf-8")

    def con_respaldo(self):
        """La rama sin uv de _comando_y_ambiente: sys.executable y el
        paquete de mentira viajando en PYTHONPATH del entorno del hijo."""
        contexto = mock.patch.dict(os.environ, {"PYTHONPATH": str(self.paquete.parent)})
        contexto.start()
        self.addCleanup(contexto.stop)
        return mock.patch("shutil.which", return_value=None)

    def test_el_hijo_real_responde_y_el_padre_lee_el_json(self):
        self.escribir_router(self.ROUTER_OK)
        with self.con_respaldo():
            veredicto = self.modulo._ejecutar_router(MENSAJE, self.hogar)
        self.assertEqual(veredicto["estado"], "ok")
        self.assertEqual(veredicto["skill"], "web-navigation")
        self.assertEqual(veredicto["motivo"], None)
        # S1: gate tal cual y fit = fits[sugerida], no el de otra skill.
        self.assertEqual((veredicto["gate"], veredicto["fit"]), (0.9, 0.87))

    ROUTER_CON_CONFIG = (
        "CFG = {'deny': ['web-navigation'], 'gate': 0.33}\n"
        "\n"
        "def load_roster():\n"
        "    return [{'name': 'web-navigation'}, {'name': 'otra'}]\n"
        "\n"
        "def load_config():\n"
        "    return CFG\n"
        "\n"
        "def filter_roster(roster, cfg):\n"
        "    return [r for r in roster if r['name'] not in cfg['deny']]\n"
        "\n"
        "def suggest(request, roster, cfg=None):\n"
        "    if cfg is None:\n"
        "        return {'suggested': roster[0]['name'], 'decision': 'suggest',"
        " 'gate': 0.5}\n"
        "    return {'suggested': roster[0]['name'], 'decision': 'suggest',"
        " 'gate': cfg['gate'], 'fits': {roster[0]['name']: 0.9}}\n"
    )

    def test_el_hijo_aplica_config_y_filtro_del_router(self):
        # Hueco B: allow/deny y jev-skill-router.json valen en la ruta
        # automatica; sin ellos sugeriria la skill vetada.
        self.escribir_router(self.ROUTER_CON_CONFIG)
        with self.con_respaldo():
            veredicto = self.modulo._ejecutar_router(MENSAJE, self.hogar)
        self.assertEqual(veredicto["estado"], "ok")
        self.assertEqual(veredicto["skill"], "otra")
        self.assertEqual((veredicto["gate"], veredicto["fit"]), (0.33, 0.9))

    def test_plugin_viejo_sin_config_ni_filtro_cae_a_la_llamada_de_siempre(self):
        for faltante in ("load_config", "filter_roster"):
            with self.subTest(sin=faltante):
                self.escribir_router(self.ROUTER_CON_CONFIG + f"del {faltante}\n")
                with self.con_respaldo():
                    veredicto = self.modulo._ejecutar_router(MENSAJE, self.hogar)
                self.assertEqual(veredicto["estado"], "ok")
                self.assertEqual(veredicto["skill"], "web-navigation")
                self.assertEqual(veredicto["gate"], 0.5)

    def test_el_hijo_sin_fits_deja_fit_none_y_gate_pasa(self):
        self.escribir_router(
            "def load_roster():\n"
            "    return [{'name': 'web-navigation'}]\n"
            "\n"
            "def suggest(request, roster):\n"
            "    return {'suggested': 'web-navigation', 'decision': 'suggest',"
            " 'gate': 0.7}\n"
        )
        with self.con_respaldo():
            veredicto = self.modulo._ejecutar_router(MENSAJE, self.hogar)
        self.assertEqual(veredicto["estado"], "ok")
        self.assertEqual((veredicto["gate"], veredicto["fit"]), (0.7, None))

    def test_el_hijo_real_abstiene_y_el_padre_lo_entiende(self):
        self.escribir_router(
            "def load_roster():\n"
            "    return []\n"
            "\n"
            "def suggest(request, roster):\n"
            "    return {'suggested': None, 'decision': 'abstain'}\n"
        )
        with self.con_respaldo():
            veredicto = self.modulo._ejecutar_router(MENSAJE, self.hogar)
        self.assertEqual(veredicto["estado"], "vacio")

    def test_claves_inesperadas_del_hijo_son_vacio(self):
        self.escribir_router(
            "def load_roster():\n"
            "    return []\n"
            "\n"
            "def suggest(request, roster):\n"
            "    return {'foo': 'bar'}\n"
        )
        with self.con_respaldo():
            veredicto = self.modulo._ejecutar_router(MENSAJE, self.hogar)
        self.assertEqual(veredicto["estado"], "vacio")

    def test_suggest_que_explota_es_fail_open_sin_excepcion(self):
        self.escribir_router(
            "def load_roster():\n"
            "    return []\n"
            "\n"
            "def suggest(request, roster):\n"
            "    raise RuntimeError('backend HTTP 500: kaboom')\n"
        )
        auto = self.modulo.JevAuto("claw", hogar=self.hogar)  # corredor real
        with self.con_respaldo():
            respuesta = auto.pre_llm_call(**turno())
        self.assertIn("JEV skill_select no respondió", respuesta["context"])
        self.assertIn("kaboom", respuesta["context"])
        self.assertEqual(len(self.registro()), 1)
        self.assertFalse(self.registro()[0]["consultado"])


class LlaveTests(Base):
    """JEV_TYPESAFE_API_KEY: secret_scope primero, entorno de respaldo."""

    def test_secret_scope_gana_sobre_el_entorno(self):
        with _agente_fake(get_secret=lambda nombre, defecto="": LLAVE_SCOPE):
            self.assertEqual(self.modulo._clave_typesafe(), LLAVE_SCOPE)

    def test_sin_secret_scope_usa_el_entorno(self):
        self.assertEqual(self.modulo._clave_typesafe(), LLAVE_ENV)

    def test_un_secret_scope_vacio_cae_al_entorno(self):
        with _agente_fake(get_secret=lambda nombre, defecto="": defecto):
            self.assertEqual(self.modulo._clave_typesafe(), LLAVE_ENV)

    def test_sin_llave_en_ninguna_parte_devuelve_vacio(self):
        with mock.patch.dict(os.environ, {"JEV_TYPESAFE_API_KEY": ""}):
            with _agente_fake(get_secret=lambda nombre, defecto="": defecto):
                self.assertEqual(self.modulo._clave_typesafe(), "")


class SecretosDelScopeTests(Base):
    """F7 completo: los values() de current_secret_scope() para tachar."""

    def test_sin_agente_no_hay_secretos(self):
        self.assertEqual(self.modulo._secretos_del_scope(), ())

    def test_scope_ausente_es_vacio(self):
        with _agente_fake(
            get_secret=lambda nombre, defecto="": LLAVE_SCOPE,
            current_secret_scope=lambda: None,
        ):
            self.assertEqual(self.modulo._secretos_del_scope(), ())

    def test_scope_con_valores_los_devuelve(self):
        alcance = {"OTRA_LLAVE": "valor-sin-forma-123", "VACIA": ""}
        with _agente_fake(
            get_secret=lambda nombre, defecto="": LLAVE_SCOPE,
            current_secret_scope=lambda: alcance,
        ):
            self.assertEqual(
                self.modulo._secretos_del_scope(), ("valor-sin-forma-123",)
            )

    def test_un_scope_roto_es_vacio_y_no_explota(self):
        class Roto:
            pass  # sin .values()

        with _agente_fake(
            get_secret=lambda nombre, defecto="": LLAVE_SCOPE,
            current_secret_scope=Roto,
        ):
            self.assertEqual(self.modulo._secretos_del_scope(), ())


class _agente_fake:
    """Instala un agent.secret_scope de mentira y lo saca al salir."""

    def __init__(self, get_secret, current_secret_scope=None):
        self.modulo_agente = types.ModuleType("agent")
        self.modulo_alcance = types.ModuleType("agent.secret_scope")
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


class HogarTests(Base):
    """El home del perfil: constants, entorno, ruta del plugin, ~/.hermes."""

    def cargar_copia(self, destino: Path, nombre: str):
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLUGIN / "jev_auto.py", destino)
        spec = importlib.util.spec_from_file_location(nombre, destino)
        copia = importlib.util.module_from_spec(spec)
        sys.modules[nombre] = copia
        spec.loader.exec_module(copia)
        self.addCleanup(sys.modules.pop, nombre, None)
        return copia

    def test_hermes_constants_gana(self):
        falso = types.ModuleType("hermes_constants")
        falso.get_hermes_home = lambda: str(self.hogar)
        with mock.patch.dict(sys.modules, {"hermes_constants": falso}):
            self.assertEqual(self.modulo._hogar_perfil(), self.hogar)

    def test_sin_constants_usa_hermes_home_del_entorno(self):
        with mock.patch.dict(sys.modules, {"hermes_constants": None}):
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hogar)}):
                self.assertEqual(self.modulo._hogar_perfil(), self.hogar)

    def test_la_ruta_del_plugin_deriva_el_perfil_del_despliegue(self):
        # El layout vivo de la PC: <perfil>/plugins/goncloud-team/jev_auto.py
        # sube dos niveles al perfil, como el _skills_root() del router.
        copia = self.cargar_copia(
            self.hogar / "perfil" / "plugins" / "goncloud-team" / "jev_auto.py",
            "jev_auto_derivacion",
        )
        with mock.patch.dict(sys.modules, {"hermes_constants": None}):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HERMES_HOME", None)
                self.assertEqual(copia._hogar_perfil(), self.hogar.resolve() / "perfil")

    def test_sin_layout_ni_entorno_cae_en_el_hermes_de_usuario(self):
        copia = self.cargar_copia(
            self.hogar / "suelto" / "jev_auto.py", "jev_auto_suelto"
        )
        with mock.patch.dict(sys.modules, {"hermes_constants": None}):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HERMES_HOME", None)
                self.assertEqual(copia._hogar_perfil(), Path.home() / ".hermes")


def _cargar_medir_skills():
    ruta = RAIZ / "scripts" / "jev_medir_skills.py"
    spec = importlib.util.spec_from_file_location("jev_medir_skills", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class MedirSkillsTests(unittest.TestCase):
    """S3: el script de medicion corre con un runner doble, sin red ni llave."""

    def test_el_fixture_trae_los_34_casos_etiquetados(self):
        medir_mod = _cargar_medir_skills()
        casos = medir_mod.cargar_casos(
            RAIZ / "tests/fixtures/jev_skills_etiquetado.jsonl"
        )
        self.assertEqual(len(casos), 34)
        por_mensaje = {c["mensaje"]: c["esperado"] for c in casos}
        self.assertEqual(por_mensaje["hay personalizadas por hacer hoy?"], "ninguna")
        self.assertEqual(por_mensaje["llegó una personalizada"], "ninguna")
        self.assertEqual(
            por_mensaje["buenos días, qué tenemos pendiente hoy"], "ninguna"
        )
        self.assertEqual(
            por_mensaje["recuérdame mañana a las 9 pagar la luz"], "apple-reminders"
        )

    def test_medir_y_la_tabla_por_umbral_con_runner_doble(self):
        medir_mod = _cargar_medir_skills()
        respuestas = {
            "pon un recordatorio": {
                "estado": "ok",
                "skill": "apple-reminders",
                "gate": 0.9,
                "fit": 0.8,
            },
            "hay personalizadas por hacer hoy?": {
                "estado": "ok",
                "skill": "apple-reminders",
                "gate": 0.7,
                "fit": 0.42,
            },
            "saluda": {"estado": "vacio", "skill": None, "gate": 0.2, "fit": None},
            "revisa el correo": {
                "estado": "ok",
                "skill": "email-inbox-triage",
                "gate": 0.9,
                "fit": 0.95,
            },
        }
        casos = [
            {"mensaje": "pon un recordatorio", "esperado": "apple-reminders"},
            {
                "mensaje": "hay personalizadas por hacer hoy?",
                "esperado": "ninguna",
            },
            {"mensaje": "saluda", "esperado": "ninguna"},
            {"mensaje": "revisa el correo", "esperado": "odoo"},
        ]
        resultado = medir_mod.medir(lambda mensaje: respuestas[mensaje], casos)
        self.assertEqual(resultado["total"], 4)
        self.assertEqual(resultado["aciertos"], 2)
        filas = {f["mensaje"]: f for f in resultado["filas"]}
        baja = filas["hay personalizadas por hacer hoy?"]
        self.assertEqual(
            (baja["suggested"], baja["gate"], baja["fit"], baja["acierto"]),
            ("apple-reminders", 0.7, 0.42, False),
        )
        self.assertIsNone(filas["saluda"]["suggested"])
        self.assertTrue(filas["saluda"]["acierto"])
        tabla = {f["umbral"]: f for f in resultado["tabla"]}
        self.assertEqual(sorted(tabla), [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
        # Con fit hay 3: 1 buena (0.8) y 2 malas (0.42 y 0.95 contra odoo).
        self.assertEqual(
            (tabla[0.3]["cargala_acertadas"], tabla[0.3]["cargala_equivocadas"]),
            (1, 2),
        )
        self.assertEqual(
            (tabla[0.6]["cargala_acertadas"], tabla[0.6]["cargala_equivocadas"]),
            (1, 1),
        )
        self.assertEqual(
            (tabla[0.9]["cargala_acertadas"], tabla[0.9]["cargala_equivocadas"]),
            (0, 1),
        )
        texto = medir_mod.formatear(resultado)
        self.assertIn("suggested=apple-reminders gate=0.70 fit=0.42", texto)
        self.assertIn("aciertos: 2/4", texto)
        self.assertIn("0.6 | 1 | 1", texto)

    def test_medir_con_un_solo_umbral_y_runner_que_falla_no_explota(self):
        medir_mod = _cargar_medir_skills()

        def corredor(mensaje):
            raise RuntimeError("sin router")

        resultado = medir_mod.medir(
            corredor,
            [{"mensaje": "algo largo y concreto", "esperado": "ninguna"}],
            umbrales=(0.6,),
        )
        self.assertEqual([f["umbral"] for f in resultado["tabla"]], [0.6])
        self.assertFalse(resultado["filas"][0]["acierto"])
        self.assertEqual(resultado["aciertos"], 0)
        self.assertIn("error: sin router", medir_mod.formatear(resultado))


class CableadoTests(unittest.TestCase):
    """El plugin registra los ganchos como registra el que ya tenia."""

    @classmethod
    def setUpClass(cls):
        hermes_falso.instalar()
        if str(PLUGIN) not in sys.path:
            sys.path.insert(0, str(PLUGIN))
        spec = importlib.util.spec_from_file_location(
            "goncloud_team_jev_cableado",
            PLUGIN / "__init__.py",
            submodule_search_locations=[str(PLUGIN)],
        )
        cls.plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.plugin
        spec.loader.exec_module(cls.plugin)

    @classmethod
    def tearDownClass(cls):
        hermes_falso.desinstalar()

    def test_register_deja_los_tres_ganchos(self):
        ctx = _CtxFalso()
        self.plugin.register(ctx)
        self.assertEqual(
            sorted(ctx.hooks),
            [
                "post_llm_call",
                "post_tool_call",
                "pre_llm_call",
                "pre_tool_call",
                "transform_llm_output",
            ],
        )

    def test_el_plugin_yaml_declara_los_ganchos(self):
        manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
        for gancho in (
            "pre_tool_call",
            "pre_llm_call",
            "transform_llm_output",
        ):
            self.assertIn(gancho, manifest["provides_hooks"])

    def test_los_ganchos_cableados_respetan_la_puerta(self):
        ctx = _CtxFalso()
        self.plugin.register(ctx)
        pre = ctx.hooks["pre_llm_call"]
        auto = pre.__self__
        auto.decisor = None  # el ruteo real jamas corre en estas pruebas
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        auto.hogar = Path(temporal.name)
        auto.corredor = lambda texto, hogar: {
            "estado": "ok",
            "skill": "web-navigation",
            "motivo": None,
            "ms": 5,
            "gate": 0.9,
            "fit": 0.9,
        }
        self.assertIsNotNone(pre(**turno()))
        self.assertIsNone(pre(**turno(platform="cron")))
        scout = _CtxFalso(perfil="scout")
        self.plugin.register(scout)
        scout.hooks["pre_llm_call"].__self__.decisor = None  # JEV real jamas en pruebas
        self.assertIsNone(scout.hooks["pre_llm_call"](**turno()))

    def test_los_cinco_ganchos_de_jev_quedan_alcanzables_y_el_de_bucle_corre(self):
        # Hermes admite varias funciones por gancho: con un ctx que las
        # guarda todas, JEV queda alcanzable en los cinco y la guarda no
        # sombrea al detector de bucle en pre_tool_call.
        class CtxMulti(_CtxFalso):
            def register_hook(self, nombre, funcion):
                self.hooks.setdefault(nombre, []).append(funcion)

        ctx = CtxMulti()
        self.plugin.register(ctx)
        jev = [
            funcion
            for funciones in ctx.hooks.values()
            for funcion in funciones
            if type(getattr(funcion, "__self__", None)).__name__ == "JevAuto"
        ]
        self.assertEqual(
            sorted(funcion.__name__ for funcion in jev),
            [
                "post_llm_call",
                "post_tool_call",
                "pre_llm_call",
                "pre_tool_call",
                "transform_llm_output",
            ],
        )
        self.assertEqual(len(ctx.hooks["pre_tool_call"]), 2)  # JEV + guarda
        auto = jev[0].__self__
        llamadas = []

        class DecisorFalso:
            def resuelva(self):
                return "", ()

            def bucle(self, ficha, entrada, clave=None, secretos=None):
                llamadas.append(entrada)

                class Decision:
                    error = None
                    opcion = "bucle"
                    confianza = 0.95

                return Decision()

        auto.decisor = DecisorFalso()
        detector = next(
            f
            for f in ctx.hooks["pre_tool_call"]
            if getattr(f, "__self__", None) is auto
        )
        detector(tool_name="read_file", args={}, session_id="s1")
        detector(tool_name="read_file", args={}, session_id="s1")
        for hilo in list(auto.hilos):
            hilo.join(5)
        self.assertEqual(len(llamadas), 1)
        self.assertTrue(any("Jev advierte" in a for a in auto._avisos.get("s1", [])))

    def test_la_consulta_registrada_deja_registro_y_pie(self):
        ctx = _CtxFalso()
        self.plugin.register(ctx)
        auto = ctx.hooks["pre_llm_call"].__self__
        auto.decisor = None  # el ruteo real jamas corre en estas pruebas
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        auto.hogar = Path(temporal.name)
        auto.corredor = lambda texto, hogar: {
            "estado": "ok",
            "skill": "web-navigation",
            "motivo": None,
            "ms": 5,
            "gate": 0.9,
            "fit": 0.9,
        }
        auto.pre_llm_call(**turno())
        pie = ctx.hooks["transform_llm_output"](
            session_id="s1",
            turn_id="t1",
            platform="telegram",
            response_text="listo",
        )
        self.assertEqual(pie, "listo\n\nJEV: web-navigation")
        filas = [
            json.loads(linea)
            for linea in (auto.hogar / "jev" / "consultas.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(filas[0]["resultado"], "web-navigation")
        self.assertNotIn(MENSAJE, json.dumps(filas))


if __name__ == "__main__":
    unittest.main()
