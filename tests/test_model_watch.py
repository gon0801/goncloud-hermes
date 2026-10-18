import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import model_watch as module  # noqa: E402

TABLE = {
    "chains": {
        "dirigir-claw": [
            "anthropic/claude-opus-5 (medium)",
            "anthropic/claude-sonnet-5",
            "zai/glm-5.3",
            "kimi/k3",
        ],
        "operativo": [
            "anthropic/claude-sonnet-5 (medium)",
            "zai/glm-5.3 (low)",
            "xai/grok-4.7 (low)",
            "opencode-go/deepseek-v4.1-flash (off)",
        ],
        "simples": [
            "anthropic/claude-haiku-5-5 (low)",
            "opencode-go/deepseek-v4.1-flash (off)",
            "zai/glm-5.3-flash (low)",
            "xai/grok-4.7 (low)",
        ],
    }
}


def view(
    meta=(),
    go=(),
    anthropic=("claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5"),
    zai=("glm-5.3", "glm-5.3-flash"),
    xai=("grok-4.7",),
    kimi=("k3",),
):
    return {
        "meta": {model: {"release_date": "2026-09-02"} for model in meta},
        "anthropic": {model: {} for model in anthropic},
        "opencode-go": {model: {} for model in go},
        "zai-coding-plan": {model: {} for model in zai},
        "xai": {model: {} for model in xai},
        "kimi-code-plan-global": {model: {} for model in kimi},
    }


BASE = view(
    meta=("muse-spark-1.3", "muse-spark-1.3-contributor"),
    go=("deepseek-v4.1-flash",),
)


class InUseTests(unittest.TestCase):
    def test_models_in_use_keep_where_they_are_used_and_on_which_route(self):
        in_use = module.models_in_use(TABLE)

        # OpenCode Go only carries DeepSeek Flash in the v6 table; the label's
        # provider decides the route, never the position.
        self.assertEqual(
            in_use["deepseek-v4.1-flash"],
            {"go": ["operativo:3", "simples:1"], "vendor": []},
        )
        self.assertEqual(
            in_use["claude-opus-5"], {"go": [], "vendor": ["dirigir-claw:0"]}
        )
        self.assertEqual(
            in_use["claude-sonnet-5"],
            {"go": [], "vendor": ["dirigir-claw:1", "operativo:0"]},
        )
        self.assertNotIn("claude-opus-5 (medium)", in_use)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.in_use = module.models_in_use(TABLE)

    def test_the_first_run_only_takes_a_baseline(self):
        self.assertEqual(module.compare(None, BASE, self.in_use), [])

    def test_a_new_model_of_a_family_in_use_is_announced(self):
        now = view(
            meta=BASE["meta"],
            go=BASE["opencode-go"],
            zai=("glm-5.3", "glm-5.3-flash", "glm-5.4"),
        )
        now["zai-coding-plan"]["glm-5.4"] = {
            "release_date": "2026-10-12",
            "context": 1048576,
        }

        self.assertEqual(
            module.compare(BASE, now, self.in_use),
            [
                {
                    "kind": "new",
                    "source": "zai-coding-plan",
                    "id": "glm-5.4",
                    "release_date": "2026-10-12",
                    "context": 1048576,
                }
            ],
        )

    def test_a_new_model_of_a_family_nobody_uses_is_not_news(self):
        now = view(meta=BASE["meta"], go=(*BASE["opencode-go"], "qwen3.9-max"))

        self.assertEqual(module.compare(BASE, now, self.in_use), [])

    def test_a_family_in_use_arriving_on_opencode_go_is_announced(self):
        now = view(meta=BASE["meta"], go=(*BASE["opencode-go"], "deepseek-v4.2-flash"))

        self.assertEqual(
            module.compare(BASE, now, self.in_use),
            [
                {
                    "kind": "new",
                    "source": "opencode-go",
                    "id": "deepseek-v4.2-flash",
                }
            ],
        )

    def test_a_model_in_use_that_leaves_its_route_is_announced_once(self):
        go = tuple(m for m in BASE["opencode-go"] if m != "deepseek-v4.1-flash")
        now = view(meta=BASE["meta"], go=go)

        notices = module.compare(BASE, now, self.in_use)

        self.assertEqual(
            notices,
            [
                {
                    "kind": "gone",
                    "source": "opencode-go",
                    "id": "deepseek-v4.1-flash",
                    "uses": ["operativo:3", "simples:1"],
                }
            ],
        )
        self.assertEqual(module.compare(now, now, self.in_use), [])

    def test_a_vendor_model_is_checked_at_its_vendor_not_at_go(self):
        # k3 runs on Kimi directly, so losing it on OpenCode Go means nothing
        # and losing it at Kimi does; DeepSeek Flash is the reverse.
        now = view(
            meta=BASE["meta"],
            go=BASE["opencode-go"],
            kimi=(),
        )

        self.assertEqual(
            module.compare(BASE, now, self.in_use),
            [
                {
                    "kind": "gone",
                    "source": "kimi-code-plan-global",
                    "id": "k3",
                    "uses": ["dirigir-claw:3"],
                }
            ],
        )

    def test_a_source_that_could_not_be_read_is_skipped_and_kept(self):
        now = {k: v for k, v in BASE.items() if k != "opencode-go"}

        self.assertEqual(module.compare(BASE, now, self.in_use), [])
        self.assertEqual(module.merge_state(BASE, now), BASE)

    def test_replaying_the_last_three_muse_releases(self):
        # Real history (models.dev, 2026): Spark 1.2 on Meta 08-05 and on Go
        # 08-19; Spark 1.3 on both 09-02; Wonder on Meta 10-12, not on Go yet.
        table = {
            "chains": {
                "main": [
                    "opencode-go/muse-spark-1.1-contributor",
                    "meta/muse-spark-1.1",
                    "zai/glm-5.3",
                    "xai/grok-4.7",
                ]
            }
        }
        in_use = module.models_in_use(table)
        steps = [
            view(meta=("muse-spark-1.1",), go=("muse-spark-1.1-contributor",)),
            view(
                meta=("muse-spark-1.1", "muse-spark-1.2"),
                go=("muse-spark-1.1-contributor",),
            ),
            view(
                meta=("muse-spark-1.1", "muse-spark-1.2"),
                go=("muse-spark-1.1-contributor", "muse-spark-1.2-contributor"),
            ),
            view(
                meta=("muse-spark-1.1", "muse-spark-1.2", "muse-spark-1.3"),
                go=(
                    "muse-spark-1.1-contributor",
                    "muse-spark-1.2-contributor",
                    "muse-spark-1.3-contributor",
                ),
            ),
        ]
        steps.append(
            view(
                meta=(*steps[-1]["meta"], "muse-wonder-1.0"),
                go=tuple(steps[-1]["opencode-go"]),
            )
        )

        announced, state = [], None
        for step in steps:
            announced.append(
                [(n["source"], n["id"]) for n in module.compare(state, step, in_use)]
            )
            state = module.merge_state(state, step)

        self.assertEqual(
            announced,
            [
                [],
                [("meta", "muse-spark-1.2")],
                [("opencode-go", "muse-spark-1.2-contributor")],
                [
                    ("meta", "muse-spark-1.3"),
                    ("opencode-go", "muse-spark-1.3-contributor"),
                ],
                [("meta", "muse-wonder-1.0")],
            ],
        )


