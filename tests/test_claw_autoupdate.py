import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import claw_autoupdate  # noqa: E402
from model_lines import Promotion, Ran  # noqa: E402

CONFIG_DIR = None


def setUpModule():
    global CONFIG_DIR
    CONFIG_DIR = tempfile.TemporaryDirectory(prefix="claw-config-")


def tearDownModule():
    CONFIG_DIR.cleanup()


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


def busy_reply():
    """``gateway suspend --wait 0 --json`` while a run is in flight (live, 2026-10-15)."""
    return Ran(
        1,
        json.dumps(
            {
                "status": "busy",
                "reason": "active-work",
                "retryAfterMs": 20000,
                "activeCount": 9,
                "blockers": [
                    {
                        "kind": "embedded-run",
                        "count": 1,
                        "message": "1 active embedded run(s)",
                    }
                ],
                "writeCustody": [],
                "requestId": "cli-1",
            }
        ),
        "",
    )


def not_yet(status, **fields):
    """A ``gateway suspend --json`` answer that grants nothing to this request."""
    return Ran(1, json.dumps({"status": status, **fields, "requestId": "r"}), "")


#: How the CLI relays the gateway's conflict RPC error (per its source): no JSON.
CONFLICT_ERROR = Ran(
    1,
    "",
    "Gateway suspend failed: Error: another gateway suspension is already prepared",
)
TIMED_OUT = Ran(1, "", "Gateway suspend failed: Error: gateway timeout after 30000ms")
SUSPEND_TTL = 120


class FakeGateway:
    """A live config file that config patch really changes; model calls are scripted.

    As on 2026.9.7, config patch writes the file, and refuses an empty patch
    and dropping an array element whose path is not a --replace-path.
    ``gateway suspend`` is busy for ``busy_polls`` polls (and for good once
    ``busy_from_patch`` patches landed), then grants a window; while one is
    held, model calls and ``gateway call config.get`` are refused as the
    gateway refuses new work. As in the gateway, a lease lasts SUSPEND_TTL s
    unless resumed, the same request id gets the lease it holds back, and
    another one gets a conflict. ``suspend_script`` overrides the next asks:
    a Ran is answered as is (granting nothing), ``"timeout"`` reaches the
    gateway but the CLI gives up before its answer. ``foreign_until`` is a
    lease someone else holds until then.
    """

    def __init__(
        self,
        config,
        answers=None,
        busy_polls=0,
        busy_from_patch=None,
        explode_on_patch=None,
        lost_patches=(),
        patch_results=None,
        suspend_script=(),
        foreign_until=None,
    ):
        handle, name = tempfile.mkstemp(suffix=".json", dir=CONFIG_DIR.name)
        os.close(handle)
        self.path = Path(name)
        self.write(config)
        self.answers = {
            "infer xai/grok-4.8": infer("OK"),
            "agent skillverif probe": turn("PONG", "claude-cli"),
            "agent skillverif verify": turn("PONG", "xai"),
            **(answers or {}),
        }
        self.busy_polls = busy_polls
        self.busy_from_patch = busy_from_patch
        self.explode_on_patch = explode_on_patch
        self.lease = (
            ("someone else", "x1", foreign_until - SUSPEND_TTL)
            if (foreign_until is not None)
            else None
        )
        self.suspend_script = list(suspend_script)
        self.acquired = []
        self.resumed = []
        self.expired = []
        self.waits = []
        self.asked_at = []
        self.requests = []
        self.answered = []
        self.timeouts = []
        self.unguarded_patches = 0
        self.sent = []
        self.calls = []
        self.now = 0.0
        self.lost_patches = lost_patches
        self.patch_results = patch_results or {}
        self.patches = 0

    @property
    def held(self):
        return self.lease[1] if self.lease else None

    @property
    def config(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, config):
        self.path.write_text(json.dumps(config), encoding="utf-8")

    def kind(self, argv):
        if argv[1:3] == ["gateway", "suspend"]:
            return "suspend"
        if argv[1:3] == ["gateway", "resume"]:
            return "resume"
        if argv[1:4] == ["gateway", "call", "config.get"]:
            return "config.get"
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

    def prepare(self, request):
        if self.lease:
            if self.lease[0] != request:
                return CONFLICT_ERROR
            doc = {"status": "ready", "suspensionId": self.held, "requestId": request}
            return Ran(0, json.dumps(doc), "")
        if self.busy_polls or (
            self.busy_from_patch is not None and self.patches >= self.busy_from_patch
        ):
            self.busy_polls = max(0, self.busy_polls - 1)
            return busy_reply()
        self.lease = (request, f"s{len(self.acquired) + 1}", self.now)
        self.acquired.append(self.held)
        return self.prepare(request)

    def suspend(self, argv):
        self.waits.append(argv[argv.index("--wait") + 1])
        self.asked_at.append(self.now)
        request = argv[argv.index("--request-id") + 1]
        self.requests.append(request)
        step = self.suspend_script.pop(0) if self.suspend_script else None
        reply = step if isinstance(step, Ran) else self.prepare(request)
        if step == "timeout":
            reply = TIMED_OUT
        self.answered.append(reply.out.startswith("{"))
        return reply

    def __call__(self, argv, timeout):
        kind = self.kind(argv)
        self.calls.append(kind)
        if self.lease and self.now >= self.lease[2] + SUSPEND_TTL:
            if self.held in self.acquired:
                self.expired.append(self.held)
            self.lease = None
        if kind in ("suspend", "resume"):
            index = argv.index("--timeout") if "--timeout" in argv else None
            self.timeouts.append(None if index is None else argv[index + 1])
        if kind == "suspend":
            return self.suspend(argv)
        if kind == "message send":
            self.sent.append((argv[argv.index("-t") + 1], argv[argv.index("-m") + 1]))
        if kind == "resume":
            self.resumed.append(argv[3])
            if argv[3] == self.held:
                self.lease = None
            return Ran(0, "{}", "")
        if self.held and kind == "config.get":
            return Ran(1, "", "")
        if self.held and kind.startswith(("agent", "infer")):
            return Ran(
                1, json.dumps({"ok": False, "error": {"message": "suspended"}}), ""
            )
        if kind == "config.get":
            doc = {"path": str(self.path), "includedPaths": [], "parsed": self.config}
            return Ran(0, json.dumps(doc), "")
        if kind in ("dry-run", "patch"):
            patch, refused = self.refusal(argv)
            if refused:
                return refused
            if kind == "dry-run":
                return Ran(0, "{}", "")
            if self.explode_on_patch == self.patches + 1:
                raise RuntimeError("the CLI wrapper broke")
            self.patches += 1
            if self.held is None:
                self.unguarded_patches += 1
            if self.patches not in self.lost_patches:
                self.write(merged(self.config, patch))
            return self.patch_results.get(self.patches, Ran(0, "{}", ""))
        if kind in self.answers:
            return self.answers[kind]
        return Ran(0, "{}", "")

    def gateway(self):
        def sleep(seconds):
            self.now += seconds

        return claw_autoupdate.Gateway(
            "openclaw",
            self,
            config_path=self.path,
            sleep=sleep,
            clock=lambda: self.now,
        )


