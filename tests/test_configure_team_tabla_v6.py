"""The v6 table (owner, 2026-10-17): every Hermes profile on his Claude subscription.

Oracle built from ~/.local/state/encargos-loop/artifacts/H-tabla/lista-v6.md,
never from the implementation: 8 lanes of 4 positions (primary + 3 fallbacks),
agent -> lane for the 21 Hermes profiles, Anthropic on the owner's Claude
subscription (never a pay-per-token API route), "off" = no reasoning, no Fable,
OpenCode Go only for Muse Spark (absent) and DeepSeek Flash.
"""

import hashlib
import importlib.util
import json
import os
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

LIVE = json.loads((ROOT / "model-routing.source.json").read_text(encoding="utf-8"))
VIVO = json.loads(
    (ROOT / "tests/fixtures/rutas-vivas-20261017.json").read_text(encoding="utf-8")
)
#: These homes model the PC before the JEV plugins (2026-10-18): the
#: reconciliation would demand both external repos at their pinned commits,
#: which a throwaway home never has. The plugin behavior has its own suite,
#: tests/test_director_plugins.py, against the real manifest.
MANIFIESTO_VACIO = str(ROOT / "tests/fixtures/director-plugins-vacio.json")
SPEC = importlib.util.spec_from_file_location(
    "configure_team", ROOT / "scripts/configure_team.py"
)
configure_team = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_team)

ZAI_URL = "https://api.z.ai/api/coding/paas/v4"
KIMI_URL = "https://api.kimi.com/coding/v1"
#: provider, extra native fields, in v6 lane order.
NATIVE = {
    "claude-opus-5-5": ("anthropic", {}),
    "claude-sonnet-5-5": ("anthropic", {}),
    "claude-haiku-5-5": ("anthropic", {}),
    "glm-5.3": ("zai", {"base_url": ZAI_URL}),
    "glm-5.3-flash": ("zai", {"base_url": ZAI_URL}),
    "k3": ("kimi-coding", {"base_url": KIMI_URL, "api_mode": "chat_completions"}),
    "grok-4.7": ("xai-oauth", {}),
    "deepseek-v4.1-flash": ("opencode-go", {}),
}
PREFIX = {
    "claude-opus-5-5": "anthropic",
    "claude-sonnet-5-5": "anthropic",
    "claude-haiku-5-5": "anthropic",
    "glm-5.3": "zai",
    "glm-5.3-flash": "zai",
    "k3": "kimi",
    "grok-4.7": "xai",
    "deepseek-v4.1-flash": "opencode-go",
}

DIRIGIR = [
    ("claude-opus-5-5", "high"),
    ("claude-sonnet-5-5", "high"),
    ("glm-5.3", "high"),
    ("k3", "high"),
]
CARRILES = {
    "dirigir": DIRIGIR,
    "dirigir-claw": [
        ("claude-opus-5-5", "medium"),
        ("claude-sonnet-5-5", "high"),
        ("glm-5.3", "high"),
        ("k3", "high"),
    ],
    "planear": [
        ("claude-opus-5-5", "high"),
        ("k3", "high"),
        ("glm-5.3", "high"),
        ("grok-4.7", "high"),
    ],
    "codigo": [
        ("claude-sonnet-5-5", "high"),
        ("k3", "high"),
        ("glm-5.3", "high"),
        ("grok-4.7", "medium"),
    ],
    "verificar": [
        ("glm-5.3", "high"),
        ("grok-4.7", "high"),
        ("claude-sonnet-5-5", "high"),
        ("k3", "high"),
    ],
    "investigar": [
        ("grok-4.7", "medium"),
        ("k3", "high"),
        ("glm-5.3", "high"),
        ("claude-sonnet-5-5", "medium"),
    ],
    "operativo": [
        ("claude-sonnet-5-5", "medium"),
        ("glm-5.3", "low"),
        ("grok-4.7", "low"),
        ("deepseek-v4.1-flash", None),
    ],
    "simples": [
        ("claude-haiku-5-5", "low"),
        ("deepseek-v4.1-flash", None),
        ("glm-5.3-flash", "low"),
        ("grok-4.7", "low"),
    ],
    "adversary": [
        ("claude-opus-5-5", "high"),
        ("grok-4.7", "high"),
        ("k3", "high"),
        ("glm-5.3", "high"),
    ],
}
PROFILE_LANE = {
    "claw": "dirigir-claw",
    "entrega": "dirigir",
    "ingenieria": "planear",
    "reviewer": "planear",
    "implementer": "codigo",
    "verifier": "verificar",
    "scout": "investigar",
    "operaciones": "operativo",
    "amazon": "operativo",
    "meli": "operativo",
    "odoo": "operativo",
    "pers": "operativo",
    "arras": "operativo",
    "mail": "simples",
    "limpia": "simples",
    "gonserver": "simples",
    "skillverif": "simples",
    "tinkabot": "simples",
    "eggbot": "simples",
    "default": "simples",
    "adversary": "adversary",
}


