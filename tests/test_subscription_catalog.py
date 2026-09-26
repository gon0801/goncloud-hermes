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
        "grok",
        "zai",
        "codex",
        "kimi",
    ],
    "operaciones": [
        "opencode_go",
        "zai",
        "codex",
        "claude",
        "grok",
        "kimi",
        "muse",
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
        "grok",
        "claude",
        "codex",
        "kimi",
    ],
    "reviewer": [
        "opencode_go",
        "claude",
        "kimi",
        "codex",
        "muse",
        "grok",
        "zai",
    ],
    "adversary": [
        "opencode_go",
        "grok",
        "kimi",
        "zai",
        "muse",
        "claude",
        "codex",
    ],
    "verifier": [
        "opencode_go",
        "zai",
        "muse",
        "kimi",
        "claude",
        "codex",
        "deepseek",
    ],
    "scout": [
        "opencode_go",
        "zai",
        "muse",
        "kimi",
        "deepseek",
        "claude",
        "codex",
    ],
}
EXPECTED_ADAPTERS["entrega"] = EXPECTED_ADAPTERS["claw"]

EXPECTED_MODELS = {
    "claw": [
        "muse-spark-1.3-contributor",
        "claude-opus-5",
        "muse-spark-1.3",
        "grok-4.6",
        "glm-5.3",
        "gpt-5.6-sol",
        "k3",
    ],
    "operaciones": [
        "deepseek-v4.1-flash",
        "glm-5.3-flash",
        "gpt-5.6-terra",
        "claude-sonnet-5",
        "grok-4.6",
        "kimi-for-coding",
        "muse-spark-1.3",
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
        "glm-5.3-flash",
        "grok-4.6",
        "claude-sonnet-5",
        "gpt-5.6-terra",
        "kimi-for-coding",
    ],
    "reviewer": [
        "deepseek-v4.1-flash",
        "claude-fable-5-1",
        "k3",
        "gpt-6-astra",
        "muse-spark-1.3",
        "grok-4.6",
        "glm-5.3",
    ],
    "adversary": [
        "deepseek-v4.1-flash",
        "grok-4.6",
        "k3",
        "glm-5.3",
        "muse-spark-1.3",
        "claude-opus-5",
        "gpt-5.6-sol",
    ],
    "verifier": [
        "deepseek-v4.1-flash",
        "glm-5.3-flash",
        "muse-spark-1.3",
        "kimi-for-coding",
        "claude-sonnet-5",
        "gpt-5.6-terra",
        "deepseek-flash",
    ],
    "scout": [
        "deepseek-v4.1-flash",
        "glm-5.3-flash",
        "muse-spark-1.3",
        "kimi-for-coding",
        "deepseek-flash",
        "claude-sonnet-5",
        "gpt-5.6-terra",
    ],
}

API_PROFILES = {"verifier", "scout"}


