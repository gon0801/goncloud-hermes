from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.catalog import SLOT_COUNT, compile_profile_routes  # noqa: E402


def load_source() -> dict:
    return json.loads((ROOT / "model-routing.source.json").read_text(encoding="utf-8"))


EXPECTED_ADAPTERS = {
    "claw": [
        "opencode_go",
        "claude",
        "muse",
        "muse",
        "grok",
        "zai",
        "codex",
    ],
    "operaciones": [
        "opencode_go",
        "claude",
        "muse",
        "zai",
        "kimi",
        "grok",
        "codex",
    ],
    "ingenieria": [
        "opencode_go",
        "codex",
        "kimi",
        "zai",
        "muse",
        "claude",
        "grok",
    ],
    "implementer": [
        "opencode_go",
        "muse",
        "zai",
        "zai",
        "zai",
        "grok",
        "claude",
    ],
    "reviewer": [
        "opencode_go",
        "claude",
        "kimi",
        "kimi",
        "codex",
        "muse",
        "grok",
    ],
    "adversary": [
        "opencode_go",
        "grok",
        "kimi",
        "zai",
        "muse",
        "claude",
        "deepseek",
    ],
    "verifier": [
        "opencode_go",
        "codex",
        "muse",
        "kimi",
        "zai",
        "claude",
        "deepseek",
    ],
    "scout": [
        "opencode_go",
        "zai",
        "muse",
        "kimi",
        "grok",
        "claude",
        "codex",
    ],
}
EXPECTED_ADAPTERS["entrega"] = EXPECTED_ADAPTERS["claw"]

EXPECTED_MODELS = {
    "claw": [
        "muse-spark-1.3-contributor",
        "claude-sonnet-5",
        "muse-spark-1.3",
        "muse-spark-1.3",
        "grok-4.6",
        "glm-5.3",
        "gpt-5.6-terra",
    ],
    "operaciones": [
        "deepseek-v4.1-flash",
        "claude-sonnet-5",
        "muse-spark-1.3",
        "glm-5.3",
        "k3",
        "grok-4.6",
        "gpt-5.6-terra",
    ],
    "ingenieria": [
        "muse-spark-1.3-contributor",
        "gpt-5.6-sol",
        "k3",
        "glm-5.3",
        "muse-spark-1.3",
        "claude-opus-5",
        "grok-4.6",
    ],
    "implementer": [
        "muse-spark-1.3-contributor",
        "muse-spark-1.3",
        "glm-5.3",
        "glm-5.3",
        "glm-5.3",
        "grok-4.6",
        "claude-opus-5",
    ],
    "reviewer": [
        "deepseek-v4.1-flash",
        "claude-fable-5-1",
        "k3",
        "k3",
        "gpt-6-astra",
        "muse-spark-1.3",
        "grok-4.6",
    ],
    "adversary": [
        "deepseek-v4.1-flash",
        "grok-4.6",
        "k3",
        "glm-5.3",
        "muse-spark-1.3",
        "claude-opus-5",
        "deepseek-flash",
    ],
    "verifier": [
        "deepseek-v4.1-flash",
        "gpt-6-astra",
        "muse-spark-1.3",
        "k3",
        "glm-5.3",
        "claude-fable-5-1",
        "deepseek-flash",
    ],
    "scout": [
        "deepseek-v4.1-flash",
        "glm-5.3",
        "muse-spark-1.3",
        "k3",
        "grok-4.6",
        "claude-sonnet-5",
        "gpt-5.6-sol",
    ],
}

API_PROFILES = {"adversary", "verifier"}


