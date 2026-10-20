"""Respuesta rapida de "como va" sin LLM (piloto de JEV como recepcionista).

Criterios: las frases reales de avance se resuelven por codigo con el estado
real de las cadenas; lo que pide algo mas (o no es avance) deja correr el
turno normal; JEV con duda, caido o lento tambien; el hook es de solo lectura
y deja su registro; el interruptor lo apaga.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_apoyo  # noqa: E402

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

import jev_auto  # noqa: E402
import jev_avance  # noqa: E402
import jev_decisiones  # noqa: E402
from subscriptions import state as router_state  # noqa: E402

CONTRATO = "GONCLOUD_ENGINEERING_V1\n"
AVANCE_REAL = (
    "Como va?",
    "Cómo va ?",
    "Sigue?",
    "Ya quedó todo?",
    "En qué va?",
    "como va la cadena t_abc123?",
)
LARGA = "Cómo va? Ya va más de una hora la revisión"
NO_INTERCEPTAR = (
    "Ok dale",
    "Opción A",
    "Y como arreglamos lo de los skills?",
    "A ver hazlo",
    "cierra la cadena",
    "Sigue",
    "Como va la cadena? Cierrala si ya acabo",
)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


class Base(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, temporal)
        self.raiz = Path(temporal.name)
        self.claw = self.raiz / "profiles" / "claw"
        self.claw.mkdir(parents=True)
        self.local = self.raiz / "goncloud-local"
        self.local.mkdir()
        self.db = self.raiz / "subscription-router.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(router_state._SCHEMA)
        conn.close()
        self.kanban = jev_apoyo.Kanban(self.raiz / "kanban.db")
        self.ahora = time.time()
        self.vivos: set = set()
        self.jev = jev_apoyo.JevFalso({"avance": ("avance", 0.95)})

    # --- escenario ------------------------------------------------------------

    def cadena(
        self,
        wf,
        salida,
        *,
        repo="C:/dev/Orbit",
        rol=None,
        modelo="claude-sonnet-5-5",
        minutos=14,
        fallo=None,
        vivo=True,
        edad_min=1,
        extra=None,
    ):
        marca = {"workflow_id": wf, "outcome": salida, "run_id": "r1", "pid": 4242}
        marca.update(extra or {})
        ruta = self.local / f"{wf}.json"
        ruta.write_text(json.dumps(marca), encoding="utf-8")
        mtime = self.ahora - edad_min * 60
        os.utime(ruta, (mtime, mtime))
        if vivo:
            self.vivos.add(wf)
        self.kanban.tarea(wf, "claw", body=CONTRATO + json.dumps({"repository": repo}))
        if rol:
            etapa = f"{wf}_{rol}"
            self.kanban.tarea(etapa, rol)
            conn = sqlite3.connect(self.db)
            conn.execute(
                "INSERT INTO subscription_attempts (workflow_id, stage_id, slot, attempt, "
                "adapter, model, billing_mode, mode, success, failure, status, created_at) "
                "VALUES (?, ?, 0, 1, 'claude', ?, 'subscription', 'edit', 0, ?, 'running', ?)",
                (wf, etapa, modelo, fallo, iso(self.ahora - minutos * 60)),
            )
            conn.commit()
            conn.close()

    def auto(self, jev=None, perfil="claw", **kwargs):
        decisor = jev_decisiones.Decisor(
            self.claw, llamar=jev or self.jev, tope_s=kwargs.pop("tope_s", 2.0)
        )
        avance = {
            "carpetas": [self.local],
            "db": self.db,
            "ahora": self.ahora,
            "vivo": lambda marca: marca["workflow_id"] in self.vivos,
            **kwargs.pop("avance", {}),
        }
        return jev_auto.JevAuto(
            perfil,
            hogar=self.claw,
            decisor=decisor,
            kanban=self.kanban.ruta,
            avance=avance,
            **kwargs,
        )

    def pregunta(self, auto, texto, **cambios):
        datos = {
            "session_key": "k",
            "platform": "telegram",
            "message_id": "1",
            "source": {"chat_type": "dm"},
            "text": texto,
        }
        datos.update(cambios)
        return asyncio.run(auto.post_gateway_admission(**datos))

    def registro(self):
        ruta = self.claw / "jev" / "avance_rapido.jsonl"
        if not ruta.is_file():
            return []
        return [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines()]


class ClasificacionTests(unittest.TestCase):
    def test_frases_reales_de_avance_van_por_regla(self):
        for frase in AVANCE_REAL:
            self.assertEqual(jev_avance.clasificar_texto(frase), "regla", frase)

    def test_el_mensaje_largo_con_olor_a_avance_lo_confirma_jev(self):
        self.assertEqual(jev_avance.clasificar_texto(LARGA), "consultar")

    def test_lo_que_no_es_avance_no_se_intercepta(self):
        for frase in NO_INTERCEPTAR:
            self.assertIsNone(jev_avance.clasificar_texto(frase), frase)

    def test_es_corto_no_la_salta(self):
        # 'Como va?' mide 8 caracteres (< MIN_CHARS): la regla va antes del corte.
        self.assertTrue(jev_auto.jev_texto.es_corto("Como va?"))
        self.assertEqual(jev_avance.clasificar_texto("Como va?"), "regla")


class HookTests(Base):
    def test_cada_frase_de_avance_responde_sin_llm_y_sin_consultar_a_jev(self):
        self.cadena("t_a1", "running", rol="implementer")
        auto = self.auto()
        for frase in AVANCE_REAL:
            respuesta = self.pregunta(auto, frase)
            self.assertEqual(respuesta["action"], "handled", frase)
            self.assertIn("Orbit", respuesta["reply"])
        self.assertEqual(self.jev.llamadas, [])  # la regla no gasta consulta

    def test_mensaje_largo_se_confirma_con_jev_y_responde(self):
        self.cadena("t_a1", "running", rol="reviewer")
        respuesta = self.pregunta(self.auto(), LARGA)
        self.assertEqual(respuesta["action"], "handled")
        self.assertEqual(self.jev.preguntas(), ["avance"])
        self.assertIn("JEV: respuesta rápida (confianza 0.95)", respuesta["reply"])

    def test_lo_que_no_se_intercepta_corre_el_turno_normal(self):
        self.cadena("t_a1", "running", rol="implementer")
        auto = self.auto()
        for frase in NO_INTERCEPTAR:
            self.assertIsNone(self.pregunta(auto, frase), frase)
        self.assertEqual(self.jev.llamadas, [])
        self.assertEqual(self.registro(), [])

    def test_jev_con_duda_o_otra_cosa_deja_correr_el_llm(self):
        for opcion, confianza in (("avance", 0.84), ("otra", 0.99)):
            jev = jev_apoyo.JevFalso({"avance": (opcion, confianza)})
            self.assertIsNone(self.pregunta(self.auto(jev), LARGA), (opcion, confianza))
        self.assertEqual({x["motivo"] for x in self.registro()}, {"duda"})

    def test_jev_caido_o_lento_deja_correr_el_llm(self):
        caido = jev_apoyo.JevFalso(falla=RuntimeError("TypeSafe API unreachable"))
        self.assertIsNone(self.pregunta(self.auto(caido), LARGA))

        class Lento(jev_apoyo.JevFalso):
            def __call__(self, *args):
                time.sleep(1.0)
                return super().__call__(*args)

        lento = Lento({"avance": ("avance", 0.99)})
        self.assertIsNone(self.pregunta(self.auto(lento, tope_s=0.2), LARGA))
        motivos = [x["motivo"] for x in self.registro()]
        self.assertTrue(motivos[0].startswith("jev:"))
        self.assertTrue(all(x["resuelto"] is False for x in self.registro()))

    def test_el_tope_total_vence_aunque_la_lectura_se_cuelgue(self):
        self.cadena("t_a1", "running", rol="implementer")
        original = jev_auto.TOPE_AVANCE_S
        jev_auto.TOPE_AVANCE_S = 0.2
        self.addCleanup(setattr, jev_auto, "TOPE_AVANCE_S", original)
        auto = self.auto(avance={"vivo": lambda marca: time.sleep(1.0) or True})
        self.assertIsNone(self.pregunta(auto, "Como va?"))
        self.assertEqual(self.registro()[-1]["motivo"], "timeout")

    def test_solo_el_director_en_telegram_dm(self):
        self.cadena("t_a1", "running", rol="implementer")
        self.assertIsNone(self.pregunta(self.auto(perfil="scout"), "Como va?"))
        auto = self.auto()
        self.assertIsNone(self.pregunta(auto, "Como va?", platform="discord"))
        self.assertIsNone(
            self.pregunta(auto, "Como va?", source={"chat_type": "group"})
        )
        self.assertIsNone(self.pregunta(auto, ""))
        self.assertIsNotNone(self.pregunta(auto, "Como va?"))

    def test_interruptor_de_config_y_de_entorno(self):
        self.cadena("t_a1", "running", rol="implementer")
        self.assertIsNone(self.pregunta(self.auto(avance_rapido=False), "Como va?"))
        previo = os.environ.get(jev_auto.VAR_AVANCE)
        self.addCleanup(
            lambda: (
                os.environ.pop(jev_auto.VAR_AVANCE, None)
                if previo is None
                else os.environ.__setitem__(jev_auto.VAR_AVANCE, previo)
            )
        )
        os.environ[jev_auto.VAR_AVANCE] = "off"
        self.assertIsNone(self.pregunta(self.auto(), "Como va?"))
        os.environ[jev_auto.VAR_AVANCE] = "on"
        self.assertIsNotNone(self.pregunta(self.auto(), "Como va?"))

    def test_registro_cuenta_resueltos_con_ms_y_sin_el_mensaje(self):
        self.cadena("t_a1", "running", rol="implementer")
        self.pregunta(self.auto(), "Como va?")
        linea = self.registro()[0]
        self.assertTrue(linea["resuelto"])
        self.assertEqual(
            (linea["via"], linea["confianza"], linea["cadenas"]), ("regla", 1.0, 1)
        )
        self.assertIsInstance(linea["ms"], int)
        self.assertEqual(
            linea["msg_sha256"], hashlib.sha256(b"Como va?").hexdigest()[:12]
        )
        self.assertNotIn("Como va", json.dumps(linea))

    def test_es_de_solo_lectura(self):
        self.cadena("t_a1", "running", rol="implementer")
        self.cadena("t_a2", "delivered", edad_min=35)
        antes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.db, self.kanban.ruta, *self.local.glob("*.json"))
        }
        archivos = sorted(p.name for p in self.local.iterdir())
        self.pregunta(self.auto(), "Como va?")
        despues = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.db, self.kanban.ruta, *self.local.glob("*.json"))
        }
        self.assertEqual(antes, despues)
        self.assertEqual(archivos, sorted(p.name for p in self.local.iterdir()))


class RespuestaTests(Base):
    def texto(self):
        return self.pregunta(self.auto(), "Como va?")["reply"]

    def test_sin_cadenas_lo_dice(self):
        reply = self.texto()
        self.assertIn("No hay cadenas activas", reply)
        self.assertIn("JEV: respuesta rápida (confianza 1.00)", reply)

    def test_running_dice_repo_etapa_modelo_minutos_y_proceso_vivo(self):
        self.cadena("t_a1", "running", rol="implementer", minutos=14)
        reply = self.texto()
        for esperado in (
            "Orbit",
            "implementación",
            "claude-sonnet-5-5",
            "14 min",
            "sigue vivo",
        ):
            self.assertIn(esperado, reply)

    def test_running_con_driver_muerto_se_dice_trabada(self):
        self.cadena(
            "t_a1", "running", rol="reviewer", vivo=False, fallo="el CLI se cerró"
        )
        reply = self.texto()
        self.assertIn("trabada", reply)
        self.assertIn("ya no está vivo", reply)
        self.assertIn("el CLI se cerró", reply)

    def test_blocked_trae_el_motivo_en_una_linea(self):
        self.cadena(
            "t_a1", "blocked", rol="verifier", fallo="falló la\nprueba de login"
        )
        reply = self.texto()
        self.assertIn("trabada en la etapa de verificación", reply)
        self.assertIn("Motivo: falló la prueba de login.", reply)

    def test_delivered_reciente_y_lo_viejo_no_sale(self):
        self.cadena("t_a1", "delivered", edad_min=35)
        self.cadena("t_viejo", "delivered", edad_min=13 * 60)
        reply = self.texto()
        self.assertIn("entregada hace 35 min", reply)
        self.assertNotIn("t_viejo", reply)

    def test_orden_activas_primero_y_tope(self):
        self.cadena("t_e", "delivered", repo="C:/x/Entregada", edad_min=5)
        for i in range(6):
            self.cadena(
                f"t_r{i}", "running", repo=f"C:/x/Repo{i}", rol="scout", edad_min=i
            )
        reply = self.texto()
        self.assertLess(reply.index("Repo0"), reply.index("Repo1"))
        self.assertNotIn("Entregada", reply)
        self.assertIn("y 2 más", reply)

    def test_sin_kanban_ni_router_usa_el_id_y_no_inventa(self):
        self.cadena("t_a1", "running")
        auto = self.auto(avance={"db": self.raiz / "no-existe.db"})
        auto.kanban = self.raiz / "no-existe-kanban.db"
        reply = self.pregunta(auto, "Como va?")["reply"]
        self.assertIn("cadena t_a1", reply)
        self.assertIn("una etapa", reply)

    def test_vivo_no_comprobable_no_se_inventa(self):
        self.cadena("t_a1", "running", rol="scout")

        def roto(marca):
            raise OSError("sin permiso")

        reply = self.pregunta(self.auto(avance={"vivo": roto}), "Como va?")["reply"]
        self.assertIn("No pude comprobar", reply)


class RegistroTests(unittest.TestCase):
    def test_el_gancho_se_registra_y_la_config_lo_apaga(self):
        class Ctx:
            def __init__(self, config=None):
                self.profile_name = "claw"
                self.plugin_config = config
                self.ganchos = {}

            def register_hook(self, nombre, funcion):
                self.ganchos[nombre] = funcion

        ctx = Ctx()
        auto = jev_auto.registrar(ctx)
        self.assertIn("post_gateway_admission", ctx.ganchos)
        self.assertTrue(
            inspect.iscoroutinefunction(ctx.ganchos["post_gateway_admission"])
        )
        self.assertIsNone(auto.avance_rapido)
        apagado = jev_auto.registrar(Ctx({"jev_avance_rapido": "off"}))
        self.assertFalse(apagado.avance_rapido)


if __name__ == "__main__":
    unittest.main()
