import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import claw_autoupdate  # noqa: E402
from model_lines import Promotion, Ran  # noqa: E402

CONFIG = {
    "agents": {
        "defaults": {
            "model": {
                "primary": "opencode-go/deepseek-v4.1-flash",
                "fallbacks": ["xai/grok-4.7"],
            },
            "models": {
                "anthropic/claude-opus-5-5": {
                    "agentRuntime": {"id": "claude-cli"},
                    "alias": "opus",
                    "params": {"thinking": "xhigh"},
                },
                "xai/grok-4.7": {"alias": "Grok"},
            },
            "modelPolicy": {
                "allow": [
                    "anthropic/claude-opus-5-5",
                    "xai/grok-4.7",
                    "opencode-go/deepseek-v4.1-flash",
                ]
            },
        },
        "entries": {
            "main": {
                "model": {
                    "primary": "opencode-go/deepseek-v4.1-flash",
                    "fallbacks": ["anthropic/claude-opus-5-5", "xai/grok-4.7"],
                },
                "models": {
                    "anthropic/claude-opus-5-5": {"params": {"thinking": "xhigh"}},
                    "xai/grok-4.7": {"params": {"thinking": "high"}},
                },
            },
            "skillverif": {
                "model": {
                    "primary": "xai/grok-4.7",
                    "fallbacks": ["anthropic/claude-opus-5-5"],
                }
            },
        },
    },
    "models": {
        "providers": {
            "xai": {
                "baseUrl": "https://api.x.ai/v1",
                "models": [
                    {"id": "grok-4.7", "name": "Grok 4.7", "contextWindow": 500000}
                ],
            },
            "opencode-go": {
                "models": [{"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash"}]
            },
        }
    },
}
VIEW = {
    "opencode-go": {"deepseek-v4.1-flash": {}, "deepseek-v4.2-flash": {}},
    "xai": {"grok-4.7": {}, "grok-4.8": {}},
    "anthropic": {"claude-opus-5-5": {}, "claude-opus-6": {}},
}
GROK = Promotion("xai/grok-4.7", "xai/grok-4.8", ("defaults", "main", "skillverif"))
OPUS = Promotion(
    "anthropic/claude-opus-5-5", "anthropic/claude-opus-6", ("main", "skillverif")
)


class PlanTests(unittest.TestCase):
    def test_every_chain_ref_with_a_newer_release_in_its_catalog(self):
        self.assertEqual(
            claw_autoupdate.plan_claw(CONFIG, VIEW),
            [
                Promotion(
                    "opencode-go/deepseek-v4.1-flash",
                    "opencode-go/deepseek-v4.2-flash",
                    ("defaults", "main"),
                ),
                GROK,
                OPUS,
            ],
        )

    def test_an_opencode_go_account_follows_the_go_list_not_the_vendor(self):
        config = {
            "agents": {
                "entries": {"main": {"model": {"primary": "opencode-go-2/glm-5.3"}}}
            }
        }

        self.assertEqual(
            claw_autoupdate.plan_claw(
                config, {"zai-coding-plan": {"glm-5.4": {}}, "opencode-go": {}}
            ),
            [],
        )


