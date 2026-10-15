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
sys.path.insert(0, str(ROOT / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

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

    def test_api_billed_providers_stay_out_of_the_native_catalog(self):
        """deepseek is the only adapter that bills as API, not subscription.

        The subscription router reaches it deliberately, at two slots, through
        its own adapter. Leaving it in a profile's native model catalog lets an
        agent pick it directly and spend real money outside that decision.
        opencode-free is excluded for the same reason in reverse: it is not one
        of the owner's accounts.
        """
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import configure_team

        excluded = configure_team.desired_profile_config(
            "claw",
            json.loads((ROOT / "team.json").read_text(encoding="utf-8")),
            ROOT,
        )["model_catalog"]["excluded_providers"]

        self.assertIn("deepseek", excluded)
        self.assertIn("opencode-free", excluded)
        self.assertIn("copilot", excluded)
        self.assertIn("copilot-acp", excluded)

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
            # The old owned chain is migrated into the native chain compiled
            # from the authoritative table's main row.
            native = module.director_native_config(
                json.loads(routing_source.decode("utf-8"))
            )
            self.assertEqual(migrated["model"], native["model"])
            self.assertEqual(
                migrated["fallback_providers"], native["fallback_providers"]
            )
            self.assertEqual(managed["config"]["model"], migrated["model"])
            self.assertEqual(
                (
                    profile / "plugins/goncloud-team/model-routing.source.json"
                ).read_bytes(),
                routing_source,
            )
            self.assertEqual(
                migrated["model_catalog"]["excluded_providers"],
                ["copilot", "copilot-acp", "deepseek", "opencode-free"],
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

    def test_the_gateway_may_dispatch_specialist_cards_and_no_stage_card(self):
        # The gateway's own Kanban dispatcher claims any `ready` card whose
        # assignee is a profile and runs it with a native profile agent. Stage
        # cards carry their role as assignee, so an unrestricted dispatcher
        # races the local orchestrator and runs stages outside the
        # subscription router. One dispatcher serves every multiplexed profile
        # and reads the ROOT config only, where dispatch is on by default: the
        # per-profile `dispatch_in_gateway: false` this test used to assert
        # never reached it (measured 2026-10-12). What keeps it off stage cards
        # is the allowlist, so that is what must hold.
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))

        allowed = team["kanban"]["dispatch_profiles"]
        self.assertEqual(len(allowed), 11)
        self.assertEqual(set(allowed) & set(team["profiles"]), set())
        self.assertEqual(
            module.root_kanban_policy(team),
            {"auto_decompose": False, "dispatch_profiles": allowed},
        )

    def test_only_the_director_profile_says_its_cards_are_dispatched(self):
        # In a profile, `dispatch_in_gateway` only decides whether `hermes
        # kanban create` warns that nobody will run the card. The director's
        # cards for specialists do run (owner, 2026-10-03; adopted 2026-10-12).
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        worker = module.desired_profile_config("scout", team, Path("C:/home"))
        director = module.desired_profile_config(
            team["director"], team, Path("C:/home")
        )

        self.assertIs(worker["kanban"]["dispatch_in_gateway"], False)
        self.assertIs(director["kanban"]["dispatch_in_gateway"], True)

        def without_dispatch(kanban):
            return {k: v for k, v in kanban.items() if k != "dispatch_in_gateway"}

        self.assertEqual(
            without_dispatch(director["kanban"]), without_dispatch(worker["kanban"])
        )

    def test_only_the_director_converses_natively_and_workers_carry_nothing(self):
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        table = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("director_model", team)
        self.assertNotIn("director_fallbacks", team)
        native = module.director_native_config(table)

        for role in team["profiles"]:
            with self.subTest(role=role):
                desired = module.desired_profile_config(
                    role, team, Path("C:/runtime-home")
                )
                if role == team["director"]:
                    self.assertEqual(desired["model"], native["model"])
                    self.assertEqual(
                        desired["fallback_providers"], native["fallback_providers"]
                    )
                    self.assertEqual(
                        desired["agent"]["reasoning_overrides"],
                        native["agent"]["reasoning_overrides"],
                    )
                    # The primary rejects vision (400 ModelProtocolUnsupported);
                    # the owner sends the director's vision calls through a chain:
                    # GLM 5.3 Flash, then Kimi For Coding, then Claude Opus 5.
                    # Only the director gets this override.
                    self.assertEqual(
                        desired["auxiliary"],
                        {
                            "vision": {
                                "provider": "zai",
                                "model": "glm-5.3-flash",
                                "base_url": "https://api.z.ai/api/coding/paas/v4",
                                "fallback_chain": [
                                    {
                                        "provider": "kimi-coding",
                                        "model": "kimi-for-coding",
                                        "base_url": "https://api.kimi.com/coding/v1",
                                        "api_mode": "chat_completions",
                                    },
                                    {
                                        "provider": "anthropic",
                                        "model": "claude-opus-5-5",
                                    },
                                ],
                            }
                        },
                    )
                else:
                    self.assertNotIn("model", desired)
                    self.assertNotIn("fallback_providers", desired)
                    self.assertNotIn("agent", desired)
                    self.assertNotIn("auxiliary", desired)

    def test_no_profile_is_configured_through_the_text_bridge(self):
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        for role in team["profiles"]:
            desired = module.desired_profile_config(role, team, Path("C:/runtime-home"))
            self.assertNotIn("goncloud-subscriptions", json.dumps(desired))

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
        # 2026-10-03 (adopted 2026-10-12): the director carries the kanban
        # toolset. kanban_create is how it hands work to a specialist profile,
        # and unlike the CLI it subscribes the chat so the gateway reports
        # back when the card finishes. Its 14 schemas cost 23.9 KB of every
        # director call. The order is the live one: a redeploy compares the
        # whole list.
        self.assertEqual(
            director["platform_toolsets"]["telegram"],
            [
                "browser",
                "clarify",
                "code_execution",
                "coding",
                "computer_use",
                "connections",
                "delegation",
                "file",
                "goncloud_team",
                "kanban",
                "memory",
                "session_search",
                "skills",
                "terminal",
                "todo",
                "vision",
                "web",
            ],
        )
        self.assertIn("kanban", worker["platform_toolsets"]["telegram"])

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


class DirectorNativeChainTests(unittest.TestCase):
    def table(self):
        return json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )

    def test_director_chain_is_the_main_row_on_native_providers(self):
        config = module.director_native_config(self.table())

        # Slots 0 and 1 are the owner's two OpenCode Go accounts: one native
        # provider whose credential pool rotates between them.
        self.assertEqual(
            config["model"],
            {"provider": "opencode-go", "default": "muse-spark-1.3-contributor"},
        )
        self.assertEqual(
            config["fallback_providers"],
            [
                {"provider": "anthropic", "model": "claude-opus-5-5"},
                {"provider": "xai-oauth", "model": "grok-4.7"},
                {
                    "provider": "zai",
                    "model": "glm-5.3",
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                },
                {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                {
                    "provider": "kimi-coding",
                    "model": "k3",
                    "base_url": "https://api.kimi.com/coding/v1",
                    "api_mode": "chat_completions",
                },
            ],
        )
        self.assertEqual(
            config["agent"],
            {
                "reasoning_overrides": {
                    "claude-opus-5-5": "xhigh",
                    "gpt-5.6-sol": "xhigh",
                }
            },
        )

    def test_muse_never_reaches_a_native_provider(self):
        # Native meta-ai bills Meta's API; the owner pays only the Muse
        # subscription, which the workers reach through the Muse CLI.
        table = self.table()
        for row in table["chains"]:
            config = module.native_config(table, row)
            providers = [config["model"], *config["fallback_providers"]]

            self.assertNotIn("meta-ai", [entry["provider"] for entry in providers])
            self.assertNotIn("muse-wonder-1.0", config["agent"]["reasoning_overrides"])

    def test_reordering_the_table_reorders_the_chain(self):
        table = self.table()
        main = table["chains"]["main"]
        main[2], main[4] = main[4], main[2]

        chain = module.director_native_config(table)["fallback_providers"]

        self.assertEqual(
            [entry["provider"] for entry in chain[:3]],
            ["xai-oauth", "anthropic", "zai"],
        )

    def test_a_position_without_native_binding_fails_naming_the_label(self):
        table = self.table()
        table["chains"]["main"][5] = "deepseek/deepseek-flash"

        with self.assertRaisesRegex(ValueError, "deepseek/deepseek-flash"):
            module.director_native_config(table)

    def test_one_model_with_two_efforts_is_refused(self):
        table = self.table()
        table["chains"]["main"][6] = "anthropic/claude-opus-5-5 (high)"

        with self.assertRaisesRegex(ValueError, "claude-opus-5-5"):
            module.director_native_config(table)

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

    def test_bot_mode_messages_never_carry_engineering_work(self):
        # 2026-10-03: Bot Mode gave every Bot Chat a message_agent tool, a
        # second channel beside the chain. Owner's decision: the chain keeps
        # all engineering work; message_agent is for quick questions only.
        # Every profile's SOUL starts with the shared contract, so the rule
        # lives there, and the director's role must not contradict it.
        contract = (ROOT / "shared/CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("message_agent", contract)
        self.assertIn(
            "Nunca asignes, aceptes ni entregues trabajo de ingenieria por message_agent",
            contract,
        )
        claw = (ROOT / "roles/claw.md").read_text(encoding="utf-8")
        self.assertIn(
            "El codigo sigue por goncloud_local_run, no por estas tarjetas", claw
        )

    def test_the_director_hands_specialist_work_to_a_card(self):
        # 2026-10-03 (adopted 2026-10-12): work that belongs to a specialist
        # profile goes to it through a kanban card; from Telegram there is no
        # message_agent. Code still goes through the chain.
        #
        # Corrected 2026-10-12. The hand-written text sent the director to the
        # CLI (`hermes kanban create`), which subscribes nobody, so no one
        # learned that a card had finished; kanban_create subscribes the chat.
        # It also offered `operaciones` as a target, but only the allowlisted
        # specialists are dispatched: that card would sit in `ready` forever,
        # as on 2026-09-15. And it still said the dispatcher was off.
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        claw = (ROOT / "roles/claw.md").read_text(encoding="utf-8")
        section = claw.split("# Especialistas", 1)[1]
        for specialist in team["kanban"]["dispatch_profiles"]:
            with self.subTest(specialist=specialist):
                self.assertIn(f"- {specialist}:", section)
        for stage_role in team["profiles"]:
            with self.subTest(stage_role=stage_role):
                self.assertNotIn(f"- {stage_role}:", section)
        self.assertIn("La via es una tarjeta con kanban_create", claw)
        self.assertIn("No uses hermes kanban create", claw)
        self.assertNotIn("despachador nativo esta apagado", claw)

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

    def test_an_owned_key_no_longer_desired_is_retired(self):
        previous = {"agent": {"reasoning_overrides": {"a": "high", "b": "max"}}}
        existing = {
            "agent": {"reasoning_overrides": {"a": "high", "b": "max"}, "keep": 1}
        }
        desired = {"agent": {"reasoning_overrides": {"a": "high"}}}

        merged = module.merge_owned(existing, desired, previous)

        self.assertEqual(
            merged, {"agent": {"reasoning_overrides": {"a": "high"}, "keep": 1}}
        )

    def test_a_retired_key_the_user_changed_is_a_conflict(self):
        previous = {"agent": {"reasoning_overrides": {"b": "max"}}}
        existing = {"agent": {"reasoning_overrides": {"b": "low"}}}

        with self.assertRaisesRegex(ValueError, "agent.reasoning_overrides.b"):
            module.merge_owned(
                existing, {"agent": {"reasoning_overrides": {}}}, previous
            )

    def test_managed_file_conflict_leaves_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "SOUL.md"
            target.write_text("user edit", encoding="utf-8")
            with self.assertRaises(ValueError):
                module.check_file(target, "desired", "previous")
            self.assertEqual(target.read_text(encoding="utf-8"), "user edit")

    def test_check_validates_without_writing(self):
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

            fresh = subprocess.run(
                command + ["--check"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertIn("Check passed: 9 profiles", fresh.stdout)
            self.assertIn("Root dispatch allowlist: would be written", fresh.stdout)
            self.assertFalse((runtime / "profiles" / "claw").exists())
            self.assertFalse((runtime / "config.yaml").exists())

            applied = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
            root = yaml.safe_load((runtime / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(
                root["kanban"]["dispatch_profiles"], team["kanban"]["dispatch_profiles"]
            )
            self.assertIs(root["kanban"]["auto_decompose"], False)
            # The model watcher ships to the director only: it is a cron
            # script of the director's gateway, next to the table it reads.
            watcher = runtime / "profiles/claw/scripts/model_watch.py"
            self.assertEqual(
                watcher.read_text(encoding="utf-8"),
                (ROOT / "scripts/model_watch.py").read_text(encoding="utf-8"),
            )
            self.assertTrue(
                (
                    watcher.parents[1]
                    / "plugins/goncloud-team/model-routing.source.json"
                ).is_file()
            )
            self.assertFalse(
                (runtime / "profiles/scout/scripts/model_watch.py").exists()
            )
            self.assertEqual(
                (watcher.parent / "pinned-models.json").read_text(encoding="utf-8"),
                (ROOT / "scripts/pinned-models.json").read_text(encoding="utf-8"),
            )
            soul = runtime / "profiles/scout/SOUL.md"
            soul.write_text("user edit", encoding="utf-8")
            state = runtime / "profiles/claw/goncloud-managed.json"
            recorded = state.read_bytes()

            conflict = subprocess.run(
                command + ["--check"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("Local managed-file conflict", conflict.stderr)
            self.assertEqual(soul.read_text(encoding="utf-8"), "user edit")
            self.assertEqual(state.read_bytes(), recorded)

    def test_a_specialist_gets_its_native_chain_and_keeps_everything_else(self):
        # The eleven specialist profiles are the owner's: made and shaped by
        # hand, never created here. Until 2026-10-12 they had no `model:`, so
        # a card dispatched to one ran on whatever provider its environment
        # selected. Only the chain (the row team.json names) and the plugin
        # copy are managed; SOUL, toolsets and the rest stay untouched.
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
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        source = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )
        chain = module.native_config(source, team["specialist_row"])
        self.assertEqual(
            module.native_config(source, "claw"), module.director_native_config(source)
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

            def run(*extra):
                return subprocess.run(
                    [*command, *extra],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=120,
                )

            first = run()
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("Skipped specialist amazon: no such profile", first.stdout)
            self.assertFalse((runtime / "profiles/amazon").exists())

            amazon = runtime / "profiles/amazon"
            amazon.mkdir(parents=True)
            own = {"platform_toolsets": {"cli": ["browser", "web"]}, "agent": {}}
            (amazon / "config.yaml").write_text(yaml.safe_dump(own), encoding="utf-8")
            (amazon / "SOUL.md").write_text("soy amazon", encoding="utf-8")

            applied = run()
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("Configured amazon", applied.stdout)
            config = yaml.safe_load(
                (amazon / "config.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(config["model"], chain["model"])
            self.assertEqual(config["model"]["provider"], "opencode-go")
            self.assertEqual(config["fallback_providers"], chain["fallback_providers"])
            self.assertEqual(
                config["agent"]["reasoning_overrides"],
                chain["agent"]["reasoning_overrides"],
            )
            self.assertEqual(config["platform_toolsets"], own["platform_toolsets"])
            self.assertNotIn("terminal", config)
            self.assertEqual(
                (amazon / "SOUL.md").read_text(encoding="utf-8"), "soy amazon"
            )
            self.assertTrue(
                (amazon / "plugins/goncloud-team/model-routing.source.json").is_file()
            )
            self.assertFalse((amazon / "skills").exists())

            again = run()
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(
                yaml.safe_load((amazon / "config.yaml").read_text(encoding="utf-8")),
                config,
            )

            config["model"] = {"provider": "custom", "default": "mine"}
            (amazon / "config.yaml").write_text(
                yaml.safe_dump(config), encoding="utf-8"
            )
            conflict = run("--check")
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("conflict", conflict.stderr)

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
                self.assertFalse((profile / "plugins/model-providers").exists())
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
            # Runtime-only role instructions may evolve independently. An
            # explicit preserve flag must still deploy routing artifacts and
            # the owned director model without overwriting those instructions.
            claw_soul = runtime / "profiles/claw/SOUL.md"
            local_soul = claw_soul.read_text(encoding="utf-8") + "\nLOCAL RULE\n"
            claw_soul.write_text(local_soul, encoding="utf-8")
            preserved = subprocess.run(
                [*command, "--preserve-local-soul"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(preserved.returncode, 0, preserved.stderr)
            self.assertEqual(claw_soul.read_text(encoding="utf-8"), local_soul)
            claw_config = yaml.safe_load(
                (runtime / "profiles/claw/config.yaml").read_text(encoding="utf-8")
            )
            native = module.director_native_config(
                json.loads(
                    (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
                )
            )
            self.assertEqual(claw_config["model"], native["model"])
            self.assertEqual(
                claw_config["fallback_providers"], native["fallback_providers"]
            )
            self.assertEqual(
                (
                    runtime
                    / "profiles/claw/plugins/goncloud-team/model-routing.source.json"
                ).read_bytes(),
                (ROOT / "model-routing.source.json").read_bytes(),
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


class RootKanbanPolicyTests(unittest.TestCase):
    POLICY = {"auto_decompose": False, "dispatch_profiles": ["amazon", "meli"]}

    def test_policy_lands_in_the_existing_block_and_keeps_the_rest(self):
        text = (
            "# header\r\nkanban:\r\n  review_dispatch: true\r\n\r\n"
            "# next\r\nterminal:\r\n  backend: local\r\n"
        )

        result = module.apply_root_kanban(text, self.POLICY)

        parsed = yaml.safe_load(result)
        self.assertEqual(parsed["kanban"], {"review_dispatch": True, **self.POLICY})
        self.assertEqual(parsed["terminal"], {"backend": "local"})
        self.assertIn("# header\r\n", result)
        self.assertIn("# next\r\n", result)
        self.assertNotIn("\n", result.replace("\r\n", ""))
        self.assertEqual(module.apply_root_kanban(result, self.POLICY), result)

    def test_an_existing_allowlist_is_replaced_not_duplicated(self):
        text = (
            "kanban:\n  dispatch_profiles:\n    - scout\n"
            "  auto_decompose: true\nmodel:\n  default: x\n"
        )

        result = module.apply_root_kanban(text, self.POLICY)

        self.assertEqual(result.count("dispatch_profiles"), 1)
        self.assertNotIn("scout", result)
        parsed = yaml.safe_load(result)
        self.assertEqual(parsed["kanban"], self.POLICY)
        self.assertEqual(parsed["model"], {"default": "x"})

    def test_a_config_without_the_block_gets_one(self):
        self.assertEqual(
            yaml.safe_load(module.apply_root_kanban("", self.POLICY)),
            {"kanban": self.POLICY},
        )
        parsed = yaml.safe_load(
            module.apply_root_kanban("model:\n  default: x\n", self.POLICY)
        )
        self.assertEqual(parsed, {"model": {"default": "x"}, "kanban": self.POLICY})

    def test_an_inline_block_is_refused_instead_of_corrupted(self):
        with self.assertRaisesRegex(ValueError, "written inline"):
            module.apply_root_kanban("kanban: {review_dispatch: true}\n", self.POLICY)


class ManagedFileRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.temporary = Path(self.tmp.name)

    def deploy(self, runtime=None, extra_args=()):
        upstream = Path(
            os.environ.get(
                "HERMES_INSTALL_DIR",
                Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/hermes-agent",
            )
        )
        runtime = runtime or self.temporary / "runtime"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(runtime),
                "--install-dir",
                str(upstream),
                *extra_args,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "HOME": str(self.temporary / "home"),
                "LOCALAPPDATA": str(self.temporary / "local-app-data"),
                "PYTHONUTF8": "1",
            },
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return runtime

    def test_a_managed_file_no_longer_shipped_is_removed(self):
        # Deploy once, then plant a managed file as if an earlier revision had
        # shipped it (the goncloud-subscriptions provider did).
        runtime = self.deploy()
        profile = runtime / "profiles/claw"
        stale = "plugins/model-providers/goncloud-subscriptions/client.py"
        (profile / stale).parent.mkdir(parents=True, exist_ok=True)
        (profile / stale).write_text("old\n", encoding="utf-8")
        state_path = profile / "goncloud-managed.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["files"][stale] = "old\n"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        self.deploy(runtime)

        self.assertFalse((profile / stale).exists())
        self.assertFalse((profile / "plugins/model-providers").exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn(stale, state["files"])

    def test_a_dropped_managed_file_the_user_edited_is_a_conflict(self):
        runtime = self.deploy()
        profile = runtime / "profiles/claw"
        stale = "plugins/model-providers/goncloud-subscriptions/client.py"
        (profile / stale).parent.mkdir(parents=True, exist_ok=True)
        (profile / stale).write_text("edited\n", encoding="utf-8")
        state_path = profile / "goncloud-managed.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["files"][stale] = "old\n"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(AssertionError, "managed-file conflict"):
            self.deploy(runtime)
        self.assertEqual((profile / stale).read_text(encoding="utf-8"), "edited\n")

    def test_a_retired_path_escaping_its_profile_aborts_without_touching_the_sibling(
        self,
    ):
        # A managed-file record like "../scout/x" must never let retirement
        # delete or prune a sibling profile's files; containment is scoped to
        # the profile being processed, so this aborts the whole deploy first.
        runtime = self.deploy()
        claw = runtime / "profiles/claw"
        scout = runtime / "profiles/scout"
        sibling = "some/file.txt"
        (scout / sibling).parent.mkdir(parents=True, exist_ok=True)
        (scout / sibling).write_text("sibling\n", encoding="utf-8")
        escape = f"../scout/{sibling}"
        state_path = claw / "goncloud-managed.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["files"][escape] = "sibling\n"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(AssertionError, "escapes"):
            self.deploy(runtime)

        self.assertEqual((scout / sibling).read_text(encoding="utf-8"), "sibling\n")

    def test_preserve_local_soul_keeps_soul_while_retiring_other_files(self):
        runtime = self.deploy()
        profile = runtime / "profiles/claw"
        stale = "plugins/model-providers/goncloud-subscriptions/client.py"
        (profile / stale).parent.mkdir(parents=True, exist_ok=True)
        (profile / stale).write_text("old\n", encoding="utf-8")
        state_path = profile / "goncloud-managed.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["files"][stale] = "old\n"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        local_soul = (profile / "SOUL.md").read_text(
            encoding="utf-8"
        ) + "\nLOCAL RULE\n"
        (profile / "SOUL.md").write_text(local_soul, encoding="utf-8")

        self.deploy(runtime, extra_args=("--preserve-local-soul",))

        self.assertEqual((profile / "SOUL.md").read_text(encoding="utf-8"), local_soul)
        self.assertFalse((profile / stale).exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn(stale, state["files"])
        self.assertIn("SOUL.md", state["files"])


class PruneEmptyDirsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.root = Path(self.tmp.name)

    def test_prunes_empty_ancestors_up_to_stop(self):
        stop = self.root / "profile"
        nested = stop / "plugins" / "model-providers" / "goncloud-subscriptions"
        nested.mkdir(parents=True)

        module._prune_empty_dirs(nested, stop)

        self.assertTrue(stop.is_dir())
        self.assertFalse((stop / "plugins").exists())

    def test_stops_at_a_directory_that_still_has_content(self):
        stop = self.root / "profile"
        nested = stop / "plugins" / "a" / "b"
        nested.mkdir(parents=True)
        (stop / "plugins" / "keep.txt").write_text("x\n", encoding="utf-8")

        module._prune_empty_dirs(nested, stop)

        self.assertTrue((stop / "plugins").is_dir())
        self.assertFalse((stop / "plugins" / "a").exists())
        self.assertTrue((stop / "plugins" / "keep.txt").exists())

    def test_stop_itself_is_never_removed(self):
        stop = self.root / "profile"
        stop.mkdir()

        module._prune_empty_dirs(stop, stop)

        self.assertTrue(stop.is_dir())

    def test_a_directory_outside_stop_is_left_alone(self):
        stop = self.root / "profile"
        stop.mkdir()
        outside_empty = self.root / "sibling" / "empty"
        outside_empty.mkdir(parents=True)

        module._prune_empty_dirs(outside_empty, stop)

        self.assertTrue(outside_empty.is_dir())


class NoTextBridgeTests(unittest.TestCase):
    def test_no_text_tool_call_bridge_remains(self):
        self.assertFalse((ROOT / "plugins/model-providers").exists())
        self.assertFalse(
            (ROOT / "plugins/goncloud-team/subscriptions/conversation.py").exists()
        )
        for path in (ROOT / "plugins").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in (
                "tool_bridge",
                "_REISSUE_PROMPT",
                "_TOOL_BRIDGE",
                "serves_conversations",
                "<tool_call>",
            ):
                self.assertNotIn(marker, text, f"{marker} in {path}")


if __name__ == "__main__":
    unittest.main()
