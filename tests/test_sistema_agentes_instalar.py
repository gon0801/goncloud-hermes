#!/usr/bin/env python3
"""El instalador y su reversa de la ruta de raíces (H4b3, para SUA-09H).

--aplicar deja el envoltorio del ticker en el perfil DEFAULT y el cron
ticker-raices. Los crons del perfil se leen de <home>/cron/jobs.json, la
misma fuente de load_jobs de Hermes: `hermes cron list` solo imprime una
tabla humana (sin --json) y `cron remove` recibe job_id, no nombre. El id
del cron creado se guarda en instalacion.json y revertir borra por id.
--revertir quita solo lo suyo: jamás borra datos. --check no escribe nada
y no invoca a hermes. Ninguna prueba corre el hermes real."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALADOR = REPO / "scripts" / "instalar_raices.py"
FIXTURE_JOBS = REPO / "tests" / "fixtures" / "hermes-cron-jobs.json"
ID_FALSO = "aa11bb22cc33"


def cargar_instalador():
    especificacion = importlib.util.spec_from_file_location(
        "instalar_raices", INSTALADOR
    )
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


class HermesFalsoDeCrones:
    """El CLI hermes representado a nivel almacen: `cron create` valida el
    script contra <home>/scripts, ESCRIBE el job en <home>/cron/jobs.json
    con la forma de cron_jobs.py e imprime "Created job: <id>";
    `cron remove <job_id>` solo borra por id exacto (el nombre no sirve:
    igual que el hermes 0.21.5 real de la Gon-pc). `cron list --json` no
    existe y falla como en la PC. Registra cada llamada."""

    def __init__(self, home, ids=None, salida_del_create=None):
        self.home = Path(home)
        self.llamadas = []
        self.ids = list(ids or [ID_FALSO])
        self.salida_del_create = salida_del_create

    @property
    def ruta_jobs(self):
        return self.home / "cron" / "jobs.json"

    def jobs(self):
        try:
            datos = json.loads(self.ruta_jobs.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        return datos["jobs"] if isinstance(datos, dict) else datos

    def nombres(self):
        return [job.get("name") for job in self.jobs()]

    def _guardar(self, jobs):
        self.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.ruta_jobs.write_text(
            json.dumps({"jobs": jobs}, indent=2) + "\n", encoding="utf-8"
        )

    def __call__(self, argv):
        self.llamadas.append(list(argv))
        if argv[:2] == ["cron", "create"]:
            return self._crear(argv)
        if argv[:2] == ["cron", "remove"]:
            return self._quitar(argv)
        if argv[:2] == ["cron", "list"]:
            if "--json" in argv:
                return 1, "hermes: error: unrecognized arguments: --json"
            return 0, "┌───┐\nScheduled Jobs (humano)"
        return 1, f"subcomando desconocido: {argv}"

    def _crear(self, argv):
        bruto = Path(argv[argv.index("--script") + 1])
        scripts_dir = self.home / "scripts"
        ruta = bruto.resolve() if bruto.is_absolute() else (scripts_dir / bruto)
        try:
            ruta.relative_to(scripts_dir)
        except ValueError:
            return 1, f"Failed to create job: script fuera de scripts/: {bruto}"
        if not ruta.exists():
            return 1, f"Failed to create job: script not found: {ruta}"
        nombre = argv[argv.index("--name") + 1]
        job_id = self.ids.pop(0) if self.ids else "ff00ff00ff00"
        jobs = self.jobs()
        jobs.append({"id": job_id, "name": nombre, "enabled": True})
        self._guardar(jobs)
        salida = self.salida_del_create
        if salida is None:
            salida = f"Created job: {job_id}\n  Name: {nombre}\n  Schedule: every 1m"
        return 0, salida

    def _quitar(self, argv):
        job_id = argv[2]
        jobs = self.jobs()
        restantes = [job for job in jobs if job.get("id") != job_id]
        if len(restantes) == len(jobs):
            return 1, f"Failed to remove job: Job not found: {job_id}"
        self._guardar(restantes)
        return 0, f"Removed job: ticker-raices ({job_id})"


class InstaladorDeRaices(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.home = Path(self.directorio.name) / "hermes"
        self.home.mkdir(parents=True)
        self.modulo = cargar_instalador()
        self.corredor = HermesFalsoDeCrones(self.home)

    def aplicar(self):
        return self.modulo.main(
            ["--aplicar", "--home", str(self.home)], corredor=self.corredor
        )

    def revertir(self):
        return self.modulo.main(
            ["--revertir", "--home", str(self.home)], corredor=self.corredor
        )

    def chequear(self):
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.modulo.main(
                ["--check", "--home", str(self.home)], corredor=self.corredor
            )
        return codigo, bufer.getvalue()

    def sembrar_jobs_del_fixture(self):
        self.corredor.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.corredor.ruta_jobs.write_text(
            FIXTURE_JOBS.read_text(encoding="utf-8"), encoding="utf-8"
        )

    def sembrar_jobs_ilegible(self):
        self.corredor.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.corredor.ruta_jobs.write_text("{jobs: [roto", encoding="utf-8")

    def crear_datos_del_operador(self):
        (self.home / "raices").mkdir(exist_ok=True)
        (self.home / "raices" / "permisos.json").write_text(
            json.dumps({"acciones": ["read"], "toolsets": {}, "comandos": []}),
            encoding="utf-8",
        )
        (self.home / "sistema-agentes.db").write_bytes(b"db de datos")
        (self.home / "kanban.db").write_bytes(b"kanban de datos")
        (self.home / "raices" / "proyecciones").mkdir()
        (self.home / "raices" / "proyecciones" / "root.json").write_text("{}", "utf-8")
        (self.home / "scripts").mkdir(exist_ok=True)
        (self.home / "scripts" / "backup-claw.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )


class ElCheck(InstaladorDeRaices):
    """--check lee jobs.json en solo lectura y no invoca a hermes."""

    def test_check_con_el_jobs_real_encuentra_el_cron_y_no_toca_nada(self):
        self.sembrar_jobs_del_fixture()
        self.crear_datos_del_operador()
        codigo, salida = self.chequear()
        self.assertEqual(codigo, 0)
        self.assertIn("ticker-raices", salida)
        self.assertIn("presente", salida)
        self.assertEqual(
            self.corredor.llamadas, [], "check no invoca al CLI: lee jobs.json"
        )
        self.assertFalse((self.home / "profiles").exists(), "check no crea perfiles")
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())
        self.assertEqual(
            self.corredor.nombres(), ["monday-pedidos-semanal", "ticker-raices"]
        )
        self.assertEqual(
            json.loads((self.home / "raices" / "permisos.json").read_text("utf-8")),
            {"acciones": ["read"], "toolsets": {}, "comandos": []},
        )

    def test_check_con_jobs_ausente_dice_que_los_crons_no_existen(self):
        codigo, salida = self.chequear()
        self.assertEqual(codigo, 0)
        self.assertIn("ticker-raices", salida)
        self.assertIn("crear", salida)
        self.assertEqual(self.corredor.llamadas, [])

    def test_check_con_jobs_ilegible_falla_cerrado(self):
        self.sembrar_jobs_ilegible()
        codigo, salida = self.chequear()
        self.assertEqual(codigo, 1)
        self.assertIn("jobs.json", salida)
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())


class Aplicar(InstaladorDeRaices):
    def test_aplicar_crea_el_cron_en_default_y_el_envoltorio(self):
        self.assertEqual(self.aplicar(), 0)
        create = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "create"]
        ]
        self.assertEqual(len(create), 1, "un solo create")
        self.assertNotIn("-p", create[0], "el cron es del perfil default")
        self.assertEqual(
            create[0],
            [
                "cron",
                "create",
                "every 1m",
                "--name",
                "ticker-raices",
                "--script",
                "ticker-raices.py",
                "--no-agent",
                "--deliver",
                "local",
            ],
        )
        envoltorio = self.home / "scripts" / "ticker-raices.py"
        self.assertTrue(envoltorio.exists())
        self.assertIn("cron_ticker", envoltorio.read_text(encoding="utf-8"))
        self.assertFalse(
            (self.home / "profiles").exists(),
            "default es la raíz del home: nunca se crea profiles/default",
        )
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(estado["cron"], "ticker-raices")
        self.assertEqual(estado["cron_id"], ID_FALSO)
        self.assertEqual(estado["envoltorio"], "scripts/ticker-raices.py")
        self.assertEqual(estado["script_del_cron"], "ticker-raices.py")
        self.assertEqual(self.corredor.nombres(), ["ticker-raices"])

    def test_aplicar_guarda_el_id_releyendo_jobs_json_no_del_stdout(self):
        # El stdout del create viene SIN "Created job: <id>": si el estado
        # termina con el id, fue la relectura de jobs.json la que lo fijó.
        self.corredor.salida_del_create = ""
        self.assertEqual(self.aplicar(), 0)
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(estado["cron_id"], ID_FALSO)

    def test_doble_aplicar_no_duplica_el_cron(self):
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(self.aplicar(), 0)
        creates = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "create"]
        ]
        self.assertEqual(len(creates), 1, "el segundo aplicar no crea otro cron")
        self.assertEqual(self.corredor.nombres().count("ticker-raices"), 1)

    def test_aplicar_con_el_cron_ya_presente_reusa_su_id_y_no_crea_otro(self):
        self.sembrar_jobs_del_fixture()
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] == ["cron", "create"]
            ],
            [],
            "el cron del fixture ya está: ni un create",
        )
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(estado["cron_id"], "4c1f0e9d2b7a")

    def test_aplicar_con_jobs_ilegible_falla_cerrado_y_no_escribe_nada(self):
        self.sembrar_jobs_ilegible()
        self.assertEqual(self.aplicar(), 1)
        self.assertEqual(self.corredor.llamadas, [], "sin lectura no hay llamadas")
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())

    def test_aplicar_deja_un_envoltorio_que_corre_el_cron_real(self):
        # El instalador y el cron_ticker juntos en un home temporal: aplicar
        # escribe el envoltorio y registra el cron; el envoltorio delega en
        # el cron_ticker real del plugin, que con la puerta cerrada (no hay
        # gateway vivo) avisa CAPACIDAD_INCIERTA por stderr, corre la vuelta
        # cerrada y sale 0 sin crear tarjetas.
        self.assertEqual(self.aplicar(), 0)
        envoltorio = self.home / "scripts" / "ticker-raices.py"
        self.assertTrue(envoltorio.exists(), "aplicar escribio el envoltorio")
        self.assertIn("ticker-raices", self.corredor.nombres())

        entorno = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HERMES_HOME": str(self.home),
            # El doble del hermes_cli para el kanban y las capacidades: el
            # mismo arreglo del arranque del cron (F7).
            "PYTHONPATH": os.pathsep.join(
                [
                    str(REPO / "tests/fake_hermes_cli"),
                    str(REPO / "plugins/goncloud-team"),
                ]
            ),
            "PYTHONUTF8": "1",
        }
        proceso = subprocess.run(
            [sys.executable, str(envoltorio)],
            env=entorno,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proceso.returncode, 0, proceso.stderr)
        self.assertEqual(proceso.stdout, "", "el stdout del no_agent se entrega")
        self.assertIn("CAPACIDAD_INCIERTA (par-incierto)", proceso.stderr)
        self.assertIn("ruta de raices cerrada (par-incierto)", proceso.stderr)
        # La vuelta cerrada abrio el store (par-incierto, no
        # capacidad-faltante) y no despacho nada.
        self.assertTrue((self.home / "sistema-agentes.db").exists())
        self.assertFalse(
            list((self.home / "raices" / "proyecciones").glob("*.json"))
            if (self.home / "raices" / "proyecciones").exists()
            else False,
            "con la puerta cerrada la pasada del emisor no corre",
        )

    def test_aplicar_no_pisa_el_permisos_del_operador_y_sin_el_crea_la_plantilla(self):
        self.crear_datos_del_operador()
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(
            json.loads((self.home / "raices" / "permisos.json").read_text("utf-8")),
            {"acciones": ["read"], "toolsets": {}, "comandos": []},
            "los datos del operador son sagrados",
        )
        otro = tempfile.TemporaryDirectory()
        self.addCleanup(otro.cleanup)
        home2 = Path(otro.name) / "hermes"
        home2.mkdir()
        self.modulo.main(
            ["--aplicar", "--home", str(home2)],
            corredor=HermesFalsoDeCrones(home2),
        )
        plantilla = json.loads((home2 / "raices" / "permisos.json").read_text("utf-8"))
        self.assertEqual(
            plantilla,
            {"acciones": [], "toolsets": {}, "comandos": [], "cobertura": {}},
            "la plantilla niega todo: el operador la abre a mano",
        )


class Revertir(InstaladorDeRaices):
    def test_revertir_borra_por_job_id_no_por_nombre(self):
        self.crear_datos_del_operador()
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(self.revertir(), 0)
        removes = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "remove"]
        ]
        self.assertEqual(
            removes,
            [["cron", "remove", ID_FALSO]],
            "remove recibe el job_id de jobs.json, nunca el nombre",
        )
        self.assertEqual(self.corredor.nombres(), [], "el cron salió del jobs.json")
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertEqual(
            (self.home / "scripts" / "backup-claw.sh").read_text(encoding="utf-8"),
            "#!/bin/sh\nexit 0\n",
            "un script ajeno de scripts/ nunca se toca",
        )
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())
        self.assertTrue((self.home / "sistema-agentes.db").exists())
        self.assertTrue((self.home / "kanban.db").exists())
        self.assertTrue((self.home / "raices" / "permisos.json").exists())
        self.assertTrue((self.home / "raices" / "proyecciones" / "root.json").exists())
        self.assertEqual(
            json.loads((self.home / "raices" / "permisos.json").read_text("utf-8")),
            {"acciones": ["read"], "toolsets": {}, "comandos": []},
        )

    def test_revertir_sin_nombre_en_jobs_usa_el_id_guardado(self):
        self.assertEqual(self.aplicar(), 0)
        # Alguien renombró el job a mano: el id sigue siendo la verdad.
        jobs = self.corredor.jobs()
        jobs[0]["name"] = "renombrado-a-mano"
        self.corredor._guardar(jobs)
        self.assertEqual(self.revertir(), 0)
        removes = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "remove"]
        ]
        self.assertEqual(removes, [["cron", "remove", ID_FALSO]])
        self.assertEqual(self.corredor.jobs(), [])

    def test_revertir_sin_id_en_jobs_ni_en_el_estado_error_uno_y_no_borra(self):
        self.crear_datos_del_operador()
        (self.home / "scripts").mkdir(exist_ok=True)
        (self.home / "scripts" / "ticker-raices.py").write_text(
            "# envoltorio\n", "utf-8"
        )
        self.corredor.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.corredor.ruta_jobs.write_text(
            json.dumps(
                {"jobs": [{"id": "a9606c0778dd", "name": "monday-pedidos-semanal"}]}
            ),
            encoding="utf-8",
        )
        (self.home / "raices" / "instalacion.json").write_text(
            json.dumps({"version": 1, "cron": "ticker-raices", "cron_id": None}),
            encoding="utf-8",
        )
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.revertir()
        self.assertEqual(codigo, 1)
        self.assertIn("id", bufer.getvalue())
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] == ["cron", "remove"]
            ],
            [],
            "sin id no se invoca remove",
        )
        self.assertTrue((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertTrue((self.home / "raices" / "instalacion.json").exists())

    def test_revertir_con_jobs_ilegible_falla_cerrado_y_no_borra_nada(self):
        self.assertEqual(self.aplicar(), 0)
        self.sembrar_jobs_ilegible()
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.revertir()
        self.assertEqual(codigo, 1)
        self.assertIn("jobs.json", bufer.getvalue(), "el error nombra la causa")
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] == ["cron", "remove"]
            ],
            [],
        )
        self.assertTrue((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertTrue((self.home / "raices" / "instalacion.json").exists())

    def test_revertir_sin_instalacion_no_toca_nada(self):
        self.crear_datos_del_operador()
        self.assertEqual(self.revertir(), 0)
        self.assertEqual(self.corredor.llamadas, [], "sin estado no hay llamadas")
        self.assertTrue((self.home / "raices" / "permisos.json").exists())


class RemoveRoto(HermesFalsoDeCrones):
    """F2: el CLI rechaza el remove (igual que el hermes real con un
    PermissionError sobre jobs.json)."""

    def __call__(self, argv):
        if argv[:2] == ["cron", "remove"]:
            self.llamadas.append(list(argv))
            return 1, "Failed to remove job: PermissionError"
        return super().__call__(argv)


class RevertirRespetaElRemove(InstaladorDeRaices):
    """F2: si `hermes cron remove` falla, revertir NO borra envoltorio ni
    instalacion.json (igual que aplicar con un create rechazado): el error
    sale impreso y el código es 1."""

    def test_un_remove_fallido_no_borra_nada_y_sale_uno(self):
        self.assertEqual(self.aplicar(), 0)
        roto = RemoveRoto(self.home)
        roto.ids = self.corredor.ids
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.modulo.main(
                ["--revertir", "--home", str(self.home)], corredor=roto
            )
        self.assertEqual(codigo, 1)
        self.assertIn("remove", bufer.getvalue(), "el error sale impreso")
        self.assertTrue(
            (self.home / "scripts" / "ticker-raices.py").exists(),
            "el envoltorio queda: el remove falló",
        )
        self.assertTrue(
            (self.home / "raices" / "instalacion.json").exists(),
            "el estado queda: el remove falló",
        )
        self.assertEqual(self.corredor.nombres(), ["ticker-raices"])

    def test_un_remove_exitoso_si_borra_todo(self):
        self.assertEqual(self.aplicar(), 0)
        self.assertEqual(self.revertir(), 0)
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())
        self.assertEqual(self.corredor.nombres(), [])


class ElEjecutableDelCron(unittest.TestCase):
    """F1: en Windows el CLI es hermes.CMD y CreateProcess sin shell no
    resuelve el nombre pelado (WinError 2). correr_por_subprocess corre la
    RUTA que devuelve un resolvedor inyectable (shutil.which por omision,
    como capacidades._correr_hermes_version) y sin ejecutable da error claro
    con codigo 1, nunca FileNotFoundError."""

    def test_corre_la_ruta_resuelta_con_hermes_home_y_utf8(self):
        instalador = cargar_instalador()
        llamadas = []
        kwargs_vistos = {}

        def falso_run(argv, **kwargs):
            llamadas.append(list(argv))
            kwargs_vistos.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        home = Path("/su/raiz/hermes")
        with mock.patch.object(instalador.subprocess, "run", falso_run):
            codigo, salida = instalador.correr_por_subprocess(
                ["cron", "remove", ID_FALSO],
                home=home,
                resolvedor=lambda nombre: "/su/camino/bin/hermes.CMD",
            )
        self.assertEqual(codigo, 0)
        self.assertEqual(
            llamadas[0][:2],
            ["/su/camino/bin/hermes.CMD", "cron"],
            "argv[0] es la ruta resuelta, no 'hermes'",
        )
        self.assertEqual(
            kwargs_vistos.get("env", {}).get("HERMES_HOME"),
            str(home),
            "el CLI corre con el HERMES_HOME del --home, no el del proceso",
        )
        self.assertEqual(kwargs_vistos.get("encoding"), "utf-8")
        self.assertEqual(kwargs_vistos.get("errors"), "replace")

    def test_sin_hermes_resoluble_error_claro_y_codigo_uno(self):
        instalador = cargar_instalador()
        llamadas = []

        def falso_run(argv, **kwargs):
            llamadas.append(list(argv))
            raise AssertionError("sin ejecutable no se invoca subprocess")

        bufer = StringIO()
        with (
            mock.patch.object(instalador.subprocess, "run", falso_run),
            redirect_stdout(bufer),
        ):
            codigo, _salida = instalador.correr_por_subprocess(
                ["cron", "remove", ID_FALSO], resolvedor=lambda nombre: None
            )
        self.assertEqual(codigo, 1)
        self.assertEqual(llamadas, [], "sin ejecutable no se invoca subprocess")
        self.assertIn("hermes", bufer.getvalue())
        self.assertIn("no", bufer.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