class CliTests(unittest.TestCase):
    def test_a_cli_behind_its_channel_is_announced_once_per_version(self):
        installed, latest = {"muse": "1.4.0-R4302.1"}, {"muse": "1.4.6-R6000.1"}

        self.assertEqual(
            module.cli_notices(installed, latest, {}),
            [
                {
                    "kind": "cli",
                    "name": "muse",
                    "installed": "1.4.0-R4302.1",
                    "latest": "1.4.6-R6000.1",
                }
            ],
        )
        self.assertEqual(
            module.cli_notices(installed, latest, {"muse": "1.4.6-R6000.1"}), []
        )
        self.assertEqual(module.cli_notices(latest, latest, {}), [])
        self.assertEqual(module.cli_notices({}, latest, {}), [])


class BlindSourceTests(unittest.TestCase):
    def test_a_source_unreadable_three_runs_in_a_row_is_announced_once(self):
        # One failed download is noise; a watcher that has been blind for
        # days and says nothing is the failure this guards against.
        failures, said = {}, []
        for _ in range(5):
            notices, failures = module.blind_notices(
                failures, {"opencode-go": False, "models.dev": True}
            )
            said.append([n["source"] for n in notices])

        self.assertEqual(said, [[], [], ["opencode-go"], [], []])
        self.assertEqual(failures, {"opencode-go": 5})

        notices, failures = module.blind_notices(
            failures, {"opencode-go": True, "models.dev": True}
        )
        self.assertEqual((notices, failures), ([], {}))


class RenderTests(unittest.TestCase):
    def test_nothing_to_say_prints_nothing(self):
        self.assertEqual(module.render([], {}, "2026-10-13"), "")

    def test_the_message_names_the_model_the_route_and_who_uses_it(self):
        in_use = module.models_in_use(TABLE)
        text = module.render(
            [
                {
                    "kind": "new",
                    "source": "zai-coding-plan",
                    "id": "glm-5.4",
                    "release_date": "2026-10-12",
                    "context": 1048576,
                },
                {
                    "kind": "gone",
                    "source": "opencode-go",
                    "id": "deepseek-v4.1-flash",
                    "uses": ["operativo:3", "simples:1"],
                },
                {
                    "kind": "cli",
                    "name": "muse",
                    "installed": "1.4.0",
                    "latest": "1.4.6",
                },
            ],
            in_use,
            "2026-10-13",
        )

        self.assertIn("2026-10-13", text)
        self.assertIn(
            "Nuevo en Z.AI: glm-5.4 (salio 2026-10-12, contexto 1048576)", text
        )
        self.assertIn("Hoy usas de esa familia: glm-5.3, glm-5.3-flash", text)
        self.assertIn(
            "Ya no aparece en OpenCode Go: deepseek-v4.1-flash (lo usan operativo:3, simples:1)",
            text,
        )
        self.assertIn("muse atrasado: instalado 1.4.0, disponible 1.4.6", text)
        self.assertNotIn("No se cambio nada", text)


if __name__ == "__main__":
    unittest.main()
