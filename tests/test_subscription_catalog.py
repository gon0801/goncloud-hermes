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
    # The live table moves on its own to newer releases (hermes_autoupdate), so
    # tests that name models read a frozen copy; the live file keeps only the
    # invariants checked in LiveTableTests.
    return json.loads(
        (ROOT / "tests/fixtures/model-routing.source.json").read_text(encoding="utf-8")
    )


def load_live_source() -> dict:
    return json.loads((ROOT / "model-routing.source.json").read_text(encoding="utf-8"))


#: Agent -> lane for the frozen fixture and the live table.
PROFILE_LANE = {
    "claw": "dirigir-claw",
    "entrega": "dirigir",
    "ingenieria": "planear",
    "reviewer": "planear",
    "implementer": "codigo",
    "verifier": "verificar",
    "scout": "investigar",
    "adversary": "adversary",
    "operaciones": "operativo",
    "amazon": "operativo",
    "meli": "operativo",
    "odoo": "operativo",
    "pers": "operativo",
    "arras": "operativo",
    "mail": "simples",
    "limpia": "simples",
    "gonserver": "simples",
    "skillverif": "simples",
    "tinkabot": "simples",
    "eggbot": "simples",
    "default": "simples",
}

#: adapter, model and reasoning effort of every v6 lane, in order.
LANES = {
    "dirigir": [
        ("claude", "claude-opus-5-5", "high"),
        ("claude", "claude-sonnet-5-5", "high"),
        ("opencode_zai", "glm-5.3", "high"),
        ("kimi", "k3", "high"),
    ],
    "dirigir-claw": [
        ("claude", "claude-opus-5-5", "medium"),
        ("claude", "claude-sonnet-5-5", "high"),
        ("opencode_zai", "glm-5.3", "high"),
        ("kimi", "k3", "high"),
    ],
    "planear": [
        ("claude", "claude-opus-5-5", "high"),
        ("kimi", "k3", "high"),
        ("opencode_zai", "glm-5.3", "high"),
        ("grok", "grok-4.7", "high"),
    ],
    "codigo": [
        ("claude", "claude-sonnet-5-5", "high"),
        ("kimi", "k3", "high"),
        ("opencode_zai", "glm-5.3", "high"),
        ("grok", "grok-4.7", "medium"),
    ],
    "verificar": [
        ("opencode_zai", "glm-5.3", "high"),
        ("grok", "grok-4.7", "high"),
        ("claude", "claude-sonnet-5-5", "high"),
        ("kimi", "k3", "high"),
    ],
    "investigar": [
        ("grok", "grok-4.7", "medium"),
        ("kimi", "k3", "high"),
        ("opencode_zai", "glm-5.3", "high"),
        ("claude", "claude-sonnet-5-5", "medium"),
    ],
    "operativo": [
        ("claude", "claude-sonnet-5-5", "medium"),
        ("opencode_zai", "glm-5.3", "low"),
        ("grok", "grok-4.7", "low"),
        ("opencode_go", "deepseek-v4.1-flash", None),
    ],
    "simples": [
        ("claude", "claude-haiku-5-5", "low"),
        ("opencode_go", "deepseek-v4.1-flash", None),
        ("opencode_zai", "glm-5.3-flash", "low"),
        ("grok", "grok-4.7", "low"),
    ],
    "adversary": [
        ("claude", "claude-opus-5-5", "high"),
        ("grok", "grok-4.7", "high"),
        ("kimi", "k3", "high"),
        ("opencode_zai", "glm-5.3", "high"),
    ],
}


def compiled(profile):
    source = load_source()
    lane = LANES[PROFILE_LANE[profile]]
    routes = compile_profile_routes(profile, source)
    assert [
        (route.adapter, route.model, route.reasoning_effort) for route in routes
    ] == lane, profile
    return routes


