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
        "opencode_go_2",
        "claude",
        "muse",
        "grok",
        "opencode_zai",
        "codex",
        "kimi",
    ],
    "operaciones": [
        "opencode_go",
        "opencode_go_2",
        "opencode_zai",
        "codex",
        "claude",
        "grok",
        "kimi",
        "muse",
    ],
    "ingenieria": [
        "opencode_go",
        "opencode_go_2",
        "codex",
        "kimi",
        "opencode_zai",
        "muse",
        "claude",
        "grok",
    ],
    "implementer": [
        "opencode_go",
        "opencode_go_2",
        "muse",
        "opencode_zai",
        "grok",
        "claude",
        "codex",
        "kimi",
    ],
    "reviewer": [
        "opencode_go",
        "opencode_go_2",
        "claude",
        "kimi",
        "codex",
        "muse",
        "grok",
        "opencode_zai",
    ],
    "adversary": [
        "opencode_go",
        "opencode_go_2",
        "grok",
        "kimi",
        "opencode_zai",
        "muse",
        "claude",
        "codex",
    ],
    "verifier": [
        "opencode_go",
        "opencode_go_2",
        "opencode_zai",
        "muse",
        "kimi",
        "claude",
        "codex",
        "deepseek",
    ],
    "scout": [
        "opencode_go",
        "opencode_go_2",
        "opencode_zai",
        "muse",
        "kimi",
        "deepseek",
        "claude",
        "codex",
    ],
}
EXPECTED_ADAPTERS["entrega"] = EXPECTED_ADAPTERS["claw"]

EXPECTED_EFFORTS = {
    "claw": [None, None, "xhigh", "max", None, None, "xhigh", None],
    "operaciones": [None, None, None, "xhigh", "xhigh", None, "high", "max"],
    "ingenieria": [None, None, "xhigh", None, None, "max", "xhigh", None],
    "implementer": [None, None, "max", None, None, "xhigh", "xhigh", "high"],
    "reviewer": [None, None, None, None, None, "max", None, None],
    "adversary": [None, None, None, None, None, "max", "xhigh", "xhigh"],
    "verifier": [None, None, None, "max", "high", "xhigh", "xhigh", None],
    "scout": [None, None, None, "max", None, None, "xhigh", "xhigh"],
}
EXPECTED_EFFORTS["entrega"] = EXPECTED_EFFORTS["claw"]