def expected_native(lane):
    """model + fallback_providers + reasoning_overrides for one v6 lane."""
    entries = [
        {"provider": NATIVE[model][0], **NATIVE[model][1], "model": model}
        for model, _ in CARRILES[lane]
    ]
    primary, *fallbacks = entries
    extras = {
        key: value for key, value in primary.items() if key not in ("provider", "model")
    }
    return {
        "model": {
            "provider": primary["provider"],
            "default": primary["model"],
            **extras,
        },
        "fallback_providers": fallbacks,
        "agent": {
            "reasoning_overrides": {
                model: effort for model, effort in CARRILES[lane] if effort
            }
        },
    }


def load_plugins():
    plugin_dir = ROOT / "plugins" / "goncloud-team"
    sys.path.insert(0, str(plugin_dir))
    from subscriptions.catalog import compile_profile_routes

    return compile_profile_routes


class PerfilV6Tests(unittest.TestCase):
    """Every one of the 21 profiles compiles to exactly its v6 lane."""

    def test_cada_perfil_compila_su_carril_v6(self):
        compile_profile_routes = load_plugins()
        self.assertEqual(len(PROFILE_LANE), 21)
        for profile, lane in PROFILE_LANE.items():
            with self.subTest(perfil=profile, carril=lane):
                routes = compile_profile_routes(profile, LIVE)
                self.assertEqual(
                    [(route.model, route.reasoning_effort) for route in routes],
                    CARRILES[lane],
                    f"{profile} no compila su carril {lane}",
                )
                self.assertEqual(
                    [route.slot for route in routes],
                    [0, 1, 2, 3],
                    profile,
                )
                for route in routes:
                    self.assertEqual(
                        route.adapter,
                        {
                            "anthropic": "claude",
                            "zai": "opencode_zai",
                            "kimi": "kimi",
                            "xai": "grok",
                            "opencode-go": "opencode_go",
                        }[PREFIX[route.model]],
                        f"{profile} {route.label}",
                    )
                    self.assertEqual(route.billing_mode, "subscription", profile)

    def test_la_tabla_no_niega_las_reglas_del_dueno(self):
        text = json.dumps(LIVE)
        self.assertNotIn("fable", text)
        self.assertNotIn("muse-", text)
        self.assertNotIn("gpt-", text)
        self.assertNotIn("kimi-for-coding", text)
        for row, labels in LIVE["chains"].items():
            for position, label in enumerate(labels):
                with self.subTest(fila=row, posicion=position):
                    if label.split("/", 1)[0] == "opencode-go":
                        self.assertEqual(
                            label.split("/", 1)[1].split(" ")[0],
                            "deepseek-v4.1-flash",
                            "opencode-go solo para Muse Spark o DeepSeek Flash",
                        )
        self.assertIn(
            "2026-10-17: owner moved Hermes profiles to Anthropic on his Claude "
            "subscription",
            json.dumps(LIVE.get("bindings", {})) + LIVE.get("source", ""),
        )

    def test_la_cadena_nativa_de_cada_perfil_es_su_carril_v6(self):
        for profile, lane in PROFILE_LANE.items():
            with self.subTest(perfil=profile, carril=lane):
                self.assertEqual(
                    configure_team.native_config(LIVE, profile),
                    expected_native(lane),
                    profile,
                )

    def test_claw_lleva_medium_en_el_primario_y_entrega_high(self):
        claw = configure_team.native_config(LIVE, "claw")
        entrega = configure_team.native_config(LIVE, "entrega")
        self.assertEqual(
            claw["agent"]["reasoning_overrides"]["claude-opus-5-5"], "medium"
        )
        self.assertEqual(
            entrega["agent"]["reasoning_overrides"]["claude-opus-5-5"], "high"
        )
        self.assertEqual(claw["fallback_providers"], entrega["fallback_providers"])