#: The frozen fixture keeps v6 slot semantics; the live table includes v8 backups.
LIVE_LANES = {
    **LANES,
    "dirigir": [
        ("claude", "claude-opus-5-5", "high"),
        ("claude", "claude-sonnet-5-5", "high"),
        ("codex", "gpt-6.1-sol", "medium"),
        ("kimi", "k3", "high"),
    ],
    "dirigir-claw": [
        ("claude", "claude-opus-5-5", "medium"),
        ("claude", "claude-sonnet-5-5", "high"),
        ("codex", "gpt-6.1-sol", "medium"),
        ("kimi", "k3", "high"),
    ],
    "planear": [*LANES["planear"], ("codex", "gpt-6-astra", "xhigh")],
    "codigo": [*LANES["codigo"], ("codex", "gpt-6.1-sol", "xhigh")],
}


class LiveTableTests(unittest.TestCase):
    def test_the_live_table_compiles_every_configured_position(self):
        source = load_live_source()

        for chain_name, chain in source["chains"].items():
            with self.subTest(chain=chain_name):
                self.assertEqual(
                    [
                        (route.adapter, route.model)
                        for route in compile_profile_routes(chain_name, source)
                    ],
                    [(adapter, model) for adapter, model, _ in LIVE_LANES[chain_name]],
                )
        for profile, lane in PROFILE_LANE.items():
            with self.subTest(profile=profile):
                routes = compile_profile_routes(profile, source)
                self.assertEqual(len(routes), len(source["chains"][lane]))
                self.assertTrue(
                    all(route.billing_mode == "subscription" for route in routes),
                    profile,
                )


