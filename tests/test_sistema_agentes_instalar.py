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


class CreateRechazado(HermesFalsoDeCrones):
    """H5: el CLI real rechazó el create en la Gon-pc (el perfil pegajoso
    resolvió otro home y el script no estaba ahí)."""

    def _crear(self, argv):
        return (
            1,
            "Failed to create job: Script file not found: "
            "C:\\Users\\ehven\\AppData\\Local\\hermes\\scripts\\ticker-raices.py.",
        )


class CreateSinAlmacen(HermesFalsoDeCrones):
    """R4: un create que anuncia la salida que le toca pero NO escribe
    jobs.json: solo el stdout puede dar el id (o nadie)."""

    def __init__(self, home, salida):
        super().__init__(home)
        self.salida_del_create = salida

    def _crear(self, argv):
        return 0, self.salida_del_create


class CreateEscribeBytesRotos(HermesFalsoDeCrones):
    """R6: el create dice que hizo el job pero el almacén quedó con bytes
    que no son utf-8: la relectura tras crear revienta decodeando."""

    def _crear(self, argv):
        self.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.ruta_jobs.write_bytes(b"\xff\xfe{")
        return 0, "Created job: abc123def456"


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

    def sembrar_jobs_bytes_rotos(self):
        # R6: bytes que no son utf-8 (p. ej. un editor que dejó UTF-16):
        # read_text revienta decodeando, no al parsear JSON.
        self.corredor.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.corredor.ruta_jobs.write_bytes(b"\xff\xfe{")

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


class ElAlmacenConBytesRotos(InstaladorDeRaices):
    """R6 (CR 4306602154): jobs.json con bytes inválidos (b"\xff\xfe{") no
    debe tumbar al instalador con UnicodeDecodeError: es un almacén
    ilegible, fail-closed, código 1 en los tres modos."""

    def test_check_con_bytes_invalidos_sale_uno(self):
        self.sembrar_jobs_bytes_rotos()
        codigo, salida = self.chequear()
        self.assertEqual(codigo, 1)
        self.assertIn("jobs.json", salida)

    def test_aplicar_con_bytes_invalidos_sale_uno_y_no_escribe_nada(self):
        self.sembrar_jobs_bytes_rotos()
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.aplicar()
        self.assertEqual(codigo, 1)
        self.assertIn("jobs.json", bufer.getvalue())
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())

    def test_revertir_con_bytes_invalidos_sale_uno_y_no_borra_nada(self):
        self.assertEqual(self.aplicar(), 0)
        self.sembrar_jobs_bytes_rotos()
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.revertir()
        self.assertEqual(codigo, 1)
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

    def test_la_relectura_rota_tras_el_create_usa_el_stdout_del_id(self):
        # R6: si jobs.json revienta JUSTO después del create, el cron ya
        # existe: no se revienta, el id sale del stdout y el estado queda.
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.modulo.main(
                ["--aplicar", "--home", str(self.home)],
                corredor=CreateEscribeBytesRotos(self.home),
            )
        self.assertEqual(codigo, 0)
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(estado["cron_id"], "abc123def456")
        self.assertTrue((self.home / "scripts" / "ticker-raices.py").exists())


