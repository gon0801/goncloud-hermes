import contextlib
import dataclasses
from datetime import datetime
import io
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zoneinfo
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vigila_tareas as module  # noqa: E402

DATOS = ROOT / "tests" / "datos" / "vigia"

CAMPOS_LAUNCHD = {
    "label",
    "periodico",
    "keepalive",
    "cargado",
    "state",
    "runs",
    "last_exit_code",
    "rutas_que_faltan",
}
PREFIJOS = ("ai.goncloud.", "cc.goncloud.", "com.goncloud.", "claw-", "ai.openclaw.")
CLAVES_PROHIBIDAS = {
    "message",
    "prompt",
    "env",
    "argv",
    "token",
    "toolsAllow",
    "origin",
}
SECRETO = re.compile(
    r"sk-[A-Za-z0-9]{8}|ghp_|xox[bp]-|Bearer |\d{9,}:[\w-]{30,}|[\w.+-]+@[\w-]+\.\w+|6470689715"
)


def carga(nombre):
    return json.loads((DATOS / nombre).read_text(encoding="utf-8"))


def claves(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from claves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from claves(v)


class TestDatosLimpios(unittest.TestCase):
    def test_launchd_de_mac_y_mini_traen_los_campos_y_solo_los_prefijos(self):
        for nombre in ("mac-launchd.json", "mini-launchd.json"):
            filas = carga(nombre)
            self.assertTrue(filas, nombre)
            for fila in filas:
                self.assertEqual(set(fila), CAMPOS_LAUNCHD, nombre)
                self.assertTrue(fila["label"].startswith(PREFIJOS), fila["label"])
                self.assertIsInstance(fila["rutas_que_faltan"], list)

    def test_el_cron_de_la_mini_trae_todas_las_paginas(self):
        datos = carga("mini-cron.json")
        self.assertEqual(len(datos["jobs"]), datos["total"])
        self.assertGreater(datos["total"], 25)

    def test_los_trabajos_de_la_pc_traen_su_ultima_salida(self):
        for job in carga("pc-hermes-claw-jobs.json")["jobs"]:
            self.assertTrue((DATOS / job["ultima_salida"]).is_file(), job["name"])

    def test_no_queda_ningun_prompt_ni_secreto(self):
        archivos = [p for p in DATOS.rglob("*") if p.is_file()]
        self.assertGreaterEqual(len(archivos), 6)
        for ruta in archivos:
            texto = ruta.read_text(encoding="utf-8")
            self.assertIsNone(SECRETO.search(texto), ruta.name)
            if ruta.suffix == ".json":
                self.assertFalse(
                    CLAVES_PROHIBIDAS & set(claves(json.loads(texto))), ruta.name
                )
            if ruta.suffix == ".md" and ruta.name != "LEEME.md":
                self.assertNotIn("## 0. ", texto, ruta.name)

    def test_el_leeme_nombra_cada_archivo(self):
        leeme = (DATOS / "LEEME.md").read_text(encoding="utf-8")
        self.assertIn("2026-10-22", leeme)
        for ruta in DATOS.glob("*.json"):
            self.assertIn(ruta.name, leeme)


class TestPolitica(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT / "tareas-programadas.json").read_text("utf-8"))

    def carga(self, datos):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "tareas-programadas.json"
            ruta.write_text(json.dumps(datos), encoding="utf-8")
            return module.Politica.carga(ruta)

    def test_la_politica_del_repo_carga(self):
        pol = module.Politica.carga(ROOT / "tareas-programadas.json")
        self.assertEqual(pol.ventana.zona, "America/Los_Angeles")
        self.assertEqual((pol.ventana.desde, pol.ventana.hasta), ("03:45", "05:10"))
        self.assertEqual((pol.claw_rss_mb, pol.chrome_perfil_mb), (3584, 3072))
        self.assertEqual(pol.marca_fallo, "FALLO:")
        self.assertIn("ai.goncloud.vigila-modelos", pol.salidas_ok)
        self.assertEqual(len(pol.modelo_propio), 5)

    def test_una_clave_de_mas_falla_y_nombra_la_clave(self):
        for ruta, clave in ((None, "intrusa"), ("limites", "limites.intrusa")):
            datos = json.loads(json.dumps(self.base))
            (datos if ruta is None else datos[ruta])["intrusa"] = 1
            with self.assertRaisesRegex(module.PoliticaError, clave):
                self.carga(datos)

    def test_un_tipo_equivocado_falla_y_nombra_la_clave(self):
        casos = (
            ("limites", "claw_rss_mb", "3584", "limites.claw_rss_mb"),
            ("limites", "chrome_perfil_mb", True, "limites.chrome_perfil_mb"),
            ("ventana_reinicio", "desde", "25:99", "ventana_reinicio.desde"),
            ("launchd", "prefijos", "ai.", "launchd.prefijos"),
            (None, "marca_fallo", 1, "marca_fallo"),
        )
        for padre, clave, valor, esperada in casos:
            datos = json.loads(json.dumps(self.base))
            (datos if padre is None else datos[padre])[clave] = valor
            with self.assertRaisesRegex(module.PoliticaError, esperada):
                self.carga(datos)

    def test_zona_desconocida_o_ventana_al_reves_fallan(self):
        for clave, valor, esperada in (
            ("zona", "America/Nowhere", "ventana_reinicio.zona"),
            ("zona", "America/", "ventana_reinicio.zona"),
            ("desde", "05:10", "ventana_reinicio.desde"),
        ):
            datos = json.loads(json.dumps(self.base))
            datos["ventana_reinicio"][clave] = valor
            with self.assertRaisesRegex(module.PoliticaError, esperada):
                self.carga(datos)

    def test_el_repaso_nocturno_de_memoria_va_a_sonnet(self):
        pol = module.Politica.carga(ROOT / "tareas-programadas.json")
        job = {
            "name": "Memory Dreaming Promotion",
            "enabled": True,
            "payload": {"model": "anthropic/claude-sonnet-5-5"},
        }
        self.assertEqual(module.juzga_claw(job, pol, 0), [])

    def test_apagadas_a_proposito_lleva_la_entrada_decidida_y_se_valida_como_las_demas(
        self,
    ):
        self.assertEqual(
            list(self.base["apagadas_a_proposito"]),
            ["ai.goncloud.tmux-activity-watch"],
        )
        for valor in ([], {"a": 1}, {"a": ""}):
            datos = json.loads(json.dumps(self.base))
            datos["apagadas_a_proposito"] = valor
            with self.assertRaisesRegex(module.PoliticaError, "apagadas_a_proposito"):
                self.carga(datos)

    def test_una_clave_desconocida_sigue_siendo_error(self):
        self.base["otra"] = {}
        with self.assertRaisesRegex(module.PoliticaError, "clave desconocida: otra"):
            self.carga(self.base)

    def test_una_clave_que_falta_falla(self):
        del self.base["marca_fallo"]
        with self.assertRaisesRegex(module.PoliticaError, "marca_fallo"):
            self.carga(self.base)


