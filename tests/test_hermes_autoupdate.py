import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import hermes_autoupdate  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402
import model_watch  # noqa: E402
from model_lines import Promotion, Ran  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/model-routing.source.json"
PROBE_OK = '{"success": true, "marker_exact": true, "seconds": 4.2}\n'
PROBE_QUOTA = '{"success": false, "failure": "quota", "detail": "x", "seconds": 1.0}\n'
#: What hermes.cmd -p claw chat -q ... --oneshot -Q printed on the PC (2026-10-15).
PONG_TURN = "PONG\n\nsession_id: 20261015_113404_1e7391\n"


GROK_ONLY = {"xai": {"grok-4.7": {}, "grok-4.8": {}}}
#: One effort on every cell, so the promotion is one route and one probe.
K_ONLY = {"kimi-code-plan-global": {"k3": {}, "k4": {}}}


class PlanTableTests(unittest.TestCase):
    def test_each_label_follows_the_catalog_of_the_route_it_runs_on(self):
        source = json.loads(FIXTURE.read_text(encoding="utf-8"))
        catalogs = {
            "opencode-go": {"deepseek-v4.1-flash": {}, "deepseek-v4.2-flash": {}},
            "xai": {"grok-4.7": {}, "grok-4.8": {}, "grok-4.20-0309-reasoning": {}},
            "anthropic": {
                "claude-opus-5-5": {},
                "claude-opus-6": {},
                "claude-opus-5-5-20260922": {},
            },
            "zai-coding-plan": {"glm-5.3": {}, "glm-5.4": {}},
            "kimi-code-plan-global": {"k3": {}, "k4": {}},
        }

        promotions, mixed = hermes_autoupdate.plan_table(source, catalogs)

        self.assertEqual(
            promotions,
            [
                Promotion(
                    "anthropic/claude-opus-5-5",
                    "anthropic/claude-opus-6",
                    ("dirigir:0", "dirigir-claw:0", "planear:0", "adversary:0"),
                ),
                Promotion(
                    "opencode-go/deepseek-v4.1-flash",
                    "opencode-go/deepseek-v4.2-flash",
                    ("operativo:3", "simples:1"),
                ),
                Promotion(
                    "zai/glm-5.3",
                    "zai/glm-5.4",
                    (
                        "dirigir:2",
                        "dirigir-claw:2",
                        "planear:2",
                        "codigo:2",
                        "verificar:0",
                        "investigar:2",
                        "operativo:1",
                        "adversary:3",
                    ),
                ),
                Promotion(
                    "xai/grok-4.7",
                    "xai/grok-4.8",
                    (
                        "planear:3",
                        "codigo:3",
                        "verificar:1",
                        "investigar:0",
                        "operativo:2",
                        "simples:3",
                        "adversary:1",
                    ),
                ),
                Promotion(
                    "kimi/k3",
                    "kimi/k4",
                    (
                        "dirigir:3",
                        "dirigir-claw:3",
                        "planear:1",
                        "codigo:1",
                        "verificar:3",
                        "investigar:1",
                        "adversary:2",
                    ),
                ),
            ],
        )
        self.assertEqual(mixed, [])

    def test_a_go_label_moves_on_the_go_list_and_a_vendor_label_never_does(self):
        source = {
            "chains": {
                "operativo": [
                    "anthropic/claude-sonnet-5-5 (medium)",
                    "zai/glm-5.3 (low)",
                    "xai/grok-4.7 (low)",
                    "opencode-go/deepseek-v4.1-flash (off)",
                ]
            }
        }

        promotions, _ = hermes_autoupdate.plan_table(
            source,
            {
                "opencode-go": {"deepseek-v4.2-flash": {}, "glm-5.4": {}},
                "zai-coding-plan": {"glm-5.3": {}},
            },
        )

        self.assertEqual(
            promotions,
            [
                Promotion(
                    "opencode-go/deepseek-v4.1-flash",
                    "opencode-go/deepseek-v4.2-flash",
                    ("operativo:3",),
                )
            ],
        )

    def test_a_label_on_both_routes_is_skipped_and_named(self):
        source = {
            "chains": {
                "operativo": [
                    "zai/glm-5.3",
                    "opencode-go/glm-5.3",
                    "zai/glm-5.3",
                    "zai/glm-5.3",
                ]
            }
        }

        promotions, mixed = hermes_autoupdate.plan_table(
            source, {"opencode-go": {"glm-5.4": {}}, "zai-coding-plan": {"glm-5.4": {}}}
        )

        self.assertEqual((promotions, mixed), ([], ["glm-5.3"]))