class Aplicar(InstaladorDeRaices):
    def test_aplicar_crea_el_cron_en_default_y_el_envoltorio(self):
        self.assertEqual(self.aplicar(), 0)
        create = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "create"]
        ]
        self.assertEqual(len(create), 1, "un solo create")
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

    def test_aplicar_con_create_rechazado_imprime_la_salida_del_cli(self):
        # H5: "hermes rechazó el cron (código 1)" a secas no dice nada: la
        # causa del CLI ("Failed to create job: ...") sale impresa, igual
        # que el remove fallido.
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.modulo.main(
                ["--aplicar", "--home", str(self.home)],
                corredor=CreateRechazado(self.home),
            )
        self.assertEqual(codigo, 1)
        self.assertIn("Failed to create job: Script file not found:", bufer.getvalue())
        self.assertFalse(
            (self.home / "scripts" / "ticker-raices.py").exists(),
            "el create falló: el envoltorio se desenlaza",
        )

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

    def test_aplicar_con_el_cron_presente_sin_id_avisa_y_deja_cron_id_nulo(self):
        # R3 (bot F3): el job ya está pero su registro no trae id textual.
        # Mismo aviso que la rama de create; nunca null en silencio.
        self.corredor.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.corredor.ruta_jobs.write_text(
            json.dumps({"jobs": [{"name": "ticker-raices", "enabled": True}]}),
            encoding="utf-8",
        )
        bufer = StringIO()
        with redirect_stdout(bufer):
            self.assertEqual(self.aplicar(), 0)
        self.assertIn("aviso", bufer.getvalue())
        self.assertIn("id", bufer.getvalue())
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(estado["cron_id"])
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] == ["cron", "create"]
            ],
            [],
        )

    def test_el_id_respalda_del_stdout_cuando_el_create_no_escribe_jobs(self):
        # R4 (bot F4): el create anuncia "Created job: <id>" pero no dejó
        # nada en jobs.json: el id sale del stdout de todos modos.
        self.assertEqual(
            self.modulo.main(
                ["--aplicar", "--home", str(self.home)],
                corredor=CreateSinAlmacen(
                    self.home, "Created job: zz99aa66bb44\n  Name: ticker-raices"
                ),
            ),
            0,
        )
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(estado["cron_id"], "zz99aa66bb44")

    def test_sin_id_en_ninguna_fuente_avisa_y_deja_cron_id_nulo(self):
        # R4: el create "funcionó" pero ni jobs.json ni el stdout traen id:
        # el estado queda null y el aviso sale impreso.
        bufer = StringIO()
        with redirect_stdout(bufer):
            self.assertEqual(
                self.modulo.main(
                    ["--aplicar", "--home", str(self.home)],
                    corredor=CreateSinAlmacen(self.home, "hecho sin id"),
                ),
                0,
            )
        self.assertIn("aviso", bufer.getvalue())
        estado = json.loads(
            (self.home / "raices" / "instalacion.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(estado["cron_id"])

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

    def test_revertir_sin_huellas_del_cron_lo_da_por_retirado_y_sale_cero(self):
        # R1: sin id guardado y sin el nombre en el almacén, el job ya no
        # está: no se llama remove (fallaría para siempre), se desenlaza
        # envoltorio y estado y se sale 0 avisándolo.
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
        self.assertEqual(codigo, 0)
        self.assertIn("retirado", bufer.getvalue())
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] == ["cron", "remove"]
            ],
            [],
            "sin huellas no se invoca remove",
        )
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())
        self.assertTrue((self.home / "raices" / "permisos.json").exists())

    def test_revertir_con_el_cron_borrado_a_mano_desatasca_la_reversa(self):
        # R1 (bot F1): el operador borró el cron a mano (aquí: el almacén
        # completo). El id guardado ya no vive: un remove con él fallaría y
        # dejaría la reversa atascada en código 1 para siempre. Ahora: sin
        # remove, desenlace y 0.
        self.assertEqual(self.aplicar(), 0)
        self.corredor.ruta_jobs.unlink()
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.revertir()
        self.assertEqual(codigo, 0)
        self.assertIn("retirado", bufer.getvalue())
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] == ["cron", "remove"]
            ],
            [],
            "el id obsoleto no llega a remove",
        )
        self.assertFalse((self.home / "scripts" / "ticker-raices.py").exists())
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())

    def test_revertir_con_id_muerto_no_borra_un_ajeno_con_el_nombre(self):
        # R1+R2: nuestro id murió pero hay un job ajeno que se llama
        # ticker-raices. Nunca se borra por nombre: se da por retirado lo
        # nuestro y el ajeno queda intacto.
        self.assertEqual(self.aplicar(), 0)
        self.corredor._guardar(
            [{"id": "ajeno1234567", "name": "ticker-raices", "enabled": True}]
        )
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = self.revertir()
        self.assertEqual(codigo, 0)
        self.assertEqual(
            [
                llamada
                for llamada in self.corredor.llamadas
                if llamada[:2] == ["cron", "remove"]
            ],
            [],
            "el ajeno con el nombre no se toca",
        )
        self.assertEqual(self.corredor.nombres(), ["ticker-raices"], "el ajeno vive")
        self.assertIn("ticker-raices", bufer.getvalue(), "el aviso lo nombra")
        self.assertFalse((self.home / "raices" / "instalacion.json").exists())

    def test_revertir_prefiere_el_id_guardado_vivo_sobre_el_nombre(self):
        # R2: el cron_id guardado vivo manda. Nuestro job fue renombrado a
        # mano y OTRO cron ajeno ocupa el nombre: se borra el nuestro por
        # id y el ajeno jamás.
        self.assertEqual(self.aplicar(), 0)
        jobs = self.corredor.jobs()
        jobs[0]["name"] = "renombrado-a-mano"
        jobs.append({"id": "ajeno1234567", "name": "ticker-raices", "enabled": True})
        self.corredor._guardar(jobs)
        self.assertEqual(self.revertir(), 0)
        removes = [
            llamada
            for llamada in self.corredor.llamadas
            if llamada[:2] == ["cron", "remove"]
        ]
        self.assertEqual(removes, [["cron", "remove", ID_FALSO]])
        self.assertEqual(self.corredor.nombres(), ["ticker-raices"], "el ajeno vive")

    def test_revertir_con_el_nombre_presente_sin_id_en_ninguna_parte_error_uno(self):
        # El job SÍ está en el almacén (por nombre) pero ni su registro ni
        # el estado traen id textual: no hay remove seguro. Fail-closed.
        self.crear_datos_del_operador()
        (self.home / "scripts").mkdir(exist_ok=True)
        (self.home / "scripts" / "ticker-raices.py").write_text(
            "# envoltorio\n", "utf-8"
        )
        self.corredor.ruta_jobs.parent.mkdir(parents=True, exist_ok=True)
        self.corredor.ruta_jobs.write_text(
            json.dumps({"jobs": [{"name": "ticker-raices", "enabled": True}]}),
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
            llamadas[0],
            [
                "/su/camino/bin/hermes.CMD",
                "-p",
                "default",
                "cron",
                "remove",
                ID_FALSO,
            ],
            "argv[0] es la ruta resuelta y el perfil va explícito",
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

    def test_el_timeout_sale_con_codigo_distinto_de_cero_y_causa_clara(self):
        # R5 (CR 4306602145): el CLI colgado no tumba al instalador con un
        # traceback de TimeoutExpired: el corredor devuelve código != 0 con
        # la causa, para que aplicar desenlace el envoltorio y revertir la
        # imprima.
        instalador = cargar_instalador()

        def falso_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=60)

        with mock.patch.object(instalador.subprocess, "run", falso_run):
            codigo, salida = instalador.correr_por_subprocess(
                ["cron", "create", "every 1m"],
                home=Path("/su/raiz/hermes"),
                resolvedor=lambda nombre: "/su/camino/bin/hermes.CMD",
            )
        self.assertNotEqual(codigo, 0)
        self.assertIn("timeout", salida.lower())
        self.assertIn("60", salida)

    def test_con_stdout_vacio_la_salida_es_el_stderr(self):
        # H5: el error del create llegó por stdout en la Gon-pc, pero si un
        # CLI lo imprime solo en stderr, la salida que entrega el corredor
        # (create y remove) es ese stderr.
        instalador = cargar_instalador()

        def falso_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="el error del CLI"
            )

        with mock.patch.object(instalador.subprocess, "run", falso_run):
            codigo, salida = instalador.correr_por_subprocess(
                ["cron", "create", "every 1m"],
                home=Path("/su/raiz/hermes"),
                resolvedor=lambda nombre: "/su/camino/bin/hermes.CMD",
            )
        self.assertEqual(codigo, 1)
        self.assertEqual(salida, "el error del CLI")

    def test_con_ruido_en_stdout_la_causa_del_stderr_tambien_sale(self):
        # r2 (bot): un CLI que suelta ruido por stdout y la causa por stderr
        # no puede tapar la causa: la salida que entrega el corredor trae
        # ambas.
        instalador = cargar_instalador()

        def falso_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 1, stdout="aviso inofensivo", stderr="la causa real"
            )

        with mock.patch.object(instalador.subprocess, "run", falso_run):
            codigo, salida = instalador.correr_por_subprocess(
                ["cron", "create", "every 1m"],
                home=Path("/su/raiz/hermes"),
                resolvedor=lambda nombre: "/su/camino/bin/hermes.CMD",
            )
        self.assertEqual(codigo, 1)
        self.assertIn("aviso inofensivo", salida)
        self.assertIn("la causa real", salida)

    def test_toda_llamada_lleva_p_default_antes_de_cron(self):
        # H5: el CLI resuelve el perfil ACTIVO pegajoso por encima de
        # HERMES_HOME (medido en la Gon-pc: sin -p el cron cayó en claw);
        # toda llamada por subprocess.run va con -p default explícito,
        # en create y en remove.
        instalador = cargar_instalador()
        llamadas = []

        def falso_run(argv, **kwargs):
            llamadas.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(instalador.subprocess, "run", falso_run):
            for llamada in (
                ["cron", "create", "every 1m", "--name", "ticker-raices"],
                ["cron", "remove", ID_FALSO],
            ):
                instalador.correr_por_subprocess(
                    llamada,
                    home=Path("/su/raiz/hermes"),
                    resolvedor=lambda nombre: "/su/camino/bin/hermes.CMD",
                )
        self.assertEqual(len(llamadas), 2, "create y remove pasaron por subprocess")
        for argv in llamadas:
            self.assertEqual(
                argv[argv.index("cron") - 2 : argv.index("cron")],
                ["-p", "default"],
                "el perfil va explícito: -p default justo antes de cron",
            )


if __name__ == "__main__":
    unittest.main()