AHORA_MS = 1792724232000
MIN_MS = 60 * 1000


def lee_texto(nombre):
    return (DATOS / "pc-claw-salidas" / nombre).read_text(encoding="utf-8")


def politica():
    return module.Politica.carga(ROOT / "tareas-programadas.json")


def fila_launchd(**cambios):
    fila = {
        "label": "ai.goncloud.prueba",
        "periodico": True,
        "keepalive": False,
        "cargado": True,
        "state": "not running",
        "runs": 3,
        "last_exit_code": 0,
        "rutas_que_faltan": [],
    }
    return {**fila, **cambios}


def job_claw(**cambios):
    job = {
        "name": "prueba",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 12 * * *", "tz": "America/Mexico_City"},
        "payload": {"kind": "agentTurn"},
        "lastRunStatus": "ok",
        "nextRunAtMs": AHORA_MS + 60 * MIN_MS,
    }
    return {**job, **cambios}


def job_hermes(**cambios):
    job = {
        "name": "prueba",
        "enabled": True,
        "no_agent": False,
        "model": None,
        "last_status": "ok",
        "failure_streak": 0,
        "last_delivery_error": None,
        "next_run_at": "2026-10-23T22:30:00-07:00",
    }
    return {**job, **cambios}


def ms_la(texto):
    local = datetime.fromisoformat(texto).replace(
        tzinfo=ZoneInfo("America/Los_Angeles")
    )
    return int(local.timestamp() * 1000)