def effective_previous(previous, adopted):
    """The previous state with the live routing values adopted as its own."""
    import copy

    effective = copy.deepcopy(previous)
    for key, value in adopted.items():
        if key == "agent.reasoning_overrides":
            effective.setdefault("agent", {})["reasoning_overrides"] = copy.deepcopy(
                value
            )
        else:
            effective[key] = copy.deepcopy(value)
    return effective


def live_adoptions(vivo, previous):
    """The routing keys where the live config left the managed state behind."""
    adopted = {}
    for key in ("model", "fallback_providers"):
        if key in vivo and vivo[key] != previous.get(key):
            adopted[key] = vivo[key]
    overrides = vivo.get("agent", {}).get("reasoning_overrides")
    before = previous.get("agent", {}).get("reasoning_overrides")
    if overrides is not None and overrides != before:
        adopted["agent.reasoning_overrides"] = overrides
    return adopted


class AdopcionTests(unittest.TestCase):
    """David changed the models by hand: without the flag the deploy refuses."""

    def test_sin_bandera_el_conflicto_se_mantiene_con_previo_gestionado(self):
        for profile, data in VIVO.items():
            previous = data["gestionado_previo"]
            vivo = data["vivo"]
            if not previous:
                continue
            with self.subTest(perfil=profile):
                with self.assertRaisesRegex(ValueError, "user-managed model conflict"):
                    configure_team.remove_owned_native_routing(vivo, previous)

    def test_con_adopcion_el_vivo_pasa_a_ser_el_previo_y_aplica_la_v6(self):
        for profile, data in VIVO.items():
            with self.subTest(perfil=profile):
                previous = data["gestionado_previo"]
                vivo = data["vivo"]
                adopted = live_adoptions(vivo, previous)
                self.assertTrue(adopted, profile)
                effective = effective_previous(previous, adopted)
                cleaned = configure_team.remove_owned_native_routing(
                    dict(vivo), effective
                )
                merged = configure_team.merge_profile_config(
                    cleaned, expected_native(PROFILE_LANE[profile]), effective
                )
                self.assertEqual(
                    merged["model"],
                    expected_native(PROFILE_LANE[profile])["model"],
                    profile,
                )
                self.assertEqual(
                    merged["fallback_providers"],
                    expected_native(PROFILE_LANE[profile])["fallback_providers"],
                    profile,
                )
                self.assertEqual(
                    merged["agent"]["reasoning_overrides"],
                    expected_native(PROFILE_LANE[profile])["agent"][
                        "reasoning_overrides"
                    ],
                    profile,
                )

    def test_sin_cambios_del_dueno_la_adopcion_no_registra_nada(self):
        data = VIVO["amazon"]
        previous = data["gestionado_previo"]
        self.assertEqual(live_adoptions(dict(previous), previous), {})

    def test_with_adopted_routing_anida_los_overrides_del_vivo(self):
        previous = {
            "model": {"provider": "opencode-go", "default": "muse"},
            "agent": {"keep": 1},
        }
        vivo_model = {"provider": "anthropic", "default": "claude-opus-5-5"}
        adopted = {
            "model": vivo_model,
            "agent.reasoning_overrides": {"claude-opus-5-5": "high"},
        }

        effective = configure_team.with_adopted_routing(previous, adopted)

        self.assertEqual(
            effective,
            {
                "model": vivo_model,
                "agent": {
                    "keep": 1,
                    "reasoning_overrides": {"claude-opus-5-5": "high"},
                },
            },
        )
        # No mutation of the inputs.
        self.assertEqual(previous["agent"], {"keep": 1})
        self.assertNotIn("reasoning_overrides", previous.get("agent", {}))