class FakeRun:
    """Answers each command by its kind and records the kinds in order."""

    def __init__(self, answers=None):
        self.answers = {
            "git status": Ran(0, "", ""),
            "probe 3": Ran(0, PROBE_OK, ""),
            "git commit": Ran(0, "", ""),
            "configure": Ran(0, "Configured claw\n", ""),
            "configure --check": Ran(0, "Check passed\n", ""),
            "turn": Ran(0, PONG_TURN, ""),
            "git revert": Ran(0, "", ""),
            "git pull": Ran(0, "", ""),
            "git push": Ran(0, "", ""),
            "git checkout": Ran(0, "", ""),
            "git merge": Ran(0, "", ""),
            **(answers or {}),
        }
        self.calls = []
        self.argv = []

    @staticmethod
    def kind(argv):
        if argv[0] == "git":
            return f"git {argv[3]}"
        if argv[1].endswith("probe_route.py"):
            return f"probe {argv[3]}"
        if argv[1].endswith("configure_team.py"):
            return " ".join(["configure", *argv[2:]])
        return "turn"

    def __call__(self, argv, timeout):
        kind = self.kind(argv)
        self.calls.append(kind)
        self.argv.append(list(argv))
        answer = self.answers[kind]
        # A list answers its calls in turn: the PONG turn runs before and after.
        return answer.pop(0) if isinstance(answer, list) else answer


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.checkout = Path(self.tmp.name) / "checkout"
        self.checkout.mkdir()
        shutil.copy(FIXTURE, self.checkout / "model-routing.source.json")
        self.setup = hermes_autoupdate.Setup(
            self.checkout, "venv-python", "hermes.cmd", "git"
        )
        self.k3 = hermes_autoupdate.plan_table(self.table(), K_ONLY)[0]

    def table(self):
        return json.loads(
            (self.checkout / "model-routing.source.json").read_text(encoding="utf-8")
        )

    def go(self, run, budget=10_000):
        return hermes_autoupdate.autoupdate(
            self.setup, self.k3, run=run, clock=lambda: 0, deadline=budget
        )

    def test_a_failed_probe_writes_nothing(self):
        before = self.table()
        run = FakeRun({"probe 3": Ran(1, PROBE_QUOTA, "")})

        outcomes = self.go(run)

        self.assertEqual(run.calls, ["git status", "probe 3"])
        self.assertEqual(self.table(), before)
        self.assertEqual(
            [(o.status, o.detail) for o in outcomes],
            [("no cambiado", "sonda posicion 3: quota")],
        )

    def test_the_probe_runs_the_new_label_through_the_checkout(self):
        run = FakeRun({"probe 3": Ran(1, PROBE_QUOTA, "")})

        self.go(run)

        self.assertEqual(
            run.argv[1],
            [
                "venv-python",
                str(self.checkout / "scripts/probe_route.py"),
                "--position",
                "3",
                "kimi/k4 (high)",
            ],
        )

    def test_a_failed_turn_after_deploying_reverts_and_redeploys(self):
        run = FakeRun(
            {
                "turn": [
                    Ran(0, PONG_TURN, ""),
                    Ran(0, "Error: provider unavailable\n", ""),
                ]
            }
        )

        outcomes = self.go(run)

        self.assertEqual(
            run.calls,
            [
                "git status",
                "probe 3",
                "turn",
                "git commit",
                "configure",
                "configure --check",
                "turn",
                "git revert",
                "configure",
            ],
        )
        self.assertEqual(
            [(o.status, o.detail) for o in outcomes],
            [("revertido", "turno PONG: no dijo PONG")],
        )

    def test_the_real_turn_output_with_its_session_line_counts_as_pong(self):
        run = FakeRun({"turn": Ran(0, PONG_TURN, "")})

        outcomes = self.go(run)

        self.assertNotIn("git revert", run.calls)
        self.assertEqual([(o.status, o.detail) for o in outcomes], [("cambiado", "")])

    def test_a_failed_check_reverts_too(self):
        run = FakeRun({"configure --check": Ran(1, "", "conflict in claw/config.yaml")})

        outcomes = self.go(run)

        self.assertIn("git revert", run.calls)
        self.assertEqual(run.calls.count("turn"), 1)
        self.assertEqual(
            outcomes[0].detail,
            "configure_team --check: salida 1: conflict in claw/config.yaml",
        )

    def test_a_success_commits_the_new_table_deploys_and_pushes(self):
        run = FakeRun()

        outcomes = self.go(run)

        self.assertEqual(
            run.calls,
            [
                "git status",
                "probe 3",
                "turn",
                "git commit",
                "configure",
                "configure --check",
                "turn",
                "git pull",
                "git push",
            ],
        )
        self.assertEqual(
            run.argv[3],
            [
                "git",
                "-C",
                str(self.checkout),
                "commit",
                "-m",
                "auto(modelos): kimi/k3 -> kimi/k4",
                "--",
                "model-routing.source.json",
            ],
        )
        self.assertEqual(self.table()["chains"]["dirigir"][3], "kimi/k4 (high)")
        self.assertEqual(self.table()["chains"]["adversary"][2], "kimi/k4 (high)")
        self.assertNotIn("kimi/k3", json.dumps(self.table()))
        self.assertEqual([(o.status, o.detail) for o in outcomes], [("cambiado", "")])

    def test_a_turn_that_already_fails_before_the_switch_changes_nothing(self):
        # OpenCode Go down: the check after deploying could not tell the new
        # model apart, so nothing is written and the switch waits.
        before = self.table()
        run = FakeRun({"turn": Ran(1, "", "opencode-go: 503")})

        outcomes = self.go(run)

        self.assertEqual(run.calls, ["git status", "probe 3", "turn"])
        self.assertEqual(self.table(), before)
        self.assertEqual(
            [(o.status, o.detail) for o in outcomes],
            [("pendiente", "turno PONG antes de cambiar: salida 1: opencode-go: 503")],
        )

    def test_a_failed_turn_is_named_without_its_session_id(self):
        run = FakeRun(
            {
                "turn": Ran(
                    1, "Error: provider\n\nsession_id: 20261015_113404_1e7391\n", ""
                )
            }
        )

        outcomes = self.go(run)

        self.assertEqual(
            outcomes[0].detail, "turno PONG antes de cambiar: salida 1: Error: provider"
        )

    def test_a_failed_push_is_reported_and_the_switch_stands(self):
        run = FakeRun({"git push": Ran(1, "", "rejected: fetch first")})

        outcomes = self.go(run)

        self.assertEqual(self.table()["chains"]["dirigir"][3], "kimi/k4 (high)")
        self.assertEqual(
            [(o.status, o.detail) for o in outcomes],
            [("cambiado", "sin push (salida 1: rejected: fetch first)")],
        )

    def test_uncommitted_table_edits_are_never_swept_into_a_commit(self):
        run = FakeRun({"git status": Ran(0, " M model-routing.source.json\n", "")})

        outcomes = self.go(run)

        self.assertEqual(run.calls, ["git status"])
        self.assertEqual(outcomes[0].status, "no cambiado")

    def test_a_promotion_that_cannot_finish_before_the_cron_limit_waits(self):
        run = FakeRun()

        outcomes = self.go(run, budget=600)

        self.assertEqual(run.calls, ["git status"])
        self.assertEqual(
            [(o.status, o.detail) for o in outcomes],
            [("pendiente", "no cupo en el tiempo de esta corrida")],
        )