class TestJueces(unittest.TestCase):
    def setUp(self):
        self.pol = politica()

    def test_la_falla_lleva_urgencia_y_frase(self):
        urgentes = {"NO_CORRE", "TERMINO_MAL", "FALLO_DECLARADO"}
        for falla in module.Falla:
            self.assertEqual(falla.urgente, falla.name in urgentes)
        self.assertEqual(module.Falla.NO_CORRE.frase, "no está corriendo")
        self.assertEqual(
            module.Falla.FALLO_DECLARADO.frase,
            "avisó que algo falló y quedó marcada como buena",
        )

    def test_launchd_real_cargado_y_con_salida_buena_no_avisa(self):
        filas = {f["label"]: f for f in carga("mini-launchd.json")}
        for label in ("ai.goncloud.respaldo-completo", "ai.openclaw.gateway"):
            self.assertEqual(module.juzga_launchd(filas[label], self.pol), [], label)

    def test_launchd_real_que_no_esta_cargado_no_corre(self):
        filas = {f["label"]: f for f in carga("mini-launchd.json")}
        fila = filas["ai.goncloud.claw-autoupdate"]
        self.assertFalse(fila["cargado"])
        self.assertEqual(module.juzga_launchd(fila, self.pol), [module.Falla.NO_CORRE])

    def test_launchd_ruta_que_falta_no_corre_y_el_caso_shopify_del_22_oct(self):
        fila = fila_launchd(
            label="ai.goncloud.shopify-token",
            rutas_que_faltan=["/Users/gon/scripts/renueva-shopify.sh"],
            last_exit_code=127,
        )
        fallas = module.juzga_launchd(fila, self.pol)
        self.assertIn(module.Falla.NO_CORRE, fallas)
        self.assertIn(module.Falla.TERMINO_MAL, fallas)
        self.assertEqual(module.juzga_launchd(fila_launchd(), self.pol), [])

    def test_launchd_keepalive_que_no_corre_no_corre(self):
        caido = fila_launchd(keepalive=True, periodico=False, state="not running")
        vivo = fila_launchd(keepalive=True, periodico=False, state="running")
        self.assertEqual(module.juzga_launchd(caido, self.pol), [module.Falla.NO_CORRE])
        self.assertEqual(module.juzga_launchd(vivo, self.pol), [])

    def test_launchd_periodico_con_salida_mala_termino_mal(self):
        mala = fila_launchd(last_exit_code=1)
        self.assertEqual(
            module.juzga_launchd(mala, self.pol), [module.Falla.TERMINO_MAL]
        )
        nunca = fila_launchd(last_exit_code=None, runs=0)
        self.assertEqual(module.juzga_launchd(nunca, self.pol), [])
        no_periodico = fila_launchd(periodico=False, last_exit_code=1)
        self.assertEqual(module.juzga_launchd(no_periodico, self.pol), [])

    def test_launchd_salida_ok_declarada_no_avisa(self):
        propio = fila_launchd(label="ai.goncloud.vigila-modelos", last_exit_code=1)
        self.assertEqual(module.juzga_launchd(propio, self.pol), [])
        otro = fila_launchd(label="ai.goncloud.otra", last_exit_code=1)
        self.assertEqual(
            module.juzga_launchd(otro, self.pol), [module.Falla.TERMINO_MAL]
        )

    def test_launchd_real_de_la_mac_no_levanta_nada_con_la_apagada_en_la_lista(self):
        halladas = {
            f["label"]: module.juzga_launchd(f, self.pol)
            for f in carga("mac-launchd.json")
        }
        self.assertEqual(
            {n: f for n, f in halladas.items() if f},
            {},
        )
        sin_lista = dataclasses.replace(self.pol, apagadas_a_proposito={})
        self.assertEqual(
            [
                f["label"]
                for f in carga("mac-launchd.json")
                if module.juzga_launchd(f, sin_lista)
            ],
            ["ai.goncloud.tmux-activity-watch"],
        )

    def test_claw_con_ultima_corrida_en_error_termino_mal(self):
        job = {j["name"]: j for j in carga("mini-cron.json")["jobs"]}[
            "skill-collection-review-main"
        ]
        self.assertEqual(
            module.juzga_claw(job, self.pol, AHORA_MS), [module.Falla.TERMINO_MAL]
        )
        self.assertEqual(module.juzga_claw(job_claw(), self.pol, AHORA_MS), [])

    def test_claw_apagado_no_avisa_aunque_haya_fallado(self):
        job = job_claw(enabled=False, lastRunStatus="error", nextRunAtMs=1)
        self.assertEqual(module.juzga_claw(job, self.pol, AHORA_MS), [])

    def test_claw_con_proxima_corrida_vencida_hace_mas_de_90_min_no_corre(self):
        vencido = job_claw(nextRunAtMs=AHORA_MS - 91 * MIN_MS)
        puntual = job_claw(nextRunAtMs=AHORA_MS - 89 * MIN_MS)
        self.assertEqual(
            module.juzga_claw(vencido, self.pol, AHORA_MS), [module.Falla.NO_CORRE]
        )
        self.assertEqual(module.juzga_claw(puntual, self.pol, AHORA_MS), [])

    def test_claw_modelo_distinto_o_fuera_de_la_politica(self):
        acordado = job_claw(
            name="cuotas-proveedores",
            payload={"model": "anthropic/claude-haiku-5-5"},
        )
        self.assertEqual(module.juzga_claw(acordado, self.pol, AHORA_MS), [])
        distinto = job_claw(name="cuotas-proveedores", payload={"model": "zai/glm-5.3"})
        self.assertEqual(
            module.juzga_claw(distinto, self.pol, AHORA_MS),
            [module.Falla.MODELO_PROPIO],
        )
        nuevo = job_claw(name="nueva", payload={"model": "anthropic/claude-haiku-5-5"})
        self.assertEqual(
            module.juzga_claw(nuevo, self.pol, AHORA_MS), [module.Falla.MODELO_PROPIO]
        )

    def test_claw_en_la_ventana_de_reinicio_solo_si_no_es_every(self):
        en_ventana = ms_la("2026-10-23T04:00")
        cron = job_claw(nextRunAtMs=en_ventana)
        self.assertEqual(
            module.juzga_claw(cron, self.pol, AHORA_MS), [module.Falla.EN_VENTANA]
        )
        cada = job_claw(
            nextRunAtMs=en_ventana, schedule={"kind": "every", "everyMs": 1}
        )
        self.assertEqual(module.juzga_claw(cada, self.pol, AHORA_MS), [])
        for borde in ("2026-10-23T03:45", "2026-10-23T05:10"):
            job = job_claw(nextRunAtMs=ms_la(borde))
            self.assertEqual(
                module.juzga_claw(job, self.pol, AHORA_MS),
                [module.Falla.EN_VENTANA],
                borde,
            )
        for fuera in ("2026-10-23T05:11", "2026-10-23T03:44"):
            job = job_claw(nextRunAtMs=ms_la(fuera))
            self.assertEqual(module.juzga_claw(job, self.pol, AHORA_MS), [], fuera)

    def test_una_excepcion_de_ventana_no_levanta_en_ventana(self):
        en_ventana = ms_la("2026-10-23T04:00")
        exceptuada = job_claw(
            name="skill-collection-review-main", nextRunAtMs=en_ventana
        )
        self.assertEqual(module.juzga_claw(exceptuada, self.pol, AHORA_MS), [])
        otra = job_claw(name="otra", nextRunAtMs=en_ventana)
        self.assertEqual(
            module.juzga_claw(otra, self.pol, AHORA_MS), [module.Falla.EN_VENTANA]
        )

    def test_el_cron_real_de_la_mini_solo_levanta_lo_que_se_espera(self):
        halladas = {
            j["name"]: module.juzga_claw(j, self.pol, AHORA_MS)
            for j in carga("mini-cron.json")["jobs"]
        }
        self.assertEqual(
            {n: f for n, f in halladas.items() if f},
            {"skill-collection-review-main": [module.Falla.TERMINO_MAL]},
        )

    def test_hermes_con_estado_no_ok_racha_o_error_de_entrega_termino_mal(self):
        self.assertEqual(
            module.juzga_hermes("claw", job_hermes(), self.pol, AHORA_MS), []
        )
        for cambio in (
            {"last_status": "error"},
            {"failure_streak": 2},
            {"last_delivery_error": "no llegó"},
        ):
            self.assertEqual(
                module.juzga_hermes("claw", job_hermes(**cambio), self.pol, AHORA_MS),
                [module.Falla.TERMINO_MAL],
                cambio,
            )

    def test_hermes_pausado_o_que_nunca_corrio_no_avisa(self):
        pausado = job_hermes(enabled=False, last_status="error", failure_streak=4)
        self.assertEqual(module.juzga_hermes("claw", pausado, self.pol, AHORA_MS), [])
        nuevo = job_hermes(last_status=None)
        self.assertEqual(module.juzga_hermes("claw", nuevo, self.pol, AHORA_MS), [])

    def test_respuesta_final_es_solo_lo_que_sigue_al_ultimo_response(self):
        salida = lee_texto("monday-pedidos-semanal.md")
        self.assertTrue(module.respuesta_final(salida).startswith("FALLO: paso 3"))
        self.assertEqual(module.respuesta_final(lee_texto("cron-vigilante.md")), "")

    def test_hermes_fallo_declarado_con_la_salida_real_del_22_oct(self):
        jobs = {j["name"]: j for j in carga("pc-hermes-claw-jobs.json")["jobs"]}
        job = jobs["monday-pedidos-semanal"]
        self.assertEqual(job["last_status"], "ok")
        fallas = module.juzga_hermes(
            "claw", job, self.pol, AHORA_MS, lee_texto("monday-pedidos-semanal.md")
        )
        self.assertEqual(fallas, [module.Falla.FALLO_DECLARADO])

    def test_el_prompt_con_la_marca_no_cuenta_como_fallo(self):
        salida = (
            "# Cron Job: x\n## Prompt\n\nSi algo sale mal responde FALLO: así\n"
            "FALLO: ejemplo del prompt\n\n## Response\n\nTodo salió bien.\n"
        )
        self.assertEqual(
            module.juzga_hermes("claw", job_hermes(), self.pol, AHORA_MS, salida), []
        )

    def test_una_respuesta_que_menciona_la_marca_a_media_frase_no_cuenta(self):
        salida = "## Prompt\n\nx\n\n## Response\n\nNo hubo ningún FALLO: todo bien.\n"
        self.assertEqual(
            module.juzga_hermes("claw", job_hermes(), self.pol, AHORA_MS, salida), []
        )

    def test_la_respuesta_final_es_lo_que_sigue_al_ultimo_response(self):
        salida = (
            "## Prompt\n\nx\n\n## Response\n\ncita\n\n## Response\n\nFALLO: de verdad\n"
        )
        self.assertEqual(module.respuesta_final(salida), "FALLO: de verdad")

    def test_claw_con_payload_o_schedule_nulos_no_truena(self):
        job = job_claw(payload=None, schedule=None)
        self.assertEqual(module.juzga_claw(job, self.pol, AHORA_MS), [])

    def test_hermes_sin_agente_no_declara_fallo(self):
        salida = "# Cron Job: x\n\n## Response\n\nFALLO: el script lo dijo\n"
        job = job_hermes(no_agent=True)
        self.assertEqual(
            module.juzga_hermes("claw", job, self.pol, AHORA_MS, salida), []
        )

    def test_hermes_no_corre_y_modelo_propio_igual_que_claw(self):
        vencido = job_hermes(next_run_at="2026-10-22T17:00:00-07:00")
        self.assertEqual(
            module.juzga_hermes("claw", vencido, self.pol, AHORA_MS),
            [module.Falla.NO_CORRE],
        )
        nombre = "monday-pedidos-semanal"
        propio = job_hermes(name=nombre, model="claude-sonnet-5-5")
        self.assertEqual(module.juzga_hermes("claw", propio, self.pol, AHORA_MS), [])
        for malo in (
            job_hermes(name=nombre, model="claude-opus-5-5"),
            job_hermes(name="otra", model="claude-sonnet-5-5"),
        ):
            self.assertEqual(
                module.juzga_hermes("claw", malo, self.pol, AHORA_MS),
                [module.Falla.MODELO_PROPIO],
            )

    def test_los_trabajos_reales_de_la_pc_solo_levantan_el_fallo_declarado(self):
        halladas = {}
        for job in carga("pc-hermes-claw-jobs.json")["jobs"]:
            salida = lee_texto(Path(job["ultima_salida"]).name)
            fallas = module.juzga_hermes("claw", job, self.pol, AHORA_MS, salida)
            if fallas:
                halladas[job["name"]] = fallas
        self.assertEqual(
            halladas, {"monday-pedidos-semanal": [module.Falla.FALLO_DECLARADO]}
        )

    def test_memoria_de_claw_con_el_log_real(self):
        log = (DATOS / "mini-gateway-memoria.txt").read_text(encoding="utf-8")
        self.assertAlmostEqual(module.memoria_alta_mb(log, AHORA_MS), 4413.44, places=1)
        despues = AHORA_MS + 5 * 3600 * 1000
        self.assertAlmostEqual(module.memoria_alta_mb(log, despues), 2252.8, places=1)
        self.assertEqual(module.memoria_alta_mb("", AHORA_MS), 0)

    def test_limites_de_memoria_y_de_perfil(self):
        self.assertEqual(
            module.juzga_limites(4413, 1182, self.pol), [module.Falla.PESA_DEMASIADO]
        )
        self.assertEqual(
            module.juzga_limites(1000, 3073, self.pol), [module.Falla.PESA_DEMASIADO]
        )
        self.assertEqual(module.juzga_limites(3584, 3072, self.pol), [])
        perfil = carga("mini-chrome-perfil.json")["chrome_perfil_mb"]
        self.assertEqual(module.juzga_limites(2252, perfil, self.pol), [])


