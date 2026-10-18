"""Los plugins externos del director: manifiesto versionado y parche del repo.

Los literales de este archivo son la fijación: si alguien cambia un sha del
manifiesto, toca el .patch o lo pierde, estas pruebas se ponen rojas. Los
sha256 se calcularon clonando los repos públicos al sha fijado y aplicando el
parche con git, en bytes LF tal como los deja git (encargo H-jev r1,
2026-10-18).
"""

import hashlib
import importlib.util
import json
import os
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "configure_team", ROOT / "scripts/configure_team.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

MANIFIESTO = json.loads((ROOT / "director-plugins.json").read_text(encoding="utf-8"))
PATCH = ROOT / "patches/claw/jev-skill-router-mcp/0001-model-en-call-adapter.patch"

JEV_REPO = "https://github.com/ajensenwaud/hermes-jev-plugin"
JSR_REPO = "https://github.com/ydmw74/jev-skill-router"
JEV_SHA = "b3d29f71770a3447ad0b3db00658f64a8edc7dd3"
JSR_SHA = "76ee09e049dbc10c76f98cfefc6ca16b42cfad38"
PATCH_SHA = "fb7c8aa20f8f6cfd85fd4035777e3215c1c716eaf429fb11f0223f4d1c89d879"
MCP_SERVER = "src/jev_skill_router/mcp_server.py"
PRISTINE_SHA = "eb28e49d7815d1e200f42ae52c6545c529ecbfb28fb31f935d28f4c8d1aa4e04"
PATCHED_SHA = "d05f612687eab85578d7f66cb7f8860eb341e669f0a6bcf087dcc397b953d68b"
JEV_PATCH = ROOT / "patches/claw/jev/0001-secret-scope.patch"
JEV_PATCH_SHA = "4a82061e6241dc7f64c41aeb13d7115f32be44f7b23b7e93d4112a8a62f0306c"
JEV_CLIENT = "client.py"
JEV_PRISTINE_SHA = "778a2ac4ec509e56ac4ddbb47f2344c48e2cd308adb0686793ac123f9430bf67"
JEV_PATCHED_SHA = "b91c5be49e9a21d629406fe6057f41172ab1c5aca4b93dde533fd1ae3edabc74"


