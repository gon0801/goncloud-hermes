import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

SCRIPTS = ROOT / "scripts"
PC_PYTHON = r"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"

VIGIA_FALSO = (
    "import os, sys\n"
    "open(os.environ['FAKE'] + '/seco.log', 'a').write(' '.join(sys.argv) + '\\n')\n"
    "maquina = sys.argv[sys.argv.index('--maquina') + 1] if '--maquina' in sys.argv else 'modelos'\n"
    "if os.environ.get('SECO_TRACEBACK_' + maquina.upper()):\n"
    "    raise RuntimeError('truena')\n"
    "sys.exit(int(os.environ.get('SECO_RC_' + maquina.upper(), os.environ.get('SECO_RC', '0'))))\n"
)

SSH = """#!/bin/sh
while [ "$1" = -o ]; do shift 2; done
host=$1
cmd=$2
printf '%s\n' "$host: $cmd" >> "$FAKE/ssh.log"
case $host in
  gwpc)
    cat > /dev/null
    case $cmd in
      *--seco*)
        [ -z "$SECO_TRACEBACK_PC" ] || { echo 'Traceback (most recent call last):' >&2; exit 1; }
        [ -z "$SECO_SYNTAX_PC" ] || { echo '  File "vigila_tareas.py", line 1' >&2; echo 'SyntaxError: invalid syntax' >&2; exit 1; }
        [ -z "$SECO_REPORTE_PC" ] || echo 'lastRunError: ValueError: citado en el reporte'
        exit "${SECO_RC_PC:-0}" ;;
      schtasks*) eval "$cmd" ;;
      *"schtasks /delete"*) schtasks /delete /f /tn GoncloudVigia ;;
      *"if not exist"*) exit "${FAKE_PC_SIN_COPIA:-0}" ;;
    esac
    ;;
  *) [ -n "$FAKE_MINI_CAIDA" ] && exit 255
     HOME=$FAKE_MINI_HOME exec sh -c "$cmd" ;;
esac
"""

LAUNCHCTL = """#!/bin/sh
printf '%s\n' "launchctl $*" >> "$FAKE/launchctl.log"
estado="$FAKE/launchd/$(echo "$HOME" | tr / _)"
mkdir -p "$estado"
case $1 in
  bootstrap) cp "$3" "$estado/$(basename "$3" .plist)" ;;
  bootout) rm -f "$estado/${2##*/}" ;;
  print) [ -e "$estado/${2##*/}" ] || exit 113 ;;
esac
"""

ROTO = "def f(:\n    pass\n"

SCHTASKS = """#!/bin/sh
printf '%s\n' "schtasks $*" >> "$FAKE/schtasks.log"
tarea="$FAKE/schtasks.GoncloudVigia"
case $1 in
  /create) while [ $# -gt 0 ]; do [ "$1" = /tr ] && printf '%s' "$2" > "$tarea"; shift; done ;;
  /query) [ -e "$tarea" ] || exit 1; printf '<Command>%s</Command>\n' "$(cat "$tarea")" ;;
  /delete) rm -f "$tarea" ;;
esac
"""


def escribe_ejecutable(ruta, texto):
    ruta.write_text(texto, encoding="utf-8")
    ruta.chmod(ruta.stat().st_mode | stat.S_IXUSR)