def hallazgo(tarea, falla, maquina="mini", detalle=""):
    return module.Hallazgo(maquina, tarea, falla, detalle)


class TestAviso(unittest.TestCase):
    def setUp(self):
        self.pol = politica()

    def aviso(self, hallazgos, sin_leer=(), manana=True):
        return module.texto_aviso(hallazgos, list(sin_leer), self.pol, manana)

    def test_sin_nada_que_decir_devuelve_none(self):
        self.assertIsNone(self.aviso([]))
        solo_no_urgente = [hallazgo("a", module.Falla.EN_VENTANA)]
        self.assertIsNone(self.aviso(solo_no_urgente, manana=False))

    def test_una_tarea_lleva_su_nombre_llano_y_la_frase(self):
        texto = self.aviso(
            [hallazgo("ai.goncloud.shopify-token", module.Falla.NO_CORRE)]
        )
        self.assertEqual(
            texto,
            "En la Mini · Tareas programadas:\n"
            "• La renovación del token de Shopify: no está corriendo.\n"
            "Pídele a tu asistente que lo revise; lo demás sigue trabajando.",
        )

    def test_encabezado_por_maquina(self):
        for maquina, nombre in (
            ("mac", "la Mac"),
            ("mini", "la Mini"),
            ("pc", "la PC"),
        ):
            texto = self.aviso([hallazgo("x", module.Falla.NO_CORRE, maquina)])
            self.assertTrue(texto.startswith(f"En {nombre} · Tareas programadas:"))

    def test_dos_o_tres_tareas_se_juntan_y_mas_de_tres_se_cuentan(self):
        dos = [hallazgo(n, module.Falla.TERMINO_MAL) for n in ("a", "b")]
        self.assertIn("• a y b: su última corrida falló.", self.aviso(dos))
        tres = [hallazgo(n, module.Falla.TERMINO_MAL) for n in ("a", "b", "c")]
        self.assertIn("• a, b y c: su última corrida falló.", self.aviso(tres))
        cuatro = [hallazgo(n, module.Falla.TERMINO_MAL) for n in "abcd"]
        texto = self.aviso(cuatro)
        self.assertIn("• 4 tareas: su última corrida falló.", texto)
        self.assertNotIn("a, b", texto)

    def test_nombres_llanos_en_lista_van_en_minuscula_y_sin_repetir(self):
        hallazgos = [
            hallazgo("ai.goncloud.shopify-token", module.Falla.NO_CORRE),
            hallazgo("ai.goncloud.respaldo-completo", module.Falla.NO_CORRE),
            hallazgo("ai.goncloud.respaldo-completo", module.Falla.NO_CORRE),
        ]
        self.assertIn(
            "• La renovación del token de Shopify y el respaldo completo de la Mini: "
            "no está corriendo.",
            self.aviso(hallazgos),
        )

    def test_las_no_urgentes_solo_entran_por_la_manana(self):
        hallazgos = [
            hallazgo("a", module.Falla.NO_CORRE),
            hallazgo("b", module.Falla.PESA_DEMASIADO),
        ]
        tarde = self.aviso(hallazgos, manana=False)
        self.assertNotIn("memoria", tarde)
        self.assertIn(
            "• b: usa más memoria o espacio de lo normal.", self.aviso(hallazgos)
        )

    def test_lo_que_no_se_pudo_leer_entra_como_no_pude_revisar(self):
        texto = self.aviso([], [module.SinLeer("pc", "los trabajos de Hermes")])
        self.assertEqual(
            texto,
            "En la PC · Tareas programadas:\n"
            "• No pude revisar los trabajos de Hermes.\n"
            "Pídele a tu asistente que lo revise; lo demás sigue trabajando.",
        )

    def test_nunca_pasa_de_ocho_lineas(self):
        hallazgos = [hallazgo(f"t{i}", falla) for i, falla in enumerate(module.Falla)]
        sin_leer = [
            module.SinLeer("mini", "el cron"),
            module.SinLeer("mini", "launchd"),
        ]
        texto = self.aviso(hallazgos, sin_leer)
        self.assertLessEqual(len(texto.splitlines()), 8)
        self.assertTrue(texto.splitlines()[-1].startswith("Pídele"))

    def test_al_recortar_se_conserva_lo_que_no_se_pudo_revisar(self):
        hallazgos = [hallazgo(f"t{i}", falla) for i, falla in enumerate(module.Falla)]
        texto = self.aviso(hallazgos, [module.SinLeer("mini", "el cron")])
        self.assertIn("No pude revisar el cron", texto)
        self.assertIn("avisos más", texto)
        self.assertLessEqual(len(texto.splitlines()), 8)

    def test_ninguna_ruta_id_ni_error_llega_al_aviso(self):
        detalle = (
            "falta /Users/gon/scripts/renueva.sh id 35798aa2-d734-4d9a-bc6a-25634eeb1a24 "
            "error: command exited with code 127"
        )
        hallazgos = [
            hallazgo("ai.goncloud.shopify-token", falla, detalle=detalle)
            for falla in module.Falla
        ]
        texto = self.aviso(hallazgos)
        for resto in ("/Users", "renueva.sh", "35798aa2", "127", "exited", "error:"):
            self.assertNotIn(resto, texto)