EXPECTED_MODELS = {
    "claw": [
        "muse-spark-1.3-contributor",
        "muse-spark-1.3-contributor",
        "claude-opus-5",
        "muse-wonder-1.0",
        "grok-4.6",
        "glm-5.3",
        "gpt-5.6-sol",
        "k3",
    ],
    "operaciones": [
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash",
        "glm-5.3-flash",
        "gpt-5.6-terra",
        "claude-sonnet-5",
        "grok-4.6",
        "kimi-for-coding",
        "muse-wonder-1.0",
    ],
    "ingenieria": [
        "muse-spark-1.3-contributor",
        "muse-spark-1.3-contributor",
        "gpt-5.6-sol",
        "k3",
        "glm-5.3",
        "muse-wonder-1.0",
        "claude-opus-5",
        "grok-4.6",
    ],
    "implementer": [
        "muse-spark-1.3-contributor",
        "muse-spark-1.3-contributor",
        "muse-wonder-1.0",
        "glm-5.3-flash",
        "grok-4.6",
        "claude-sonnet-5",
        "gpt-5.6-terra",
        "kimi-for-coding",
    ],
    "reviewer": [
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash",
        "claude-fable-5-1",
        "k3",
        "gpt-6-astra",
        "muse-wonder-1.0",
        "grok-4.6",
        "glm-5.3",
    ],
    "adversary": [
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash",
        "grok-4.6",
        "k3",
        "glm-5.3",
        "muse-wonder-1.0",
        "claude-opus-5",
        "gpt-5.6-sol",
    ],
    "verifier": [
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash",
        "glm-5.3-flash",
        "muse-wonder-1.0",
        "kimi-for-coding",
        "claude-sonnet-5",
        "gpt-5.6-terra",
        "deepseek-flash",
    ],
    "scout": [
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash",
        "glm-5.3-flash",
        "muse-wonder-1.0",
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

    def test_the_second_go_account_copies_primary_in_every_chain(self):
        # Owner, 2026-09-27: position 2 is an exact copy of Primary on the
        # second OpenCode Go account, for main and for every agent.
        source = load_source()
        for chain_name, chain in source["chains"].items():
            with self.subTest(chain=chain_name):
                self.assertEqual(chain[1], chain[0])
        for profile in EXPECTED_ADAPTERS:
            with self.subTest(profile=profile):
                routes = compile_profile_routes(profile, source)
                self.assertEqual(routes[0].adapter, "opencode_go")
                self.assertEqual(routes[1].adapter, "opencode_go_2")
                self.assertEqual(
                    (routes[1].model, routes[1].label, routes[1].reasoning_effort),
                    (routes[0].model, routes[0].label, routes[0].reasoning_effort),
                )

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
                    "opencode-go/muse-spark-1.3-contributor",
                    "meta/muse-wonder-1.0 (max)",
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
            [route.adapter for route in routes[3:6]],
            ["opencode_zai", "opencode_zai", "opencode_zai"],
        )
        self.assertEqual({route.model for route in routes[3:6]}, {"glm-5.3"})
        self.assertEqual([route.slot for route in routes[3:6]], [3, 4, 5])

    def test_kimi_for_coding_binds_to_kimi_and_high_stays_on_the_label(self):
        # kimi-for-coding is a Kimi family id, not k3; (high) is provenance.
        source = {
            "chains": {
                "scout": [
                    "opencode-go/deepseek-v4.1-flash",
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

        self.assertEqual(routes[2].adapter, "kimi")
        self.assertEqual(routes[2].model, "kimi-for-coding")
        self.assertEqual(routes[2].label, "kimi/kimi-for-coding (high)")
        self.assertEqual(routes[3].adapter, "kimi")
        self.assertEqual(routes[3].model, "kimi-for-coding")
        self.assertEqual(routes[3].label, "kimi/kimi-for-coding")
        self.assertEqual(routes[4].adapter, "opencode_zai")
        self.assertEqual(routes[4].model, "glm-5.3-flash")

    def test_parenthetical_annotations_stay_on_the_label_not_the_model_id(self):
        source = load_source()
        claw = compile_profile_routes("claw", source)
        muse = claw[3]
        opus = claw[2]
        sol = claw[6]

        self.assertEqual(muse.adapter, "muse")
        self.assertEqual(muse.model, "muse-wonder-1.0")
        self.assertEqual(muse.label, "meta/muse-wonder-1.0 (max)")
        self.assertEqual(opus.adapter, "claude")
        self.assertEqual(opus.model, "claude-opus-5")
        self.assertEqual(opus.label, "anthropic/claude-opus-5 (xhigh)")
        self.assertEqual(sol.adapter, "codex")
        self.assertEqual(sol.model, "gpt-5.6-sol")
        self.assertEqual(sol.label, "openai/gpt-5.6-sol (xhigh)")

        scout = compile_profile_routes("scout", source)
        self.assertEqual(scout[6].model, "claude-sonnet-5")
        self.assertEqual(scout[6].label, "anthropic/claude-sonnet-5 (xhigh)")

        operaciones = compile_profile_routes("operaciones", source)
        self.assertEqual(operaciones[6].model, "kimi-for-coding")
        self.assertEqual(operaciones[6].label, "kimi/kimi-for-coding (high)")

    def test_every_table_cell_preserves_its_literal_reasoning_effort(self):
        source = load_source()

        for profile, efforts in EXPECTED_EFFORTS.items():
            with self.subTest(profile=profile):
                routes = compile_profile_routes(profile, source)
                self.assertEqual(
                    [
                        (route.adapter, route.model, route.reasoning_effort)
                        for route in routes
                    ],
                    list(
                        zip(
                            EXPECTED_ADAPTERS[profile],
                            EXPECTED_MODELS[
                                "claw" if profile == "entrega" else profile
                            ],
                            efforts,
                            strict=True,
                        )
                    ),
                )

        main = compile_profile_routes("claw", source)
        self.assertEqual(
            (main[2].adapter, main[2].model, main[2].reasoning_effort),
            ("claude", "claude-opus-5", "xhigh"),
        )
        self.assertEqual(
            (main[3].adapter, main[3].model, main[3].reasoning_effort),
            ("muse", "muse-wonder-1.0", "max"),
        )
        self.assertEqual(
            (main[7].adapter, main[7].model, main[7].reasoning_effort),
            ("kimi", "k3", None),
        )

    def test_unknown_parenthetical_annotation_is_rejected(self):
        source = load_source()
        source["chains"]["main"][2] = "anthropic/claude-opus-5 (maximum)"

        with self.assertRaisesRegex(ValueError, "unknown reasoning effort"):
            compile_profile_routes("claw", source)

    def test_the_zen_free_tier_name_is_not_sent_to_the_muse_cli(self):
        # "-free" names OpenCode Zen's free tier, where it is a real model id.
        # The Muse subscription serves the same family without that suffix and
        # answers "model does not exist" when it is sent, so the suffix is
        # provenance on the label exactly like " dup", never part of the id.
        source = {
            "chains": {
                "scout": [
                    "opencode-go/deepseek-v4.1-flash",
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

        muse = compile_profile_routes("scout", source)[2]
        self.assertEqual(muse.adapter, "muse")
        self.assertEqual(muse.model, "muse-spark-1.3-contributor")
        self.assertEqual(muse.label, "meta/muse-spark-1.3-contributor-free")

    def test_the_source_table_itself_is_never_rewritten(self):
        source = load_source()

        compile_profile_routes("claw", source)
        compile_profile_routes("ingenieria", source)

        self.assertEqual(
            source["columns"],
            ["Primary", "Primary Go 2", "F1", "F2", "F3", "F4", "F5", "F6"],
        )
        self.assertEqual(
            source["chains"]["main"][0],
            "opencode-go-resp/muse-spark-1.3-contributor",
        )
        self.assertEqual(source["chains"]["main"][3], "meta/muse-wonder-1.0 (max)")
        self.assertEqual(
            source["chains"]["ingenieria"][2], "openai/gpt-5.6-sol (xhigh)"
        )
        self.assertEqual(source["chains"]["main"][2], "anthropic/claude-opus-5 (xhigh)")

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

    def test_glm_routes_bind_to_a_tool_capable_adapter(self):
        """Every stage works on a repository, so its model needs hands.

        The Z.AI Coding Plan reached over plain HTTP gives the model no file or
        shell tools, so the most it can honestly do is report that it could not
        look, which is exactly what happened on a real scout stage. The same
        subscription reached through the OpenCode CLI gets the same toolset as
        every other slot, and still bills as a subscription rather than an API.
        """
        source = load_source()
        seen = 0
        for profile in EXPECTED_ADAPTERS:
            for route in compile_profile_routes(profile, source):
                if route.model.startswith("glm-"):
                    seen += 1
                    self.assertEqual(
                        route.adapter, "opencode_zai", f"{profile} {route}"
                    )
                    self.assertEqual(route.billing_mode, "subscription", profile)
        self.assertGreater(seen, 0, "the owner table should route some glm slot")

    def test_only_deepseek_family_slots_are_api_billed(self):
        routes = compile_profile_routes("reviewer", load_source())

        self.assertEqual(routes[0].adapter, "opencode_go")
        self.assertEqual(routes[0].model, "deepseek-v4.1-flash")
        self.assertEqual(routes[0].billing_mode, "subscription")
        self.assertEqual(routes[2].adapter, "claude")
        self.assertEqual(routes[2].model, "claude-fable-5-1")
        self.assertEqual(routes[4].adapter, "codex")
        self.assertEqual(routes[4].model, "gpt-6-astra")

        scout = compile_profile_routes("scout", load_source())
        self.assertEqual(scout[5].adapter, "deepseek")
        self.assertEqual(scout[5].model, "deepseek-flash")
        self.assertEqual(scout[5].billing_mode, "api")
        self.assertEqual(scout[7].adapter, "codex")
        self.assertEqual(scout[7].billing_mode, "subscription")

        adversary = compile_profile_routes("adversary", load_source())
        self.assertEqual(adversary[7].adapter, "codex")
        self.assertEqual(adversary[7].model, "gpt-5.6-sol")
        self.assertEqual(adversary[7].billing_mode, "subscription")

        verifier = compile_profile_routes("verifier", load_source())
        self.assertEqual(verifier[7].adapter, "deepseek")
        self.assertEqual(verifier[7].model, "deepseek-flash")
        self.assertEqual(verifier[7].billing_mode, "api")

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
        with self.assertRaisesRegex(ValueError, "exactly 8 slots"):
            compile_profile_routes(
                "scout",
                {
                    **source,
                    "chains": {**source["chains"], "scout": ["zai/glm-5.3"]},
                },
            )


if __name__ == "__main__":
    unittest.main()