class AdopcionEndToEndTests(unittest.TestCase):
    """configure() against a stub Hermes runtime, with the live PC state."""

    @classmethod
    def setUpClass(cls):
        activo = mock.patch.dict(
            os.environ, {configure_team.DIRECTOR_PLUGINS_OVERRIDE: MANIFIESTO_VACIO}
        )
        activo.start()
        cls.addClassCleanup(activo.stop)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.runtime_ok = Path(cls.tmp.name) / "con-bandera"
        cls.runtime_rechazo = Path(cls.tmp.name) / "sin-bandera"
        cls.deploy(cls.runtime_rechazo, adopt=False, expect_ok=False)
        cls.deploy(cls.runtime_ok, adopt=True, expect_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def command(runtime, adopt):
        return [
            sys.executable,
            str(ROOT / "scripts/configure_team.py"),
            "--home",
            str(runtime),
            "--install-dir",
            str(ROOT / "tests/fixtures/hermes_stub"),
            *(["--adoptar-ruteo-manual"] if adopt else []),
        ]

    @staticmethod
    def deploy(runtime, adopt, expect_ok):
        fresh = not (runtime / "profiles" / "claw").exists()
        if fresh:
            # La revision desplegada del estado es HEAD de este checkout: el
            # fixture trae la de la PC del dueno, y en CI el checkout no
            # contiene ese historial, asi que refuse_regression rechazaria
            # antes del conflicto que esta prueba ejercita. "Desplegado
            # desde este arbol" es exactamente HEAD (self-contained: el
            # adoptado y el rechazado comparten revision y la prueba no
            # depende del historial del clone).
            desplegada = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if not desplegada:
                raise unittest.SkipTest("sin git no hay arbol desplegado que verificar")
            for profile, data in VIVO.items():
                directory = runtime / "profiles" / profile
                directory.mkdir(parents=True)
                (directory / "config.yaml").write_text(
                    yaml.safe_dump(data["vivo"]), encoding="utf-8"
                )
                (directory / "goncloud-managed.json").write_text(
                    json.dumps(
                        {
                            "config": data["gestionado_previo"],
                            "source_revision": desplegada,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        result = subprocess.run(
            AdopcionEndToEndTests.command(runtime, adopt),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if expect_ok:
            if result.returncode != 0:
                raise AssertionError(
                    f"deploy con bandera fallo: {result.stderr}\n{result.stdout}"
                )
            assert result.stdout.count("Configured ") == 20, result.stdout
        else:
            if result.returncode == 0:
                raise AssertionError(
                    f"sin bandera el deploy debio rechazar: {result.stdout}"
                )
            assert "conflict" in result.stderr, result.stderr
        return result

    def test_los_20_perfiles_quedan_en_v6_y_el_registro_queda(self):
        for profile, data in VIVO.items():
            with self.subTest(perfil=profile):
                config = yaml.safe_load(
                    (self.runtime_ok / "profiles" / profile / "config.yaml").read_text(
                        encoding="utf-8"
                    )
                )
                desired = configure_team.native_config(LIVE, profile)
                self.assertEqual(config["model"], desired["model"], profile)
                self.assertEqual(
                    config["fallback_providers"],
                    desired["fallback_providers"],
                    profile,
                )
                self.assertEqual(
                    config["agent"]["reasoning_overrides"],
                    desired["agent"]["reasoning_overrides"],
                    profile,
                )
                state = json.loads(
                    (
                        self.runtime_ok / "profiles" / profile / "goncloud-managed.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(state["config"]["model"], desired["model"], profile)
                self.assertEqual(
                    state["config"]["fallback_providers"],
                    desired["fallback_providers"],
                    profile,
                )
                self.assertEqual(
                    state["config"]["agent"],
                    desired["agent"],
                    profile,
                )
                record = state["adopted_manual_routing"]
                self.assertEqual(len(record), 1, profile)
                entry = next(iter(record.values()))
                self.assertIn("2026-10-17", entry["reason"], profile)
                self.assertIn("model", entry["keys"], profile)
                self.assertIn("fallback_providers", entry["keys"], profile)
                # The hand-written reasoning overrides are part of the same
                # manual rerouting: adopted as previous, then replaced by the
                # v6 lane's.
                self.assertIn("agent.reasoning_overrides", entry["keys"], profile)

    def test_sin_bandera_el_rechazo_nombra_el_conflicto(self):
        runtime = self.runtime_rechazo
        result = self.deploy(runtime, adopt=False, expect_ok=False)
        self.assertIn("user-managed model conflict", result.stderr)

    def test_reaplicar_sin_bandera_ya_no_conflicta_y_no_duplica_el_registro(self):
        result = subprocess.run(
            self.command(self.runtime_ok, adopt=False),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        for profile in VIVO:
            state = json.loads(
                (
                    self.runtime_ok / "profiles" / profile / "goncloud-managed.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(state["adopted_manual_routing"]), 1, profile)

    def test_check_con_bandera_valida_sin_escribir(self):
        runtime = Path(self.tmp.name) / "check"
        self.deploy(runtime, adopt=True, expect_ok=True)
        before = (runtime / "profiles/claw/config.yaml").read_bytes()
        result = subprocess.run(
            [*self.command(runtime, adopt=True), "--check"],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((runtime / "profiles/claw/config.yaml").read_bytes(), before)


class ReaplicacionIdempotenteTests(unittest.TestCase):
    """Deploy, deploy again: the second run changes nothing and conflicts on
    nothing (the 8:35 watcher deploys like this every day)."""

    @classmethod
    def setUpClass(cls):
        activo = mock.patch.dict(
            os.environ, {configure_team.DIRECTOR_PLUGINS_OVERRIDE: MANIFIESTO_VACIO}
        )
        activo.start()
        cls.addClassCleanup(activo.stop)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cleanup_temp_directory, cls.tmp)
        cls.runtime = Path(cls.tmp.name) / "runtime"
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        # The eleven specialists are the owner's: on the PC they already
        # exist, so every deploy configures all 20 managed profiles.
        for role in team["kanban"]["dispatch_profiles"]:
            directory = cls.runtime / "profiles" / role
            directory.mkdir(parents=True)
            (directory / "config.yaml").write_text(
                "platform_toolsets:\n  cli: [browser, web]\n", encoding="utf-8"
            )
        cls.deploy()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def deploy(cls, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(cls.runtime),
                "--install-dir",
                str(ROOT / "tests/fixtures/hermes_stub"),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result

    @classmethod
    def snapshot(cls):
        # SHA-256 per file: any byte change shows up and a mismatching dict
        # still renders a diff fast (raw bytes would choke difflib).
        return {
            p.relative_to(cls.runtime).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in cls.runtime.rglob("*")
            if p.is_file()
        }

    def test_01_la_segunda_aplicacion_no_cambia_nada(self):
        before = self.snapshot()

        second = self.deploy()
        after = self.snapshot()

        self.assertEqual(second.stdout.count("Configured "), 20)
        self.assertEqual(after, before, "la segunda aplicacion reescribio archivos")
        third = self.deploy()
        self.assertEqual(self.snapshot(), before, "la tercera aplicacion reescribio")
        self.assertEqual(third.stdout.count("Configured "), 20)

    def test_02_el_model_editado_a_manode_un_perfil_conflicta_sin_bandera(self):
        config_path = self.runtime / "profiles/scout/config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["model"] = {"provider": "test-provider", "default": "test-model"}
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        before = self.snapshot()

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(self.runtime),
                "--install-dir",
                str(ROOT / "tests/fixtures/hermes_stub"),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user-managed model conflict", result.stderr)
        # The refused deploy leaves the owner's edit in place.
        self.assertEqual(
            yaml.safe_load(config_path.read_text(encoding="utf-8"))["model"],
            {"provider": "test-provider", "default": "test-model"},
        )
        self.assertEqual(self.snapshot(), before)

    def test_03_con_bandera_el_edit_se_adopta_y_aplica_la_v6(self):
        result = self.deploy("--adoptar-ruteo-manual")
        self.assertEqual(result.stdout.count("Configured "), 20)

        config_path = self.runtime / "profiles/scout/config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            config["model"],
            configure_team.native_config(LIVE, "scout")["model"],
        )
        state = json.loads(
            (self.runtime / "profiles/scout/goncloud-managed.json").read_text(
                encoding="utf-8"
            )
        )
        record = state["adopted_manual_routing"]
        self.assertEqual(len(record), 1)
        entry = next(iter(record.values()))
        self.assertIn("2026-10-17", entry["reason"])
        self.assertIn("model", entry["keys"])
        # Idempotent again: a normal redeploy changes nothing.
        before = self.snapshot()
        self.deploy()
        self.assertEqual(self.snapshot(), before)


class SkillMotorDelDirector(unittest.TestCase):
    """G.1: la skill motor se publica SOLO en el paquete del director (claw),
    junto a sus otros files; ningún otro perfil la recibe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.runtime = Path(self.tmp.name) / "runtime"
        self.runtime.mkdir()

    def deploy(self, *extra):
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
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )

    def test_la_skill_llega_al_paquete_de_claw_y_a_ningun_otro(self):
        resultado = self.deploy()
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        esperada = (ROOT / "skills" / "motor" / "SKILL.md").read_text("utf-8")
        publicada = (
            self.runtime / "profiles" / "claw" / "skills" / "motor" / "SKILL.md"
        ).read_text("utf-8")
        self.assertEqual(publicada, esperada)
        perfiles = sorted(
            p.name
            for p in (self.runtime / "profiles").iterdir()
            if p.is_dir() and p.name != "claw"
        )
        self.assertTrue(perfiles, "el deploy crea los perfiles de equipo")
        for perfil in perfiles:
            self.assertFalse(
                (self.runtime / "profiles" / perfil / "skills" / "motor").exists(),
                f"la skill no viaja al paquete de {perfil}",
            )

    def test_el_check_no_escribe_la_skill(self):
        resultado = self.deploy("--check")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertFalse((self.runtime / "profiles").exists())


class RaizDefaultTests(unittest.TestCase):
    """The `default` profile is the home ROOT: only its routing is managed.

    Measured on the PC (2026-10-18): %LOCALAPPDATA%\\hermes\\config.yaml is the
    default profile and carries the owner's own keys (platforms, gateway,
    secrets). The v6 lanes say default rides `simples`, so the deploy must set
    exactly that lane's routing there — first adoption only with the flag,
    idempotent after, and every other key byte-identical.
    """

    RAIZ = (
        "# config del dueno\n"
        "platforms:\n"
        "  cli:\n"
        "    - browser\n"
        "gateway:\n"
        "  port: 8734\n"
        "model:\n"
        "  provider: opencode-go\n"
        "  default: muse-spark-1.3-contributor\n"
        "secreto: no-tocar\n"
    )

    def setUp(self):
        activo = mock.patch.dict(
            os.environ, {configure_team.DIRECTOR_PLUGINS_OVERRIDE: MANIFIESTO_VACIO}
        )
        activo.start()
        self.addCleanup(activo.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.runtime = Path(self.tmp.name) / "runtime"
        self.runtime.mkdir()
        (self.runtime / "config.yaml").write_text(self.RAIZ, encoding="utf-8")

    def raiz(self):
        return (self.runtime / "config.yaml").read_text(encoding="utf-8")

    @staticmethod
    def claves_ajenas(texto):
        # kanban is also managed at the root (the dispatch allowlist,
        # apply_root_kanban, since before the v6 table).
        doc = yaml.safe_load(texto) or {}
        doc.pop("model", None)
        doc.pop("fallback_providers", None)
        doc.pop("agent", None)
        doc.pop("kanban", None)
        return doc

    def deploy(self, *extra):
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
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )

    def test_sin_bandera_la_raiz_conflicta_y_no_se_toca(self):
        before = self.raiz()

        result = self.deploy()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflict", result.stderr)
        self.assertEqual(self.raiz(), before)
        self.assertFalse((self.runtime / "goncloud-managed.json").exists())

    def test_con_bandera_la_raiz_queda_en_el_carril_simples(self):
        result = self.deploy("--adoptar-ruteo-manual")
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        despues = self.raiz()

        config = yaml.safe_load(despues)
        self.assertEqual(
            config["model"],
            {"provider": "anthropic", "default": "claude-haiku-5-5"},
        )
        self.assertEqual(
            config["fallback_providers"],
            [
                {
                    "provider": "opencode-go",
                    "model": "deepseek-v4.1-flash",
                },
                {
                    "provider": "zai",
                    "model": "glm-5.3-flash",
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                },
                {"provider": "xai-oauth", "model": "grok-4.7"},
            ],
        )
        self.assertEqual(
            config["agent"]["reasoning_overrides"],
            {
                "claude-haiku-5-5": "low",
                "glm-5.3-flash": "low",
                "grok-4.7": "low",
            },
        )
        # Every other key survives byte for byte, comments included.
        self.assertEqual(self.claves_ajenas(despues), self.claves_ajenas(self.RAIZ))
        for fragment in (
            "# config del dueno\n",
            "platforms:\n  cli:\n    - browser\n",
            "gateway:\n  port: 8734\n",
            "secreto: no-tocar\n",
        ):
            self.assertIn(fragment, despues, fragment)

        # Its own record, with date and reason for the adopted live model.
        state = json.loads(
            (self.runtime / "goncloud-managed.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["config"],
            configure_team.native_config(LIVE, "default"),
        )
        record = state["adopted_manual_routing"]
        self.assertEqual(len(record), 1)
        entry = next(iter(record.values()))
        self.assertIn("2026-10-17", entry["reason"])
        self.assertIn("model", entry["keys"])

    def test_segunda_aplicacion_idempotente_y_check_verde(self):
        self.deploy("--adoptar-ruteo-manual")
        before = self.raiz()

        second = self.deploy()

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.raiz(), before)
        state = json.loads(
            (self.runtime / "goncloud-managed.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(state["adopted_manual_routing"]), 1)
        checked = self.deploy("--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_una_raiz_sin_model_previo_recibe_el_carril_sin_bandera(self):
        (self.runtime / "config.yaml").write_text(
            "platforms:\n  cli: [web]\n", encoding="utf-8"
        )

        result = self.deploy()

        self.assertEqual(result.returncode, 0, result.stderr)
        config = yaml.safe_load(self.raiz())
        self.assertEqual(config["model"]["default"], "claude-haiku-5-5")
        self.assertEqual(config["platforms"], {"cli": ["web"]})

    def test_la_raiz_respeta_allow_regression(self):
        # A root record deployed from a revision this checkout does not
        # contain must fail the run without the flag and pass with it —
        # like every profile, not killing the whole deploy behind it.
        foreign = "1" * 40
        (self.runtime / "config.yaml").write_text(
            "platforms:\n  cli: [web]\n", encoding="utf-8"
        )
        (self.runtime / "goncloud-managed.json").write_text(
            json.dumps({"config": {}, "source_revision": foreign}),
            encoding="utf-8",
        )

        sin_bandera = self.deploy("--check")
        self.assertNotEqual(sin_bandera.returncode, 0)
        self.assertIn("does not contain", sin_bandera.stderr)

        con_bandera = self.deploy("--check", "--allow-regression")
        self.assertEqual(con_bandera.returncode, 0, con_bandera.stderr)


if __name__ == "__main__":
    unittest.main()