class RenamePatchTests(unittest.TestCase):
    def test_the_rename_rewrites_every_use_and_moves_the_provider_row(self):
        patch, replace = claw_autoupdate.rename_patch(
            CONFIG, "xai/grok-4.7", "xai/grok-4.8"
        )

        self.assertEqual(
            patch,
            {
                "agents": {
                    "defaults": {
                        "model": {"fallbacks": ["xai/grok-4.8"]},
                        "models": {
                            "xai/grok-4.7": None,
                            "xai/grok-4.8": {"alias": "Grok"},
                        },
                        "modelPolicy": {
                            "allow": [
                                "anthropic/claude-opus-5-5",
                                "xai/grok-4.8",
                                "opencode-go/deepseek-v4.1-flash",
                            ]
                        },
                    },
                    "entries": {
                        "main": {
                            "model": {
                                "fallbacks": [
                                    "anthropic/claude-opus-5-5",
                                    "xai/grok-4.8",
                                ]
                            },
                            "models": {
                                "xai/grok-4.7": None,
                                "xai/grok-4.8": {"params": {"thinking": "high"}},
                            },
                        },
                        "skillverif": {"model": {"primary": "xai/grok-4.8"}},
                    },
                },
                "models": {
                    "providers": {
                        "xai": {
                            "models": [
                                {
                                    "id": "grok-4.8",
                                    "name": "Grok 4.8",
                                    "contextWindow": 500000,
                                }
                            ]
                        }
                    }
                },
            },
        )
        # Same lengths, yet each of these loses grok-4.7: the CLI refuses that
        # without --replace-path (seen live on models.providers.xai.models).
        self.assertEqual(
            replace,
            [
                "agents.defaults.model.fallbacks",
                "agents.defaults.modelPolicy.allow",
                "agents.entries.main.model.fallbacks",
                "models.providers.xai.models",
            ],
        )

    def test_renaming_an_already_renamed_config_is_an_empty_patch(self):
        done = claw_autoupdate.renamed(CONFIG, "xai/grok-4.7", "xai/grok-4.8")

        self.assertEqual(
            claw_autoupdate.rename_patch(done, "xai/grok-4.7", "xai/grok-4.8"),
            ({}, []),
        )

    def test_staging_only_adds_and_the_rename_after_it_drops_from_four_arrays(self):
        stage = claw_autoupdate.staged(CONFIG, "xai/grok-4.7", "xai/grok-4.8")

        self.assertEqual(
            claw_autoupdate.merge_patch(CONFIG, stage),
            (
                {
                    "agents": {
                        "defaults": {
                            "models": {"xai/grok-4.8": {"alias": "Grok"}},
                            "modelPolicy": {
                                "allow": [
                                    "anthropic/claude-opus-5-5",
                                    "xai/grok-4.7",
                                    "opencode-go/deepseek-v4.1-flash",
                                    "xai/grok-4.8",
                                ]
                            },
                        }
                    },
                    "models": {
                        "providers": {
                            "xai": {
                                "models": [
                                    {
                                        "id": "grok-4.7",
                                        "name": "Grok 4.7",
                                        "contextWindow": 500000,
                                    },
                                    {
                                        "id": "grok-4.8",
                                        "name": "Grok 4.8",
                                        "contextWindow": 500000,
                                    },
                                ]
                            }
                        }
                    },
                },
                [],
            ),
        )
        self.assertEqual(
            claw_autoupdate.rename_patch(stage, "xai/grok-4.7", "xai/grok-4.8")[1],
            [
                "agents.defaults.model.fallbacks",
                "agents.defaults.modelPolicy.allow",
                "agents.entries.main.model.fallbacks",
                "models.providers.xai.models",
            ],
        )

    def test_a_built_in_provider_needs_no_row(self):
        patch, _ = claw_autoupdate.rename_patch(
            CONFIG, "anthropic/claude-opus-5-5", "anthropic/claude-opus-6"
        )

        self.assertNotIn("models", patch)
        self.assertEqual(
            patch["agents"]["defaults"]["models"]["anthropic/claude-opus-6"],
            {
                "agentRuntime": {"id": "claude-cli"},
                "alias": "opus",
                "params": {"thinking": "xhigh"},
            },
        )

    def test_a_path_key_that_is_not_an_identifier_is_quoted(self):
        before = {"models": {"providers": {"opencode-go": {"models": [1, 2]}}}}
        after = {"models": {"providers": {"opencode-go": {"models": [2]}}}}

        self.assertEqual(
            claw_autoupdate.merge_patch(before, after)[1],
            ['models.providers["opencode-go"].models'],
        )


#: What ``openclaw infer model run --json`` printed live (2026-10-15) for a ref
#: the gateway did not know: pretty JSON on stdout, the reason on stderr.
UNKNOWN_MODEL = Ran(
    1,
    json.dumps(
        {
            "ok": False,
            "error": {
                "type": "cli_error",
                "message": "Unknown model: xai/grok-4.8",
            },
        },
        indent=2,
    )
    + "\n",
    "[openclaw] Error: infer\n[openclaw] Reason: model run failed\n",
)


class FailureDetailTests(unittest.TestCase):
    def test_the_json_error_message_beats_the_closing_brace(self):
        self.assertEqual(
            claw_autoupdate.failure(UNKNOWN_MODEL), "Unknown model: xai/grok-4.8"
        )

    def test_without_json_the_reason_line_is_used(self):
        done = Ran(1, "", "[openclaw] Error\n[openclaw] Reason: quota exhausted\n")

        self.assertEqual(claw_autoupdate.failure(done), "quota exhausted")

    def test_without_either_the_exit_and_last_line_are_used(self):
        self.assertEqual(
            claw_autoupdate.failure(Ran(2, "", "boom\n")), "salida 2: boom"
        )

    def test_a_long_message_is_one_bounded_line(self):
        doc = {"ok": False, "error": {"message": "a\nb " + "x" * 300}}

        detail = claw_autoupdate.failure(Ran(1, json.dumps(doc), ""))

        self.assertEqual(detail, ("a b " + "x" * 300)[:200])


