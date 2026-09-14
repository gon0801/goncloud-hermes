from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.catalog import compile_profile_routes  # noqa: E402


def load_source() -> dict:
    return json.loads((ROOT / "model-routing.source.json").read_text(encoding="utf-8"))


class SlotSemanticsTests(unittest.TestCase):
    def test_slots_override_historical_prefix_after_f1(self):
        routes = compile_profile_routes("claw", load_source())

        self.assertEqual(
            [route.adapter for route in routes],
            [
                "opencode_go",
                "opencode_zen",
                "claude",
                "kimi",
                "muse",
                "zai",
                "grok",
                "codex",
            ],
        )

    def test_duplicate_slot_and_entrega_inheritance(self):
        source = load_source()

        self.assertEqual(
            compile_profile_routes("entrega", source),
            compile_profile_routes("claw", source),
        )
        verifier = compile_profile_routes("verifier", source)
        self.assertEqual(len(verifier), 8)
        self.assertTrue(verifier[3].label.endswith(" dup"))
        self.assertEqual(verifier[3].model, "muse-spark-1.3-contributor-free")
        self.assertEqual(verifier[3].adapter, "muse")

    def test_every_profile_compiles_eight_subscription_routes(self):
        source = load_source()

        for profile in (
            "claw",
            "operaciones",
            "ingenieria",
            "implementer",
            "reviewer",
            "adversary",
            "verifier",
            "scout",
            "entrega",
        ):
            routes = compile_profile_routes(profile, source)
            self.assertEqual(len(routes), 8, profile)
            self.assertEqual([route.slot for route in routes], list(range(8)))
            self.assertEqual(
                [route.billing_mode for route in routes].count("api"),
                1 if profile == "scout" else 0,
                profile,
            )

    def test_only_deepseek_is_api_billed_and_prefixes_never_bind_slots(self):
        routes = compile_profile_routes("reviewer", load_source())

        # Slot 0/1 carry deepseek models but bill through OpenCode Go/Zen.
        self.assertEqual(routes[0].adapter, "opencode_go")
        self.assertEqual(routes[0].model, "deepseek-v4.1-flash")
        self.assertEqual(routes[0].billing_mode, "subscription")
        self.assertEqual(routes[1].adapter, "opencode_zen")
        self.assertEqual(routes[1].model, "deepseek-v4-flash")
        self.assertEqual(routes[1].billing_mode, "subscription")
        # Historical opencode/ prefix at F2+ binds by family instead.
        self.assertEqual(routes[2].adapter, "claude")
        self.assertEqual(routes[2].model, "claude-fable-5-1")
        self.assertEqual(routes[6].adapter, "codex")
        self.assertEqual(routes[6].model, "gpt-6-astra")

        scout = compile_profile_routes("scout", load_source())
        self.assertEqual(scout[7].adapter, "deepseek")
        self.assertEqual(scout[7].model, "deepseek-flash")
        self.assertEqual(scout[7].billing_mode, "api")

    def test_unknown_family_and_profile_are_rejected(self):
        source = load_source()

        with self.assertRaisesRegex(ValueError, "unknown model family"):
            compile_profile_routes(
                "scout",
                {
                    **source,
                    "chains": {
                        **source["chains"],
                        "scout": ["zai/glm-5.3"] + ["mystery/model"] * 7,
                    },
                },
            )
        with self.assertRaisesRegex(ValueError, "No model chain"):
            compile_profile_routes("pirate", source)
        with self.assertRaisesRegex(ValueError, "eight"):
            compile_profile_routes(
                "scout",
                {
                    **source,
                    "chains": {**source["chains"], "scout": ["zai/glm-5.3"]},
                },
            )


if __name__ == "__main__":
    unittest.main()
