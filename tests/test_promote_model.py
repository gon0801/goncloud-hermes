import copy
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


module = load("promote_model")
claw_models = load("claw_models")

SOURCE = {
    "columns": ["Primary", "Primary Go 2", "F1", "F2"],
    "chains": {
        "main": [
            "opencode-go-resp/muse-spark-1.3-contributor",
            "opencode-go-resp/muse-spark-1.3-contributor",
            "anthropic/claude-opus-5 (xhigh)",
            "meta/muse-spark-1.3 (max)",
        ],
        "scout": [
            "opencode-go/deepseek-v4.1-flash",
            "opencode-go/deepseek-v4.1-flash",
            "meta/muse-spark-1.3 (max)",
            "kimi/k3",
        ],
    },
}
MAPPING = {
    "second_account_suffix": "-2",
    "id_overrides": {},
    "agents": {
        "main": {"row": "main", "thinking": True},
        "scout": {"row": "scout", "thinking": True},
    },
}


class ReplaceTests(unittest.TestCase):
    def test_every_cell_with_the_model_changes_and_keeps_its_effort(self):
        source = copy.deepcopy(SOURCE)

        cells = module.replace_label(
            source, "meta/muse-spark-1.3", "meta/muse-wonder-1.0"
        )

        self.assertEqual(cells, ["main:3", "scout:2"])
        self.assertEqual(source["chains"]["main"][3], "meta/muse-wonder-1.0 (max)")
        self.assertEqual(source["chains"]["scout"][2], "meta/muse-wonder-1.0 (max)")
        # The contributor model on OpenCode Go is another model: untouched.
        self.assertEqual(source["chains"]["main"][:3], SOURCE["chains"]["main"][:3])
        self.assertEqual(source["columns"], SOURCE["columns"])

    def test_a_model_the_table_does_not_use_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "no cell uses meta/muse-spark-9"):
            module.replace_label(copy.deepcopy(SOURCE), "meta/muse-spark-9", "meta/x")

    def test_a_direct_cell_needs_a_family_the_router_binds(self):
        # Positions 2+ pick their adapter from the model name; an unknown
        # family would take down the whole row at compile time.
        with self.assertRaisesRegex(ValueError, "wonder-1.0.*main:3"):
            module.replace_label(
                copy.deepcopy(SOURCE), "meta/muse-spark-1.3", "meta/wonder-1.0"
            )

    def test_an_opencode_go_cell_takes_any_model_name(self):
        source = copy.deepcopy(SOURCE)

        cells = module.replace_label(
            source, "opencode-go/deepseek-v4.1-flash", "opencode-go/qwen3.9-max"
        )

        self.assertEqual(cells, ["scout:0", "scout:1"])


class KeepClawTests(unittest.TestCase):
    def test_keeping_claw_leaves_its_chains_exactly_as_they_were(self):
        # Claw may not be ready for the new model (its Meta key is rejected
        # today), so the promotion can move hermes and hold claw on the old ID.
        before = claw_models.desired_claw(SOURCE, MAPPING)
        source, mapping = copy.deepcopy(SOURCE), copy.deepcopy(MAPPING)

        module.replace_label(source, "meta/muse-spark-1.3", "meta/muse-wonder-1.0")
        module.hold_claw(mapping, "meta/muse-spark-1.3", "meta/muse-wonder-1.0")

        self.assertEqual(
            mapping["id_overrides"], {"meta/muse-wonder-1.0": "meta/muse-spark-1.3"}
        )
        self.assertEqual(claw_models.desired_claw(source, mapping), before)

    def test_without_the_hold_claw_follows_the_table(self):
        source, mapping = copy.deepcopy(SOURCE), copy.deepcopy(MAPPING)

        module.replace_label(source, "meta/muse-spark-1.3", "meta/muse-wonder-1.0")
        entry = claw_models.desired_claw(source, mapping)["agents"]["entries"]["main"]

        self.assertEqual(entry["model"]["fallbacks"][2], "meta/muse-wonder-1.0")
        self.assertEqual(
            entry["models"]["meta/muse-wonder-1.0"], {"params": {"thinking": "max"}}
        )

    def test_releasing_a_hold_makes_claw_follow_again(self):
        mapping = copy.deepcopy(MAPPING)
        module.hold_claw(mapping, "meta/muse-spark-1.3", "meta/muse-wonder-1.0")

        module.release_claw(mapping, "meta/muse-wonder-1.0")

        self.assertEqual(mapping["id_overrides"], {})
        with self.assertRaisesRegex(ValueError, "no hold"):
            module.release_claw(mapping, "meta/muse-wonder-1.0")


class ProbeListTests(unittest.TestCase):
    def test_one_probe_per_route_the_change_touches(self):
        source = copy.deepcopy(SOURCE)
        cells = module.replace_label(
            source, "meta/muse-spark-1.3", "meta/muse-wonder-1.0"
        )

        self.assertEqual(
            module.probes(source, cells),
            ['python scripts/probe_route.py --position 3 "meta/muse-wonder-1.0 (max)"'],
        )

    def test_a_model_on_both_go_accounts_is_probed_on_each(self):
        source = copy.deepcopy(SOURCE)
        cells = module.replace_label(
            source,
            "opencode-go-resp/muse-spark-1.3-contributor",
            "opencode-go-resp/muse-wonder-1.0-contributor",
        )

        self.assertEqual(
            module.probes(source, cells),
            [
                'python scripts/probe_route.py --position 0 "opencode-go-resp/muse-wonder-1.0-contributor"',
                'python scripts/probe_route.py --position 1 "opencode-go-resp/muse-wonder-1.0-contributor"',
            ],
        )


if __name__ == "__main__":
    unittest.main()
