"""El vigilante sin modelo de JEV y su instalador (encargo H-jev-hermes).

Criterios: etapa blocked simulada produce aviso; silencio nocturno y
antirrepeticion probados; en reposo no consulta nada; JEV caido no tumba la
vuelta; solo atorado/abandonado con confianza >= 0.8 avisa.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_apoyo  # noqa: E402

import jev_decisiones  # noqa: E402
import jev_estado  # noqa: E402

vigilante = importlib.import_module("raices.jev_vigilante")
RAIZ = jev_apoyo.RAIZ

# 2026-10-20 20:00 UTC = 13:00 en Vancouver (PDT, UTC-7): horario despierto.
DIA = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
# 2026-10-20 08:00 UTC = 01:00 en Vancouver: silencio.
NOCHE = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


def ts(momento: datetime) -> int:
    return int(momento.timestamp())


class Base(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.raiz = Path(temporal.name)
        self.claw = self.raiz / "profiles" / "claw"
        self.claw.mkdir(parents=True)
        self.kanban = jev_apoyo.Kanban(self.raiz / "kanban.db")
        self.jev = jev_apoyo.JevFalso({"vigilancia": ("atorado", 0.93)})
        self.enviados = []

    def enviar(self, texto):
        self.enviados.append(texto)
        return len(self.enviados)

    def vuelta(self, ahora=DIA, jev=None, enviar=None):
        decisor = jev_decisiones.Decisor(self.claw, llamar=jev or self.jev)
        return vigilante.correr(
            home=self.raiz,
            ahora=ahora,
            enviar=enviar or self.enviar,
            decisor=decisor,
        )

    def cadena_bloqueada(self, hace_s=60):
        self.kanban.cadena(
            etapas=(("scout", "done"), ("implementer", "blocked")),
            at=ts(DIA) - hace_s,
        )

    def tarjeta_quieta(self, hace_min=20, assignee="amazon", id="t_q"):
        self.kanban.tarea(id, assignee, "running", at=ts(DIA) - hace_min * 60)


class CandidatosTests(Base):
    def test_en_reposo_no_consulta_a_jev_ni_envia_nada(self):
        self.kanban.tarea("t_ok", "amazon", "running", at=ts(DIA) - 120)  # reciente
        self.kanban.cadena(at=ts(DIA) - 120)  # etapa viva, con avance reciente
        self.assertEqual(self.vuelta(), [])
        self.assertEqual(self.jev.llamadas, [])
        self.assertEqual(self.enviados, [])

    def test_sin_kanban_ni_seguimiento_tampoco(self):
        (self.raiz / "kanban.db").unlink()
        self.assertEqual(self.vuelta(), [])
        self.assertEqual(self.jev.llamadas, [])

    def test_etapa_blocked_produce_aviso_del_vigilante(self):
        self.cadena_bloqueada()
        avisadas = self.vuelta()
        self.assertEqual(avisadas, ["cadena:t_raiz:blocked"])
        self.assertEqual(self.jev.preguntas(), ["vigilancia"])
        (texto,) = self.enviados
        self.assertIn("etapa bloqueada", texto)
        self.assertIn("implementer", texto)
        self.assertIn("0.93", texto)

    def test_enlaces_como_el_kanban_real_etapa_padre_y_raiz_hija(self):
        self.cadena_bloqueada()
        conn = jev_estado.abrir_kanban(self.raiz / "kanban.db")
        self.addCleanup(conn.close)
        enlaces = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
        self.assertIn(("t_raiz_implementer", "t_raiz"), [tuple(e) for e in enlaces])
        self.assertNotIn("t_raiz", [e[0] for e in enlaces])
        etapas = {e["id"] for e in jev_estado._etapas(conn, "t_raiz")}
        self.assertEqual(etapas, {"t_raiz_scout", "t_raiz_implementer"})

    def test_cadena_sin_avance_mas_de_diez_minutos(self):
        self.kanban.cadena(at=ts(DIA) - 15 * 60)
        self.assertEqual(self.vuelta(), ["cadena:t_raiz:sin-avance"])

    def test_el_latido_cuenta_como_avance(self):
        self.kanban.cadena(at=ts(DIA) - 3600)
        self.kanban.evento("t_raiz_implementer", "heartbeat", at=ts(DIA) - 120)
        self.assertEqual(self.vuelta(), [])
        self.assertEqual(self.jev.llamadas, [])

    def test_cadena_terminada_no_es_candidata(self):
        self.kanban.cadena(etapas=(("implementer", "blocked"),), at=ts(DIA) - 3600)
        self.kanban.estado("t_raiz", "done")
        self.assertEqual(self.vuelta(), [])

    def test_tarjeta_de_especialista_sin_avance(self):
        self.tarjeta_quieta(20)
        self.assertEqual(self.vuelta(), ["tarjeta:t_q"])

    def test_tarjeta_de_quien_no_es_especialista_se_ignora(self):
        self.tarjeta_quieta(60, assignee="scout")
        self.assertEqual(self.vuelta(), [])
        self.assertEqual(self.jev.llamadas, [])

    def test_mensaje_de_david_sin_respuesta_y_promesa_sin_cumplir(self):
        seguimiento = self.claw / "jev" / "seguimiento.json"
        seguimiento.parent.mkdir()
        seguimiento.write_text(
            json.dumps(
                {
                    "mensajes": {
                        "s1|t1": {"at": ts(DIA) - 11 * 60},
                        "s1|t9": {"at": ts(DIA) - 3 * 60},
                    },
                    "promesas": {
                        "s1|t2": {"at": ts(DIA) - 31 * 60},
                        "s1|t3": {"at": ts(DIA) - 20 * 60},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.vuelta(), ["mensaje:s1|t1", "promesa:s1|t2"])

    def test_casos_de_hace_mas_de_seis_horas_son_historia(self):
        seguimiento = self.claw / "jev" / "seguimiento.json"
        seguimiento.parent.mkdir()
        seguimiento.write_text(
            json.dumps(
                {"mensajes": {"s1|t1": {"at": ts(DIA) - 7 * 3600}}, "promesas": {}}
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.vuelta(), [])


class UmbralTests(Base):
    def test_normal_no_avisa_y_no_se_reconsulta_en_treinta_minutos(self):
        self.tarjeta_quieta()
        jev = jev_apoyo.JevFalso({"vigilancia": ("normal", 0.97)})
        self.assertEqual(self.vuelta(jev=jev), [])
        self.vuelta(DIA + timedelta(minutes=5), jev=jev)
        self.assertEqual(len(jev.llamadas), 1)
        self.vuelta(DIA + timedelta(minutes=31), jev=jev)
        self.assertEqual(len(jev.llamadas), 2)
        self.assertEqual(self.enviados, [])

    def test_atorado_con_confianza_menor_a_08_no_avisa(self):
        self.tarjeta_quieta()
        jev = jev_apoyo.JevFalso({"vigilancia": ("atorado", 0.79)})
        self.assertEqual(self.vuelta(jev=jev), [])
        self.assertEqual(self.enviados, [])

    def test_atorado_con_08_exacto_y_abandonado_avisan(self):
        self.tarjeta_quieta(id="t_a")
        self.assertEqual(
            self.vuelta(jev=jev_apoyo.JevFalso({"vigilancia": ("atorado", 0.8)})),
            ["tarjeta:t_a"],
        )
        self.tarjeta_quieta(id="t_b", assignee="meli")
        self.assertEqual(
            self.vuelta(jev=jev_apoyo.JevFalso({"vigilancia": ("abandonado", 0.9)})),
            ["tarjeta:t_b"],
        )

    def test_jev_caido_no_tumba_la_vuelta_y_la_proxima_reintenta(self):
        self.tarjeta_quieta()
        caido = jev_apoyo.JevFalso(falla=RuntimeError("TypeSafe unreachable"))
        self.assertEqual(self.vuelta(jev=caido), [])
        self.assertEqual(self.enviados, [])
        self.assertEqual(self.vuelta(DIA + timedelta(minutes=5)), ["tarjeta:t_q"])

    def test_si_el_envio_falla_no_cuenta_como_avisado(self):
        self.tarjeta_quieta()
        self.assertEqual(self.vuelta(enviar=lambda texto: None), [])
        self.assertEqual(self.vuelta(DIA + timedelta(minutes=5)), ["tarjeta:t_q"])


class AntirrepeticionTests(Base):
    def test_no_repite_antes_de_treinta_minutos_y_despues_si(self):
        self.tarjeta_quieta()
        self.assertEqual(self.vuelta(DIA), ["tarjeta:t_q"])
        self.assertEqual(self.vuelta(DIA + timedelta(minutes=5)), [])
        self.assertEqual(self.vuelta(DIA + timedelta(minutes=29)), [])
        self.assertEqual(len(self.enviados), 1)
        self.assertEqual(len(self.jev.llamadas), 1, "ni se reconsulta a JEV")
        self.assertEqual(self.vuelta(DIA + timedelta(minutes=31)), ["tarjeta:t_q"])
        self.assertEqual(len(self.enviados), 2)

    def test_a_lo_mucho_tres_avisos_por_caso(self):
        self.tarjeta_quieta()
        for n in range(6):
            self.vuelta(DIA + timedelta(minutes=31 * n))
        self.assertEqual(len(self.enviados), 3)

    def test_un_caso_viejo_no_reavisa_cada_dia(self):
        # Atorado desde hace 8 h: tres avisos y ya. Al dia siguiente, nada mas.
        self.tarjeta_quieta(hace_min=8 * 60)
        for n in range(6):
            self.vuelta(DIA + timedelta(minutes=31 * n))
        self.assertEqual(len(self.enviados), 3)
        self.assertEqual(self.vuelta(DIA + timedelta(hours=26)), [])
        self.assertEqual(len(self.enviados), 3)

    def test_lo_quieto_de_hace_mas_de_un_dia_es_historia(self):
        self.tarjeta_quieta(hace_min=25 * 60)
        self.kanban.cadena(
            etapas=(("implementer", "blocked"),), at=ts(DIA) - 25 * 3600
        )
        self.assertEqual(self.vuelta(), [])
        self.assertEqual(self.jev.llamadas, [])

    def test_casos_distintos_no_se_estorban(self):
        self.tarjeta_quieta(id="t_a")
        self.vuelta(DIA)
        self.tarjeta_quieta(id="t_b", assignee="odoo")
        self.assertEqual(self.vuelta(DIA + timedelta(minutes=5)), ["tarjeta:t_b"])


class SilencioTests(Base):
    def test_de_23_a_08_en_vancouver_calla_y_ni_consulta(self):
        self.kanban.tarea("t_q", "amazon", "running", at=ts(NOCHE) - 20 * 60)
        self.assertEqual(self.vuelta(NOCHE), [])
        self.assertEqual(self.jev.llamadas, [])
        self.assertEqual(self.enviados, [])

    def test_con_ingenieria_abierta_no_calla_de_noche(self):
        self.kanban.cadena(
            etapas=(("scout", "done"), ("implementer", "blocked")), at=ts(NOCHE) - 60
        )
        self.assertEqual(self.vuelta(NOCHE), ["cadena:t_raiz:blocked"])
        self.assertEqual(len(self.enviados), 1)

    def test_los_bordes_23_00_calla_y_08_00_ya_habla(self):
        self.assertTrue(
            vigilante.en_silencio(datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc))
        )  # 23:00 PDT
        self.assertFalse(
            vigilante.en_silencio(datetime(2026, 10, 21, 5, 59, tzinfo=timezone.utc))
        )  # 22:59
        self.assertTrue(
            vigilante.en_silencio(datetime(2026, 10, 20, 14, 59, tzinfo=timezone.utc))
        )  # 07:59
        self.assertFalse(
            vigilante.en_silencio(datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc))
        )  # 08:00

    def test_con_ingenieria_abierta_de_noche_solo_habla_la_cadena(self):
        self.kanban.cadena(
            etapas=(("scout", "done"), ("implementer", "blocked")), at=ts(NOCHE) - 60
        )
        self.kanban.tarea("t_q", "amazon", "running", at=ts(NOCHE) - 20 * 60)
        seguimiento = self.claw / "jev" / "seguimiento.json"
        seguimiento.parent.mkdir()
        seguimiento.write_text(
            json.dumps({"mensajes": {"m": {"at": ts(NOCHE) - 1800}}}), encoding="utf-8"
        )
        self.assertEqual(self.vuelta(NOCHE), ["cadena:t_raiz:blocked"])
        self.assertEqual(len(self.jev.llamadas), 1, "ni consulta lo que callaria")

    def test_amanece_y_avisa_lo_que_callo(self):
        self.kanban.tarea("t_n", "amazon", "running", at=ts(NOCHE) - 3600)
        self.assertEqual(self.vuelta(NOCHE), [])
        manana = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)  # 09:00 PDT
        self.assertEqual(self.vuelta(manana), ["tarjeta:t_n"])


class CronTests(Base):
    def test_main_sin_novedad_sale_0_y_no_imprime(self):
        salida = io.StringIO()
        original = vigilante.correr
        vigilante.correr = lambda *a, **k: []
        self.addCleanup(setattr, vigilante, "correr", original)
        with redirect_stdout(salida):
            self.assertEqual(vigilante.main(), 0)
        self.assertEqual(salida.getvalue(), "")

    def test_main_con_un_fallo_imprime_una_linea_de_error_y_sale_1(self):
        def roto(*a, **k):
            raise RuntimeError("kanban ilegible")

        original = vigilante.correr
        vigilante.correr = roto
        self.addCleanup(setattr, vigilante, "correr", original)
        salida = io.StringIO()
        with redirect_stdout(salida):
            self.assertEqual(vigilante.main(), 1)
        self.assertEqual(
            salida.getvalue().strip(), "ERROR jev vigilante: kanban ilegible"
        )


class InstaladorTests(unittest.TestCase):
    def setUp(self):
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        self.home = Path(temporal.name)
        spec = importlib.util.spec_from_file_location(
            "instalar_jev_prueba", RAIZ / "scripts" / "instalar_jev_vigilante.py"
        )
        self.instalador = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.instalador
        spec.loader.exec_module(self.instalador)
        self.llamadas = []

    def corredor(self, argv):
        self.llamadas.append(argv)
        if argv[:2] == ["cron", "create"]:
            jobs = self.home / "cron" / "jobs.json"
            jobs.parent.mkdir(exist_ok=True)
            jobs.write_text(
                json.dumps(
                    {"jobs": [{"id": "abc123", "name": argv[argv.index("--name") + 1]}]}
                ),
                encoding="utf-8",
            )
            return 0, "Created job: abc123"
        if argv[:2] == ["cron", "remove"]:
            (self.home / "cron" / "jobs.json").write_text(
                json.dumps({"jobs": []}), encoding="utf-8"
            )
            return 0, "Removed"
        return 1, "comando raro"

    def correr(self, *args):
        with redirect_stdout(io.StringIO()):
            return self.instalador.main(
                ["--home", str(self.home), *args], corredor=self.corredor
            )

    def test_aplicar_crea_el_cron_de_cada_cinco_minutos_sin_agente(self):
        self.assertEqual(self.correr("--aplicar"), 0)
        (crear,) = self.llamadas
        self.assertEqual(
            crear,
            [
                "cron",
                "create",
                "every 5m",
                "--name",
                "jev-vigilante",
                "--script",
                "jev-vigilante.py",
                "--no-agent",
                "--deliver",
                "local",
            ],
        )
        envoltorio = (self.home / "scripts" / "jev-vigilante.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from raices import jev_vigilante", envoltorio)
        estado = json.loads(
            (self.home / "raices" / "instalacion-jev.json").read_text(encoding="utf-8")
        )
        self.assertEqual(estado["cron_id"], "abc123")

    def test_aplicar_dos_veces_no_duplica_el_cron(self):
        self.correr("--aplicar")
        self.correr("--aplicar")
        self.assertEqual(
            sum(1 for llamada in self.llamadas if llamada[:2] == ["cron", "create"]), 1
        )

    def test_revertir_quita_el_job_el_envoltorio_y_el_estado(self):
        self.correr("--aplicar")
        self.assertEqual(self.correr("--revertir"), 0)
        self.assertIn(["cron", "remove", "abc123"], self.llamadas)
        self.assertFalse((self.home / "scripts" / "jev-vigilante.py").exists())
        self.assertFalse((self.home / "raices" / "instalacion-jev.json").exists())

    def test_no_toca_la_instalacion_de_raices(self):
        raices = self.home / "raices"
        raices.mkdir()
        propio = raices / "instalacion.json"
        propio.write_text('{"version": 2}', encoding="utf-8")
        self.correr("--aplicar")
        self.correr("--revertir")
        self.assertEqual(propio.read_text(encoding="utf-8"), '{"version": 2}')

    def test_un_create_rechazado_no_deja_envoltorio_y_sale_1(self):
        self.corredor = lambda argv: (1, "rechazado")
        self.assertEqual(self.correr("--aplicar"), 1)
        self.assertFalse((self.home / "scripts" / "jev-vigilante.py").exists())


class ReglaRetiradaTests(unittest.TestCase):
    def test_roles_claw_ya_no_prohibe_parchar_a_mano(self):
        texto = (RAIZ / "roles" / "claw.md").read_text(encoding="utf-8")
        self.assertNotIn("Prohibido parchar", texto)
        self.assertIn("# JEV (decisiones rapidas)", texto)


if __name__ == "__main__":
    unittest.main()