class InstalaVigias(unittest.TestCase):
    def setUp(self):
        directorio = tempfile.TemporaryDirectory(prefix="instala-vigias-")
        self.addCleanup(cleanup_temp_directory, directorio)
        self.tmp = Path(directorio.name)
        self.fake = self.tmp / "fake"
        self.mac = self.tmp / "mac"
        self.mini = self.tmp / "mini"
        for d in (self.fake, self.mac, self.mini, self.fake / "bin"):
            d.mkdir(parents=True)
        for nombre, texto in (
            ("ssh", SSH),
            ("launchctl", LAUNCHCTL),
            ("schtasks", SCHTASKS),
        ):
            escribe_ejecutable(self.fake / "bin" / nombre, texto)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for nombre in ("vigila_tareas.py", "vigila_modelos.py"):
            (self.repo / "scripts" / nombre).write_text(VIGIA_FALSO)
        for nombre in (
            "tareas-programadas.json",
            "model-routing.source.json",
            "claw-routing.json",
            "team.json",
        ):
            (self.repo / nombre).write_text("{}")
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "base"], check=True)
        self.sha = subprocess.run(
            [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.agentes = self.mac / "Library" / "LaunchAgents"
        self.agentes.mkdir(parents=True)
        (self.agentes / "ai.goncloud.vigila-modelos.plist").write_text(
            "<string>/Users/dn/dev/goncloud-hermes/scripts/vigila_modelos.py</string>"
        )

    def corre(self, script="instala_vigias.sh", *args, **extra):
        env = {
            **os.environ,
            "PATH": f"{self.fake / 'bin'}:{os.environ['PATH']}",
            "HOME": str(self.mac),
            "FAKE": str(self.fake),
            "FAKE_MINI_HOME": str(self.mini),
            "REPO": str(self.repo),
            "MOTOR_SHA": self.sha,
            "PYTHON_MAC": sys.executable,
            "PYTHON_MINI": sys.executable,
            **extra,
        }
        return subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def lee(self, nombre):
        ruta = self.fake / nombre
        return ruta.read_text() if ruta.exists() else ""

    def plist(self, home, etiqueta):
        with open(home / "Library" / "LaunchAgents" / f"{etiqueta}.plist", "rb") as f:
            return plistlib.load(f)

    def cargado(self, home, etiqueta):
        estado = self.fake / "launchd" / str(home).replace("/", "_")
        return (estado / etiqueta).exists()

    def instala(self, **extra):
        hecho = self.corre(**extra)
        self.assertEqual(hecho.returncode, 0, hecho.stdout + hecho.stderr)
        return hecho


class Instalacion(InstalaVigias):
    def test_deja_la_copia_fija_y_carga_el_vigia_en_mac_y_mini(self):
        copia_vieja = self.mac / "goncloud-vigia"
        copia_vieja.mkdir()
        (copia_vieja / "viejo").write_text("x")
        self.instala()
        for home, maquina in ((self.mac, "mac"), (self.mini, "mini")):
            dest = home / "goncloud-vigia"
            self.assertTrue((dest / "scripts" / "vigila_tareas.py").is_file())
            self.assertTrue((dest / "team.json").is_file())
            args = self.plist(home, "ai.goncloud.vigila-tareas")
            self.assertEqual(args["StartInterval"], 1800)
            self.assertEqual(
                args["ProgramArguments"][1:],
                [
                    f"{dest}/scripts/vigila_tareas.py",
                    "--maquina",
                    maquina,
                    "--raiz",
                    str(dest),
                ],
            )
            self.assertTrue(self.cargado(home, "ai.goncloud.vigila-tareas"))
        self.assertTrue(
            (copia_vieja.with_name("goncloud-vigia.anterior") / "viejo").is_file()
        )
        self.assertFalse((self.mac / "goncloud-vigia.nuevo").exists())

    def test_vigila_modelos_corre_desde_la_copia_fija_y_guarda_el_plist_viejo(self):
        self.instala()
        dest = self.mac / "goncloud-vigia"
        args = self.plist(self.mac, "ai.goncloud.vigila-modelos")["ProgramArguments"]
        self.assertEqual(
            args[1:4], [f"{dest}/scripts/vigila_modelos.py", "--raiz", str(dest)]
        )
        viejo = self.agentes / "ai.goncloud.vigila-modelos.plist.anterior"
        self.assertIn("/dev/goncloud-hermes", viejo.read_text())
        self.assertTrue(self.cargado(self.mac, "ai.goncloud.vigila-modelos"))
        self.assertTrue((dest / "avisos").is_symlink())

    def test_recarga_con_bootout_antes_de_bootstrap(self):
        self.instala()
        lineas = self.lee("launchctl.log").splitlines()
        etiqueta = "ai.goncloud.vigila-tareas"
        salida = next(
            i
            for i, linea in enumerate(lineas)
            if "bootout" in linea and etiqueta in linea
        )
        entrada = next(
            i
            for i, linea in enumerate(lineas)
            if "bootstrap" in linea and etiqueta in linea
        )
        self.assertLess(salida, entrada)

    def test_corre_el_seco_antes_de_encender_cada_vigia(self):
        self.instala()
        seco = self.lee("seco.log")
        for maquina in ("mac", "mini"):
            self.assertIn(f"vigila_tareas.py --maquina {maquina} --raiz", seco)
        self.assertIn("vigila_modelos.py --raiz", seco)
        self.assertEqual(seco.count("--seco"), 3)
        ssh = self.lee("ssh.log").splitlines()
        seco_pc = next(
            i for i, linea in enumerate(ssh) if "gwpc" in linea and "--seco" in linea
        )
        tarea_pc = next(i for i, linea in enumerate(ssh) if "schtasks /create" in linea)
        self.assertLess(seco_pc, tarea_pc)
        registro = (self.mac / "Library/Logs/goncloud/instala-vigias.log").read_text()
        for quien in (
            "mac: vigila_tareas",
            "mac: vigila_modelos",
            "mini: vigila_tareas",
            "pc: vigila_tareas",
        ):
            self.assertIn(f"{quien} --seco salió con 0", registro)

    def test_pc_crea_la_tarea_cada_30_minutos_con_el_python_de_hermes(self):
        self.instala()
        crea = next(
            linea
            for linea in self.lee("schtasks.log").splitlines()
            if "/create" in linea
        )
        self.assertIn("/tn GoncloudVigia /sc minute /mo 30", crea)
        tarea = self.lee("schtasks.GoncloudVigia")
        self.assertTrue(tarea.startswith(f'"{PC_PYTHON}"'), tarea)
        self.assertIn(
            r'"%LOCALAPPDATA%\goncloud-vigia\scripts\vigila_tareas.py" --maquina pc',
            tarea,
        )
        self.assertIn(r'--raiz "%LOCALAPPDATA%\goncloud-vigia"', tarea)

    def test_si_el_seco_truena_ese_vigia_no_se_enciende_y_la_copia_vieja_queda(self):
        copia_vieja = self.mini / "goncloud-vigia"
        copia_vieja.mkdir()
        (copia_vieja / "viejo").write_text("x")
        hecho = self.corre(SECO_RC_MINI="2")
        self.assertNotEqual(hecho.returncode, 0)
        self.assertIn("FALLA mini", hecho.stdout)
        self.assertTrue((copia_vieja / "viejo").is_file())
        self.assertFalse(self.cargado(self.mini, "ai.goncloud.vigila-tareas"))
        self.assertTrue(self.cargado(self.mac, "ai.goncloud.vigila-tareas"))

    def test_un_error_de_sintaxis_no_se_enciende(self):
        (self.repo / "scripts/vigila_tareas.py").write_text(ROTO)
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "commit", "-qam", "roto"], check=True)
        sha = subprocess.run(
            [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        hecho = self.corre(MOTOR_SHA=sha)
        self.assertNotEqual(hecho.returncode, 0, hecho.stdout)
        self.assertFalse(
            self.cargado(self.mini, "ai.goncloud.vigila-tareas"), hecho.stdout
        )

    def test_sintaxis_en_la_pc_no_crea_la_tarea(self):
        hecho = self.corre(SECO_SYNTAX_PC="1")
        self.assertNotEqual(hecho.returncode, 0, hecho.stdout)
        self.assertNotIn("schtasks /create", self.lee("schtasks.log"))

    def test_un_error_citado_en_el_reporte_no_bloquea(self):
        hecho = self.corre(SECO_REPORTE_PC="1")
        self.assertEqual(hecho.returncode, 0, hecho.stdout + hecho.stderr)

    def test_si_el_seco_de_la_pc_truena_no_crea_la_tarea(self):
        hecho = self.corre(SECO_RC_PC="2")
        self.assertNotEqual(hecho.returncode, 0)
        self.assertIn("FALLA pc", hecho.stdout)
        self.assertNotIn("schtasks /create", self.lee("ssh.log"))

    def test_una_maquina_caida_no_frena_a_las_otras(self):
        hecho = self.corre(FAKE_MINI_CAIDA="1")
        self.assertNotEqual(hecho.returncode, 0)
        self.assertIn("FALLA mini", hecho.stdout)
        self.assertTrue(self.cargado(self.mac, "ai.goncloud.vigila-tareas"))
        self.assertIn("schtasks /create", self.lee("ssh.log"))

    def test_sin_motor_sha_usa_origin_head(self):
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "update-ref",
                "refs/remotes/origin/HEAD",
                self.sha,
            ],
            check=True,
        )
        hecho = self.corre(MOTOR_SHA="")
        self.assertEqual(hecho.returncode, 0, hecho.stdout + hecho.stderr)
        registro = (self.mac / "Library/Logs/goncloud/instala-vigias.log").read_text()
        self.assertIn(f"instala_vigias {self.sha}", registro)
        self.assertTrue(
            (self.mac / "goncloud-vigia/scripts/vigila_tareas.py").is_file()
        )

    def test_sin_motor_sha_y_sin_origin_head_no_instala(self):
        hecho = self.corre(MOTOR_SHA="")
        self.assertNotEqual(hecho.returncode, 0)
        self.assertIn("no encuentro", hecho.stdout)
        self.assertFalse((self.mac / "goncloud-vigia").exists())

    def test_un_vigia_posix_que_truena_no_se_enciende_y_la_copia_vieja_queda(self):
        for maquina, home in (("mini", self.mini), ("mac", self.mac)):
            with self.subTest(maquina=maquina):
                self.setUp()
                copia_vieja = home / "goncloud-vigia"
                copia_vieja.mkdir(exist_ok=True)
                (copia_vieja / "viejo").write_text("x")
                hecho = self.corre(**{f"SECO_TRACEBACK_{maquina.upper()}": "1"})
                self.assertNotEqual(hecho.returncode, 0)
                self.assertIn(f"FALLA {maquina}", hecho.stdout)
                self.assertIn("truena (traceback)", hecho.stdout)
                self.assertTrue((copia_vieja / "viejo").is_file())
                self.assertFalse(self.cargado(home, "ai.goncloud.vigila-tareas"))

    def test_un_vigia_de_la_pc_que_truena_no_crea_la_tarea(self):
        hecho = self.corre(SECO_TRACEBACK_PC="1")
        self.assertNotEqual(hecho.returncode, 0)
        self.assertIn("FALLA pc", hecho.stdout)
        self.assertIn("truena (traceback)", hecho.stdout)
        self.assertNotIn("schtasks /create", self.lee("ssh.log"))


