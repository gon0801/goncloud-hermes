import importlib.util
import tempfile
import unittest
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "configure_team", ROOT / "scripts/configure_team.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ConfigurationTests(unittest.TestCase):
    def test_routing_translation_preserves_order_and_normalizes_aliases(self):
        chain = [
            "opencode-go/muse-spark-1.3-contributor",
            "opencode/muse-spark-1.3-contributor-free",
            "opencode-go-resp/muse-spark-1.3-contributor",
            "kimi/k3",
            "opencode/muse-spark-1.3-contributor-free dup",
        ]

        translated = module.translate_model_chain(chain)

        self.assertEqual(
            translated,
            [
                {
                    "provider": "opencode-go",
                    "model": "muse-spark-1.3-contributor",
                },
                {
                    "provider": "opencode-zen",
                    "model": "muse-spark-1.3-contributor-free",
                },
                {
                    "provider": "opencode-go",
                    "model": "muse-spark-1.3-contributor",
                },
                {"provider": "kimi-coding", "model": "k3"},
                {
                    "provider": "opencode-zen",
                    "model": "muse-spark-1.3-contributor-free",
                },
            ],
        )

    def test_profile_routing_uses_main_for_claw_and_entrega(self):
        routing = {
            "profile_names": {"main": "claw"},
            "profile_inheritance": {"entrega": "main"},
            "chains": {
                "main": ["opencode-go/muse", "opencode/muse-free"],
                "operaciones": ["zai/glm"],
            },
        }

        self.assertEqual(
            module.model_config_for_profile("claw", routing),
            {
                "model": {"provider": "opencode-go", "default": "muse"},
                "fallback_providers": [
                    {"provider": "opencode-zen", "model": "muse-free"}
                ],
            },
        )
        self.assertEqual(
            module.model_config_for_profile("entrega", routing),
            module.model_config_for_profile("claw", routing),
        )

    def test_routing_translation_rejects_unknown_provider_alias(self):
        with self.assertRaisesRegex(ValueError, "Unknown model provider alias"):
            module.translate_model_chain(["mystery/model"])

    def test_explicit_first_routing_apply_takes_over_seeded_model(self):
        existing = {
            "model": {"provider": "seeded", "default": "seeded-model"},
            "unmanaged": "preserved",
        }
        desired = {
            "model": {"provider": "opencode-go", "default": "muse"},
            "fallback_providers": [{"provider": "opencode-zen", "model": "muse-free"}],
        }

        merged = module.merge_profile_config(
            existing,
            desired,
            previous={},
            fresh=False,
            take_over_model=True,
        )

        self.assertEqual(merged["model"], desired["model"])
        self.assertEqual(merged["fallback_providers"], desired["fallback_providers"])
        self.assertEqual(merged["unmanaged"], "preserved")

    def test_managed_routing_refuses_later_local_model_edit(self):
        previous = {"model": {"provider": "opencode-go", "default": "muse"}}
        existing = {"model": {"provider": "custom", "default": "local-edit"}}
        desired = {"model": {"provider": "opencode-go", "default": "muse-v2"}}

        with self.assertRaisesRegex(ValueError, "model.provider"):
            module.merge_profile_config(
                existing,
                desired,
                previous=previous,
                fresh=False,
                take_over_model=False,
            )

    def test_failed_verification_routes_report_to_reviewer(self):
        verifier = (ROOT / "roles/verifier.md").read_text(encoding="utf-8")
        self.assertIn("informe FAIL", verifier)
        self.assertIn("completa la tarjeta con ese informe", verifier)
        self.assertIn("NO aprobar el artefacto", verifier)
        self.assertNotIn("no completes exitosamente la etapa", verifier)

    def test_preserve_user_model_and_credentials(self):
        existing = {
            "model": {"provider": "custom", "base_url": "https://example.invalid"},
            "secret": "do-not-touch",
        }
        merged = module.merge_owned(
            existing, {"kanban": {"max_in_progress": 3}}, {}, fresh=True
        )
        self.assertEqual(merged["model"], existing["model"])
        self.assertEqual(merged["secret"], "do-not-touch")
        self.assertNotIn("kanban", existing)

    def test_reapplication_and_controlled_upgrade(self):
        old = {"kanban": {"max_in_progress": 3}}
        desired = {"kanban": {"max_in_progress": 4}}
        self.assertEqual(module.merge_owned(old, old, old), old)
        self.assertEqual(module.merge_owned(old, desired, old), desired)

    def test_refuse_overwriting_local_configuration(self):
        with self.assertRaisesRegex(ValueError, "kanban.max_in_progress"):
            module.merge_owned(
                {"kanban": {"max_in_progress": 7}},
                {"kanban": {"max_in_progress": 3}},
                {"kanban": {"max_in_progress": 2}},
            )

    def test_refuse_unmanaged_existing_value(self):
        with self.assertRaises(ValueError):
            module.merge_owned(
                {"terminal": {"cwd": "elsewhere"}}, {"terminal": {"cwd": "here"}}, {}
            )

    def test_reject_nonmapping_section(self):
        with self.assertRaises(ValueError):
            module.merge_owned(
                {"terminal": "local"}, {"terminal": {"cwd": "here"}}, {}, fresh=True
            )

    def test_managed_file_conflict_leaves_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "SOUL.md"
            target.write_text("user edit", encoding="utf-8")
            with self.assertRaises(ValueError):
                module.check_file(target, "desired", "previous")
            self.assertEqual(target.read_text(encoding="utf-8"), "user edit")

    def test_real_profiles_apply_twice_without_touching_credentials(self):
        import json
        import yaml

        upstream = Path(
            os.environ.get(
                "HERMES_INSTALL_DIR",
                Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/hermes-agent",
            )
        )
        self.assertTrue(
            (upstream / "hermes_cli/profiles.py").is_file(),
            "Run tests with the installed Hermes runtime",
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "hermes-test"
            command = [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(runtime),
                "--install-dir",
                str(upstream),
            ]
            env = {**os.environ, "PYTHONUTF8": "1"}
            first = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            roles = json.loads((ROOT / "team.json").read_text())["profiles"]
            self.assertEqual(len(roles), 9)
            for role in roles:
                profile = runtime / "profiles" / role
                config = yaml.safe_load(
                    (profile / "config.yaml").read_text(encoding="utf-8")
                )
                self.assertIn("goncloud-team", config["plugins"]["enabled"])
                self.assertIn("goncloud_team", config["platform_toolsets"]["cli"])
                self.assertTrue(
                    (profile / "plugins/goncloud-team/__init__.py").is_file()
                )
                self.assertTrue((profile / "memories").is_dir())
            credentials = runtime / "profiles/claw/.env"
            loader = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from hermes_cli.plugins import discover_plugins, get_plugin_toolsets; discover_plugins(); assert any(t[0] == 'goncloud_team' for t in get_plugin_toolsets())",
                ],
                cwd=upstream,
                env={**env, "HERMES_HOME": str(runtime / "profiles/claw")},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(loader.returncode, 0, loader.stderr)
            credentials.write_text("UNUSED_TEST_KEY=sentinel\n", encoding="utf-8")
            config_path = runtime / "profiles/claw/config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["model"] = {"provider": "test-provider", "default": "test-model"}
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            second = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(credentials.read_text(), "UNUSED_TEST_KEY=sentinel\n")
            self.assertEqual(
                yaml.safe_load(config_path.read_text())["model"], config["model"]
            )


if __name__ == "__main__":
    unittest.main()
