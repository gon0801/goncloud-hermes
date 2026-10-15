import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import model_watch as module  # noqa: E402

TABLE = {
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
    }
}


def view(meta=(), go=(), anthropic=("claude-opus-5",)):
    return {
        "meta": {model: {"release_date": "2026-09-02"} for model in meta},
        "anthropic": {model: {} for model in anthropic},
        "opencode-go": {model: {} for model in go},
    }


BASE = view(
    meta=("muse-spark-1.3", "muse-spark-1.3-contributor"),
    go=("muse-spark-1.3-contributor", "deepseek-v4.1-flash", "qwen3.8-max"),
)


class InUseTests(unittest.TestCase):
    def test_models_in_use_keep_where_they_are_used(self):
        in_use = module.models_in_use(TABLE)

        self.assertEqual(in_use["muse-spark-1.3-contributor"], ["main:0", "main:1"])
        self.assertEqual(in_use["muse-spark-1.3"], ["main:3", "scout:2"])
        self.assertEqual(in_use["claude-opus-5"], ["main:2"])
        self.assertNotIn("claude-opus-5 (xhigh)", in_use)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.in_use = module.models_in_use(TABLE)

    def test_the_first_run_only_takes_a_baseline(self):
        self.assertEqual(module.compare(None, BASE, self.in_use), [])

    def test_a_new_model_of_a_family_in_use_is_announced(self):
        now = view(
            meta=("muse-spark-1.3", "muse-spark-1.3-contributor", "muse-wonder-1.0"),
            go=BASE["opencode-go"],
        )
        now["meta"]["muse-wonder-1.0"] = {
            "release_date": "2026-10-12",
            "context": 1048576,
        }

        self.assertEqual(
            module.compare(BASE, now, self.in_use),
            [
                {
                    "kind": "new",
                    "source": "meta",
                    "id": "muse-wonder-1.0",
                    "release_date": "2026-10-12",
                    "context": 1048576,
                }
            ],
        )

    def test_a_new_model_of_a_family_nobody_uses_is_not_news(self):
        now = view(meta=BASE["meta"], go=(*BASE["opencode-go"], "qwen3.9-max"))

        self.assertEqual(module.compare(BASE, now, self.in_use), [])

    def test_a_family_in_use_arriving_on_opencode_go_is_announced(self):
        now = view(
            meta=BASE["meta"], go=(*BASE["opencode-go"], "muse-wonder-1.0-contributor")
        )

        self.assertEqual(
            module.compare(BASE, now, self.in_use),
            [
                {
                    "kind": "new",
                    "source": "opencode-go",
                    "id": "muse-wonder-1.0-contributor",
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
                    "uses": ["scout:0", "scout:1"],
                }
            ],
        )
        self.assertEqual(module.compare(now, now, self.in_use), [])

    def test_a_direct_model_is_checked_at_its_vendor_not_at_go(self):
        # muse-spark-1.3 runs on Meta directly (position 2+), so losing it on
        # OpenCode Go means nothing and losing it at Meta does.
        now = view(meta=("muse-spark-1.3-contributor",), go=BASE["opencode-go"])

        self.assertEqual(
            module.compare(BASE, now, self.in_use),
            [
                {
                    "kind": "gone",
                    "source": "meta",
                    "id": "muse-spark-1.3",
                    "uses": ["main:3", "scout:2"],
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
            "chains": {"main": ["go/muse-spark-1.1-contributor", "meta/muse-spark-1.1"]}
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
                    "source": "meta",
                    "id": "muse-wonder-1.0",
                    "release_date": "2026-10-12",
                    "context": 1048576,
                },
                {
                    "kind": "new",
                    "source": "opencode-go",
                    "id": "muse-wonder-1.0-contributor",
                },
                {
                    "kind": "gone",
                    "source": "opencode-go",
                    "id": "deepseek-v4.1-flash",
                    "uses": ["scout:0", "scout:1"],
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
            "Nuevo en Meta: muse-wonder-1.0 (salio 2026-10-12, contexto 1048576)", text
        )
        self.assertIn(
            "Hoy usas de esa familia: muse-spark-1.3, muse-spark-1.3-contributor", text
        )
        self.assertIn("Nuevo en OpenCode Go: muse-wonder-1.0-contributor", text)
        self.assertIn(
            "Ya no aparece en OpenCode Go: deepseek-v4.1-flash (lo usan scout:0, scout:1)",
            text,
        )
        self.assertIn("muse atrasado: instalado 1.4.0, disponible 1.4.6", text)
        self.assertNotIn("No se cambio nada", text)


if __name__ == "__main__":
    unittest.main()