class ManifiestoTests(unittest.TestCase):
    def test_fija_exactamente_los_dos_plugins_en_orden(self):
        self.assertEqual(MANIFIESTO["director"], "claw")
        self.assertEqual(
            [plugin["name"] for plugin in MANIFIESTO["plugins"]],
            ["jev", "jev-skill-router-mcp"],
        )
        jev, jsr = MANIFIESTO["plugins"]
        self.assertEqual(
            jev,
            {
                "name": "jev",
                "repo": JEV_REPO,
                "sha": JEV_SHA,
                "toolsets": ["jev"],
                "patches": [
                    {
                        "file": JEV_CLIENT,
                        "patch": "patches/claw/jev/0001-secret-scope.patch",
                        "sha256_pristine": JEV_PRISTINE_SHA,
                        "sha256_patched": JEV_PATCHED_SHA,
                    }
                ],
            },
        )
        self.assertEqual(jsr["repo"], JSR_REPO)
        self.assertEqual(jsr["sha"], JSR_SHA)
        self.assertEqual(jsr["toolsets"], [])
        self.assertEqual(
            jsr["patches"],
            [
                {
                    "file": MCP_SERVER,
                    "patch": "patches/claw/jev-skill-router-mcp/0001-model-en-call-adapter.patch",
                    "sha256_pristine": PRISTINE_SHA,
                    "sha256_patched": PATCHED_SHA,
                }
            ],
        )

    def test_el_patch_del_repo_no_cambia_ni_se_pierde(self):
        for ruta, sha, archivo in (
            (JEV_PATCH, JEV_PATCH_SHA, JEV_CLIENT),
            (PATCH, PATCH_SHA, MCP_SERVER),
        ):
            with self.subTest(patch=ruta.name):
                self.assertTrue(ruta.is_file())
                self.assertEqual(hashlib.sha256(ruta.read_bytes()).hexdigest(), sha)
                lineas = ruta.read_text(encoding="utf-8").splitlines()
                self.assertIn(f"diff --git a/{archivo} b/{archivo}", lineas)
                self.assertIn(f"--- a/{archivo}", lineas)
                self.assertIn(f"+++ b/{archivo}", lineas)

    def test_los_parches_quedan_fuera_de_la_conversion_de_fin_de_linea(self):
        # Con autocrlf el .patch baja con CRLF y su sha256 cambia; peor: el
        # git apply del despliegue leeria un parche CRLF. La regla vive en
        # .gitattributes y esta prueba la lee con git check-attr para que
        # valga en cualquier arbol que corra la bateria.
        try:
            resultado = subprocess.run(
                ["git", "check-attr", "text", "--", PATCH.relative_to(ROOT).as_posix()],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except OSError:
            self.skipTest("git no esta disponible")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("text: unset", resultado.stdout)


class ConfiguracionDelDirectorTests(unittest.TestCase):
    def test_solo_el_director_declara_los_plugins_y_su_toolset(self):
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        for role in team["profiles"]:
            with self.subTest(rol=role):
                desired = module.desired_profile_config(role, team, Path("C:/home"))
                if role == team["director"]:
                    self.assertEqual(
                        desired["plugins"]["enabled"],
                        ["goncloud-team", "jev", "jev-skill-router-mcp"],
                    )
                    esperados = [*module._DIRECTOR_TOOLSETS, "jev"]
                    self.assertEqual(desired["platform_toolsets"]["cli"], esperados)
                    self.assertEqual(
                        desired["platform_toolsets"]["telegram"], esperados
                    )
                    self.assertEqual(desired["platform_toolsets"]["cli"][-1], "jev")
                else:
                    self.assertEqual(desired["plugins"]["enabled"], ["goncloud-team"])
                    self.assertNotIn("jev", desired["platform_toolsets"]["cli"])
                    self.assertNotIn("jev", desired["platform_toolsets"]["telegram"])

    def test_el_config_vivo_de_claw_hoy_mergea_sin_conflicto(self):
        # El config.yaml vivo de la PC ya declara los plugins del manifiesto y
        # el toolset con jev al final; el deploy debe leerlo como reconciliado,
        # sin conflicto y sin diff (encargo H-jev r1, 2026-10-18).
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        con_jev = [*module._DIRECTOR_TOOLSETS, "jev"]
        existing = {
            "plugins": {"enabled": ["goncloud-team", "jev", "jev-skill-router-mcp"]},
            "platform_toolsets": {"cli": con_jev, "telegram": list(con_jev)},
        }
        previous = {
            "plugins": {"enabled": ["goncloud-team"]},
            "platform_toolsets": {
                "cli": list(module._DIRECTOR_TOOLSETS),
                "telegram": list(module._DIRECTOR_TOOLSETS),
            },
        }
        desired = module.desired_profile_config("claw", team, Path("C:/home"))

        merged = module.merge_profile_config(existing, desired, previous)

        self.assertEqual(merged["plugins"], existing["plugins"])
        self.assertEqual(merged["platform_toolsets"], existing["platform_toolsets"])


PRISTINA = "def call_adapter():\n    return {}\n"
PATCHADA = "def call_adapter():\n    document = {'model': 'x'}\n    return {}\n"
MCP_SERVIDOR = "src/jev_skill_router/mcp_server.py"
JEV_CLIENTE = "client.py"
PRISTINA_JEV = "def get_secret(nombre):\n    return os.environ[nombre]\n"
PATCHADA_JEV = (
    "def get_secret(nombre):\n    return agent.secret_scope.get_secret(nombre)\n"
)


def _sha256(datos):
    return hashlib.sha256(datos).hexdigest()


def _git_en(ruta, *args):
    # Los fixtures se crean sin conversion de fin de linea, cueste lo que
    # cueste el autocrlf del sistema o del GIT_CONFIG_GLOBAL del entorno: el
    # -c gana sobre cualquier config global.
    return subprocess.run(
        ["git", "-C", str(ruta), "-c", "core.autocrlf=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _con_chmod(func, ruta, _exc):
    # Los objetos de .git quedan de solo lectura y el borrador los rechaza
    # (WinError 5 en la PC): permiso al que fallo y a su padre — en POSIX el
    # permiso de borrar vive en el directorio — y reintento.
    os.chmod(ruta, stat.S_IRWXU)
    padre = os.path.dirname(ruta)
    if padre:
        try:
            os.chmod(padre, stat.S_IRWXU)
        except OSError:
            pass
    func(ruta)


def _borrar(ruta):
    """rmtree con el handler de solo-lectura, en la API del Python de turno.

    El Python 3.11 del venv de Hermes en la PC no tiene onexc (llego en
    3.12) y su onerror recibe exc_info en vez de la excepcion: el handler es
    uno solo porque ignora el tercer argumento.
    """
    if sys.version_info >= (3, 12):
        shutil.rmtree(ruta, onexc=_con_chmod)
    else:
        shutil.rmtree(ruta, onerror=_con_chmod)


def _limpiar(tmp):
    """TemporaryDirectory cleanup that survives git's read-only files."""
    try:
        tmp.cleanup()
    except OSError:
        _borrar(tmp.name)


def _repo(ruta, archivos):
    # Los archivos del fixture van en bytes: un write_text en Windows
    # traduce \n a CRLF y el sha256 ya no es el que fija la prueba.
    ruta.mkdir(parents=True)
    for nombre, texto in archivos.items():
        destino = ruta / nombre
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(texto.encode("utf-8"))
    _git_en(ruta, "init", "-q")
    _git_en(ruta, "add", "-A")
    _git_en(
        ruta, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"
    )
    return _git_en(ruta, "rev-parse", "HEAD").stdout.strip()


class ConciliacionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(_limpiar, tmp)
        self.tmp = tmp
        base = Path(tmp.name)
        # La PC clona con core.autocrlf=true de sistema (Git for Windows): el
        # mismo config hostil, simulado con GIT_CONFIG_GLOBAL, reproduce aqui
        # lo que el checkout CRLF le hizo a los fixtures (encargo H-jev r2).
        # El patcher se registra despues de la limpieza: con LIFO el entorno
        # se restaura antes de borrar el directorio.
        hostil = base / "gitconfig-autocrlf-true"
        hostil.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
        activo = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(hostil)})
        activo.start()
        self.addCleanup(activo.stop)
        repo_jev = base / "jev"
        self.jev_sha = _repo(
            repo_jev, {"plugin.yaml": "name: jev\n", JEV_CLIENTE: PRISTINA_JEV}
        )
        self.jev_sha256_pristino = _sha256(PRISTINA_JEV.encode())
        (repo_jev / JEV_CLIENTE).write_text(PATCHADA_JEV, encoding="utf-8")
        diff_jev = _git_en(
            repo_jev,
            "diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
            JEV_CLIENTE,
        ).stdout
        (base / "0001-jev-sintetico.patch").write_bytes(diff_jev.encode("utf-8"))
        self.jev_sha256_parchado = _sha256(PATCHADA_JEV.encode())
        _git_en(repo_jev, "checkout", "--", JEV_CLIENTE)
        repo_jsr = base / "jsr"
        self.jsr_sha = _repo(repo_jsr, {MCP_SERVIDOR: PRISTINA})
        self.sha256_pristino = _sha256(PRISTINA.encode())
        (repo_jsr / MCP_SERVIDOR).write_text(PATCHADA, encoding="utf-8")
        diff = _git_en(
            repo_jsr,
            "diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--",
            MCP_SERVIDOR,
        ).stdout
        (base / "0001-sintetico.patch").write_bytes(diff.encode("utf-8"))
        self.sha256_parchado = _sha256(PATCHADA.encode())
        _git_en(repo_jsr, "checkout", "--", MCP_SERVIDOR)
        self.plugins = [
            {
                "name": "jev",
                "repo": "https://example.com/jev",
                "sha": self.jev_sha,
                "toolsets": ["jev"],
                "patches": [
                    {
                        "file": JEV_CLIENTE,
                        "patch": str(base / "0001-jev-sintetico.patch"),
                        "sha256_pristine": self.jev_sha256_pristino,
                        "sha256_patched": self.jev_sha256_parchado,
                    }
                ],
            },
            {
                "name": "jev-skill-router-mcp",
                "repo": "https://example.com/jsr",
                "sha": self.jsr_sha,
                "toolsets": [],
                "patches": [
                    {
                        "file": MCP_SERVIDOR,
                        "patch": str(base / "0001-sintetico.patch"),
                        "sha256_pristine": self.sha256_pristino,
                        "sha256_patched": self.sha256_parchado,
                    }
                ],
            },
        ]
        self.perfil = base / "perfiles" / "claw"
        for plugin, plantilla in zip(self.plugins, ("jev", "jsr")):
            shutil.copytree(base / plantilla, self.perfil / "plugins" / plugin["name"])
        self.servidor = self.perfil / "plugins" / "jev-skill-router-mcp" / MCP_SERVIDOR
        self.cliente = self.perfil / "plugins" / "jev" / JEV_CLIENTE
        # jev arranca parchado (verde) y jsr prístino ("Parche por aplicar"):
        # el estado vivo del director tras el r1; cada prueba que necesita el
        # otro estado lo escribe.
        self.cliente.write_bytes(PATCHADA_JEV.encode("utf-8"))

    def test_parchado_esta_verde_y_apply_no_hace_nada(self):
        self.servidor.write_bytes(PATCHADA.encode("utf-8"))
        self.assertEqual(
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False),
            [],
        )
        antes = self.servidor.read_bytes()
        module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True)
        self.assertEqual(self.servidor.read_bytes(), antes)

    def test_prisitino_reporta_y_apply_lo_deja_parchado(self):
        notas = module.conciliar_plugins_director(
            self.perfil, self.plugins, aplicar=False
        )
        self.assertEqual(
            notas, [f"Parche por aplicar: jev-skill-router-mcp: {MCP_SERVIDOR}"]
        )
        module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True)
        self.assertEqual(self.servidor.read_text(), PATCHADA)
        self.assertEqual(_sha256(self.servidor.read_bytes()), self.sha256_parchado)
        segunda = module.conciliar_plugins_director(
            self.perfil, self.plugins, aplicar=True
        )
        estable = self.servidor.read_bytes()
        self.assertEqual(segunda, [])
        self.assertEqual(self.servidor.read_bytes(), estable)

    def test_jev_parchado_esta_verde_y_apply_no_hace_nada(self):
        # El estado que dejó el parche manual de la PC (encargo H-jev2): el
        # client.py de jev ya trae el secret_scope, check verde y apply es
        # un no-op byte a byte.
        self.servidor.write_bytes(PATCHADA.encode("utf-8"))
        self.assertEqual(
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False),
            [],
        )
        antes = self.cliente.read_bytes()
        module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True)
        self.assertEqual(self.cliente.read_bytes(), antes)

    def test_jev_pristino_reporta_y_apply_lo_deja_parchado(self):
        self.servidor.write_bytes(PATCHADA.encode("utf-8"))
        self.cliente.write_bytes(PRISTINA_JEV.encode("utf-8"))
        notas = module.conciliar_plugins_director(
            self.perfil, self.plugins, aplicar=False
        )
        self.assertEqual(notas, [f"Parche por aplicar: jev: {JEV_CLIENTE}"])
        module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True)
        self.assertEqual(self.cliente.read_text(), PATCHADA_JEV)
        self.assertEqual(_sha256(self.cliente.read_bytes()), self.jev_sha256_parchado)
        self.assertEqual(
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True),
            [],
        )

    def test_otro_contenido_es_rojo_sin_tocar_nada(self):
        self.servidor.write_text("otro\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no coincide"):
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False)
        with self.assertRaisesRegex(ValueError, "no coincide"):
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True)
        self.assertEqual(self.servidor.read_text(), "otro\n")

    def test_prisitino_con_crlf_aplica_y_queda_en_lf(self):
        # Lo que ve la PC: `hermes plugins` clono con autocrlf=true y el
        # archivo prístino esta en CRLF. El contenido es prístino, el check
        # lo reporta y el apply canóniza y deja los bytes LF del sha.
        self.servidor.write_bytes(PRISTINA.replace("\n", "\r\n").encode("utf-8"))
        notas = module.conciliar_plugins_director(
            self.perfil, self.plugins, aplicar=False
        )
        self.assertEqual(
            notas, [f"Parche por aplicar: jev-skill-router-mcp: {MCP_SERVIDOR}"]
        )
        module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True)
        self.assertEqual(self.servidor.read_bytes(), PATCHADA.encode("utf-8"))
        self.assertEqual(
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True),
            [],
        )

    def test_el_parchado_en_crlf_canoniza_a_lf(self):
        self.servidor.write_bytes(PATCHADA.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False),
            [],
        )
        module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=True)
        self.assertEqual(self.servidor.read_bytes(), PATCHADA.encode("utf-8"))

    def test_otro_sha_es_rojo_con_instruccion(self):
        self.plugins[1]["sha"] = "0" * 40
        with self.assertRaises(ValueError) as contexto:
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False)
        mensaje = str(contexto.exception)
        self.assertIn("fija 0000000", mensaje)
        self.assertIn("hermes plugins", mensaje)

    def test_plugin_ausente_es_rojo_con_instruccion(self):
        _borrar(self.perfil / "plugins" / "jev")
        with self.assertRaises(ValueError) as contexto:
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False)
        mensaje = str(contexto.exception)
        self.assertIn("no está instalado", mensaje)
        self.assertIn("hermes plugins", mensaje)
        self.assertIn("https://example.com/jev", mensaje)
        self.assertIn(self.jev_sha, mensaje)

    def test_cambios_con_seguimiento_ajenos_son_rojos(self):
        (self.perfil / "plugins" / "jev" / "plugin.yaml").write_text(
            "name: jev\neditado\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "fuera del parche"):
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False)

    def test_lo_untracked_se_tolera(self):
        self.servidor.write_bytes(PATCHADA.encode("utf-8"))
        (self.perfil / "plugins" / "jev" / ".hermes-catalog.json").write_text(
            "{}", encoding="utf-8"
        )
        cache = (
            self.perfil
            / "plugins"
            / "jev-skill-router-mcp"
            / "src"
            / "jev_skill_router"
            / "__pycache__"
        )
        cache.mkdir(parents=True)
        (cache / "x.pyc").write_bytes(b"x")
        self.assertEqual(
            module.conciliar_plugins_director(self.perfil, self.plugins, aplicar=False),
            [],
        )

    def test_la_limpieza_gana_a_los_objetos_de_solo_lectura(self):
        # Simulacion del WinError 5 de la PC: git deja .git/objects de solo
        # lectura. En POSIX el 0o444 del archivo no basta para reproducirlo,
        # asi que los directorios de .git van en 0o555 y el borrador corre
        # por _borrar: sin el chmod, falla en las dos plataformas. El arbol
        # va dentro del tmp del setUp, cuya limpieza ya tolera solo-lectura.
        base = Path(self.tmp.name) / "solo-lectura"
        objetos = base / "repo" / ".git" / "objects" / "pack"
        objetos.mkdir(parents=True)
        (objetos / "pack-abc.pack").write_bytes(b"x")
        for elemento in sorted(objetos.rglob("*")):
            elemento.chmod(0o444 if elemento.is_file() else 0o555)
        directorio = objetos
        while directorio != Path(self.tmp.name):
            directorio.chmod(0o555)
            directorio = directorio.parent
        _borrar(base)
        self.assertFalse(base.exists())

    def test_el_mismo_handler_sirve_para_la_api_onerror_de_3_11(self):
        # La PC corre 3.11: ahi _borrar usa onerror y el handler recibe
        # exc_info en el tercer argumento. Esta prueba ejercita esa rama
        # tambien en un Python nuevo, donde onerror sigue vivo pero
        # deprecado.
        base = Path(self.tmp.name) / "onerror"
        objetos = base / ".git" / "objects"
        objetos.mkdir(parents=True)
        (objetos / "b9").mkdir()
        objeto = objetos / "b9" / "b957c0"
        objeto.write_bytes(b"x")
        objeto.chmod(0o444)
        objetos.chmod(0o555)
        objetos.parent.chmod(0o555)
        shutil.rmtree(base, onerror=_con_chmod)
        self.assertFalse(base.exists())


class DespliegueFrescoTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, tmp)
        self.temporary = Path(tmp.name)
        self.runtime = self.temporary / "runtime"

    def run_configure(self, *extra):
        # El yaml de esta Mac vive en el user-site del HOME real; con el HOME
        # aislado el subprocess no lo resolvería, así que va explícito (en la
        # PC el runtime ya trae el suyo y esto no cambia nada).
        env = {
            **os.environ,
            "HOME": str(self.temporary / "home"),
            "LOCALAPPDATA": str(self.temporary / "local-app-data"),
            "PYTHONUTF8": "1",
            # Los mensajes de configure_team van en UTF-8; sin esta variable
            # el hijo de 3.11 los escribe en la code page y el stderr capturado
            # sale mojibake ("estÃ¡") y la prueba no encuentra el texto.
            "PYTHONIOENCODING": "utf-8",
        }
        usersite = site.getusersitepackages()
        if Path(usersite).is_dir():
            previo = os.environ.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                f"{usersite}{os.pathsep}{previo}" if previo else str(usersite)
            )
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(self.runtime),
                "--install-dir",
                str(ROOT / "tests/fixtures/hermes_stub"),
                *extra,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )

    def test_un_home_fresco_declara_los_plugins_y_avisa_sin_fallar(self):
        resultado = self.run_configure()
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("hermes plugins", resultado.stdout)
        config = yaml.safe_load(
            (self.runtime / "profiles" / "claw" / "config.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            config["plugins"]["enabled"],
            ["goncloud-team", "jev", "jev-skill-router-mcp"],
        )
        self.assertEqual(
            config["platform_toolsets"]["cli"],
            [*module._DIRECTOR_TOOLSETS, "jev"],
        )
        # configure declara los plugins pero nunca los instala.
        self.assertFalse(
            (self.runtime / "profiles" / "claw" / "plugins" / "jev").exists()
        )

    def test_perfil_sin_plugins_es_rojo_con_instruccion(self):
        self.run_configure()
        resultado = self.run_configure("--check")
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("no está instalado", resultado.stderr)
        self.assertIn("hermes plugins", resultado.stderr)
