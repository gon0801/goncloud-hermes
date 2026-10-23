import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import claw_parche  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402
import test_claw_autoupdate as base  # noqa: E402

CONFIG = {
    "agents": {
        "defaults": {
            "model": {"primary": "xai/grok-4.7", "fallbacks": ["zai/glm-5.3-flash"]}
        },
        "entries": {"main": {"model": {"primary": "xai/grok-4.7"}}},
    },
    "session": {"reset": {"idleMinutes": 60, "mode": "idle"}},
    "plugins": {"entries": {"memory-core": {"config": {"dreaming": {"x": 1}}}}},
}


def setUpModule():
    base.setUpModule()


def tearDownModule():
    base.tearDownModule()


def claw_gateway(fake, run):
    return claw_parche.Gateway(
        "openclaw",
        run,
        config_path=fake.path,
        sleep=lambda seconds: setattr(fake, "now", fake.now + seconds),
        clock=lambda: fake.now,
    )


class ParcheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.dir)
        self.respaldos = Path(self.dir.name) / "respaldos"

    def go(self, fake, *args):
        out = io.StringIO()
        gateway = fake.gateway()
        argv = [*args, "--respaldos", str(self.respaldos), "--config", str(fake.path)]
        original = claw_parche.Gateway
        claw_parche.Gateway = lambda *a, **k: gateway
        try:
            with contextlib.redirect_stdout(out):
                code = claw_parche.main(argv, run=fake)
        finally:
            claw_parche.Gateway = original
        return code, out.getvalue()

    def respaldos_escritos(self):
        return (
            sorted(self.respaldos.glob("*.pre.json")) if self.respaldos.exists() else []
        )

    def test_sesiones_writes_the_fragment_and_keeps_the_rest(self):
        fake = base.FakeGateway(CONFIG)

        code, _ = self.go(fake, "sesiones")

        self.assertEqual(code, 0)
        self.assertEqual(
            fake.config["session"],
            {
                "reset": {"idleMinutes": 240, "mode": "idle"},
                "maintenance": {"pruneAfter": "2d"},
            },
        )
        self.assertEqual(fake.config["agents"], CONFIG["agents"])
        self.assertEqual(fake.calls.count("patch"), 1)
        self.assertEqual(fake.calls.count("dry-run"), 1)
        self.assertEqual(fake.unguarded_patches, 0)
        self.assertEqual(fake.resumed, fake.acquired)

    def test_dry_run_writes_nothing(self):
        fake = base.FakeGateway(CONFIG)

        code, _ = self.go(fake, "sesiones", "--dry-run")

        self.assertEqual(code, 0)
        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(fake.calls.count("patch"), 0)
        self.assertEqual(fake.calls.count("dry-run"), 1)
        self.assertEqual(self.respaldos_escritos(), [])
        self.assertEqual(fake.resumed, fake.acquired)

    def test_an_objective_already_applied_writes_nothing(self):
        applied = claw_parche.overlay(CONFIG, claw_parche.OBJETIVOS["dreaming"]())
        fake = base.FakeGateway(applied)

        code, out = self.go(fake, "dreaming")

        self.assertEqual(code, 0)
        self.assertIn("ya esta asi", out)
        self.assertEqual(fake.config, applied)
        self.assertEqual(fake.calls.count("patch"), 0)
        self.assertEqual(self.respaldos_escritos(), [])

    def test_revierte_leaves_the_config_as_it_was(self):
        fake = base.FakeGateway(CONFIG)
        self.go(fake, "dreaming")
        self.assertNotEqual(fake.config, CONFIG)
        (respaldo,) = self.respaldos_escritos()

        code, _ = self.go(fake, "--revierte", str(respaldo))

        self.assertEqual(code, 0)
        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(fake.unguarded_patches, 0)

    def test_revierte_restores_an_array_the_patch_shortened(self):
        fake = base.FakeGateway(CONFIG)
        self.go(fake, "defaults-v6")
        self.assertNotEqual(
            fake.config["agents"]["defaults"], CONFIG["agents"]["defaults"]
        )
        (respaldo,) = self.respaldos_escritos()

        code, _ = self.go(fake, "--revierte", str(respaldo))

        self.assertEqual(code, 0)
        self.assertEqual(fake.config, CONFIG)

    def test_the_backup_holds_only_the_part_that_changes(self):
        fake = base.FakeGateway(CONFIG)

        self.go(fake, "sesiones")

        (respaldo,) = self.respaldos_escritos()
        self.assertRegex(respaldo.name, r"^sesiones-\d{8}-\d{6}\.pre\.json$")
        doc = json.loads(respaldo.read_text(encoding="utf-8"))
        self.assertEqual(
            doc["patch"],
            {"session": {"reset": {"idleMinutes": 60}, "maintenance": None}},
        )

    def test_without_a_ready_pause_nothing_is_written(self):
        fake = base.FakeGateway(CONFIG, busy_polls=10**6)

        code, out = self.go(fake, "sesiones")

        self.assertEqual(code, 1)
        self.assertIn("no escribi nada", out)
        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(fake.calls.count("patch"), 0)
        self.assertEqual(fake.calls.count("dry-run"), 0)
        self.assertEqual(self.respaldos_escritos(), [])

    def test_a_refused_dry_run_writes_nothing_and_resumes(self):
        fake = base.FakeGateway(CONFIG)
        real = base.FakeGateway.__call__

        def refusing(argv, timeout):
            if fake.kind(argv) == "dry-run":
                return base.Ran(1, "", "invalid")
            return real(fake, argv, timeout)

        fake.gateway = lambda: claw_gateway(fake, refusing)

        code, _ = self.go(fake, "sesiones")

        self.assertEqual(code, 1)
        self.assertEqual(fake.config, CONFIG)
        self.assertEqual(fake.resumed, fake.acquired)
        self.assertEqual(self.respaldos_escritos(), [])

    def test_defaults_v6_follows_the_table(self):
        fake = base.FakeGateway(CONFIG)
        source = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )
        row = source["profile_inheritance"]["default"]
        primary = source["chains"][row][0].split(" (")[0]

        self.go(fake, "defaults-v6")

        self.assertEqual(fake.config["agents"]["defaults"]["model"]["primary"], primary)

    def test_an_applied_objective_does_not_wait_for_a_busy_gateway(self):
        applied = claw_parche.overlay(CONFIG, claw_parche.OBJETIVOS["sesiones"]())
        fake = base.FakeGateway(applied, busy_polls=10**6)

        code, out = self.go(fake, "sesiones")

        self.assertEqual(code, 0)
        self.assertIn("ya esta asi", out)

    def test_dreaming_sets_the_model_and_the_override(self):
        fake = base.FakeGateway(CONFIG)

        self.go(fake, "dreaming")

        entry = fake.config["plugins"]["entries"]["memory-core"]
        self.assertEqual(entry["config"]["dreaming"]["x"], 1)
        self.assertEqual(
            entry["config"]["dreaming"]["model"], "anthropic/claude-sonnet-5-5"
        )
        self.assertIs(entry["subagent"]["allowModelOverride"], True)


if __name__ == "__main__":
    unittest.main()
