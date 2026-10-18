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
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

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
                "patches": [],
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
        self.assertTrue(PATCH.is_file())
        self.assertEqual(hashlib.sha256(PATCH.read_bytes()).hexdigest(), PATCH_SHA)
        lineas = PATCH.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"diff --git a/{MCP_SERVER} b/{MCP_SERVER}", lineas)
        self.assertIn(f"--- a/{MCP_SERVER}", lineas)
        self.assertIn(f"+++ b/{MCP_SERVER}", lineas)


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