class RunnerTests(unittest.TestCase):
    def go(self, fake, promotion=GROK, unguarded=0):
        outcome = claw_autoupdate.promote(fake.gateway(), promotion)
        self.assertEqual(
            sorted(fake.resumed + fake.expired),
            fake.acquired,
            "every lease is resumed once, or expired because its id never came back",
        )
        self.assertEqual(fake.unguarded_patches, unguarded)
        self.assertEqual(set(fake.waits) - {"0"}, set(), "never suspend --wait N>0")
        self.assertEqual(set(fake.timeouts), {"30000"}, "never the CLI's 10 s")
        for i in range(1, len(fake.asked_at)):
            if fake.requests[i] == fake.requests[i - 1]:
                gap = fake.asked_at[i] - fake.asked_at[i - 1]
                self.assertTrue(
                    gap >= 300 or (gap == 0 and not fake.answered[i - 1]),
                    f"ask {i} came {gap} s after the last one",
                )
        return outcome

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

    def test_a_failed_patch_still_resumes_its_window(self):
        fake = FakeGateway(CONFIG, patch_results={1: Ran(1, "", "boom")})

        outcome = self.go(fake)

        self.assertEqual(
            (outcome.status, outcome.detail),
            ("no cambiado", "preparar: config patch: salida 1: boom"),
        )
        self.assertEqual(fake.acquired, ["s1", "s2"])
        self.assertEqual(fake.config, CONFIG)

    def test_a_crash_inside_a_window_still_resumes_it(self):
        fake = FakeGateway(CONFIG, explode_on_patch=1)

        outcome = self.go(fake)

        self.assertEqual(
            (outcome.status, outcome.detail),
            ("no cambiado", "RuntimeError: the CLI wrapper broke"),
        )
        self.assertEqual(fake.resumed, ["s1", "s2"])
        self.assertEqual(fake.config, CONFIG)

    def test_a_busy_gateway_is_asked_every_five_minutes_until_it_grants(self):
        fake = FakeGateway(CONFIG, busy_polls=2)

        outcome = self.go(fake)

        self.assertEqual(outcome, claw_autoupdate.Outcome(GROK, "cambiado"))
        self.assertEqual(fake.asked_at, [0, 300, 600, 600])
        self.assertEqual(fake.acquired, ["s1", "s2"])

    def test_an_ask_that_timed_out_but_was_granted_is_used_and_resumed_once(self):
        fake = FakeGateway(CONFIG, suspend_script=["timeout"])

        outcome = self.go(fake)

        self.assertEqual(outcome, claw_autoupdate.Outcome(GROK, "cambiado"))
        self.assertEqual(fake.asked_at, [0, 0, 0])
        self.assertEqual((fake.acquired, fake.resumed), (["s1", "s2"], ["s1", "s2"]))

    def test_a_lease_whose_id_never_came_back_is_left_to_expire(self):
        fake = FakeGateway(CONFIG, suspend_script=["timeout", "timeout"])

        outcome = self.go(fake)

        self.assertEqual(outcome, claw_autoupdate.Outcome(GROK, "cambiado"))
        self.assertEqual(fake.asked_at, [0, 0, 300, 300])
        self.assertEqual((fake.expired, fake.resumed), (["s1"], ["s2", "s3"]))

    def test_busy_draining_and_conflict_keep_waiting(self):
        for name, script, foreign_until, asked_at in [
            ("busy", [busy_reply()], None, [0, 300, 300]),
            (
                "draining",
                [not_yet("draining", retryAfterMs=20000)],
                None,
                [0, 300, 300],
            ),
            (
                "conflict",
                [not_yet("conflict", expiresAtMs=120000)],
                None,
                [0, 300, 300],
            ),
            ("conflict, as the CLI relays it", [], 200, [0, 0, 300, 300]),
        ]:
            with self.subTest(name):
                fake = FakeGateway(
                    CONFIG, suspend_script=script, foreign_until=foreign_until
                )

                outcome = self.go(fake)

                self.assertEqual(outcome, claw_autoupdate.Outcome(GROK, "cambiado"))
                self.assertEqual(fake.asked_at, asked_at)

    def test_one_request_id_serves_each_window_and_each_window_has_its_own(self):
        fake = FakeGateway(CONFIG, busy_polls=2)

        self.go(fake)

        self.assertEqual(len(fake.requests), 4)
        self.assertEqual(len(set(fake.requests[:3])), 1)
        self.assertNotEqual(fake.requests[3], fake.requests[0])
        for request in fake.requests:
            self.assertRegex(request, r"^claw-autoupdate-[0-9a-f-]{36}$")

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

        outcome = self.go(fake, unguarded=1)

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
        fake = FakeGateway(CONFIG, busy_polls=10**9)

        outcome = self.go(fake)

        self.assertNotIn("patch", fake.calls)
        self.assertEqual(fake.acquired, [])
        self.assertEqual(
            (outcome.status, outcome.detail),
            ("pendiente", "no hubo un momento sin corridas"),
        )
        # The gateway hints 20 s; each ask closes its admission, so 300 s it is.
        self.assertEqual(fake.asked_at, list(range(0, 2401, 300)))