class SlotSemanticsTests(unittest.TestCase):
    def test_primary_binds_by_family_like_every_other_slot(self):
        # Owner, 2026-10-17: no position is forced to an account; Anthropic on
        # his Claude subscription carries most primaries.
        routes = compile_profile_routes("claw", load_source())

        self.assertEqual(
            [route.adapter for route in routes],
            [row[0] for row in LANES["dirigir-claw"]],
        )
        self.assertEqual(
            [route.model for route in routes], [row[1] for row in LANES["dirigir-claw"]]
        )

    def test_claw_takes_medium_on_the_primary_and_entrega_high(self):
        claw = compile_profile_routes("claw", load_source())
        entrega = compile_profile_routes("entrega", load_source())

        self.assertEqual(claw[0].reasoning_effort, "medium")
        self.assertEqual(entrega[0].reasoning_effort, "high")
        self.assertEqual(
            [(r.adapter, r.model) for r in claw],
            [(r.adapter, r.model) for r in entrega],
        )
        self.assertNotEqual(
            [r.reasoning_effort for r in claw], [r.reasoning_effort for r in entrega]
        )

    def test_entrega_inherits_its_lane_and_default_the_simples_lane(self):
        source = load_source()

        self.assertEqual(
            compile_profile_routes("entrega", source),
            compile_profile_routes("dirigir", source),
        )
        self.assertEqual(
            compile_profile_routes("default", source),
            compile_profile_routes("simples", source),
        )

    def test_duplicate_labels_are_distinct_attempts(self):
        source = {
            "profile_inheritance": {"implementer": "codigo"},
            "chains": {
                "codigo": [
                    "zai/glm-5.3",
                    "zai/glm-5.3",
                    "zai/glm-5.3",
                    "xai/grok-4.7 (medium)",
                ]
            },
        }
        routes = compile_profile_routes("implementer", source)

        self.assertEqual(len(routes), len(source["chains"]["codigo"]))
        self.assertEqual(
            [route.adapter for route in routes[:3]],
            ["opencode_zai", "opencode_zai", "opencode_zai"],
        )
        self.assertEqual({route.model for route in routes[:3]}, {"glm-5.3"})
        self.assertEqual([route.slot for route in routes[:3]], [0, 1, 2])

    def test_kimi_for_coding_binds_to_kimi_and_high_stays_on_the_label(self):
        # kimi-for-coding is a Kimi family id, not k3; (high) is provenance.
        source = {
            "profile_inheritance": {"scout": "investigar"},
            "chains": {
                "investigar": [
                    "kimi/kimi-for-coding (high)",
                    "kimi/kimi-for-coding",
                    "zai/glm-5.3-flash",
                    "xai/grok-4.7",
                ]
            },
        }
        routes = compile_profile_routes("scout", source)

        self.assertEqual(routes[0].adapter, "kimi")
        self.assertEqual(routes[0].model, "kimi-for-coding")
        self.assertEqual(routes[0].label, "kimi/kimi-for-coding (high)")
        self.assertEqual(routes[1].adapter, "kimi")
        self.assertEqual(routes[1].model, "kimi-for-coding")
        self.assertEqual(routes[1].label, "kimi/kimi-for-coding")
        self.assertEqual(routes[2].adapter, "opencode_zai")
        self.assertEqual(routes[2].model, "glm-5.3-flash")

    def test_every_kimi_release_binds_to_kimi_and_other_k_names_do_not(self):
        source = {
            "profile_inheritance": {"scout": "investigar"},
            "chains": {
                "investigar": [
                    "kimi/k3",
                    "kimi/k4",
                    "kimi/k3-256k",
                    "kimi/kimi-for-coding",
                ]
            },
        }

        routes = compile_profile_routes("scout", source)

        self.assertEqual(
            [(route.adapter, route.model) for route in routes],
            [
                ("kimi", "k3"),
                ("kimi", "k4"),
                ("kimi", "k3-256k"),
                ("kimi", "kimi-for-coding"),
            ],
        )
        source["chains"]["investigar"][1] = "kling/kling-1"
        with self.assertRaisesRegex(ValueError, "unknown model family: kling-1"):
            compile_profile_routes("scout", source)

    def test_parenthetical_annotations_stay_on_the_label_not_the_model_id(self):
        source = load_source()
        claw = compile_profile_routes("claw", source)

        self.assertEqual(claw[0].label, "anthropic/claude-opus-5-5 (medium)")
        self.assertEqual(claw[0].model, "claude-opus-5-5")
        scout = compile_profile_routes("scout", source)
        self.assertEqual(scout[0].label, "xai/grok-4.7 (medium)")
        self.assertEqual(scout[0].model, "grok-4.7")
        self.assertEqual(scout[3].label, "anthropic/claude-sonnet-5-5 (medium)")

    def test_every_table_cell_preserves_its_literal_reasoning_effort(self):
        for profile, lane in PROFILE_LANE.items():
            with self.subTest(profile=profile):
                routes = compiled(profile)
                self.assertEqual(
                    [(r.model, r.reasoning_effort) for r in routes],
                    [(model, effort) for _, model, effort in LANES[lane]],
                    profile,
                )

    def test_off_marks_a_position_that_sends_no_reasoning_parameter(self):
        source = load_source()

        for profile in ("operaciones", "mail"):
            routes = compile_profile_routes(profile, source)
            deepseek = next(r for r in routes if r.model.startswith("deepseek"))
            self.assertIsNone(deepseek.reasoning_effort, profile)
            self.assertEqual(deepseek.label.endswith("(off)"), True, profile)

        with self.assertRaisesRegex(ValueError, "unknown reasoning effort"):
            compile_profile_routes(
                "operaciones",
                {
                    **source,
                    "chains": {
                        **source["chains"],
                        "operativo": [
                            *source["chains"]["operativo"][:3],
                            "opencode-go/deepseek-v4.1-flash (maximum)",
                        ],
                    },
                },
            )

    def test_unknown_parenthetical_annotation_is_rejected(self):
        source = load_source()
        source["chains"]["dirigir-claw"][0] = "anthropic/claude-opus-5-5 (maximum)"

        with self.assertRaisesRegex(ValueError, "unknown reasoning effort"):
            compile_profile_routes("claw", source)

    def test_the_zen_free_tier_name_is_not_sent_to_the_muse_cli(self):
        # "-free" names OpenCode Zen's free tier, where it is a real model id.
        # The Muse subscription serves the same family without that suffix and
        # answers "model does not exist" when it is sent, so the suffix is
        # provenance on the label exactly like " dup", never part of the id.
        source = {
            "profile_inheritance": {"verifier": "verificar"},
            "chains": {
                "verificar": [
                    "meta/muse-spark-1.3-contributor-free",
                    "kimi/k3",
                    "zai/glm-5.3",
                    "xai/grok-4.7",
                ]
            },
        }

        muse = compile_profile_routes("verifier", source)[0]
        self.assertEqual(muse.adapter, "muse")
        self.assertEqual(muse.model, "muse-spark-1.3-contributor")
        self.assertEqual(muse.label, "meta/muse-spark-1.3-contributor-free")

    def test_the_source_table_itself_is_never_rewritten(self):
        source = load_source()

        compile_profile_routes("claw", source)
        compile_profile_routes("ingenieria", source)

        self.assertEqual(source["columns"], ["Primary", "F1", "F2", "F3"])
        self.assertEqual(
            source["chains"]["dirigir-claw"][0],
            "anthropic/claude-opus-5-5 (medium)",
        )
        self.assertEqual(
            source["chains"]["dirigir"][0], "anthropic/claude-opus-5-5 (high)"
        )

    def test_every_profile_compiles_the_owner_table(self):
        source = load_source()

        for profile, lane in PROFILE_LANE.items():
            with self.subTest(profile=profile):
                routes = compile_profile_routes(profile, source)
                count = len(source["chains"][lane])
                self.assertEqual(len(routes), count, profile)
                self.assertEqual(
                    [route.slot for route in routes], list(range(count))
                )
                self.assertEqual(
                    [(r.adapter, r.model, r.reasoning_effort) for r in routes],
                    LANES[lane],
                    profile,
                )
                self.assertEqual(
                    [route.billing_mode for route in routes],
                    ["subscription"] * count,
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
        for profile, lane in PROFILE_LANE.items():
            for route in compile_profile_routes(profile, source):
                if route.model.startswith("glm-"):
                    seen += 1
                    self.assertEqual(
                        route.adapter, "opencode_zai", f"{profile} {route}"
                    )
                    self.assertEqual(route.billing_mode, "subscription", profile)
        self.assertGreater(seen, 0, "the owner table should route some glm slot")

    def test_deepseek_flash_rides_the_go_subscription_never_the_api(self):
        # Owner, 2026-10-17: the pay-per-token deepseek adapter is deliberately
        # unbound; the only DeepSeek in the table is DeepSeek Flash on Go.
        for profile in ("operaciones", "amazon", "mail", "default"):
            with self.subTest(profile=profile):
                routes = compile_profile_routes(profile, load_source())
                deepseek = [r for r in routes if r.model.startswith("deepseek")]
                self.assertEqual(len(deepseek), 1, profile)
                self.assertEqual(deepseek[0].adapter, "opencode_go", profile)
                self.assertEqual(deepseek[0].billing_mode, "subscription", profile)
        self.assertIsNone(
            next(
                (
                    adapter
                    for family, adapter in (
                        ("muse-", "muse"),
                        ("claude-", "claude"),
                        ("gpt-", "codex"),
                        ("glm-", "opencode_zai"),
                        ("grok-", "grok"),
                    )
                    if adapter == "deepseek"
                ),
                None,
            )
        )

    def test_unknown_family_and_profile_are_rejected(self):
        source = load_source()

        with self.assertRaisesRegex(ValueError, "unknown model family"):
            compile_profile_routes(
                "scout",
                {
                    **source,
                    "chains": {
                        **source["chains"],
                        "investigar": ["zai/glm-5.3"] + ["mystery/model"] * 3,
                    },
                },
            )
        with self.assertRaisesRegex(ValueError, "No model chain"):
            compile_profile_routes("pirate", source)
        with self.assertRaisesRegex(ValueError, "empty model chain"):
            compile_profile_routes(
                "scout",
                {
                    **source,
                    "chains": {**source["chains"], "investigar": []},
                },
            )


if __name__ == "__main__":
    unittest.main()
