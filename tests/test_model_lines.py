import ast
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import model_lines  # noqa: E402


def catalog(*ids, deprecated=()):
    return {
        **{model: {} for model in ids},
        **{model: {"status": "deprecated"} for model in deprecated},
    }


class FamilyTests(unittest.TestCase):
    def test_family_prefixes_are_the_ones_the_router_binds(self):
        # The watcher must call "the same family" what the plugin does, or a
        # new model the router would accept goes unannounced or unfollowed.
        tree = ast.parse(
            (ROOT / "plugins/goncloud-team/subscriptions/catalog.py").read_text(
                encoding="utf-8"
            )
        )
        bound = next(
            ast.literal_eval(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_FAMILY_ADAPTERS"
        )

        self.assertEqual(set(model_lines.FAMILIES), {prefix for prefix, _ in bound})

    def test_kimi_is_every_k_with_a_number_and_nothing_else_starting_with_k(self):
        self.assertEqual(
            {
                model: model_lines.family_of(model)
                for model in ("k3", "k4", "k3-256k", "kimi-for-coding", "kling-1")
            },
            {
                "k3": r"k\d",
                "k4": r"k\d",
                "k3-256k": r"k\d",
                "kimi-for-coding": "kimi-",
                "kling-1": None,
            },
        )


class ParseTests(unittest.TestCase):
    def test_the_single_version_run_becomes_the_line(self):
        cases = {
            "grok-4.7": ("grok-{}", (4, 7)),
            "gpt-5.6-sol": ("gpt-{}-sol", (5, 6)),
            "claude-opus-5-5": ("claude-opus-{}", (5, 5)),
            "glm-5.3-flash": ("glm-{}-flash", (5, 3)),
            "k3": ("k{}", (3,)),
            "muse-spark-1.3-contributor": ("muse-spark-{}-contributor", (1, 3)),
            "deepseek-v4.1-flash": ("deepseek-v{}-flash", (4, 1)),
        }
        for model, (line, version) in cases.items():
            with self.subTest(model=model):
                self.assertEqual(
                    model_lines.parse(model), model_lines.Versioned(line, version)
                )

    def test_ids_that_are_not_a_release_to_follow_have_no_line(self):
        for model in (
            "kimi-for-coding",
            "deepseek-flash",
            "claude-opus-5-5-20260922",
            "grok-4.20-0309-reasoning",
            "muse-spark-1.3-contributor-free",
            "gpt-6-preview",
            "glm-5.3-exp",
            "claude-3-5-sonnet-v2",
        ):
            with self.subTest(model=model):
                self.assertIsNone(model_lines.parse(model))

    def test_version_text_is_the_run_as_written(self):
        self.assertEqual(model_lines.version_text("claude-opus-5-5"), "5-5")
        self.assertEqual(model_lines.version_text("grok-4.7"), "4.7")
        self.assertIsNone(model_lines.version_text("kimi-for-coding"))


class NewerInLineTests(unittest.TestCase):
    def test_the_cases_the_owner_will_meet(self):
        cases = [
            ("grok-4.7", catalog("grok-4.8", "grok-4.20-0309-reasoning"), "grok-4.8"),
            (
                "gpt-5.6-sol",
                catalog("gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna"),
                "gpt-6.1-sol",
            ),
            (
                "claude-opus-5-5",
                catalog("claude-opus-6", "claude-opus-5-5-20260922", "claude-sonnet-6"),
                "claude-opus-6",
            ),
            (
                "glm-5.3-flash",
                catalog("glm-5.4", "glm-5.4-flash", "glm-5.3-highspeed"),
                "glm-5.4-flash",
            ),
            ("k3", catalog("k3-256k", "k4"), "k4"),
            ("kimi-for-coding", catalog("kimi-for-coding", "k4"), None),
            ("deepseek-flash", catalog("deepseek-flash", "deepseek-v5-flash"), None),
            (
                "muse-spark-1.3-contributor",
                catalog("muse-spark-1.4", "muse-spark-1.4-contributor"),
                "muse-spark-1.4-contributor",
            ),
        ]
        for current, available, expected in cases:
            with self.subTest(current=current):
                self.assertEqual(
                    model_lines.newer_in_line(current, available), expected
                )

    def test_a_deprecated_newer_id_is_skipped(self):
        self.assertEqual(
            model_lines.newer_in_line(
                "grok-4.7", catalog("grok-4.8", deprecated=("grok-4.9",))
            ),
            "grok-4.8",
        )
        self.assertIsNone(
            model_lines.newer_in_line("grok-4.7", catalog(deprecated=("grok-4.9",)))
        )

    def test_versions_compare_zero_padded(self):
        # (6,) and (6, 0) are the same release; (6, 1) is above both.
        self.assertIsNone(
            model_lines.newer_in_line("gpt-6-sol", catalog("gpt-6.0-sol"))
        )
        self.assertEqual(
            model_lines.newer_in_line("gpt-6-sol", catalog("gpt-6.1-sol")),
            "gpt-6.1-sol",
        )
        self.assertIsNone(
            model_lines.newer_in_line(
                "gpt-6.1-sol", catalog("gpt-6-sol", "gpt-6.1-sol")
            )
        )


class FreshNoticeTests(unittest.TestCase):
    def test_a_notice_is_said_again_only_when_its_words_change(self):
        quota = {"openai/gpt-5.6-sol->openai/gpt-6-sol": "sonda 6: quota"}

        self.assertEqual(model_lines.fresh_notices(quota, {}), ["sonda 6: quota"])
        self.assertEqual(model_lines.fresh_notices(quota, quota), [])
        self.assertEqual(
            model_lines.fresh_notices(
                {"openai/gpt-5.6-sol->openai/gpt-6-sol": "sonda 6: auth"}, quota
            ),
            ["sonda 6: auth"],
        )


class RanTests(unittest.TestCase):
    def test_why_names_the_exit_and_the_last_line(self):
        self.assertEqual(model_lines.Ran(1, "uno\ndos\n\n", "").why(), "salida 1: dos")
        self.assertEqual(
            model_lines.Ran(None, "", "sin respuesta tras 30 s").why(),
            "sin respuesta tras 30 s",
        )

    def test_the_real_runner_reports_exit_output_and_timeout(self):
        done = model_lines.run(
            [
                sys.executable,
                "-c",
                "import sys; print('hola'); print('mal', file=sys.stderr); sys.exit(3)",
            ],
            30,
        )
        self.assertEqual(done, model_lines.Ran(3, "hola\n", "mal\n"))
        slow = model_lines.run([sys.executable, "-c", "import time; time.sleep(5)"], 1)
        self.assertEqual((slow.code, slow.err), (None, "sin respuesta tras 1 s"))

    def test_a_timeout_kills_the_grandchild_that_holds_the_output_open(self):
        # hermes.cmd -> python -> configure_team -> git: the grandchild inherits
        # the pipes, and on Windows CPython's own timeout then waits on it forever.
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "grandchild.pid"
            parent = (
                "import subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c',"
                " 'import time; time.sleep(30)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "child.wait()\n"
            )
            started = time.monotonic()
            done = model_lines.run([sys.executable, "-c", parent], 1)
            took = time.monotonic() - started
            grandchild = int(pid_file.read_text())

        self.assertEqual((done.code, done.err), (None, "sin respuesta tras 1 s"))
        self.assertLess(took, 5)
        deadline = time.monotonic() + 5
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(grandchild), "the grandchild outlived the timeout")

    def test_an_interrupt_kills_and_reaps_the_child(self):
        started = []

        class Interrupted(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.append(self)

            def communicate(self, *args, **kwargs):
                if self is started[0]:
                    raise KeyboardInterrupt
                return super().communicate(*args, **kwargs)

        with mock.patch.object(model_lines.subprocess, "Popen", Interrupted):
            with self.assertRaises(KeyboardInterrupt):
                model_lines.run(
                    [sys.executable, "-c", "import time; time.sleep(30)"], 30
                )

        child = started[0]
        child.stdout.close()
        child.stderr.close()
        self.assertEqual(
            child.returncode, 1 if os.name == "nt" else -signal.SIGKILL, "reaped"
        )


def alive(pid):
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return str(pid) in listed.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