def lector(que, hallazgos=(), error=None):
    def leer(pol, ahora_ms):
        if error:
            raise error
        return list(hallazgos)

    return module.Lector(que, leer)


class TestMain(unittest.TestCase):
    def corre(self, lectores, *extra, raiz=ROOT, ahora=1792724232.0):
        with tempfile.TemporaryDirectory() as tmp:
            self.cola = Path(tmp) / "cola"
            self.registro = Path(tmp) / "vigia.log"
            argv = [
                "--maquina", "mini", "--raiz", str(raiz),
                "--aviso", str(self.cola), "--registro", str(self.registro),
                "--estado", str(Path(tmp) / "estado.json"),
                *extra,
            ]  # fmt: skip
            self.codigo = module.main(argv, lectores=lectores, ahora=ahora)
            self.avisos = (
                [p.read_text("utf-8") for p in sorted(self.cola.glob("*"))]
                if self.cola.exists()
                else []
            )
            self.log = (
                self.registro.read_text("utf-8") if self.registro.exists() else ""
            )

    def test_la_pc_sin_base_de_zonas_no_truena(self):
        pc = module.Lector("las tareas programadas de Hermes", lambda p, a: [])
        ZoneInfo.clear_cache()
        zoneinfo.reset_tzpath([])
        self.addCleanup(zoneinfo.reset_tzpath)
        self.addCleanup(ZoneInfo.clear_cache)
        with mock.patch.dict(sys.modules, {"tzdata": None}):
            codigo = module.main(
                ["--maquina", "pc", "--seco", "--raiz", str(ROOT)],
                lectores=[pc],
                ahora=1792724232.0,
            )
        self.assertNotEqual(codigo, 2)

    def test_sin_hallazgos_sale_0_y_no_encola(self):
        self.corre([lector("x")])
        self.assertEqual((self.codigo, self.avisos, self.log), (0, [], ""))

    def test_con_hallazgo_urgente_sale_1_encola_el_aviso_y_deja_el_detalle_en_el_registro(
        self,
    ):
        h = hallazgo(
            "ai.goncloud.shopify-token", module.Falla.NO_CORRE, detalle="falta /x/y.sh"
        )
        self.corre([lector("x", [h])])
        self.assertEqual(self.codigo, 1)
        self.assertEqual(len(self.avisos), 1)
        self.assertIn(
            "La renovación del token de Shopify: no está corriendo.", self.avisos[0]
        )
        self.assertNotIn("/x/y.sh", self.avisos[0])
        self.assertIn("falta /x/y.sh", self.log)

    def test_seco_imprime_el_detalle_no_encola_y_sale_igual(self):
        h = hallazgo("a", module.Falla.TERMINO_MAL, detalle="detalle-visible")
        with contextlib.redirect_stdout(io.StringIO()) as salida:
            self.corre([lector("x", [h])], "--seco")
        self.assertIn("detalle-visible", salida.getvalue())
        self.assertEqual((self.codigo, self.avisos, self.log), (1, [], ""))

    def test_solo_no_urgentes_salen_0_y_avisan_por_la_manana(self):
        h = hallazgo("a", module.Falla.EN_VENTANA)
        manana = datetime(2026, 10, 22, 9, 0).timestamp()
        tarde = datetime(2026, 10, 22, 15, 0).timestamp()
        self.corre([lector("x", [h])], ahora=manana)
        self.assertEqual((self.codigo, len(self.avisos)), (0, 1))
        self.corre([lector("x", [h])], ahora=tarde)
        self.assertEqual((self.codigo, self.avisos), (0, []))

    def test_una_lectura_que_truena_entra_al_aviso_y_no_tumba_al_vigia(self):
        h = hallazgo("a", module.Falla.NO_CORRE)
        self.corre([
            lector("las tareas de claw", error=RuntimeError("sin red")),
            lector("launchd", [h]),
        ])  # fmt: skip
        self.assertEqual(self.codigo, 1)
        self.assertIn("• No pude revisar las tareas de claw.", self.avisos[0])
        self.assertNotIn("sin red", self.avisos[0])
        self.assertIn("sin red", self.log)

    def test_si_solo_falla_la_lectura_sale_0_pero_avisa(self):
        self.corre([lector("la PC", error=OSError("no contesta"))])
        self.assertEqual((self.codigo, len(self.avisos)), (0, 1))

    def test_si_el_registro_no_se_puede_escribir_el_aviso_sale_igual(self):
        h = hallazgo("ai.goncloud.shopify-token", module.Falla.NO_CORRE)
        with tempfile.TemporaryDirectory() as tmp:
            cola = Path(tmp) / "cola"
            registro = Path(tmp) / "archivo"
            registro.write_text("x")
            codigo = module.main(
                ["--maquina", "mini", "--aviso", str(cola),
                 "--registro", str(registro / "vigia.log"),
                 "--estado", str(Path(tmp) / "estado.json")],
                lectores=[lector("x", [h])],
                ahora=1792724232.0,
            )  # fmt: skip
            self.assertEqual(codigo, 2)
            self.assertEqual(len(list(cola.glob("*.txt"))), 1)

    def test_politica_rota_sale_2(self):
        with tempfile.TemporaryDirectory() as raiz:
            (Path(raiz) / "tareas-programadas.json").write_text('{"intrusa": 1}')
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.corre([lector("x")], raiz=raiz)
        self.assertEqual(self.codigo, 2)
        self.assertIn("intrusa", err.getvalue())

    def test_raiz_sin_politica_sale_2(self):
        with (
            tempfile.TemporaryDirectory() as raiz,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.corre([lector("x")], raiz=raiz)
        self.assertEqual(self.codigo, 2)


class TestEstado(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.estado = self.dir / "estado.json"

    def corre(self, hallazgos, cuando, *extra):
        cola = self.dir / "cola"
        argv = [
            "--maquina", "mini", "--raiz", str(ROOT), "--aviso", str(cola),
            "--registro", str(self.dir / "vigia.log"),
            "--estado", str(self.estado), *extra,
        ]  # fmt: skip
        module.main(argv, lectores=[lector("x", hallazgos)], ahora=cuando.timestamp())
        avisos = [p.read_text("utf-8") for p in sorted(cola.glob("*.txt"))]
        for p in cola.glob("*.txt"):
            p.unlink()
        return avisos

    def test_el_mismo_hallazgo_en_tres_corridas_avisa_una_sola_vez(self):
        h = hallazgo("ai.goncloud.shopify-token", module.Falla.NO_CORRE)
        cuando = [datetime(2026, 10, 22, hora, 0) for hora in (14, 15, 16)]
        avisos = [self.corre([h], c) for c in cuando]
        self.assertEqual([len(a) for a in avisos], [1, 0, 0])
        self.assertIn("no está corriendo", avisos[0][0])

    def test_un_hallazgo_que_desaparece_avisa_que_se_arreglo_una_vez(self):
        h = hallazgo("ai.goncloud.shopify-token", module.Falla.NO_CORRE)
        self.corre([h], datetime(2026, 10, 22, 14, 0))
        arreglo = self.corre([], datetime(2026, 10, 22, 15, 0))
        self.assertEqual(
            arreglo,
            [
                "En la Mini · Tareas programadas:\n"
                "• La renovación del token de Shopify: ya se arregló.\n" + module.CIERRE
            ],
        )
        self.assertEqual(self.corre([], datetime(2026, 10, 22, 16, 0)), [])

    def test_si_vuelve_despues_de_arreglarse_se_avisa_de_nuevo(self):
        h = hallazgo("a", module.Falla.NO_CORRE)
        self.corre([h], datetime(2026, 10, 22, 14, 0))
        self.corre([], datetime(2026, 10, 22, 15, 0))
        self.assertEqual(len(self.corre([h], datetime(2026, 10, 22, 16, 0))), 1)

    def test_al_dia_siguiente_la_primera_corrida_de_la_manana_lo_recuerda_una_vez(self):
        h = hallazgo("ai.goncloud.shopify-token", module.Falla.NO_CORRE)
        self.corre([h], datetime(2026, 10, 22, 14, 0))
        self.assertEqual(self.corre([h], datetime(2026, 10, 22, 20, 0)), [])
        recordatorio = self.corre([h], datetime(2026, 10, 23, 8, 0))
        self.assertEqual(
            recordatorio,
            [
                "En la Mini · Tareas programadas:\n"
                "Sigue pendiente desde 22 de octubre:\n"
                "• La renovación del token de Shopify: no está corriendo.\n"
                + module.CIERRE
            ],
        )
        self.assertEqual(self.corre([h], datetime(2026, 10, 23, 9, 0)), [])
        self.assertEqual(self.corre([h], datetime(2026, 10, 23, 15, 0)), [])
        self.assertEqual(len(self.corre([h], datetime(2026, 10, 24, 8, 0))), 1)

    def test_lo_avisado_hoy_no_se_recuerda_hoy(self):
        h = hallazgo("a", module.Falla.NO_CORRE)
        self.assertEqual(len(self.corre([h], datetime(2026, 10, 22, 8, 0))), 1)
        self.assertEqual(self.corre([h], datetime(2026, 10, 22, 9, 0)), [])

    def test_un_no_urgente_de_la_tarde_se_avisa_la_manana_siguiente(self):
        h = hallazgo("a", module.Falla.EN_VENTANA)
        self.assertEqual(self.corre([h], datetime(2026, 10, 22, 15, 0)), [])
        self.assertEqual(self.corre([], datetime(2026, 10, 22, 16, 0)), [])
        self.assertEqual(self.corre([h], datetime(2026, 10, 22, 17, 0)), [])
        self.assertEqual(len(self.corre([h], datetime(2026, 10, 23, 8, 0))), 1)

    def test_un_estado_danado_se_trata_como_vacio_y_se_reescribe(self):
        h = hallazgo("a", module.Falla.NO_CORRE)
        for roto in (
            "{no es json",
            "[]",
            '{"hallazgos": 3}',
            '{"hallazgos": {"x": 1}}',
            '{"hallazgos": {"x": "2026-10-22"}}',
            '{"hallazgos": {"otra|NO_CORRE|a": "2026-10-22"}}',
            '{"hallazgos": {"|NO_CORRE|a": "2026-10-22"}}',
        ):
            self.estado.write_text(roto, encoding="utf-8")
            self.assertEqual(self.corre([], datetime(2026, 10, 22, 13, 0)), [])
            self.assertEqual(
                json.loads(self.estado.read_text("utf-8"))["hallazgos"], {}
            )
            self.estado.write_text(roto, encoding="utf-8")
            self.assertEqual(len(self.corre([h], datetime(2026, 10, 22, 14, 0))), 1)
            self.assertEqual(self.corre([h], datetime(2026, 10, 22, 15, 0)), [])
            self.assertIn(
                "mini|NO_CORRE|a",
                json.loads(self.estado.read_text("utf-8"))["hallazgos"],
            )

    def test_una_fecha_imposible_en_el_estado_no_truena_la_manana(self):
        h = hallazgo("a", module.Falla.NO_CORRE)
        for fecha in ("2026-13-01", "2026-02-30"):
            self.estado.write_text(
                json.dumps(
                    {"hallazgos": {"mini|NO_CORRE|a": fecha}, "recordatorio": ""}
                ),
                encoding="utf-8",
            )
            self.assertEqual(len(self.corre([h], datetime(2026, 10, 23, 8, 0))), 1)
            self.assertEqual(
                json.loads(self.estado.read_text("utf-8"))["hallazgos"],
                {"mini|NO_CORRE|a": "2026-10-23"},
            )

    def test_una_lectura_que_truena_no_declara_arreglado_ni_repite_el_aviso(self):
        h = hallazgo("ai.goncloud.shopify-token", module.Falla.NO_CORRE)
        self.corre([h], datetime(2026, 10, 22, 14, 0))
        cola = self.dir / "cola"
        argv = [
            "--maquina", "mini", "--raiz", str(ROOT), "--aviso", str(cola),
            "--registro", str(self.dir / "vigia.log"), "--estado", str(self.estado),
        ]  # fmt: skip
        caida = lector("las tareas de launchd", error=RuntimeError("timeout"))
        module.main(
            argv, lectores=[caida], ahora=datetime(2026, 10, 22, 15, 0).timestamp()
        )
        textos = [p.read_text("utf-8") for p in cola.glob("*.txt")]
        for p in cola.glob("*.txt"):
            p.unlink()
        self.assertEqual(len(textos), 1)
        self.assertIn("No pude revisar las tareas de launchd", textos[0])
        self.assertNotIn("ya se arregl", textos[0])
        self.assertIn(
            "mini|NO_CORRE|ai.goncloud.shopify-token", self.estado.read_text("utf-8")
        )
        self.assertEqual(self.corre([h], datetime(2026, 10, 22, 16, 0)), [])

    def test_una_lectura_que_truena_no_recuerda_lo_que_no_vio(self):
        h = hallazgo("a", module.Falla.NO_CORRE)
        self.corre([h], datetime(2026, 10, 22, 14, 0))
        estado, hoy = module.lee_estado(self.estado, "mini"), "2026-10-23"
        _, arreglados, pendientes, nuevo = module.concilia(estado, [], True, hoy, False)
        self.assertEqual((arreglados, pendientes), ([], {}))
        self.assertEqual(nuevo["hallazgos"], estado["hallazgos"])

    def test_lo_que_se_apaga_a_proposito_no_se_anuncia_como_arreglado(self):
        tarea = "ai.goncloud.tmux-activity-watch"
        self.corre(
            [hallazgo(tarea, module.Falla.NO_CORRE)], datetime(2026, 10, 22, 14, 0)
        )
        self.assertEqual(self.corre([], datetime(2026, 10, 22, 15, 0)), [])
        self.assertEqual(module.lee_estado(self.estado, "mini")["hallazgos"], {})

    def test_la_clave_de_otra_maquina_no_se_declara_arreglada(self):
        self.estado.write_text(
            json.dumps(
                {
                    "hallazgos": {
                        "mac|NO_CORRE|ai.goncloud.shopify-token": "2026-10-22"
                    },
                    "recordatorio": "",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.corre([], datetime(2026, 10, 22, 15, 0)), [])

    def test_una_tarea_apagada_que_falla_distinto_si_se_avisa_y_se_arregla(self):
        tarea = "ai.goncloud.tmux-activity-watch"
        mal = hallazgo(tarea, module.Falla.TERMINO_MAL)
        self.assertEqual(len(self.corre([mal], datetime(2026, 10, 22, 14, 0))), 1)
        arreglo = self.corre([], datetime(2026, 10, 22, 15, 0))
        self.assertEqual(len(arreglo), 1)
        self.assertIn("ya se arregl", arreglo[0])

    def test_sin_archivo_de_estado_se_crea_en_su_carpeta(self):
        self.estado = self.dir / "nueva" / "estado.json"
        self.corre(
            [hallazgo("a", module.Falla.NO_CORRE)], datetime(2026, 10, 22, 14, 0)
        )
        self.assertEqual(
            json.loads(self.estado.read_text("utf-8"))["hallazgos"],
            {"mini|NO_CORRE|a": "2026-10-22"},
        )
        self.assertEqual(list(self.estado.parent.glob(".*")), [])

    def test_seco_no_lee_ni_escribe_el_estado(self):
        h = hallazgo("a", module.Falla.NO_CORRE)
        with (
            mock.patch.object(module, "lee_estado") as lee,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.corre([h], datetime(2026, 10, 22, 14, 0), "--seco")
        lee.assert_not_called()
        self.assertFalse(self.estado.exists())
        self.corre([h], datetime(2026, 10, 22, 14, 0))
        antes = self.estado.read_text("utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.corre([], datetime(2026, 10, 23, 8, 0), "--seco")
        self.assertEqual(self.estado.read_text("utf-8"), antes)

    def test_el_estado_se_escribe_con_os_replace(self):
        with mock.patch.object(module.os, "replace", wraps=module.os.replace) as r:
            self.corre(
                [hallazgo("a", module.Falla.NO_CORRE)], datetime(2026, 10, 22, 14, 0)
            )
        self.assertIn(self.estado, [Path(c.args[1]) for c in r.call_args_list])

    def test_la_ruta_por_omision_vive_fuera_de_la_copia_instalada(self):
        ruta = module._ruta_del_estado()
        self.assertEqual(ruta.name, "estado.json")
        self.assertNotIn(ROOT, ruta.parents)

    def test_las_rutas_por_omision_son_las_del_encargo(self):
        with mock.patch.object(module.sys, "platform", "darwin"):
            self.assertEqual(
                module._ruta_del_estado(),
                Path.home() / ".local/state/goncloud-vigia/estado.json",
            )
        with (
            mock.patch.object(module.sys, "platform", "win32"),
            mock.patch.dict(module.os.environ, {"LOCALAPPDATA": ""}),
        ):
            self.assertEqual(
                str(module._ruta_del_estado()).replace("\\", "/").split("/")[-2:],
                ["goncloud-vigia-estado", "estado.json"],
            )


class TestApagadasAProposito(unittest.TestCase):
    def setUp(self):
        base = json.loads((ROOT / "tareas-programadas.json").read_text("utf-8"))
        self.assertIn("ai.goncloud.tmux-activity-watch", base["apagadas_a_proposito"])
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "tareas-programadas.json"
            ruta.write_text(json.dumps(base), encoding="utf-8")
            self.pol = module.Politica.carga(ruta)

    def aviso(self, filas):
        hallazgos = module.hallazgos_launchd(filas, "mac", self.pol)
        return module.texto_aviso(hallazgos, [], self.pol, True)

    def test_una_apagada_a_proposito_no_avisa_no_corre_y_otra_si(self):
        texto = self.aviso(
            [
                fila_launchd(label="ai.goncloud.tmux-activity-watch", cargado=False),
                fila_launchd(label="ai.goncloud.shopify-token", cargado=False),
            ]
        )
        self.assertIn("La renovación del token de Shopify: no está corriendo", texto)
        self.assertNotIn("tmux", texto)

    def test_solo_la_apagada_en_la_lista_queda_sin_aviso(self):
        fila = fila_launchd(label="ai.goncloud.tmux-activity-watch", cargado=False)
        self.assertIsNone(self.aviso([fila]))

    def test_sigue_avisando_lo_demas_de_una_tarea_apagada(self):
        fila = fila_launchd(
            label="ai.goncloud.tmux-activity-watch", cargado=False, last_exit_code=3
        )
        self.assertEqual(
            module.juzga_launchd(fila, self.pol), [module.Falla.TERMINO_MAL]
        )

    def test_claw_y_hermes_tambien_respetan_la_lista(self):
        atrasada = {"nextRunAtMs": AHORA_MS - 3 * 60 * MIN_MS}
        self.assertEqual(
            module.juzga_claw(job_claw(**atrasada), self.pol, AHORA_MS),
            [module.Falla.NO_CORRE],
        )
        self.pol.apagadas_a_proposito["prueba"] = "motivo"
        self.assertEqual(
            module.juzga_claw(job_claw(**atrasada), self.pol, AHORA_MS), []
        )
        atrasado = job_hermes(next_run_at="2026-10-20T10:00:00-07:00")
        self.assertEqual(module.juzga_hermes("claw", atrasado, self.pol, AHORA_MS), [])


class TestLectores(unittest.TestCase):
    def test_el_cron_de_claw_se_lee_pagina_por_pagina(self):
        paginas = [
            {"jobs": [{"name": "a"}, {"name": "b"}], "hasMore": True, "nextOffset": 2},
            {"jobs": [{"name": "c"}], "hasMore": False, "nextOffset": None},
        ]
        pedidos = []

        def falso(argv):
            pedidos.append(json.loads(argv[argv.index("--params") + 1])["offset"])
            salida = json.dumps(paginas[len(pedidos) - 1])
            return subprocess.CompletedProcess(argv, 0, salida, "")

        original = module._corre
        module._corre = falso
        try:
            jobs = module.lee_cron_claw()
        finally:
            module._corre = original
        self.assertEqual([j["name"] for j in jobs], ["a", "b", "c"])
        self.assertEqual(pedidos, [0, 2])

    def test_hermes_lee_jobs_y_la_ultima_salida_de_cada_perfil(self):
        with tempfile.TemporaryDirectory() as tmp:
            perfil = Path(tmp) / "claw" / "cron"
            (perfil / "output" / "abc").mkdir(parents=True)
            (perfil / "jobs.json").write_text(
                json.dumps(
                    {"jobs": [{"id": "abc", "name": "t"}, {"id": "zzz", "name": "u"}]}
                )
            )
            (perfil / "output" / "abc" / "2026-10-21_10-00-00.md").write_text("viejo")
            (perfil / "output" / "abc" / "2026-10-22_10-00-00.md").write_text("nuevo")
            (Path(tmp) / "sin-cron").mkdir()
            salida = module.lee_hermes(Path(tmp))
        self.assertEqual(
            [(p, j["name"], s) for p, j, s in salida],
            [("claw", "t", "nuevo"), ("claw", "u", None)],
        )

    def test_launchd_real_de_esta_maquina_trae_los_campos(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "ai.goncloud.x.plist"
            plist.write_bytes(
                plistlib.dumps(
                    {
                        "Label": "ai.goncloud.x",
                        "ProgramArguments": ["/bin/sh", "/no/existe/nada.sh"],
                        "StartInterval": 60,
                    }
                )
            )
            (Path(tmp) / "otro.plist").write_bytes(
                plistlib.dumps({"Label": "com.apple.x"})
            )
            with mock.patch.object(
                module,
                "_corre",
                lambda argv: subprocess.CompletedProcess(argv, 113, "", ""),
            ):
                filas = module.lee_launchd(("ai.goncloud.",), Path(tmp))
        self.assertEqual(len(filas), 1)
        self.assertEqual(set(filas[0]), CAMPOS_LAUNCHD)
        self.assertEqual(filas[0]["rutas_que_faltan"], ["/no/existe/nada.sh"])
        self.assertTrue(filas[0]["periodico"])
        self.assertFalse(filas[0]["cargado"])

    def test_un_plist_nuestro_que_no_se_puede_leer_no_se_salta(self):
        roto = {
            "ai.goncloud.binario.plist": b"bplist00 roto",
            "ai.goncloud.xml.plist": b"<?xml version='1.0'?><plist><dict>",
            "ai.goncloud.sinpermiso.plist": plistlib.dumps(
                {"Label": "ai.goncloud.sinpermiso"}
            ),
            "com.apple.roto.plist": b"bplist00 roto",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for nombre, datos in roto.items():
                (Path(tmp) / nombre).write_bytes(datos)
            (Path(tmp) / "ai.goncloud.sinpermiso.plist").chmod(0)
            try:
                with mock.patch.object(
                    module,
                    "_corre",
                    lambda argv: subprocess.CompletedProcess(argv, 113, "", ""),
                ):
                    filas = module.lee_launchd(("ai.goncloud.",), Path(tmp))
            finally:
                (Path(tmp) / "ai.goncloud.sinpermiso.plist").chmod(0o600)
            self.assertEqual(
                [f["label"] for f in filas],
                ["ai.goncloud.binario", "ai.goncloud.sinpermiso", "ai.goncloud.xml"],
            )
            pol = politica()
            for fila in filas:
                self.assertEqual(
                    module.juzga_launchd(fila, pol), [module.Falla.NO_CORRE]
                )


if __name__ == "__main__":
    unittest.main()
