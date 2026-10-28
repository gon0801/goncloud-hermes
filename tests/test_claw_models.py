import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "claw_models", ROOT / "scripts/claw_models.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

MAPPING = {"second_account_suffix": "-2", "id_overrides": {}}
ROW = [
    "go/muse-a",
    "go/muse-a",
    "anthropic/opus (xhigh)",
    "meta/muse-b (max)",
    "xai/grok",
    "zai/glm",
    "openai/gpt (xhigh)",
    "kimi/k3",
]


class ChainTests(unittest.TestCase):
    def test_position_one_runs_on_the_second_account(self):
        # In the table position 1 repeats position 0: same model, second
        # OpenCode Go account. Claw names that account as its own provider.
        agent = module.desired_agent(ROW, MAPPING, thinking=True)

        self.assertEqual(agent["model"]["primary"], "go/muse-a")
        self.assertEqual(agent["model"]["fallbacks"][0], "go-2/muse-a")
        self.assertEqual(len(agent["model"]["fallbacks"]), 7)

    def test_position_one_is_left_alone_when_it_is_another_model(self):
        row = ["go/muse-a", "zai/glm", *ROW[2:]]

        agent = module.desired_agent(row, MAPPING, thinking=True)

        self.assertEqual(agent["model"]["fallbacks"][0], "zai/glm")

    def test_effort_markers_become_thinking_and_leave_the_id(self):
        agent = module.desired_agent(ROW, MAPPING, thinking=True)

        self.assertEqual(
            agent["model"]["fallbacks"][1:3], ["anthropic/opus", "meta/muse-b"]
        )
        self.assertEqual(
            agent["models"],
            {
                "anthropic/opus": {"params": {"thinking": "xhigh"}},
                "meta/muse-b": {"params": {"thinking": "max"}},
                "openai/gpt": {"params": {"thinking": "xhigh"}},
            },
        )

    def test_an_agent_without_thinking_gets_only_the_chain(self):
        # The eleven specialists run the operaciones chain in claw but carry
        # no per-model thinking there (measured 2026-10-12).
        agent = module.desired_agent(ROW, MAPPING, thinking=False)

        self.assertEqual(agent["model"]["fallbacks"][1], "anthropic/opus")
        self.assertNotIn("models", agent)

    def test_a_repeated_row_on_the_second_account_does_not_get_a_second_suffix(self):
        mapping = {
            "second_account_suffix": "-2",
            "id_overrides": {"go/muse-a": "go-2/muse-a"},
        }

        agent = module.desired_agent(ROW, mapping, thinking=True)

        self.assertEqual(agent["model"]["primary"], "go-2/muse-a")
        self.assertEqual(agent["model"]["fallbacks"][0], "go-2/muse-a")

    def test_an_override_renames_the_id_for_claw_only(self):
        # The same model can have one ID on the route hermes uses and another
        # on the route claw uses (Muse CLI vs Meta API). The table keeps the
        # hermes ID; the override gives claw's.
        mapping = {
            "second_account_suffix": "-2",
            "id_overrides": {"meta/muse-b": "meta/muse-b-1.0", "go/muse-a": "go/a2"},
        }

        agent = module.desired_agent(ROW, mapping, thinking=True)

        self.assertEqual(agent["model"]["primary"], "go/a2")
        self.assertEqual(agent["model"]["fallbacks"][0], "go-2/a2")
        self.assertEqual(agent["model"]["fallbacks"][2], "meta/muse-b-1.0")
        self.assertEqual(
            agent["models"]["meta/muse-b-1.0"], {"params": {"thinking": "max"}}
        )
        self.assertNotIn("meta/muse-b", agent["models"])

    def test_a_row_that_is_not_in_the_table_is_an_error(self):
        source = {"chains": {"main": ROW}}
        mapping = {
            **MAPPING,
            "agents": {"amazon": {"row": "ventas", "thinking": False}},
        }

        with self.assertRaisesRegex(ValueError, "amazon.*ventas"):
            module.desired_claw(source, mapping)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.source = {"chains": {"main": ROW}}
        self.mapping = {
            **MAPPING,
            "agents": {
                "main": {"row": "main", "thinking": True},
                "amazon": {"row": "main", "thinking": False},
            },
        }
        self.desired = module.desired_claw(self.source, self.mapping)

    def live(self):
        return json.loads(json.dumps(self.desired))

    def test_no_drift_when_claw_matches_the_table(self):
        live = self.live()
        # Keys the generator does not own must not read as drift.
        live["agents"]["entries"]["main"]["models"]["anthropic/opus"][
            "agentRuntime"
        ] = {"id": "claude-cli"}
        live["agents"]["entries"]["amazon"]["heartbeat"] = {"every": "0m"}

        self.assertEqual(module.drift(self.desired, live, self.mapping), [])

    def test_a_different_model_is_reported_with_its_position(self):
        live = self.live()
        live["agents"]["entries"]["main"]["model"]["fallbacks"][3] = "xai/grok-old"

        self.assertEqual(
            module.drift(self.desired, live, self.mapping),
            ["main position 4: claw has xai/grok-old, the table wants xai/grok"],
        )

    def test_missing_and_stale_thinking_are_reported(self):
        live = self.live()
        models = live["agents"]["entries"]["main"]["models"]
        del models["openai/gpt"]
        models["kimi/old"] = {"params": {"thinking": "high"}}
        models["meta/muse-b"]["params"]["thinking"] = "xhigh"

        self.assertEqual(
            module.drift(self.desired, live, self.mapping),
            [
                "main thinking kimi/old: claw has high, the table wants nothing",
                "main thinking meta/muse-b: claw has xhigh, the table wants max",
                "main thinking openai/gpt: claw has nothing, the table wants xhigh",
            ],
        )

    def test_thinking_is_not_compared_for_agents_that_do_not_carry_it(self):
        live = self.live()
        live["agents"]["entries"]["amazon"]["models"] = {
            "meta/muse-b": {"params": {"thinking": "max"}}
        }

        self.assertEqual(module.drift(self.desired, live, self.mapping), [])

    def test_agents_on_only_one_side_are_reported(self):
        live = self.live()
        del live["agents"]["entries"]["amazon"]
        live["agents"]["entries"]["nuevo"] = {"model": {"primary": "x/y"}}

        self.assertEqual(
            module.drift(self.desired, live, self.mapping),
            [
                "amazon: in claw-routing.json but not in claw",
                "nuevo: in claw but not in claw-routing.json",
            ],
        )


class RealFilesTests(unittest.TestCase):
    def test_every_claw_agent_points_at_a_full_row_of_the_table(self):
        source = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )
        mapping = json.loads((ROOT / "claw-routing.json").read_text(encoding="utf-8"))
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))

        desired = module.desired_claw(source, mapping)["agents"]["entries"]

        self.assertEqual(set(desired), set(mapping["agents"]))
        for agent, entry in desired.items():
            with self.subTest(agent=agent):
                row = source["chains"][mapping["agents"][agent]["row"]]
                self.assertEqual(entry["model"]["primary"], row[0].split(" ")[0])
                self.assertEqual(len(entry["model"]["fallbacks"]), len(row) - 1)
        # Every specialist the gateway may dispatch has a chain in claw too.
        self.assertLessEqual(
            set(team["kanban"]["dispatch_profiles"]), set(mapping["agents"])
        )


if __name__ == "__main__":
    unittest.main()