class SlotSemanticsTests(unittest.TestCase):
    def test_primary_is_go_and_later_slots_bind_by_family(self):
        routes = compile_profile_routes("claw", load_source())

        self.assertEqual([route.adapter for route in routes], EXPECTED_ADAPTERS["claw"])
        self.assertEqual([route.model for route in routes], EXPECTED_MODELS["claw"])

    def test_duplicate_positions_and_entrega_inheritance(self):
        source = load_source()

        self.assertEqual(
            compile_profile_routes("entrega", source),
            compile_profile_routes("claw", source),
        )
        implementer = compile_profile_routes("implementer", source)
        self.assertEqual(len(implementer), SLOT_COUNT)
        self.assertEqual(
            [route.adapter for route in implementer[2:5]],
            ["zai", "zai", "zai"],
        )
        self.assertEqual({route.model for route in implementer[2:5]}, {"glm-5.3"})
        self.assertEqual([route.slot for route in implementer[2:5]], [2, 3, 4])

    def test_parenthetical_annotations_stay_on_the_label_not_the_model_id(self):
        source = load_source()
        claw = compile_profile_routes("claw", source)
        muse = claw[2]
        codex = claw[6]

        self.assertEqual(muse.adapter, "muse")
        self.assertEqual(muse.model, "muse-spark-1.3")
        self.assertEqual(muse.label, "meta/muse-spark-1.3 (max)")
        self.assertEqual(codex.adapter, "codex")
        self.assertEqual(codex.model, "gpt-5.6-terra")
        self.assertEqual(codex.label, "openai/gpt-5.6-terra (xhigh)")

        scout = compile_profile_routes("scout", source)
        self.assertEqual(scout[5].model, "claude-sonnet-5")
        self.assertEqual(scout[5].label, "anthropic/claude-sonnet-5 (xhigh)")

    def test_the_zen_free_tier_name_is_not_sent_to_the_muse_cli(self):
        # "-free" names OpenCode Zen's free tier, where it is a real model id.
        # The Muse subscription serves the same family without that suffix and
        # answers "model does not exist" when it is sent, so the suffix is
        # provenance on the label exactly like " dup", never part of the id.
        source = {
            "chains": {
                "scout": [
                    "opencode-go/deepseek-v4.1-flash",
                    "meta/muse-spark-1.3-contributor-free",
                    "kimi/k3",
                    "zai/glm-5.3",
                    "xai/grok-4.6",
                    "anthropic/claude-sonnet-5",
                    "openai/gpt-6-astra",
                ]
            }
        }

        muse = compile_profile_routes("scout", source)[1]
        self.assertEqual(muse.adapter, "muse")
        self.assertEqual(muse.model, "muse-spark-1.3-contributor")
        self.assertEqual(muse.label, "meta/muse-spark-1.3-contributor-free")

    def test_the_source_table_itself_is_never_rewritten(self):
        source = load_source()

        compile_profile_routes("claw", source)
        compile_profile_routes("ingenieria", source)

        self.assertEqual(
            source["columns"], ["Primary", "F1", "F2", "F3", "F4", "F5", "F6"]
        )
        self.assertEqual(source["chains"]["main"][2], "meta/muse-spark-1.3 (max)")
        self.assertEqual(
            source["chains"]["ingenieria"][1], "openai/gpt-5.6-sol (xhigh)"
        )
        self.assertEqual(source["chains"]["main"][1], "anthropic/claude-sonnet-5")

    def test_every_profile_compiles_the_owner_table(self):
        source = load_source()

        for profile, adapters in EXPECTED_ADAPTERS.items():
            with self.subTest(profile=profile):
                routes = compile_profile_routes(profile, source)
                self.assertEqual(len(routes), SLOT_COUNT, profile)
                self.assertEqual(
                    [route.slot for route in routes], list(range(SLOT_COUNT))
                )
                self.assertEqual([route.adapter for route in routes], adapters)
                self.assertEqual(
                    [route.model for route in routes],
                    EXPECTED_MODELS["claw" if profile == "entrega" else profile],
                )
                self.assertEqual(
                    [route.billing_mode for route in routes].count("api"),
                    1 if profile in API_PROFILES else 0,
                    profile,
                )

    def test_only_deepseek_family_slots_are_api_billed(self):
        routes = compile_profile_routes("reviewer", load_source())

        self.assertEqual(routes[0].adapter, "opencode_go")
        self.assertEqual(routes[0].model, "deepseek-v4.1-flash")
        self.assertEqual(routes[0].billing_mode, "subscription")
        self.assertEqual(routes[1].adapter, "claude")
        self.assertEqual(routes[1].model, "claude-fable-5-1")
        self.assertEqual(routes[4].adapter, "codex")
        self.assertEqual(routes[4].model, "gpt-6-astra")

        scout = compile_profile_routes("scout", load_source())
        self.assertEqual(scout[6].adapter, "codex")
        self.assertEqual(scout[6].billing_mode, "subscription")

        adversary = compile_profile_routes("adversary", load_source())
        self.assertEqual(adversary[6].adapter, "deepseek")
        self.assertEqual(adversary[6].model, "deepseek-flash")
        self.assertEqual(adversary[6].billing_mode, "api")

    def test_unknown_family_and_profile_are_rejected(self):
        source = load_source()

        with self.assertRaisesRegex(ValueError, "unknown model family"):
            compile_profile_routes(
                "scout",
                {
                    **source,
                    "chains": {
                        **source["chains"],
                        "scout": ["zai/glm-5.3"] + ["mystery/model"] * 6,
                    },
                },
            )
        with self.assertRaisesRegex(ValueError, "No model chain"):
            compile_profile_routes("pirate", source)
        with self.assertRaisesRegex(ValueError, "seven"):
            compile_profile_routes(
                "scout",
                {
                    **source,
                    "chains": {**source["chains"], "scout": ["zai/glm-5.3"]},
                },
            )


if __name__ == "__main__":
    unittest.main()
