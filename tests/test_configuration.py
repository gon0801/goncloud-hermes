import importlib.util
import json
import tempfile
import unittest
import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "configure_team", ROOT / "scripts/configure_team.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ConfigurationTests(unittest.TestCase):
    def test_routing_source_is_installed_without_native_fallback_chain(self):
        existing = {
            "model": {"provider": "opencode-go", "default": "muse"},
            "fallback_providers": [{"provider": "opencode-zen", "model": "muse-free"}],
            "plugins": {"enabled": ["goncloud-team"]},
        }
        previous = {
            "model": existing["model"],
            "fallback_providers": existing["fallback_providers"],
        }

        cleaned = module.remove_owned_native_routing(existing, previous)

        self.assertNotIn("model", cleaned)
        self.assertNotIn("fallback_providers", cleaned)
        self.assertIn("plugins", cleaned)

    def test_cleanup_refuses_to_delete_user_changed_model(self):
        previous = {"model": {"provider": "opencode-go", "default": "muse"}}
        existing = {"model": {"provider": "custom", "default": "user-choice"}}

        with self.assertRaisesRegex(ValueError, "user-managed model conflict"):
            module.remove_owned_native_routing(existing, previous)

    def test_compatibility_apply_migrates_owned_native_routing_idempotently(self):
        upstream = Path(
            os.environ.get(
                "HERMES_INSTALL_DIR",
                Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/hermes-agent",
            )
        )
        self.assertTrue((upstream / "hermes_cli/profiles.py").is_file())
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            runtime = temporary / "runtime"
            command = [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(runtime),
                "--install-dir",
                str(upstream),
            ]
            env = {
                **os.environ,
                "HOME": str(temporary / "home"),
                "LOCALAPPDATA": str(temporary / "local-app-data"),
                "PYTHONUTF8": "1",
            }
            bootstrap = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)

            profile = runtime / "profiles/claw"
            config_path = profile / "config.yaml"
            state_path = profile / "goncloud-managed.json"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            native_model = {"provider": "opencode-go", "default": "muse"}
            native_fallbacks = [{"provider": "opencode-zen", "model": "muse-free"}]
            config["model"] = native_model
            config["fallback_providers"] = native_fallbacks
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            state["config"]["model"] = native_model
            state["config"]["fallback_providers"] = native_fallbacks
            state_path.write_text(json.dumps(state), encoding="utf-8")

            routing_source = (ROOT / "model-routing.source.json").read_bytes()
            applied = subprocess.run(
                [*command, "--apply-model-routing"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            managed = json.loads(state_path.read_text(encoding="utf-8"))
            team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
            # The old owned chain is gone; the director carries exactly the
            # owner's conversation chain and nothing of the old block.
            self.assertEqual(migrated["model"], team["director_model"])
            self.assertEqual(migrated["fallback_providers"], team["director_fallbacks"])
            self.assertEqual(managed["config"]["model"], team["director_model"])
            self.assertEqual(
                managed["config"]["fallback_providers"], team["director_fallbacks"]
            )
            self.assertEqual(
                (
                    profile / "plugins/goncloud-team/model-routing.source.json"
                ).read_bytes(),
                routing_source,
            )
            self.assertEqual(
                migrated["model_catalog"]["excluded_providers"],
                ["copilot", "copilot-acp"],
            )

            first_config = config_path.read_bytes()
            first_state = state_path.read_bytes()
            reapplied = subprocess.run(
                [*command, "--apply-model-routing"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(reapplied.returncode, 0, reapplied.stderr)
            self.assertEqual(config_path.read_bytes(), first_config)
            self.assertEqual(state_path.read_bytes(), first_state)

    def test_failed_verification_routes_report_to_reviewer(self):
        verifier = (ROOT / "roles/verifier.md").read_text(encoding="utf-8")
        self.assertIn("informe FAIL", verifier)
        self.assertIn("completa la tarjeta con ese informe", verifier)
        self.assertIn("NO aprobar el artefacto", verifier)
        self.assertNotIn("no completes exitosamente la etapa", verifier)

    def test_gateway_never_dispatches_goncloud_stage_cards_natively(self):
        # The gateway's own Kanban dispatcher claims any `ready` card with an
        # assignee and runs it with a native profile agent. Stage cards carry
        # their role as assignee, so with dispatch on, the gateway would race
        # the local orchestrator and run stages outside the subscription router.
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))

        self.assertIs(team["kanban"].get("dispatch_in_gateway"), False)

    def test_only_the_director_converses_natively_and_workers_carry_nothing(self):
        # The director must be able to converse on Telegram, so it gets the
        # owner's conversation chain. Workers get no native model and no
        # fallback chain: their models come from the slot table through the
        # subscription router, and a native chain there would be the competing
        # router the recovery removed.
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        self.assertEqual(set(team["director_model"]), {"provider", "default"})

        for role in team["profiles"]:
            with self.subTest(role=role):
                desired = module.desired_profile_config(
                    role, team, Path("C:/runtime-home")
                )
                if role == team["director"]:
                    self.assertEqual(desired["model"], team["director_model"])
                    self.assertEqual(
                        desired["fallback_providers"], team["director_fallbacks"]
                    )
                else:
                    self.assertNotIn("model", desired)
                    self.assertNotIn("fallback_providers", desired)

    def test_the_director_chain_uses_only_same_subscription_positions_in_table_order(
        self,
    ):
        # Hermes can only converse through native API providers. Of the
        # owner's `main` chain, only these reach the *same* subscription that
        # way; Claude Max, ChatGPT, Grok and Muse would bill another account,
        # which is exactly the wrong-account-class chain the recovery removed.
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        table = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )
        native = {
            "opencode-go": "opencode-go",
            "opencode": "opencode-zen",
            "kimi": "kimi-coding",
            "zai": "zai",
        }
        allowed_in_order = []
        for label in table["chains"]["main"]:
            prefix, model = label.removesuffix(" dup").split("/", 1)
            if prefix in native:
                allowed_in_order.append((native[prefix], model))

        chain = [
            (team["director_model"]["provider"], team["director_model"]["default"])
        ]
        chain += [(f["provider"], f["model"]) for f in team["director_fallbacks"]]

        self.assertTrue(set(chain) <= set(allowed_in_order), chain)
        order = [allowed_in_order.index(entry) for entry in chain]
        self.assertEqual(order, sorted(order))
        self.assertEqual(chain[0], allowed_in_order[0])

    def test_director_gets_the_full_toolset_and_workers_the_baseline(self):
        # Owner decision (2026-09-15): the director drives Odoo and Edge over
        # Telegram, so it carries the full toolset (browser, computer_use, …).
        # Baking it in stops the director from re-editing its own config every
        # session and keeps a redeploy from fighting that edit. Workers stay
        # minimal; the guard against the director wandering into logs and
        # plugin code is its role text, not the toolset.
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        for role in team["profiles"]:
            desired = module.desired_profile_config(role, team, Path("C:/home"))
            expected = (
                module._DIRECTOR_TOOLSETS
                if role == team["director"]
                else module._WORKER_TOOLSETS
            )
            with self.subTest(role=role):
                self.assertEqual(desired["platform_toolsets"]["cli"], expected)
                self.assertEqual(desired["platform_toolsets"]["telegram"], expected)

        worker = module.desired_profile_config("scout", team, Path("C:/home"))
        director = module.desired_profile_config(
            team["director"], team, Path("C:/home")
        )
        for machine_control in ("browser", "computer_use"):
            self.assertNotIn(machine_control, worker["platform_toolsets"]["telegram"])
            self.assertIn(machine_control, director["platform_toolsets"]["telegram"])

    def test_the_director_role_reports_a_blocked_workflow_and_stops(self):
        claw = (ROOT / "roles/claw.md").read_text(encoding="utf-8")
        self.assertIn("goncloud_local_status", claw)
        self.assertIn("no leas logs", claw)
        self.assertIn("no revises el codigo del plugin", claw)

    def test_reapply_refuses_a_tree_that_does_not_contain_the_deployed_revision(self):
        # 2026-09-15 01:08: the profiles carried the plugin from
        # feat/subscription-router-recovery; a reapply from feat/hermes-team
        # (a sibling branch without those commits) silently downgraded every
        # managed file and goncloud_local_run lost check_stop_after. A reapply
        # must refuse a HEAD that does not contain the deployed revision.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git = ["git", "-C", str(repo)]
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="t",
                GIT_AUTHOR_EMAIL="t@t",
                GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@t",
            )

            def run(*args):
                return subprocess.run(
                    git + list(args), check=True, capture_output=True, env=env
                )

            run("init", "-q", "-b", "base")
            (repo / "a").write_text("1")
            run("add", "a")
            run("commit", "-q", "-m", "base")
            run("checkout", "-q", "-b", "deployed")
            (repo / "a").write_text("2")
            run("commit", "-q", "-am", "deployed")
            deployed = module.current_revision(repo)
            run("checkout", "-q", "base")
            run("checkout", "-q", "-b", "sibling")
            (repo / "b").write_text("x")
            run("add", "b")
            run("commit", "-q", "-m", "sibling")

            self.assertFalse(module.tree_contains(deployed, repo))
            with self.assertRaisesRegex(ValueError, "does not contain"):
                module.refuse_regression({"source_revision": deployed}, repo)
            module.refuse_regression(
                {"source_revision": deployed}, repo, allow_regression=True
            )

            run("merge", "-q", "--no-edit", "deployed")
            self.assertTrue(module.tree_contains(deployed, repo))
            module.refuse_regression({"source_revision": deployed}, repo)
            module.refuse_regression({}, repo)

    def test_the_director_role_keeps_business_work_off_the_native_board(self):
        # 2026-09-15: after a gateway restart the director handed an Odoo
        # lookup to `operaciones` with kanban_create. Nothing executes those
        # cards (dispatcher off, no worker gateways, the driver only runs the
        # engineering chain), so the card sat in `ready` forever. Business
        # work is the director's own job with its browser and vault.
        claw = (ROOT / "roles/claw.md").read_text(encoding="utf-8")
        self.assertNotIn("Trabajo de negocio va a operaciones", claw)
        self.assertIn("kanban_create", claw)
        self.assertIn("navegador", claw)
        self.assertIn("kanban dispatch", claw)

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
            # A worker's model is the user's: unmanaged, so it survives reapply.
            scout_path = runtime / "profiles/scout/config.yaml"
            scout = yaml.safe_load(scout_path.read_text(encoding="utf-8"))
            scout["model"] = {"provider": "test-provider", "default": "test-model"}
            scout_path.write_text(yaml.safe_dump(scout), encoding="utf-8")
            second = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(credentials.read_text(), "UNUSED_TEST_KEY=sentinel\n")
            self.assertEqual(
                yaml.safe_load(scout_path.read_text())["model"], scout["model"]
            )
            # The director's model is owned: a hand edit is a conflict the
            # configurator refuses to overwrite, leaving the edit in place.
            claw_path = runtime / "profiles/claw/config.yaml"
            claw = yaml.safe_load(claw_path.read_text(encoding="utf-8"))
            claw["model"] = {"provider": "test-provider", "default": "test-model"}
            claw_path.write_text(yaml.safe_dump(claw), encoding="utf-8")
            third = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120
            )
            self.assertNotEqual(third.returncode, 0)
            self.assertIn("model", third.stderr)
            self.assertEqual(
                yaml.safe_load(claw_path.read_text())["model"], claw["model"]
            )
            self.assertEqual(credentials.read_text(), "UNUSED_TEST_KEY=sentinel\n")


if __name__ == "__main__":
    unittest.main()
