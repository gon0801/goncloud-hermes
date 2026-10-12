import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


module = load("pinned_models")
model_watch = load("model_watch")


class FindTests(unittest.TestCase):
    def test_model_ids_are_found_with_their_line(self):
        text = (
            'model = "gpt-6-astra"\n'
            "ANTHROPIC_MODEL=deepseek-v4-pro\n"
            "model: claude-sonnet-5\n"
            '"model": "zai-coding-plan/glm-5.3-flash",\n'
            "--model kimi-code/k3-256k and muse-spark-1.3-contributor\n"
        )

        self.assertEqual(
            module.find_ids(text),
            [
                (1, "gpt-6-astra"),
                (2, "deepseek-v4-pro"),
                (3, "claude-sonnet-5"),
                (4, "glm-5.3-flash"),
                (5, "k3-256k"),
                (5, "muse-spark-1.3-contributor"),
            ],
        )

    def test_tool_and_file_names_that_look_like_families_are_not_models(self):
        # These all start with a family prefix and are not model IDs.
        text = (
            "claude-code-harness claude-mem claude-cli kimi-claude glm-claude "
            "kimi-code grok-build muse-bin-1.4.6 gpt-reserve deepseek-launcher "
            "k3s claude-kimi.ps1 pk3 ~/.kimi-code/config.toml"
        )

        self.assertEqual(module.find_ids(text), [])

    def test_a_context_suffix_is_not_part_of_the_id(self):
        self.assertEqual(
            module.find_ids('X = "deepseek-flash[1m]"'), [(1, "deepseek-flash")]
        )


class ScanTests(unittest.TestCase):
    def test_scan_groups_every_place_that_pins_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
            (root / "agents").mkdir()
            (root / "agents/x.md").write_text(
                "---\nmodel: claude-sonnet-5\n---\n", encoding="utf-8"
            )
            (root / "agents/y.md").write_text(
                "---\nmodel: claude-sonnet-5\n---\n", encoding="utf-8"
            )

            found = module.scan(
                [str(root / "a.toml"), str(root / "agents/*.md"), str(root / "nope")]
            )

        self.assertEqual(sorted(found), ["claude-sonnet-5", "gpt-6-astra"])
        self.assertEqual(
            [place.rsplit("/", 2)[-2:] for place in found["claude-sonnet-5"]],
            [["agents", "x.md:2"], ["agents", "y.md:2"]],
        )


class ClassifyTests(unittest.TestCase):
    VIEW = {
        "anthropic": {
            "claude-sonnet-5": {},
            "claude-opus-4-6": {"status": "deprecated"},
        },
        "opencode-go": {"deepseek-v4-pro": {}},
    }

    def test_each_pin_is_checked_against_every_catalog(self):
        pins = {
            "claude-sonnet-5": ["a:1"],
            "claude-opus-4-6": ["b:1"],
            "deepseek-v4-pro": ["c:1"],
            "deepseek-v4-flash": ["c:2"],
        }

        self.assertEqual(
            module.classify(pins, self.VIEW),
            {
                "claude-sonnet-5": "ok",
                "claude-opus-4-6": "deprecated",
                "deepseek-v4-pro": "ok",
                "deepseek-v4-flash": "not in the catalogs",
            },
        )


class WatcherPinsTests(unittest.TestCase):
    def test_the_watcher_says_when_a_pinned_model_leaves_the_catalogs(self):
        before = {
            "opencode-go": {"deepseek-v4-pro": {}, "kimi-k3": {}},
            "anthropic": {},
        }
        now = {"opencode-go": {"kimi-k3": {}}, "anthropic": {}}
        pins = {
            "deepseek-v4-pro": ["~/bin/deepseek:6"],
            "kimi-k3": ["~/bin/kimi-claude:8"],
        }

        self.assertEqual(
            model_watch.compare(before, now, {}, pins),
            [
                {
                    "kind": "gone",
                    "source": "opencode-go",
                    "id": "deepseek-v4-pro",
                    "uses": ["~/bin/deepseek:6"],
                }
            ],
        )

    def test_a_pin_that_is_also_in_the_table_is_reported_once(self):
        before = {"opencode-go": {"deepseek-v4.1-flash": {}}}
        now = {"opencode-go": {}}
        in_use = {"deepseek-v4.1-flash": ["scout:0"]}

        notices = model_watch.compare(
            before, now, in_use, {"deepseek-v4.1-flash": ["review.py:75"]}
        )

        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["uses"], ["scout:0", "review.py:75"])


if __name__ == "__main__":
    unittest.main()
