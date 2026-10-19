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
    "[Replying to: \"¿ya quedó lo de las guías?\"]\n"
    "[Gon] revisa las ordenes flex para hoy y las recolecciones\n"
    "JEV skill_select: ninguna\n"
    "Jev sugiere: amazon (0.91)\n"
    "System: el turno corre en telegram\n"
    "2026-10-19 08:15 CST\n"
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
        return jev_auto.JevAuto(
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

    def esperar(self, auto):
        for hilo in list(auto.hilos):
            hilo.join(5)

    def filas(self, perfil, archivo="decisiones.jsonl"):
        ruta = self.hogar(perfil) / "jev" / archivo
        if not ruta.is_file():
            return []
        return [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines()]


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
        self.assertEqual(
            sorted(jev_decisiones.FINES), ["bloqueado", "quedo", "seguir"]
        )

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
            self.assertIsNone(auto.pre_tool_call(tool_name="x", args={}, session_id="s1"))
            self.assertIsNone(auto.pre_tool_call(tool_name="x", args={}, session_id="s1"))
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
        self.assertEqual(
            respuesta, {"context": "Jev sugiere: no respondió, decide tú"}
        )


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
        siguiente = auto.pre_llm_call(**turno(turn_id="t2", user_message=ENCARGO + " ya"))
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
            '[The user sent a voice message~ Here\'s what they said: '
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
            self.assertNotIn("System:", jev.llamadas[0][0])


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

    def test_jev_sugirio_amazon_y_claw_despacho_a_meli_se_guarda_y_sirve_de_ejemplo(
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
        # la siguiente ruteada lleva el ejemplo en sus instructions
        auto.pre_llm_call(**turno(turn_id="t2", user_message=ENCARGO + " otra vez"))
        instrucciones = jev.llamadas[-1][1]["ruteo"]["instructions"]
        self.assertIn(ENCARGO, instrucciones)
        self.assertIn("-> meli (no amazon)", instrucciones)

    def test_la_cadena_de_ingenieria_es_la_correcta_cuando_se_despacha_con_local_run(
        self,
    ):
        auto, _ = self.claw(("claw", 0.9))
        auto.pre_llm_call(**turno())
        auto.post_tool_call(tool_name="goncloud_local_run", args={}, session_id="s1")
        auto.post_llm_call(**turno(assistant_response="Arranque la cadena."))
        (c,) = self.filas("claw", "correcciones.jsonl")
        self.assertEqual((c["sugerido"], c["correcto"]), ("claw", "ingenieria"))

    def test_lo_resolvio_solo_el_correcto_es_claw(self):
        auto, _ = self.claw()
        auto.pre_llm_call(**turno())
        auto.post_llm_call(**turno(assistant_response="Ya lo revise yo: todo ok."))
        (c,) = self.filas("claw", "correcciones.jsonl")
        self.assertEqual((c["sugerido"], c["correcto"]), ("amazon", "claw"))

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

    def test_solo_las_ultimas_15_son_ejemplos(self):
        casa = self.hogar("claw")
        for i in range(20):
            jev_decisiones.guardar_correccion(casa, f"encargo numero {i}", "claw", "odoo")
        ejemplos = jev_decisiones.ejemplos_de(casa)
        self.assertEqual(ejemplos.count("-> odoo"), 15)
        self.assertIn("encargo numero 19", ejemplos)
        self.assertNotIn("encargo numero 4\"", ejemplos)

    def test_la_correccion_taja_las_llaves(self):
        casa = self.hogar("claw")
        jev_decisiones.guardar_correccion(
            casa, "usa la llave sk-super-secreta-123 en odoo", "claw", "odoo",
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
        self.assertEqual(etapa.sugerir("Arregla el bridge de OpenClaw que falla", "scout"),
                         "Jev sugiere: no respondió, decide tú")
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
