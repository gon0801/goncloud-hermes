"""JEV tanda 2 en Hermes (David 2026-10-19): todos los perfiles, bucle, texto
limpio, correcciones y ahorro.

Criterios: clasificacion tipo/modo en un perfil que no es claw; el detector de
bucle jamas bloquea ni espera a JEV; cleanPrompt con un mensaje real de
Telegram envuelto; una correccion guardada y luego usada como ejemplo; los
textos cortos no gastan consulta; JEV caido no bloquea ningun turno.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_apoyo  # noqa: E402,F401
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

import jev_auto  # noqa: E402
import jev_decisiones  # noqa: E402
import jev_texto  # noqa: E402
import local_orchestrator  # noqa: E402

ENCARGO = "revisa las ordenes flex para hoy y las recolecciones"

TELEGRAM_ENVUELTO = (
    '[Replying to: "¿ya quedó lo de las guías?"]\n'
    "[Gon] revisa las ordenes flex para hoy y las recolecciones\n"
    "JEV skill_select: ninguna\n"
    "Jev sugiere: amazon (0.91)\n"
)


def turno(**cambios):
    base = {
        "session_id": "s1",
        "turn_id": "t1",
        "platform": "telegram",
        "user_message": ENCARGO,
    }
    base.update(cambios)
    return base


class Falso:
    """JEV programable por pregunta; registra cada llamada, sin red."""

    def __init__(self, respuestas, demora=0.0, falla=None):
        self.respuestas = respuestas
        self.demora = demora
        self.falla = falla
        self.llamadas = []

    def __call__(self, state, preguntas, timeout):
        self.llamadas.append((state, preguntas))
        if self.demora:
            time.sleep(self.demora)
        if self.falla:
            raise self.falla
        return {
            "answers": {
                nombre: {
                    "type": "choice",
                    "choice": self.respuestas[nombre][0],
                    "confidence": self.respuestas[nombre][1],
                }
                for nombre in preguntas
                if nombre in self.respuestas
            }
        }


class Base(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, temporal)
        self.raiz = Path(temporal.name)

    def hogar(self, perfil):
        casa = self.raiz / "profiles" / perfil
        casa.mkdir(parents=True, exist_ok=True)
        return casa

    def auto(self, perfil, jev, **kwargs):
        casa = self.hogar(perfil)
        decisor = jev_decisiones.Decisor(casa, llamar=jev, tope_s=2.0)
        auto = jev_auto.JevAuto(
            perfil,
            hogar=casa,
            decisor=decisor,
            corredor=lambda texto, hogar: {
                "estado": "vacio",
                "skill": None,
                "motivo": None,
                "ms": 1,
            },
            kanban=self.raiz / "sin-kanban.db",
            **kwargs,
        )
        self.addCleanup(self.esperar, auto)
        return auto

    def esperar(self, auto):
        for hilo in list(auto.hilos):
            hilo.join(5)

    def filas(self, perfil, archivo="decisiones.jsonl"):
        ruta = self.hogar(perfil) / "jev" / archivo
        if not ruta.is_file():
            return []
        return [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines()]


class CasoFondoLento(Base):
    """Dispara un hilo jev-fondo lento y NO lo espera a mano."""

    def test_fondo_lento_sin_esperar(self):
        jev = Falso({"fin": ("quedo", 0.95)}, demora=0.5)
        auto = self.auto("pers", jev)
        auto.post_llm_call(
            **turno(assistant_response="Listo: 3 personalizadas nuevas anotadas.")
        )
        self.assertTrue(any(h.name == "jev-fondo" for h in auto.hilos))


class CleanupEsperaHilosTests(Base):
    def test_el_cleanup_espera_los_hilos_de_fondo_antes_de_borrar(self):
        caso = CasoFondoLento("test_fondo_lento_sin_esperar")
        resultado = unittest.TestResult()
        caso.run(resultado)
        self.assertEqual(resultado.errors, [])
        self.assertEqual(resultado.failures, [])
        vivos = [h.name for h in threading.enumerate() if h.name == "jev-fondo"]
        self.assertEqual(vivos, [])


class ClasificacionEnCualquierPerfilTests(Base):
    def test_un_perfil_que_no_es_claw_clasifica_tipo_y_modo_en_una_llamada(self):
        jev = Falso({"tipo": ("operacion", 0.93), "modo": ("directo", 0.88)})
        auto = self.auto("amazon", jev)
        respuesta = auto.pre_llm_call(**turno(platform="kanban"))
        self.assertEqual(
            respuesta,
            {"context": "Jev sugiere: tipo=operacion (0.93), modo=directo (0.88)"},
        )
        self.assertEqual(len(jev.llamadas), 1)
        self.assertEqual(sorted(jev.llamadas[0][1]), ["modo", "tipo"])
        filas = {f["pregunta"]: f for f in self.filas("amazon")}
        self.assertEqual(filas["tipo"]["salida"], "operacion")
        self.assertEqual(filas["modo"]["salida"], "directo")
        self.assertEqual(filas["tipo"]["entrada"]["input"], ENCARGO)
        self.assertEqual(filas["tipo"]["entrada"]["rawLen"], len(ENCARGO))

    def test_las_opciones_son_las_del_encargo(self):
        self.assertEqual(
            sorted(jev_decisiones.TIPOS),
            [
                "codigo",
                "comunicacion",
                "consulta",
                "diagnostico",
                "investigacion",
                "operacion",
            ],
        )
        self.assertEqual(
            sorted(jev_decisiones.MODOS), ["aclarar", "directo", "investigar"]
        )
        self.assertEqual(sorted(jev_decisiones.FINES), ["bloqueado", "quedo", "seguir"])

    def test_la_misma_pregunta_en_el_mismo_turno_no_se_repite(self):
        jev = Falso({"tipo": ("codigo", 0.9), "modo": ("directo", 0.9)})
        auto = self.auto("scout", jev)
        primero = auto.pre_llm_call(**turno())
        segundo = auto.pre_llm_call(**turno())
        self.assertEqual(primero, segundo)
        self.assertEqual(len(jev.llamadas), 1)

    def test_el_fin_del_turno_se_evalua_y_se_registra_sin_retener_el_turno(self):
        jev = Falso({"fin": ("quedo", 0.95)}, demora=0.3)
        auto = self.auto("pers", jev)
        inicio = time.monotonic()
        auto.post_llm_call(
            **turno(assistant_response="Listo: 3 personalizadas nuevas anotadas.")
        )
        self.assertLess(time.monotonic() - inicio, 0.25)  # no espera a JEV
        self.esperar(auto)
        (fila,) = self.filas("pers")
        self.assertEqual((fila["pregunta"], fila["salida"]), ("fin", "quedo"))

    def test_el_registro_sin_fin_para_un_turno_sin_respuesta(self):
        jev = Falso({"fin": ("quedo", 0.95)})
        auto = self.auto("pers", jev)
        auto.post_llm_call(**turno(assistant_response=""))
        self.esperar(auto)
        self.assertEqual(jev.llamadas, [])


class JevCaidoNoBloqueaTests(Base):
    def test_jev_caido_en_cualquier_gancho_no_lanza_ni_bloquea(self):
        jev = Falso({}, falla=RuntimeError("TypeSafe caido"))
        for perfil in ("claw", "mail"):
            auto = self.auto(perfil, jev)
            auto.pre_llm_call(**turno())
            self.assertIsNone(
                auto.pre_tool_call(tool_name="x", args={}, session_id="s1")
            )
            self.assertIsNone(
                auto.pre_tool_call(tool_name="x", args={}, session_id="s1")
            )
            self.assertIsNone(
                auto.post_tool_call(tool_name="x", args={}, session_id="s1")
            )
            self.assertIsNone(
                auto.post_llm_call(**turno(assistant_response="respuesta larga ok"))
            )
            self.esperar(auto)

    def test_el_perfil_no_claw_sin_plugin_jev_instalado_sigue_en_fail_open(self):
        auto = self.auto("mail", None)  # sin llamar inyectado: la carga real
        auto.decisor.hogar = self.hogar("mail")
        respuesta = auto.pre_llm_call(**turno())
        self.assertEqual(respuesta, {"context": "Jev sugiere: no respondió, decide tú"})


class DetectorDeBucleTests(Base):
    def llamar_dos_veces(self, auto, **extra):
        for _ in range(2):
            salida = auto.pre_tool_call(
                tool_name="read_file", args={"path": "a.py"}, session_id="s1", **extra
            )
            self.assertIsNone(salida)  # jamas bloquea

    def test_misma_herramienta_dos_veces_con_090_encola_aviso_al_siguiente_turno(self):
        jev = Falso({"bucle": ("bucle", 0.95)})
        auto = self.auto("claw", jev)
        auto.pre_llm_call(**turno())
        self.llamar_dos_veces(auto)
        self.esperar(auto)
        siguiente = auto.pre_llm_call(
            **turno(turn_id="t2", user_message=ENCARGO + " ya")
        )
        self.assertIn("Jev advierte: read_file", siguiente["context"])
        # se entrega una sola vez
        otro = auto.pre_llm_call(**turno(turn_id="t3", user_message=ENCARGO + " otra"))
        self.assertNotIn("Jev advierte", otro["context"])

    def test_debajo_de_090_no_hay_aviso(self):
        jev = Falso({"bucle": ("bucle", 0.89)})
        auto = self.auto("scout", jev)
        self.llamar_dos_veces(auto)
        self.esperar(auto)
        self.assertEqual(auto._avisos, {})

    def test_una_repeticion_normal_no_avisa(self):
        jev = Falso({"bucle": ("avanza", 0.99)})
        auto = self.auto("scout", jev)
        self.llamar_dos_veces(auto)
        self.esperar(auto)
        self.assertEqual(auto._avisos, {})

    def test_una_herramienta_distinta_no_consulta(self):
        jev = Falso({"bucle": ("bucle", 0.99)})
        auto = self.auto("scout", jev)
        auto.pre_tool_call(tool_name="a", args={}, session_id="s1")
        auto.pre_tool_call(tool_name="b", args={}, session_id="s1")
        self.esperar(auto)
        self.assertEqual(jev.llamadas, [])

    def test_con_jev_lento_el_gancho_vuelve_al_instante(self):
        jev = Falso({"bucle": ("bucle", 0.95)}, demora=1.0)
        auto = self.auto("scout", jev)
        inicio = time.monotonic()
        self.llamar_dos_veces(auto)
        self.assertLess(time.monotonic() - inicio, 0.5)
        self.esperar(auto)

    def test_tope_de_consultas_de_bucle_por_turno(self):
        jev = Falso({"bucle": ("avanza", 0.99)})
        auto = self.auto("scout", jev)
        for _ in range(10):
            auto.pre_tool_call(tool_name="x", args={}, session_id="s1")
        self.esperar(auto)
        self.assertEqual(len(jev.llamadas), jev_auto.TOPE_BUCLES_POR_TURNO)


class BucleFueraDeChatTests(Base):
    def test_un_turno_nuevo_reinicia_el_tope_sin_pre_llm_call(self):
        jev = Falso({"bucle": ("avanza", 0.99)})
        auto = self.auto("claw", jev)
        for turno_id in ("c1", "c2"):
            for _ in range(10):
                auto.pre_tool_call(
                    tool_name="x", args={}, session_id="cron", turn_id=turno_id
                )
        self.esperar(auto)
        self.assertEqual(len(jev.llamadas), 2 * jev_auto.TOPE_BUCLES_POR_TURNO)

    def test_el_libro_de_lineas_tambien_se_poda(self):
        auto = self.auto("claw", Falso({"bucle": ("avanza", 0.99)}))
        for n in range(40):
            auto._lineas[(f"s{n}", "t")] = ["Jev sugiere: claw (90%)"]
        auto._podar()
        self.assertLessEqual(len(auto._lineas), 16)

    def test_los_libros_de_bucle_no_crecen_sin_tope(self):
        auto = self.auto("claw", Falso({"bucle": ("avanza", 0.99)}))
        for n in range(40):
            auto.pre_tool_call(tool_name="x", args={}, session_id=f"s{n}", turn_id="t")
        self.esperar(auto)
        self.assertLessEqual(len(auto._ultima), 16)
        self.assertLessEqual(len(auto._bucles_turno), 16)


class TextoLimpioTests(unittest.TestCase):
    def test_un_mensaje_de_telegram_envuelto_queda_en_lo_que_escribio_david(self):
        self.assertEqual(
            jev_texto.limpiar(TELEGRAM_ENVUELTO),
            "revisa las ordenes flex para hoy y las recolecciones",
        )

    def test_bloques_de_metadatos_notas_y_encabezados(self):
        bruto = (
            "Conversation info (untrusted metadata):\n```json\n"
            '{"chat_id": "123", "message_id": 7}\n```\n'
            "[System note: el usuario respondio a un mensaje]\n"
            "[Telegram Gon id:123] manda el reporte de promos de Gmail\n"
        )
        self.assertEqual(
            jev_texto.limpiar(bruto), "manda el reporte de promos de Gmail"
        )

    def test_una_nota_de_voz_conserva_la_transcripcion(self):
        bruto = (
            "[The user sent a voice message~ Here's what they said: "
            '"revisa el server por ssh"]'
        )
        self.assertEqual(jev_texto.limpiar(bruto), "revisa el server por ssh")

    def test_un_texto_normal_no_cambia(self):
        self.assertEqual(
            jev_texto.limpiar("Revisa [la tabla] de arras, por favor"),
            "Revisa [la tabla] de arras, por favor",
        )

    def test_el_registro_lleva_el_texto_limpio_y_rawlen_del_crudo(self):
        with tempfile.TemporaryDirectory() as temporal:
            casa = Path(temporal)
            jev = Falso({"tipo": ("operacion", 0.9), "modo": ("directo", 0.9)})
            decisor = jev_decisiones.Decisor(casa, llamar=jev)
            auto = jev_auto.JevAuto("amazon", hogar=casa, decisor=decisor)
            auto.pre_llm_call(**turno(user_message=TELEGRAM_ENVUELTO))
            for hilo in list(auto.hilos):
                hilo.join(5)
            filas = [
                json.loads(x)
                for x in (casa / "jev" / "decisiones.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            entrada = filas[0]["entrada"]
            self.assertEqual(entrada["input"], ENCARGO)
            self.assertEqual(entrada["rawLen"], len(TELEGRAM_ENVUELTO))
            self.assertNotIn("Replying", jev.llamadas[0][0])


class TextoNoVaciaMensajesRealesTests(unittest.TestCase):
    """Un mensaje real de David jamas se pierde por parecer un envoltorio."""

    def test_mensajes_reales_que_antes_quedaban_vacios_se_conservan(self):
        for real in (
            "Fecha de entrega de las recolecciones de Seller Flex: ya salio?",
            "Date range de la consulta de ordenes: ultimos 30 dias",
            "Time out al conectar por SSH al server gonserver",
            "System: el servidor de pagos esta caido, revisalo",
        ):
            with self.subTest(real=real):
                self.assertEqual(jev_texto.limpiar(real), real)

    def test_un_contexto_con_bloque_de_codigo_de_david_se_conserva(self):
        real = "Te paso el contexto:\n```\nerror 500 en /pagos\n```\nrevisalo"
        self.assertEqual(jev_texto.limpiar(real), real)

    def test_el_encabezado_exacto_del_gateway_si_se_quita(self):
        bruto = (
            "Conversation info (untrusted metadata):\n```json\n"
            '{"chat_id": "1"}\n```\nrevisa el reporte de ventas de hoy'
        )
        self.assertEqual(jev_texto.limpiar(bruto), "revisa el reporte de ventas de hoy")

    def test_replying_to_multilinea_se_quita_completo(self):
        bruto = (
            '[Replying to: "primera linea de la cita\nsegunda linea\ntercera"]\n'
            "[Gon] manda el reporte de promos de Gmail"
        )
        self.assertEqual(
            jev_texto.limpiar(bruto), "manda el reporte de promos de Gmail"
        )

    def test_si_solo_hay_envoltorio_se_usa_el_texto_original(self):
        self.assertEqual(
            jev_texto.limpiar("JEV skill_select: ninguna"),
            "JEV skill_select: ninguna",
        )

    def test_replying_to_your_previous_message_se_quita(self):
        bruto = (
            "[Replying to your previous message: ¿ya quedó lo de las guías?]\n"
            "[Gon] revisa las ordenes flex para hoy y las recolecciones"
        )
        self.assertEqual(
            jev_texto.limpiar(bruto),
            "revisa las ordenes flex para hoy y las recolecciones",
        )

    def test_mensaje_real_con_fecha_y_envoltorio_conserva_la_fecha(self):
        bruto = (
            '[Replying to: "ya?"]\n'
            "Fecha de entrega de las recolecciones de Seller Flex: cuando sale?"
        )
        self.assertEqual(
            jev_texto.limpiar(bruto),
            "Fecha de entrega de las recolecciones de Seller Flex: cuando sale?",
        )


class TextosCortosTests(Base):
    def test_un_texto_de_menos_de_15_caracteres_no_consulta_a_jev(self):
        for perfil in ("claw", "odoo"):
            jev = Falso({"tipo": ("consulta", 0.9), "ruteo": ("claw", 0.9)})
            auto = self.auto(perfil, jev)
            self.assertIsNone(auto.pre_llm_call(**turno(user_message="¿ya quedó?")))
            self.assertEqual(jev.llamadas, [])
        self.assertTrue(jev_texto.es_corto("a" * 14))
        self.assertFalse(jev_texto.es_corto("a" * 15))

    def test_un_texto_corto_deja_pasar_el_aviso_de_bucle_pendiente(self):
        auto = self.auto("claw", Falso({}))
        auto._avisos["s1"] = ["Jev advierte: x se repitio"]
        respuesta = auto.pre_llm_call(**turno(user_message="¿ya?"))
        self.assertEqual(respuesta, {"context": "Jev advierte: x se repitio"})


class CorreccionesTests(Base):
    def claw(self, ruteo=("amazon", 0.93)):
        jev = Falso({"ruteo": ruteo})
        return self.auto("claw", jev), jev

    def test_jev_sugirio_amazon_y_claw_despacho_a_meli_se_guarda_sin_inyectar(
        self,
    ):
        auto, jev = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="kanban_create",
            args={"assignee": "meli", "title": "x"},
            session_id="s1",
            status="ok",
        )
        auto.post_llm_call(**turno(assistant_response="Se lo di a meli."))
        self.esperar(auto)
        (correccion,) = self.filas("claw", "correcciones.jsonl")
        self.assertEqual(
            (correccion["sugerido"], correccion["correcto"]), ("amazon", "meli")
        )
        self.assertEqual(correccion["texto"], ENCARGO)
        self.assertFalse(correccion["confirmada"])
        # Registrada si, inyectada no: sin senal explicita de David no es
        # ejemplo en la siguiente ruteada.
        auto.pre_llm_call(**turno(turn_id="t2", user_message=ENCARGO + " otra vez"))
        instrucciones = jev.llamadas[-1][1]["ruteo"]["instructions"]
        self.assertNotIn("Ejemplos de correcciones", instrucciones)
        self.assertNotIn("-> meli (no amazon)", instrucciones)

    def _no_inyecta(self, auto, jev):
        """La correccion automatica quedo registrada pero fuera del prompt."""
        (correccion,) = self.filas("claw", "correcciones.jsonl")
        self.assertFalse(correccion["confirmada"])
        self.assertEqual(jev_decisiones.ejemplos_de(self.hogar("claw")), "")
        auto.pre_llm_call(**turno(turn_id="tx", user_message=ENCARGO + " otra vez"))
        instrucciones = jev.llamadas[-1][1]["ruteo"]["instructions"]
        self.assertNotIn("Ejemplos de correcciones", instrucciones)

    def test_falso_turno_cortado_se_registra_pero_no_inyecta(self):
        auto, jev = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="terminal", args={"command": "ls"}, session_id="s1", status="ok"
        )
        # Respuesta cortada a mitad, sin '?': parece "lo resolvio solo".
        auto.post_llm_call(
            **turno(
                assistant_response="Ya revise las ordenes y las guias quedaron a mit"
            )
        )
        self._no_inyecta(auto, jev)

    def test_falsa_aclaracion_sin_signo_se_registra_pero_no_inyecta(self):
        auto, jev = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="terminal", args={"command": "ls"}, session_id="s1", status="ok"
        )
        auto.post_llm_call(
            **turno(assistant_response="Necesito el pais antes de seguir con esto")
        )
        self._no_inyecta(auto, jev)

    def test_falsa_marca_heredada_de_otro_turno_no_inyecta(self):
        auto, jev = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="terminal", args={"command": "ls"}, session_id="s1", status="ok"
        )
        auto.post_llm_call(**turno(assistant_response="Para que pais lo reviso?"))
        # Turno siguiente sin herramientas: la marca de trabajo es heredada.
        auto.post_llm_call(
            **turno(
                turn_id="t2",
                assistant_response="Listo, ya quedo revisado todo el encargo.",
            )
        )
        self._no_inyecta(auto, jev)

    def test_falso_send_message_no_inyecta(self):
        auto, jev = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="send_message", args={}, session_id="s1", status="ok"
        )
        auto.post_llm_call(
            **turno(assistant_response="Ya avise por el canal lo que encontre hoy")
        )
        self._no_inyecta(auto, jev)

    def test_con_senal_explicita_de_david_si_inyecta_y_conserva_el_texto(self):
        auto, jev = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_llm_call(**turno(assistant_response="Para que pais lo reviso?"))
        auto.pre_llm_call(**turno(turn_id="t2", user_message="no, eso va con meli"))
        self.assertNotIn("s1", auto._correcciones)
        (correccion,) = self.filas("claw", "correcciones.jsonl")
        self.assertTrue(correccion["confirmada"])
        self.assertEqual(correccion["texto"], ENCARGO)
        self.assertEqual(
            (correccion["sugerido"], correccion["correcto"]), ("amazon", "meli")
        )
        auto.pre_llm_call(**turno(turn_id="t3", user_message=ENCARGO + " otra vez"))
        instrucciones = jev.llamadas[-1][1]["ruteo"]["instructions"]
        self.assertIn(ENCARGO, instrucciones)
        self.assertIn("-> meli (no amazon)", instrucciones)

    def test_la_senal_explicita_solo_acepta_destinos_reales(self):
        self.assertEqual(
            jev_decisiones.confirmacion_explicita("eso era para amazon"), "amazon"
        )
        self.assertEqual(
            jev_decisiones.confirmacion_explicita("no, eso va con meli"), "meli"
        )
        self.assertEqual(
            jev_decisiones.confirmacion_explicita("eso era para ingeniería"),
            "ingenieria",
        )
        self.assertIsNone(jev_decisiones.confirmacion_explicita("eso era para nadie"))
        self.assertIsNone(
            jev_decisiones.confirmacion_explicita("revisa eso era para amazon hoy")
        )
        self.assertIsNone(jev_decisiones.confirmacion_explicita(ENCARGO))

    def test_una_correccion_vieja_sin_marca_de_confirmada_no_inyecta(self):
        casa = self.hogar("claw")
        ruta = casa / "jev" / "correcciones.jsonl"
        ruta.parent.mkdir(parents=True)
        ruta.write_text(
            json.dumps({"texto": ENCARGO, "sugerido": "amazon", "correcto": "meli"})
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(jev_decisiones.ejemplos_de(casa), "")

    def test_la_cadena_de_ingenieria_es_la_correcta_cuando_se_despacha_con_local_run(
        self,
    ):
        auto, _ = self.claw(("claw", 0.9))
        auto.pre_llm_call(**turno())
        auto.post_tool_call(tool_name="goncloud_local_run", args={}, session_id="s1")
        auto.post_llm_call(**turno(assistant_response="Arranque la cadena."))
        (c,) = self.filas("claw", "correcciones.jsonl")
        self.assertEqual((c["sugerido"], c["correcto"]), ("claw", "ingenieria"))

    def test_lo_resolvio_solo_con_trabajo_comprobable_el_correcto_es_claw(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="terminal", args={"command": "ls"}, session_id="s1", status="ok"
        )
        auto.post_llm_call(**turno(assistant_response="Ya lo revise yo: todo ok."))
        (c,) = self.filas("claw", "correcciones.jsonl")
        self.assertEqual((c["sugerido"], c["correcto"]), ("amazon", "claw"))

    def test_una_aclaracion_sin_despacho_no_guarda_correccion_falsa(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_llm_call(**turno(assistant_response="Para que pais lo reviso?"))
        self.assertEqual(self.filas("claw", "correcciones.jsonl"), [])
        self.assertIn("s1", auto._correcciones)  # sigue pendiente

    def test_un_turno_cortado_sin_respuesta_ni_despacho_no_guarda(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(tool_name="terminal", args={}, session_id="s1", status="ok")
        auto.post_llm_call(**turno(assistant_response=""))
        self.assertEqual(self.filas("claw", "correcciones.jsonl"), [])

    def test_trabajo_con_una_pregunta_final_no_es_resolverlo_solo(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(tool_name="terminal", args={}, session_id="s1", status="ok")
        auto.post_llm_call(**turno(assistant_response="Encontre dos, cual uso?"))
        self.assertEqual(self.filas("claw", "correcciones.jsonl"), [])

    def test_aclaracion_y_despacho_posterior_a_lo_sugerido_no_genera_correccion(self):
        # "MX" (<15 caracteres) no abre un registro nuevo: el despacho del
        # turno siguiente se enlaza a la sugerencia original.
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_llm_call(**turno(assistant_response="Para que pais?"))
        auto.pre_llm_call(**turno(turn_id="t2", user_message="MX"))
        auto.post_tool_call(
            tool_name="kanban_create", args={"assignee": "amazon"}, session_id="s1"
        )
        auto.post_llm_call(
            **turno(turn_id="t2", user_message="MX", assistant_response="Listo.")
        )
        self.esperar(auto)
        self.assertEqual(self.filas("claw", "correcciones.jsonl"), [])
        self.assertNotIn("s1", auto._correcciones)

    def test_aclaracion_y_despacho_posterior_distinto_se_enlaza_al_texto_original(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_llm_call(**turno(assistant_response="Para que pais?"))
        auto.pre_llm_call(**turno(turn_id="t2", user_message="MX"))
        auto.post_tool_call(
            tool_name="kanban_create", args={"assignee": "meli"}, session_id="s1"
        )
        auto.post_llm_call(
            **turno(turn_id="t2", user_message="MX", assistant_response="Listo.")
        )
        (c,) = self.filas("claw", "correcciones.jsonl")
        self.assertEqual(c["texto"], ENCARGO)
        self.assertEqual((c["sugerido"], c["correcto"]), ("amazon", "meli"))

    def test_la_sugerencia_sin_desenlace_expira_a_los_3_turnos(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        for n in range(jev_auto.TURNOS_PENDIENTE):
            self.assertIn("s1", auto._correcciones)
            auto.post_llm_call(**turno(turn_id=f"c{n}", assistant_response="Y?"))
        self.assertNotIn("s1", auto._correcciones)

    def test_una_respuesta_larga_a_la_aclaracion_conserva_el_texto_original(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_llm_call(**turno(assistant_response="Para que pais?"))
        auto.pre_llm_call(
            **turno(
                turn_id="t2",
                user_message=(
                    "Es para Mexico, las guias de Seller Flex de esta manana "
                    "y tambien las recolecciones del almacen"
                ),
            )
        )
        self.assertEqual(auto._correcciones["s1"]["texto"], ENCARGO)
        auto.post_tool_call(
            tool_name="kanban_create", args={"assignee": "meli"}, session_id="s1"
        )
        auto.post_llm_call(**turno(turn_id="t2", assistant_response="Se lo di a meli."))
        (c,) = self.filas("claw", "correcciones.jsonl")
        self.assertEqual(c["texto"], ENCARGO)
        self.assertEqual((c["sugerido"], c["correcto"]), ("amazon", "meli"))

    def test_el_archivo_de_correcciones_se_rota_en_vez_de_crecer_sin_tope(self):
        casa = self.hogar("claw")
        for i in range(jev_decisiones.TOPE_CORRECCIONES + 5):
            jev_decisiones.guardar_correccion(casa, f"encargo {i}", "claw", "odoo")
        filas = self.filas("claw", "correcciones.jsonl")
        self.assertLessEqual(len(filas), jev_decisiones.TOPE_CORRECCIONES)
        self.assertEqual(
            filas[-1]["texto"], f"encargo {jev_decisiones.TOPE_CORRECCIONES + 4}"
        )

    def test_si_despacho_a_lo_sugerido_no_hay_correccion(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="kanban_create", args={"assignee": "amazon"}, session_id="s1"
        )
        auto.post_llm_call(**turno(assistant_response="Se lo di a amazon."))
        self.assertEqual(self.filas("claw", "correcciones.jsonl"), [])

    def test_un_assignee_que_no_es_especialista_no_cuenta_y_un_error_tampoco(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="kanban_create", args={"assignee": "scout"}, session_id="s1"
        )
        auto.post_tool_call(
            tool_name="kanban_create",
            args={"assignee": "meli"},
            session_id="s1",
            status="error",
        )
        self.assertEqual(auto._correcciones["s1"]["despachos"], [])

    def test_solo_las_ultimas_15_confirmadas_son_ejemplos(self):
        casa = self.hogar("claw")
        for i in range(20):
            jev_decisiones.guardar_correccion(
                casa, f"encargo numero {i}", "claw", "odoo", confirmada=True
            )
        ejemplos = jev_decisiones.ejemplos_de(casa)
        self.assertEqual(ejemplos.count("-> odoo"), 15)
        self.assertIn("encargo numero 19", ejemplos)
        self.assertNotIn('encargo numero 4"', ejemplos)

    def test_la_correccion_taja_las_llaves(self):
        casa = self.hogar("claw")
        jev_decisiones.guardar_correccion(
            casa,
            "usa la llave sk-super-secreta-123 en odoo",
            "claw",
            "odoo",
            secretos=("sk-super-secreta-123",),
        )
        crudo = (casa / "jev" / "correcciones.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("sk-super-secreta-123", crudo)

    def test_solo_el_director_aprende(self):
        auto = self.auto("amazon", Falso({"tipo": ("consulta", 0.9)}))
        auto.pre_llm_call(**turno())
        auto.post_tool_call(
            tool_name="kanban_create", args={"assignee": "meli"}, session_id="s1"
        )
        auto.post_llm_call(**turno(assistant_response="listo"))
        self.assertEqual(self.filas("amazon", "correcciones.jsonl"), [])


class RosterRicoTests(unittest.TestCase):
    def test_las_descripciones_llevan_las_palabras_reales_de_david(self):
        texto = " ".join(jev_decisiones.RUTAS.values()).lower()
        for palabra in (
            "seller flex",
            "recolecciones",
            "mercado libre",
            "gmail",
            "promos",
            "personalizadas",
            "cajas",
            "odoo",
            "arras",
            "ssh",
            "bridge",
            "openclaw",
            "plugin",
            "api",
            "bot nuevo",
        ):
            self.assertIn(palabra, texto, palabra)
        self.assertIn("server", texto)


class EtapasDeLaCadenaTests(Base):
    def test_la_etapa_recibe_jev_sugiere_en_su_prompt(self):
        jev = Falso({"tipo": ("codigo", 0.93), "modo": ("investigar", 0.9)})
        etapa = jev_auto.JevEtapa(
            jev_decisiones.Decisor(self.hogar("claw"), llamar=jev)
        )
        linea = etapa.sugerir("Arregla el bridge de OpenClaw que falla", "scout")
        self.assertEqual(
            linea, "Jev sugiere: tipo=codigo (0.93), modo=investigar (0.90)"
        )
        prompt = local_orchestrator.LocalOrchestrator._instruction(
            "Arregla el bridge de OpenClaw que falla", "duties", "scout", linea
        )
        self.assertIn(linea, prompt)
        sin = local_orchestrator.LocalOrchestrator._instruction(
            "Arregla el bridge de OpenClaw que falla", "duties", "scout"
        )
        self.assertNotIn("Jev sugiere", sin)

    def test_jev_caido_la_etapa_sigue_sin_linea(self):
        jev = Falso({}, falla=RuntimeError("caido"))
        etapa = jev_auto.JevEtapa(
            jev_decisiones.Decisor(self.hogar("claw"), llamar=jev)
        )
        # sin respuesta de JEV la etapa no recibe ni la linea de "no respondio"
        self.assertEqual(
            etapa.sugerir("Arregla el bridge de OpenClaw que falla", "scout"), ""
        )
        etapa.cerrar("Arregla el bridge", "scout", {"status": "completed"})  # no lanza

    def test_el_cierre_de_etapa_se_registra(self):
        jev = Falso({"fin": ("quedo", 0.9)})
        etapa = jev_auto.JevEtapa(
            jev_decisiones.Decisor(self.hogar("claw"), llamar=jev)
        )
        etapa.cerrar(
            "Arregla el bridge de OpenClaw que falla",
            "verifier",
            {"status": "completed", "summary": "pruebas verdes"},
        )
        (fila,) = self.filas("claw")
        self.assertEqual((fila["pregunta"], fila["salida"]), ("fin", "quedo"))


if __name__ == "__main__":
    unittest.main()