class ConfigReadTests(unittest.TestCase):
    def test_the_config_is_read_from_its_file_inside_an_open_window(self):
        fake = FakeGateway(CONFIG)
        gateway = fake.gateway()
        staged = claw_autoupdate.staged(CONFIG, "xai/grok-4.7", "xai/grok-4.8")

        with gateway.exclusive(0):
            before = gateway.config()
            fake.write(staged)
            after = gateway.config()

        self.assertEqual((before, after), (CONFIG, staged))
        self.assertEqual(fake.calls, ["suspend", "resume"])

    def test_a_file_caught_mid_write_is_read_again(self):
        fake = FakeGateway(CONFIG)
        whole = fake.path.read_text(encoding="utf-8")
        fake.path.write_text(whole[: len(whole) // 2], encoding="utf-8")
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            fake.path.write_text(whole, encoding="utf-8")

        gateway = claw_autoupdate.Gateway(
            "openclaw", fake, config_path=fake.path, sleep=sleep
        )

        self.assertEqual(gateway.config(), CONFIG)
        self.assertEqual(sleeps, [1])

    def test_a_file_that_never_parses_fails_without_saying_what_it_holds(self):
        fake = FakeGateway(CONFIG)
        fake.path.write_text('{"apiKey": "sk-secreto', encoding="utf-8")
        sleeps = []
        gateway = claw_autoupdate.Gateway(
            "openclaw", fake, config_path=fake.path, sleep=sleeps.append
        )

        with self.assertRaises(RuntimeError) as caught:
            gateway.config()

        self.assertEqual(
            str(caught.exception), f"{fake.path} no es JSON legible tras 4 lecturas"
        )
        self.assertEqual(sleeps, [1, 1, 1])


class HoldTests(unittest.TestCase):
    def test_a_reverted_switch_is_told_once_and_not_tried_again(self):
        fake = FakeGateway(
            CONFIG, {"agent skillverif verify": turn("Error de proveedor", "xai")}
        )
        said, patches, last = [], [], {}
        for _ in range(2):
            before = fake.calls.count("patch")
            lines, last = claw_autoupdate.autoupdate(
                fake.gateway(), {"xai": VIEW["xai"]}, last
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

        missing = Path(CONFIG_DIR.name) / "no-hay.json"
        gateway = claw_autoupdate.Gateway("openclaw", down, config_path=missing)
        held = {"revertido:xai/grok-4.7 -> xai/grok-4.8": "- Revertido: ..."}

        lines, remembered = claw_autoupdate.said(gateway, VIEW, held)
        again, _ = claw_autoupdate.said(gateway, VIEW, remembered)

        self.assertEqual(
            lines,
            [
                "- La autoactualizacion de claw fallo: FileNotFoundError: "
                f"[Errno 2] No such file or directory: '{missing}'"
            ],
        )
        self.assertEqual(again, [])
        self.assertEqual(
            remembered["revertido:xai/grok-4.7 -> xai/grok-4.8"], "- Revertido: ..."
        )


class DailyTests(unittest.TestCase):
    def run_daily(self, fake, view, state):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claw_autoupdate.state.json"
            if state is not None:
                path.write_text(json.dumps(state), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = claw_autoupdate.daily(
                        fake.gateway(), view, path, "6470689715", today="2026-10-15"
                    )
            after = json.loads(path.read_text(encoding="utf-8"))
        return code, after

    def test_a_switch_is_sent_to_the_owner_and_remembered(self):
        fake = FakeGateway(CONFIG)

        code, state = self.run_daily(fake, {"xai": VIEW["xai"]}, None)

        self.assertEqual(code, 0)
        self.assertEqual(
            fake.sent,
            [
                (
                    "6470689715",
                    "Claw: modelos 2026-10-15\n- Cambiado: xai/grok-4.7 -> "
                    "xai/grok-4.8 en defaults, main, skillverif.",
                )
            ],
        )
        self.assertEqual(state, {"notices": {}})

    def test_a_notice_telegram_did_not_take_is_not_remembered(self):
        fake = FakeGateway(
            CONFIG,
            {
                "infer xai/grok-4.8": Ran(1, "", "quota"),
                "message send": Ran(1, "", "telegram down"),
            },
        )

        code, state = self.run_daily(fake, {"xai": VIEW["xai"]}, {"notices": {}})

        self.assertEqual(code, 1)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(state, {"notices": {}})

    def test_a_notice_telegram_took_is_not_sent_again(self):
        fake = FakeGateway(CONFIG, {"infer xai/grok-4.8": Ran(1, "", "quota")})

        _, state = self.run_daily(fake, {"xai": VIEW["xai"]}, None)
        code, _ = self.run_daily(fake, {"xai": VIEW["xai"]}, state)

        self.assertEqual(code, 0)
        self.assertEqual(
            [text for _, text in fake.sent],
            [
                "Claw: modelos 2026-10-15\n- No cambiado: xai/grok-4.7 -> "
                "xai/grok-4.8 (sonda (high): salida 1: quota)."
            ],
        )

    def test_a_send_that_exits_zero_with_ok_false_failed(self):
        refused = Ran(
            0, json.dumps({"ok": False, "error": {"message": "chat not found"}}), ""
        )
        fake = FakeGateway(CONFIG, {"message send": refused})

        self.assertEqual(fake.gateway().send("1", "hola"), "chat not found")
        self.assertIsNone(FakeGateway(CONFIG).gateway().send("1", "hola"))

    def test_nothing_new_sends_nothing(self):
        fake = FakeGateway(CONFIG)

        code, state = self.run_daily(fake, {"xai": {"grok-4.7": {}}}, None)

        self.assertEqual((code, fake.sent, state), (0, [], {"notices": {}}))


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