class Comprobacion(InstalaVigias):
    def setUp(self):
        super().setUp()
        self.instala()

    def comprueba(self, **extra):
        return self.corre("instala_vigias_checklist.sh", **extra)

    def test_sale_0_con_las_tres_maquinas_bien_y_acepta_seco_1(self):
        hecho = self.comprueba()
        self.assertEqual(hecho.returncode, 0, hecho.stdout)
        for maquina in ("mac", "mini", "pc"):
            self.assertIn(f"OK {maquina}", hecho.stdout)
        self.assertEqual(self.comprueba(SECO_RC="1").returncode, 0)

    def test_falla_si_el_seco_truena(self):
        hecho = self.comprueba(SECO_RC="2")
        self.assertEqual(hecho.returncode, 1)
        self.assertIn("--seco salió con 2", hecho.stdout)

    def test_falla_si_un_vigia_truena_aunque_salga_con_1(self):
        for maquina, home in (("mini", self.mini), ("mac", self.mac)):
            ruta = home / "goncloud-vigia/scripts/vigila_tareas.py"
            original = ruta.read_text()
            ruta.write_text("raise RuntimeError('truena')\n")
            hecho = self.comprueba()
            ruta.write_text(original)
            self.assertEqual(hecho.returncode, 1, hecho.stdout)
            self.assertNotIn(f"OK {maquina}", hecho.stdout)
            self.assertIn(f"FALLA {maquina}", hecho.stdout)
        hecho = self.comprueba(SECO_TRACEBACK_PC="1")
        self.assertEqual(hecho.returncode, 1, hecho.stdout)
        self.assertIn("FALLA pc", hecho.stdout)

    def test_sintaxis_no_pasa(self):
        (self.mini / "goncloud-vigia/scripts/vigila_tareas.py").write_text(ROTO)
        hecho = self.comprueba()
        self.assertNotEqual(hecho.returncode, 0, hecho.stdout)
        self.assertNotIn("OK mini", hecho.stdout)
        hecho = self.comprueba(SECO_SYNTAX_PC="1")
        self.assertNotEqual(hecho.returncode, 0, hecho.stdout)
        self.assertIn("FALLA pc", hecho.stdout)

    def test_falla_si_falta_la_copia_fija(self):
        shutil.rmtree(self.mini / "goncloud-vigia")
        hecho = self.comprueba()
        self.assertEqual(hecho.returncode, 1)
        self.assertIn("FALLA mini: falta la copia fija", hecho.stdout)

    def test_falla_si_el_vigia_no_esta_cargado(self):
        estado = self.fake / "launchd" / str(self.mini).replace("/", "_")
        (estado / "ai.goncloud.vigila-tareas").unlink()
        hecho = self.comprueba()
        self.assertEqual(hecho.returncode, 1)
        self.assertIn(
            "FALLA mini: ai.goncloud.vigila-tareas no está cargado", hecho.stdout
        )

    def test_falla_si_el_plist_apunta_a_dev_o_a_motor(self):
        for ruta in (f"{self.mac}/dev/x", f"{self.mac}/.motor/avisos"):
            plist = self.agentes / "ai.goncloud.vigila-tareas.plist"
            original = plist.read_text()
            plist.write_text(
                original.replace("</array>", f"<string>{ruta}</string></array>", 1)
            )
            hecho = self.comprueba()
            self.assertEqual(hecho.returncode, 1, ruta)
            self.assertIn("apunta a ~/dev o ~/.motor", hecho.stdout)
            plist.write_text(original)

    def test_falla_si_la_tarea_de_la_pc_apunta_a_dev(self):
        tarea = self.fake / "schtasks.GoncloudVigia"
        tarea.write_text(
            r'"C:\Python\python.exe" "C:\Users\ehven\dev\vigila_tareas.py"'
        )
        hecho = self.comprueba()
        self.assertEqual(hecho.returncode, 1)
        self.assertIn("FALLA pc", hecho.stdout)

    def test_falla_si_la_tarea_de_la_pc_no_existe(self):
        (self.fake / "schtasks.GoncloudVigia").unlink()
        hecho = self.comprueba()
        self.assertEqual(hecho.returncode, 1)
        self.assertIn("la tarea GoncloudVigia no existe", hecho.stdout)


