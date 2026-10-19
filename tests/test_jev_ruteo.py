"""Ruteo y cierre de JEV por codigo (encargo H-jev-hermes).

Criterios: un mensaje simulado produce skill_select + ruteo sin que el modelo
lo pida; la etapa o tarjeta que termino (incluido blocked) trae su cierre en
el turno siguiente; JEV caido, lento o respondiendo mal no bloquea el turno;
el umbral 0.85 manda; cada decision deja su linea en decisiones.jsonl sin el
texto del mensaje.
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
import jev_apoyo  # noqa: E402

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

import jev_auto  # noqa: E402
import jev_decisiones  # noqa: E402

MENSAJE = "revisa las ordenes de amazon de hoy"


def turno(**cambios):
    base = {
        "session_id": "s1",
        "turn_id": "t1",
        "platform": "telegram",
        "user_message": MENSAJE,
    }
    base.update(cambios)
    return base


def corredor_ok(texto, hogar):
    return {"estado": "ok", "skill": "web-navigation", "motivo": None, "ms": 5}


class Base(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, temporal)
        self.raiz = Path(temporal.name)
        self.claw = self.raiz / "profiles" / "claw"
        self.claw.mkdir(parents=True)
        self.kanban = jev_apoyo.Kanban(self.raiz / "kanban.db")
        self.jev = jev_apoyo.JevFalso(
            {"ruteo": ("amazon", 0.93), "cierre": ("quedo", 0.91)}
        )

    def auto(self, jev=None, **kwargs):
        decisor = jev_decisiones.Decisor(
            self.claw, llamar=jev or self.jev, tope_s=kwargs.pop("tope_s", 2.0)
        )
        kwargs.setdefault("hogar", self.claw)
        kwargs.setdefault("corredor", corredor_ok)
        kwargs.setdefault("kanban", self.kanban.ruta)
        return jev_auto.JevAuto("claw", decisor=decisor, **kwargs)

    def decisiones(self):
        ruta = self.claw / "jev" / "decisiones.jsonl"
        if not ruta.is_file():
            return []
        return [
            json.loads(linea) for linea in ruta.read_text(encoding="utf-8").splitlines()
        ]


class DecisorTests(Base):
    def decisor(self, jev=None, **kwargs):
        return jev_decisiones.Decisor(self.claw, llamar=jev or self.jev, **kwargs)

    def test_rutear_devuelve_la_opcion_y_la_confianza(self):
        decision = self.decisor().rutear(MENSAJE)
        self.assertEqual((decision.opcion, decision.confianza), ("amazon", 0.93))
        self.assertIsNone(decision.error)
        self.assertTrue(decision.sigue)
        _, preguntas, tope = self.jev.llamadas[0]
        self.assertEqual(tope, 2.0)
        opciones = preguntas["ruteo"]["criteria"]
        self.assertEqual(preguntas["ruteo"]["type"], "choice")
        self.assertIn("claw", opciones)
        self.assertIn("ingenieria", opciones)
        for especialista in (
            "amazon pers meli mail limpia odoo arras gonserver skillverif tinkabot eggbot"
        ).split():
            self.assertIn(especialista, opciones)

    def test_el_umbral_es_085_y_debajo_decide_claw(self):
        for confianza, sigue in (
            (0.85, True),
            (0.99, True),
            (0.849, False),
            (0.5, False),
        ):
            jev = jev_apoyo.JevFalso({"ruteo": ("meli", confianza)})
            decision = self.decisor(jev).rutear(MENSAJE)
            self.assertEqual(decision.sigue, sigue, confianza)
        bajo = jev_decisiones.linea_ruteo(
            jev_decisiones.Decision("ruteo", "meli", 0.77)
        )
        self.assertEqual(
            bajo, "Jev sugiere: meli (0.77), bajo el umbral 0.85: decide tú"
        )
        alto = jev_decisiones.linea_ruteo(
            jev_decisiones.Decision("ruteo", "amazon", 0.93)
        )
        self.assertEqual(alto, "Jev sugiere: amazon (0.93)")

    def test_jev_que_lanza_no_lanza_hacia_afuera(self):
        jev = jev_apoyo.JevFalso(falla=RuntimeError("TypeSafe API unreachable"))
        decision = self.decisor(jev).rutear(MENSAJE)
        self.assertIsNone(decision.opcion)
        self.assertIn("unreachable", decision.error)
        self.assertFalse(decision.sigue)

    def test_jev_lento_vence_a_los_dos_segundos_de_tope(self):
        liberar = threading.Event()
        self.addCleanup(liberar.set)

        def colgado(state, preguntas, timeout):
            liberar.wait(10)

        inicio = time.monotonic()
        decision = self.decisor(colgado, tope_s=0.2).rutear(MENSAJE)
        self.assertLess(time.monotonic() - inicio, 1.5)
        self.assertIn("vencio el tope", decision.error)

    def test_el_tope_por_omision_es_de_dos_segundos_y_sin_reintentos(self):
        self.assertEqual(jev_decisiones.TOPE_JEV_S, 2.0)
        self.assertEqual(jev_decisiones.Decisor(self.claw).tope_s, 2.0)

    def test_respuesta_sin_opcion_valida_o_sin_confianza_es_error(self):
        malas = (
            {"answers": {"ruteo": {"choice": "nadie", "confidence": 0.9}}},
            {"answers": {"ruteo": {"choice": "amazon"}}},
            {"answers": {}},
            "basura",
        )
        for mala in malas:
            decision = self.decisor(lambda s, p, t, mala=mala: mala).rutear(MENSAJE)
            self.assertIsNotNone(decision.error, mala)
            self.assertFalse(decision.sigue)

    def test_la_confianza_cae_en_la_probabilidad_de_la_opcion(self):
        respuesta = {
            "answers": {
                "ruteo": {"choice": "odoo", "probabilities": {"odoo": 0.9, "claw": 0.1}}
            }
        }
        decision = self.decisor(lambda s, p, t: respuesta).rutear(MENSAJE)
        self.assertEqual(decision.confianza, 0.9)

    def test_la_confianza_nan_o_infinita_no_es_certeza(self):
        for bruta in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bruta=bruta):
                decision = self.decisor(
                    lambda s, p, t, bruta=bruta: {
                        "answers": {"ruteo": {"choice": "odoo", "confidence": bruta}}
                    }
                ).rutear(MENSAJE)
                self.assertEqual(decision.error, "respuesta sin confianza")
                self.assertIsNone(decision.confianza)
                self.assertFalse(decision.sigue)

    def test_la_llave_y_los_secretos_resueltos_en_el_hilo_del_gancho_se_tachan(self):
        # F3: los valores resueltos fuera de los hilos viajan ya resueltos.
        # F4: lo tachado es la llave resuelta y los valores que se le pasan,
        # no solo el scope.
        llave = "sk-resuelta-en-el-hilo-1"
        captura = {}

        def llamar(estado, preguntas, timeout):
            captura["estado"] = estado
            return {"answers": {"ruteo": {"choice": "claw", "confidence": 0.9}}}

        self.decisor(llamar).rutear(
            f"habla con la llave {llave} y con valor-solo-del-env-7",
            clave=llave,
            secretos=("valor-solo-del-env-7",),
        )
        estado = captura["estado"]
        self.assertNotIn(llave, estado)
        self.assertNotIn("valor-solo-del-env-7", estado)

    def test_los_valores_que_solo_viven_en_el_env_del_perfil_se_tachan(self):
        # F4: sin resolucion previa, el .env del perfil entra al tachado.
        (self.claw / ".env").write_text(
            "OTRA_CREDENCIAL=valor-env-sin-forma-9\n", encoding="utf-8"
        )
        captura = {}

        def llamar(estado, preguntas, timeout):
            captura["estado"] = estado
            return {"answers": {"ruteo": {"choice": "claw", "confidence": 0.9}}}

        self.decisor(llamar).rutear("menciona valor-env-sin-forma-9 y sigue")
        self.assertNotIn("valor-env-sin-forma-9", captura["estado"])

    def test_lo_que_sale_a_jev_va_redactado(self):
        secreto = "sk-prueba-secreta-1234567890"
        self.decisor().rutear(f"usa la llave {secreto} para entrar")
        estado, _, _ = self.jev.llamadas[0]
        self.assertNotIn(secreto, estado)
        self.assertIn("David", estado)

    def test_el_registro_deja_entrada_salida_confianza_latencia_y_error(self):
        self.decisor().rutear(MENSAJE)
        self.decisor(jev_apoyo.JevFalso(falla=RuntimeError("caido"))).rutear(MENSAJE)
        ok, mal = self.decisiones()
        self.assertEqual(ok["pregunta"], "ruteo")
        self.assertEqual(
            (ok["salida"], ok["confianza"], ok["error"]), ("amazon", 0.93, None)
        )
        self.assertEqual(ok["entrada"]["len"], len(MENSAJE))
        self.assertIsInstance(ok["ms"], int)
        self.assertIn("at", ok)
        self.assertIsNone(mal["salida"])
        self.assertEqual(mal["error"], "caido")
        self.assertNotIn(MENSAJE, json.dumps([ok, mal]))

    def test_sin_el_plugin_jev_instalado_falla_cerrado_y_corto(self):
        decision = jev_decisiones.Decisor(self.claw).rutear(MENSAJE)
        self.assertIn("plugin jev no instalado", decision.error)


class RuteoPorMensajeTests(Base):
    def test_un_mensaje_produce_skill_select_y_ruteo_sin_que_el_modelo_lo_pida(self):
        auto = self.auto()
        respuesta = auto.pre_llm_call(**turno())
        lineas = respuesta["context"].split("\n")
        self.assertEqual(lineas[0], "JEV skill_select: web-navigation (cárgala)")
        self.assertEqual(lineas[1], "Jev sugiere: amazon (0.93)")
        self.assertEqual(self.jev.preguntas(), ["ruteo"])

    def test_bajo_el_umbral_la_linea_deja_decidir_a_claw(self):
        jev = jev_apoyo.JevFalso({"ruteo": ("mail", 0.77)})
        respuesta = self.auto(jev).pre_llm_call(**turno())
        self.assertIn(
            "Jev sugiere: mail (0.77), bajo el umbral 0.85: decide tú",
            respuesta["context"],
        )

    def test_jev_caido_no_bloquea_el_turno_y_skill_select_sigue(self):
        jev = jev_apoyo.JevFalso(falla=RuntimeError("sin red"))
        respuesta = self.auto(jev).pre_llm_call(**turno())
        lineas = respuesta["context"].split("\n")
        self.assertEqual(lineas[0], "JEV skill_select: web-navigation (cárgala)")
        self.assertEqual(lineas[1], "Jev sugiere: no respondió, decide tú")

    def test_jev_colgado_no_pasa_de_su_tope_ni_bloquea_el_turno(self):
        liberar = threading.Event()
        self.addCleanup(liberar.set)
        auto = self.auto(lambda s, p, t: liberar.wait(10), tope_s=0.2)
        inicio = time.monotonic()
        respuesta = auto.pre_llm_call(**turno())
        self.assertLess(time.monotonic() - inicio, 2.0)
        self.assertIn("JEV skill_select: web-navigation", respuesta["context"])
        self.assertIn("no respondió", respuesta["context"])

    def test_una_sola_consulta_por_turno(self):
        auto = self.auto()
        primero = auto.pre_llm_call(**turno())
        segundo = auto.pre_llm_call(**turno())
        self.assertEqual(primero, segundo)
        self.assertEqual(len(self.jev.llamadas), 1)
        self.assertEqual(len(self.decisiones()), 1)

    def test_solo_claw_en_telegram_o_cli_rutea(self):
        decisor = jev_decisiones.Decisor(self.claw, llamar=self.jev)
        otro = jev_auto.JevAuto(
            "scout", hogar=self.claw, corredor=corredor_ok, decisor=decisor
        )
        self.assertIsNone(otro.pre_llm_call(**turno()))
        auto = self.auto()
        for plataforma in ("cron", "bot-chat", "kanban", ""):
            self.assertIsNone(auto.pre_llm_call(**turno(platform=plataforma)))
        self.assertEqual(self.jev.llamadas, [])

    def test_sin_decisor_el_contexto_es_solo_el_de_skill_select(self):
        auto = jev_auto.JevAuto("claw", hogar=self.claw, corredor=corredor_ok)
        respuesta = auto.pre_llm_call(**turno())
        self.assertEqual(
            respuesta, {"context": "JEV skill_select: web-navigation (cárgala)"}
        )

    def test_el_registro_de_ruteo_no_lleva_el_texto_del_mensaje(self):
        self.auto().pre_llm_call(**turno())
        (fila,) = self.decisiones()
        self.assertEqual(fila["pregunta"], "ruteo")
        self.assertNotIn("amazon de hoy", json.dumps(fila))


class CierreTests(Base):
    def primer_turno(self, auto):
        """El primer turno fija el cursor: la historia no se cierra de golpe."""
        auto.pre_llm_call(**turno(turn_id="t0"))

    def test_la_tarjeta_de_especialista_que_termino_trae_su_cierre_en_el_turno_siguiente(
        self,
    ):
        self.kanban.tarea("t_a1", "amazon", "done", title="ordenes de hoy")
        self.kanban.evento("t_a1", "completed")  # historia: anterior al primer turno
        auto = self.auto()
        self.primer_turno(auto)
        self.assertNotIn(
            "Jev cierre", auto.pre_llm_call(**turno(turn_id="t0"))["context"]
        )
        self.kanban.tarea("t_a2", "amazon", "done", title="ordenes de ayer")
        self.kanban.corrida("t_a2", "5 ordenes procesadas, 0 errores")
        self.kanban.evento("t_a2", "completed")
        contexto = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        self.assertIn("Jev cierre: t_a2 (amazon, completed) quedo (0.91)", contexto)
        self.assertNotIn("t_a1", contexto)
        ficha = next(s for s, p, _ in self.jev.llamadas if "cierre" in p)
        self.assertIn("5 ordenes procesadas", ficha)
        # El cursor ya paso ese evento: el turno que sigue no lo repite.
        siguiente = auto.pre_llm_call(**turno(turn_id="t2"))["context"]
        self.assertNotIn("Jev cierre", siguiente)

    def test_una_etapa_blocked_de_cadena_goncloud_tambien_cierra(self):
        # El grafo lo arma el codigo real del doble: la raiz NO es padre de
        # las etapas en task_links y las etapas iniciales no llevan
        # idempotency_key; el parentesco vive en el evento "decomposed".
        ids = self.kanban.cadena(etapas=(("scout", "done"), ("implementer", "running")))
        auto = self.auto()
        self.primer_turno(auto)
        self.assertTrue(
            self.kanban.bloquear(ids["implementer"], "falta acceso al repo")
        )
        jev = self.jev
        jev.respuestas["cierre"] = ("bloqueado", 0.9)
        contexto = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        self.assertIn(
            "Jev cierre: "
            + ids["implementer"]
            + " (implementer, blocked) bloqueado (0.90)",
            contexto,
        )
        ficha = next(s for s, p, _ in jev.llamadas if "cierre" in p)
        self.assertIn("falta acceso al repo", ficha)

    def test_una_etapa_inicial_completada_tambien_cierra(self):
        # scout termina con su resultado en tasks.result y corrida con
        # metadata: el reporte sale del esquema real, no de columnas a mano.
        ids = self.kanban.cadena(etapas=(("scout", "running"),))
        auto = self.auto()
        self.primer_turno(auto)
        self.kanban.completar(
            ids["scout"],
            resultado="relev listo",
            metadata={"goncloud_local": {"terminal": "handoff del scout"}},
        )
        contexto = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        self.assertIn("Jev cierre: " + ids["scout"], contexto)
        # El reporte del esquema real viaja en la ficha a JEV, no en la linea.
        ficha = next(s for s, p, _ in self.jev.llamadas if "cierre" in p)
        self.assertIn("relev listo", ficha)
        self.assertIn("handoff del scout", ficha)

    def test_lo_que_no_es_especialista_ni_etapa_se_ignora(self):
        auto = self.auto()
        self.primer_turno(auto)
        self.kanban.tarea("t_x", "scout", "done")  # sin cadena Goncloud
        self.kanban.evento("t_x", "completed")
        contexto = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        self.assertNotIn("Jev cierre", contexto)
        self.assertEqual(self.jev.preguntas().count("cierre"), 0)

    def test_bajo_el_umbral_el_cierre_deja_decidir_a_claw(self):
        self.jev.respuestas["cierre"] = ("corregir", 0.6)
        auto = self.auto()
        self.primer_turno(auto)
        self.kanban.tarea("t_m", "meli", "done")
        self.kanban.evento("t_m", "completed")
        contexto = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        self.assertIn(
            "Jev cierre: t_m (meli, completed) corregir (0.60), bajo el umbral 0.85: decide tú",
            contexto,
        )

    def test_a_lo_mucho_tres_cierres_por_turno_y_el_resto_sale_despues(self):
        auto = self.auto()
        self.primer_turno(auto)
        for n in range(5):
            self.kanban.tarea(f"t_{n}", "odoo", "done")
            self.kanban.evento(f"t_{n}", "completed")
        primero = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        segundo = auto.pre_llm_call(**turno(turn_id="t2"))["context"]
        self.assertEqual(primero.count("Jev cierre"), 3)
        self.assertEqual(segundo.count("Jev cierre"), 2)
        for n in range(5):
            self.assertEqual((primero + segundo).count(f"Jev cierre: t_{n} "), 1)

    def test_con_jev_caido_el_turno_sigue_sin_linea_de_cierre(self):
        auto = self.auto(jev_apoyo.JevFalso(falla=RuntimeError("caido")))
        self.primer_turno(auto)
        self.kanban.tarea("t_m", "meli", "done")
        self.kanban.evento("t_m", "completed")
        contexto = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        self.assertNotIn("Jev cierre", contexto)
        self.assertIn("JEV skill_select", contexto)

    def test_sin_kanban_el_turno_sigue(self):
        auto = self.auto(kanban=self.raiz / "no-existe.db")
        contexto = auto.pre_llm_call(**turno())["context"]
        self.assertIn("Jev sugiere: amazon", contexto)

    def test_el_kanban_se_busca_en_la_raiz_cuando_no_se_inyecta(self):
        auto = self.auto(kanban=None)
        self.primer_turno(auto)
        self.assertTrue((self.claw / "jev" / "cierre-cursor.json").is_file())
        self.kanban.tarea("t_p", "pers", "done")
        self.kanban.evento("t_p", "completed")
        contexto = auto.pre_llm_call(**turno(turn_id="t1"))["context"]
        self.assertIn("Jev cierre: t_p (pers, completed)", contexto)


class RegistrarTests(Base):
    def test_register_cablea_el_decisor_solo_para_el_director(self):
        class Ctx:
            def __init__(self, perfil):
                self.profile_name = perfil
                self.hooks = {}

            def register_hook(self, nombre, funcion):
                self.hooks[nombre] = funcion

        director, otro = Ctx("claw"), Ctx("scout")
        jev_auto.registrar(director)
        jev_auto.registrar(otro)
        self.assertEqual(
            sorted(director.hooks), ["pre_llm_call", "transform_llm_output"]
        )
        self.assertIsNotNone(director.hooks["pre_llm_call"].__self__.decisor)
        self.assertIsNone(otro.hooks["pre_llm_call"].__self__.decisor)


if __name__ == "__main__":
    unittest.main()