class SlotSemanticsTests(unittest.TestCase):
    def test_primary_is_go_and_later_slots_bind_by_family(self):
        routes = compile_profile_routes("claw", load_source())

        self.assertEqual([route.adapter for route in routes], EXPECTED_ADAPTERS["claw"])
        self.assertEqual([route.model for route in routes], EXPECTED_MODELS["claw"])

    def test_entrega_inherits_main(self):
        source = load_source()

        self.assertEqual(
            compile_profile_routes("entrega", source),
            compile_profile_routes("claw", source),
        )

    def test_duplicate_labels_are_distinct_attempts(self):
        source = {
            "chains": {
                "implementer": [
                    "opencode-go/muse-spark-1.3-contributor",
                    "meta/muse-spark-1.3 (max)",
                    "zai/glm-5.3",
                    "zai/glm-5.3",
                    "zai/glm-5.3",
                    "xai/grok-4.6",
                    "anthropic/claude-opus-5 (xhigh)",
                ]
            }
        }
        routes = compile_profile_routes("implementer", source)

        self.assertEqual(len(routes), SLOT_COUNT)
        self.assertEqual(
            [route.adapter for route in routes[2:5]],
            ["zai", "zai", "zai"],
        )
        self.assertEqual({route.model for route in routes[2:5]}, {"glm-5.3"})
        self.assertEqual([route.slot for route in routes[2:5]], [2, 3, 4])

    def test_kimi_for_coding_binds_to_kimi_and_high_stays_on_the_label(self):
        # kimi-for-coding is a Kimi family id, not k3; (high) is provenance.
        source = {
            "chains": {
                "scout": [
                    "opencode-go/deepseek-v4.1-flash",
                    "kimi/kimi-for-coding (high)",
                    "kimi/kimi-for-coding",
                    "zai/glm-5.3-flash",
                    "xai/grok-4.6",
                    "anthropic/claude-sonnet-5",
                    "openai/gpt-6-astra",
                ]
            }
        }
        routes = compile_profile_routes("scout", source)

        self.assertEqual(routes[1].adapter, "kimi")
        self.assertEqual(routes[1].model, "kimi-for-coding")
        self.assertEqual(routes[1].label, "kimi/kimi-for-coding (high)")
        self.assertEqual(routes[2].adapter, "kimi")
        self.assertEqual(routes[2].model, "kimi-for-coding")
        self.assertEqual(routes[2].label, "kimi/kimi-for-coding")
        self.assertEqual(routes[3].adapter, "zai")
        self.assertEqual(routes[3].model, "glm-5.3-flash")

    def test_parenthetical_annotations_stay_on_the_label_not_the_model_id(self):
        source = load_source()
        claw = compile_profile_routes("claw", source)
        muse = claw[2]
        opus = claw[1]
        sol = claw[5]

        self.assertEqual(muse.adapter, "muse")
        self.assertEqual(muse.model, "muse-spark-1.3")
        self.assertEqual(muse.label, "meta/muse-spark-1.3 (max)")
        self.assertEqual(opus.adapter, "claude")
        self.assertEqual(opus.model, "claude-opus-5")
        self.assertEqual(opus.label, "anthropic/claude-opus-5 (xhigh)")
        self.assertEqual(sol.adapter, "codex")
        self.assertEqual(sol.model, "gpt-5.6-sol")
        self.assertEqual(sol.label, "openai/gpt-5.6-sol (xhigh)")

        scout = compile_profile_routes("scout", source)
        self.assertEqual(scout[5].model, "claude-sonnet-5")
        self.assertEqual(scout[5].label, "anthropic/claude-sonnet-5 (xhigh)")

        operaciones = compile_profile_routes("operaciones", source)
        self.assertEqual(operaciones[5].model, "kimi-for-coding")
        self.assertEqual(operaciones[5].label, "kimi/kimi-for-coding (high)")

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
        self.assertEqual(
            source["chains"]["main"][0],
            "opencode-go-resp/muse-spark-1.3-contributor",
        )
        self.assertEqual(source["chains"]["main"][2], "meta/muse-spark-1.3 (max)")
        self.assertEqual(
            source["chains"]["ingenieria"][1], "openai/gpt-5.6-sol (xhigh)"
        )
        self.assertEqual(source["chains"]["main"][1], "anthropic/claude-opus-5 (xhigh)")

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
        self.assertEqual(routes[3].adapter, "codex")
        self.assertEqual(routes[3].model, "gpt-6-astra")

        scout = compile_profile_routes("scout", load_source())
        self.assertEqual(scout[4].adapter, "deepseek")
        self.assertEqual(scout[4].model, "deepseek-flash")
        self.assertEqual(scout[4].billing_mode, "api")
        self.assertEqual(scout[6].adapter, "codex")
        self.assertEqual(scout[6].billing_mode, "subscription")

        adversary = compile_profile_routes("adversary", load_source())
        self.assertEqual(adversary[6].adapter, "codex")
        self.assertEqual(adversary[6].model, "gpt-5.6-sol")
        self.assertEqual(adversary[6].billing_mode, "subscription")

        verifier = compile_profile_routes("verifier", load_source())
        self.assertEqual(verifier[6].adapter, "deepseek")
        self.assertEqual(verifier[6].model, "deepseek-flash")
        self.assertEqual(verifier[6].billing_mode, "api")

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