class ReportTests(unittest.TestCase):
    def test_switches_are_said_and_failures_only_when_their_words_change(self):
        grok = Promotion("xai/grok-4.7", "xai/grok-4.8", ("planear:3", "operativo:2"))
        sol = Promotion(
            "anthropic/claude-opus-5-5", "anthropic/claude-opus-6", ("dirigir:0",)
        )
        outcomes = [
            hermes_autoupdate.Outcome(grok, "cambiado"),
            hermes_autoupdate.Outcome(sol, "no cambiado", "sonda posicion 0: quota"),
        ]

        lines, remembered = hermes_autoupdate.report(outcomes, [], {})
        again, _ = hermes_autoupdate.report(outcomes[1:], [], remembered)

        self.assertEqual(
            lines,
            [
                "- Cambiado: xai/grok-4.7 -> xai/grok-4.8 en planear:3, operativo:2.",
                "- No cambiado: anthropic/claude-opus-5-5 -> anthropic/claude-opus-6 "
                "(sonda posicion 0: quota).",
            ],
        )
        self.assertEqual(
            remembered,
            {
                "anthropic/claude-opus-5-5 -> anthropic/claude-opus-6": (
                    "- No cambiado: anthropic/claude-opus-5-5 -> "
                    "anthropic/claude-opus-6 (sonda posicion 0: quota)."
                )
            },
        )
        self.assertEqual(again, [])


class WatcherHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.here = Path(self.tmp.name) / "scripts"
        self.here.mkdir()
        checkout = Path(self.tmp.name) / "checkout"
        checkout.mkdir()
        shutil.copy(FIXTURE, checkout / "model-routing.source.json")
        (self.here / "autoupdate.json").write_text(
            json.dumps(
                {"checkout": str(checkout), "python": "py", "hermes": "hermes.cmd"}
            ),
            encoding="utf-8",
        )

    def test_a_quota_failure_is_told_once_across_daily_runs(self):
        said, last = [], {}
        for _ in range(3):
            lines, last = hermes_autoupdate.step(
                self.here,
                K_ONLY,
                last,
                started=0,
                run=FakeRun({"probe 3": Ran(1, PROBE_QUOTA, "")}),
                clock=lambda: 0,
            )
            said.append(lines)

        self.assertEqual(
            said,
            [
                ["- No cambiado: kimi/k3 -> kimi/k4 (sonda posicion 3: quota)."],
                [],
                [],
            ],
        )

    def test_a_reverted_switch_is_told_once_and_not_tried_again(self):
        checkout = Path(self.tmp.name) / "checkout"
        said, calls, last = [], [], {}
        for _ in range(2):
            run = FakeRun(
                {"turn": [Ran(0, PONG_TURN, ""), Ran(0, "Error: provider\n", "")]}
            )
            lines, last = hermes_autoupdate.step(
                self.here, K_ONLY, last, started=0, run=run, clock=lambda: 0
            )
            said.append(lines)
            calls.append(run.calls)
            # The fake git revert leaves the file; the real one restores it.
            shutil.copy(FIXTURE, checkout / "model-routing.source.json")

        notice = (
            "- Revertido: kimi/k3 -> kimi/k4 (turno PONG: no dijo PONG). "
            "No lo vuelvo a intentar hasta que lo revises."
        )
        self.assertEqual(said, [[notice], []])
        self.assertEqual(calls[1], [])
        self.assertEqual(last, {"revertido:kimi/k3 -> kimi/k4": notice})

    def test_without_its_setup_file_the_watcher_switches_nothing(self):
        (self.here / "autoupdate.json").unlink()

        self.assertEqual(
            hermes_autoupdate.step(self.here, K_ONLY, {}, started=0), ([], {})
        )

    def test_a_crash_in_the_autoupdate_is_reported_and_the_report_goes_on(self):
        (self.here / "autoupdate.json").write_text("{", encoding="utf-8")
        held = {"revertido:kimi/k3 -> kimi/k4": "- Revertido: ..."}

        lines, remembered = model_watch.follow_releases(self.here, K_ONLY, held, 0)

        self.assertEqual(len(lines), 1)
        self.assertTrue(
            lines[0].startswith("- La autoactualizacion fallo: JSONDecodeError")
        )
        self.assertEqual(remembered["revertido:kimi/k3 -> kimi/k4"], "- Revertido: ...")
        self.assertEqual(
            model_watch.follow_releases(self.here, K_ONLY, remembered, 0)[0], []
        )

    def test_the_message_carries_the_switch_and_no_promise_that_nothing_changed(self):
        text = model_watch.render(
            [],
            {},
            "2026-10-15",
            ["- Cambiado: kimi/k3 -> kimi/k4 en dirigir:3."],
        )

        self.assertEqual(
            text,
            "Modelos: novedades del 2026-10-15\n"
            "- Cambiado: kimi/k3 -> kimi/k4 en dirigir:3.",
        )


if __name__ == "__main__":
    unittest.main()