class IdleTests(unittest.TestCase):
    def test_only_this_runner_s_own_cron_run_may_be_in_flight(self):
        def status(**work):
            return {"shutdownBudget": {"activeWork": work}}

        self.assertTrue(claw_autoupdate.is_idle(status(modelRuns=0, cronRuns=1)))
        self.assertFalse(claw_autoupdate.is_idle(status(modelRuns=0, cronRuns=2)))
        self.assertFalse(claw_autoupdate.is_idle(status(modelRuns=1, cronRuns=0)))
        self.assertFalse(claw_autoupdate.is_idle({}))


def merged(config, patch):
    result = copy.deepcopy(config)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merged(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def turn(text, winner):
    return Ran(
        0,
        json.dumps(
            {
                "runId": "r1",
                "result": {
                    "payloads": [{"text": text}],
                    "meta": {"executionTrace": {"winnerProvider": winner}},
                },
            }
        ),
        "",
    )


def infer(text, ok=True):
    return Ran(0, json.dumps({"ok": ok, "outputs": [{"text": text}]}), "")


def array_drops(before, patch, path=()):
    """Paths of the arrays ``patch`` would drop an element from."""
    found = []
    for key, value in patch.items():
        old = before.get(key) if isinstance(before, dict) else None
        if isinstance(value, dict):
            found += array_drops(old or {}, value, (*path, key))
        elif isinstance(value, list) and isinstance(old, list):
            if any(item not in value for item in old):
                found.append(claw_autoupdate._path_text((*path, key)))
    return found


class FakeGateway:
    """A live config that config patch really changes; model calls are scripted.

    Its config patch refuses what the real CLI refuses (2026.9.7): an empty
    patch, and dropping an array element whose path is not a --replace-path.
    """

    def __init__(
        self,
        config,
        answers=None,
        busy=False,
        busy_from_patch=None,
        lost_patches=(),
        patch_results=None,
        stale_reads=None,
    ):
        self.config = copy.deepcopy(config)
        self.shown = self.config
        self.answers = {
            "infer xai/grok-4.8": infer("OK"),
            "agent skillverif probe": turn("PONG", "claude-cli"),
            "agent skillverif verify": turn("PONG", "xai"),
            **(answers or {}),
        }
        self.busy = busy
        self.busy_from_patch = busy_from_patch
        self.calls = []
        self.now = 0.0
        self.lost_patches = lost_patches
        self.patch_results = patch_results or {}
        self.stale_reads = stale_reads or {}
        self.stale_left = 0
        self.patches = 0

    def kind(self, argv):
        if argv[1:4] == ["gateway", "call", "status"]:
            return "status"
        if argv[1:4] == ["gateway", "call", "config.get"]:
            return "config"
        if argv[1:3] == ["config", "patch"]:
            return "dry-run" if "--dry-run" in argv else "patch"
        if argv[1:3] == ["infer", "model"]:
            return f"infer {argv[argv.index('--model') + 1]}"
        if argv[1] == "agent":
            return f"agent {argv[3]} {argv[5].rsplit('-', 1)[1]}"
        return " ".join(argv[1:3])

    def refusal(self, argv):
        patch = json.loads(Path(argv[4]).read_text(encoding="utf-8"))
        if not patch:
            return patch, Ran(1, "", "patch did not contain any config updates")
        replace = [
            argv[i + 1] for i, word in enumerate(argv) if word == "--replace-path"
        ]
        for path in array_drops(self.config, patch):
            if path not in replace:
                return patch, Ran(
                    1, "", f"Refusing to replace {path}; it would remove entries"
                )
        return patch, None

    def __call__(self, argv, timeout):
        kind = self.kind(argv)
        self.calls.append(kind)
        if kind == "status":
            busy = self.busy or (
                self.busy_from_patch is not None
                and self.patches >= self.busy_from_patch
            )
            work = {"rootRequests": 1 if busy else 0, "cronRuns": 1}
            return Ran(0, json.dumps({"shutdownBudget": {"activeWork": work}}), "")
        if kind == "config":
            if self.stale_left:
                self.stale_left -= 1
            else:
                self.shown = self.config
            return Ran(0, json.dumps({"hash": "h", "parsed": self.shown}), "")
        if kind in ("dry-run", "patch"):
            patch, refused = self.refusal(argv)
            if refused:
                return refused
            if kind == "dry-run":
                return Ran(0, "{}", "")
            self.patches += 1
            if self.patches not in self.lost_patches:
                self.config = merged(self.config, patch)
                self.stale_left = self.stale_reads.get(self.patches, 0)
            return self.patch_results.get(self.patches, Ran(0, "{}", ""))
        if kind in self.answers:
            return self.answers[kind]
        return Ran(0, "{}", "")

    def gateway(self):
        def sleep(seconds):
            self.now += seconds

        return claw_autoupdate.Gateway(
            "openclaw", self, sleep=sleep, clock=lambda: self.now
        )


class RunnerTests(unittest.TestCase):
    def go(self, fake, promotion=GROK, budget=10_000):
        return claw_autoupdate.promote(fake.gateway(), promotion, lambda: budget)

    def test_a_failed_probe_leaves_the_live_config_exactly_as_it_was(self):
        fake = FakeGateway(
            CONFIG, {"infer xai/grok-4.8": Ran(1, "", "model not found")}
        )

        outcome = self.go(fake)

        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(
            (outcome.status, outcome.detail),
            ("no cambiado", "sonda (high): salida 1: model not found"),
        )
        self.assertEqual(
            [kind for kind in fake.calls if kind in ("patch", "infer xai/grok-4.8")],
            ["patch", "infer xai/grok-4.8", "patch"],
        )

    def test_a_probe_failure_notice_quotes_the_error_openclaw_gave(self):
        fake = FakeGateway(CONFIG, {"infer xai/grok-4.8": UNKNOWN_MODEL})

        outcome = self.go(fake)

        self.assertEqual(outcome.detail, "sonda (high): Unknown model: xai/grok-4.8")

    def test_a_failed_check_after_the_rename_restores_the_snapshot(self):
        fake = FakeGateway(
            CONFIG, {"agent skillverif verify": turn("Error de proveedor", "xai")}
        )

        outcome = self.go(fake)

        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(
            (outcome.status, outcome.detail),
            ("revertido", "turno en skillverif: no dijo PONG"),
        )
        self.assertEqual(fake.calls.count("patch"), 3)

    def test_a_success_leaves_only_the_new_ref_in_the_live_config(self):
        fake = FakeGateway(CONFIG)

        outcome = self.go(fake)

        self.assertEqual(outcome, claw_autoupdate.Outcome(GROK, "cambiado"))
        self.assertEqual(
            fake.config, claw_autoupdate.renamed(CONFIG, "xai/grok-4.7", "xai/grok-4.8")
        )
        self.assertNotIn("grok-4.7", json.dumps(fake.config))
        self.assertIn("agent skillverif verify", fake.calls)

    def test_a_rename_that_did_not_land_is_caught_and_restored(self):
        # The patch command said ok, yet the live config still names the old
        # ref: the check reads the config back instead of trusting the exit.
        fake = FakeGateway(CONFIG, lost_patches=(2,))

        outcome = self.go(fake)

        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(
            (outcome.status, outcome.detail),
            ("revertido", "xai/grok-4.7 sigue en la config"),
        )

    def test_a_rename_that_timed_out_after_landing_is_restored_from_a_fresh_read(
        self,
    ):
        fake = FakeGateway(
            CONFIG, patch_results={2: Ran(None, "", "sin respuesta tras 60 s")}
        )

        outcome = self.go(fake)

        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(
            (outcome.status, outcome.detail),
            ("revertido", "config patch: sin respuesta tras 60 s"),
        )

    def test_a_config_that_reloads_late_is_waited_for_not_restored(self):
        fake = FakeGateway(CONFIG, stale_reads={2: 3})

        outcome = self.go(fake)

        self.assertEqual(outcome, claw_autoupdate.Outcome(GROK, "cambiado"))
        self.assertEqual(
            fake.config, claw_autoupdate.renamed(CONFIG, "xai/grok-4.7", "xai/grok-4.8")
        )

    def test_staging_left_by_a_busy_run_is_picked_up_by_the_next_one(self):
        first = FakeGateway(CONFIG, busy_from_patch=1)

        waiting = self.go(first)
        second = FakeGateway(first.config)
        done = self.go(second)

        self.assertEqual(
            (waiting.status, waiting.detail),
            ("pendiente", "no hubo un momento sin corridas"),
        )
        self.assertEqual(
            first.config, claw_autoupdate.staged(CONFIG, "xai/grok-4.7", "xai/grok-4.8")
        )
        self.assertEqual(done, claw_autoupdate.Outcome(GROK, "cambiado"))
        self.assertEqual(
            second.config,
            claw_autoupdate.renamed(CONFIG, "xai/grok-4.7", "xai/grok-4.8"),
        )

    def test_a_restore_while_runs_are_in_flight_says_so(self):
        fake = FakeGateway(
            CONFIG,
            {"agent skillverif verify": turn("Error de proveedor", "xai")},
            busy_from_patch=2,
        )

        outcome = self.go(fake)

        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(
            outcome.detail,
            "turno en skillverif: no dijo PONG; "
            "restaure con corridas en curso, pude cortar alguna",
        )

    def test_anthropic_is_probed_by_a_real_turn_through_claude_cli(self):
        fake = FakeGateway(
            CONFIG, {"agent skillverif probe": turn("PONG", "anthropic")}
        )

        outcome = self.go(fake, OPUS)

        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(
            outcome.detail, "sonda (xhigh): contesto anthropic, no claude-cli"
        )
        self.assertNotIn("infer anthropic/claude-opus-6", fake.calls)

    def test_nothing_is_written_while_runs_are_in_flight(self):
        fake = FakeGateway(CONFIG, busy=True)

        outcome = self.go(fake)

        self.assertNotIn("patch", fake.calls)
        self.assertEqual(
            (outcome.status, outcome.detail),
            ("pendiente", "no hubo un momento sin corridas"),
        )

    def test_a_promotion_that_cannot_finish_before_the_cron_limit_waits(self):
        fake = FakeGateway(CONFIG)

        outcome = self.go(fake, budget=300)

        self.assertNotIn("patch", fake.calls)
        self.assertEqual(outcome.status, "pendiente")


class HoldTests(unittest.TestCase):
    def test_a_reverted_switch_is_told_once_and_not_tried_again(self):
        fake = FakeGateway(
            CONFIG, {"agent skillverif verify": turn("Error de proveedor", "xai")}
        )
        said, patches, last = [], [], {}
        for _ in range(2):
            before = fake.calls.count("patch")
            lines, last = claw_autoupdate.autoupdate(
                fake.gateway(), {"xai": VIEW["xai"]}, last, started=0
            )
            said.append(lines)
            patches.append(fake.calls.count("patch") - before)

        notice = (
            "- Revertido: xai/grok-4.7 -> xai/grok-4.8 "
            "(turno en skillverif: no dijo PONG). "
            "No lo vuelvo a intentar hasta que lo revises."
        )
        self.assertEqual(said, [[notice], []])
        self.assertEqual(patches, [3, 0])
        self.assertEqual(last, {"revertido:xai/grok-4.7 -> xai/grok-4.8": notice})
        self.assertEqual(fake.config, CONFIG)

    def test_a_run_that_crashes_says_so_once_and_keeps_the_pairs_on_hold(self):
        def down(argv, timeout):
            return Ran(1, "", "gateway down")

        gateway = claw_autoupdate.Gateway("openclaw", down)
        held = {"revertido:xai/grok-4.7 -> xai/grok-4.8": "- Revertido: ..."}

        lines, remembered = claw_autoupdate.said(gateway, VIEW, held, started=0)
        again, _ = claw_autoupdate.said(gateway, VIEW, remembered, started=0)

        self.assertEqual(
            lines,
            [
                "- La autoactualizacion de claw fallo: RuntimeError: "
                "gateway call config.get: salida 1: gateway down"
            ],
        )
        self.assertEqual(again, [])
        self.assertEqual(
            remembered["revertido:xai/grok-4.7 -> xai/grok-4.8"], "- Revertido: ..."
        )


class ReportTests(unittest.TestCase):
    def test_a_failure_is_said_once_until_its_words_change(self):
        failed = [claw_autoupdate.Outcome(GROK, "no cambiado", "sonda (high): quota")]

        first, remembered = claw_autoupdate.report(failed, {})
        second, _ = claw_autoupdate.report(failed, remembered)
        switched, _ = claw_autoupdate.report(
            [claw_autoupdate.Outcome(GROK, "cambiado")], remembered
        )

        self.assertEqual(
            first,
            ["- No cambiado: xai/grok-4.7 -> xai/grok-4.8 (sonda (high): quota)."],
        )
        self.assertEqual(second, [])
        self.assertEqual(
            switched,
            ["- Cambiado: xai/grok-4.7 -> xai/grok-4.8 en defaults, main, skillverif."],
        )


if __name__ == "__main__":
    unittest.main()