class Rollback(InstalaVigias):
    def test_pone_el_anterior_y_quita_los_vigias_nuevos(self):
        for home in (self.mac, self.mini):
            (home / "goncloud-vigia").mkdir(exist_ok=True)
            (home / "goncloud-vigia" / "viejo").write_text("x")
        self.instala()
        hecho = self.corre("instala_vigias_rollback.sh")
        self.assertEqual(hecho.returncode, 0, hecho.stdout + hecho.stderr)
        for home in (self.mac, self.mini):
            self.assertTrue((home / "goncloud-vigia" / "viejo").is_file())
            self.assertFalse((home / "goncloud-vigia" / "scripts").exists())
            self.assertFalse(
                (home / "Library/LaunchAgents/ai.goncloud.vigila-tareas.plist").exists()
            )
            self.assertFalse(self.cargado(home, "ai.goncloud.vigila-tareas"))
        modelos = self.agentes / "ai.goncloud.vigila-modelos.plist"
        self.assertIn("/dev/goncloud-hermes", modelos.read_text())
        self.assertTrue(self.cargado(self.mac, "ai.goncloud.vigila-modelos"))
        self.assertFalse((self.fake / "schtasks.GoncloudVigia").exists())

    def test_sin_anterior_quita_tambien_la_copia_nueva(self):
        self.instala()
        self.assertEqual(self.corre("instala_vigias_rollback.sh").returncode, 0)
        self.assertFalse((self.mini / "goncloud-vigia").exists())


if __name__ == "__main__":
    unittest.main()
