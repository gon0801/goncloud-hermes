from __future__ import annotations

import sys
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from muse_bridge import (  # noqa: E402
    MuseBridgeError,
    build_muse_command,
    delegate_to_muse,
    parse_muse_jsonl,
    windows_path_to_wsl,
)


class MuseBridgeTests(unittest.TestCase):
    def test_windows_workspace_is_translated_without_shell_interpolation(self):
        self.assertEqual(
            windows_path_to_wsl(Path(r"C:\dev\client project")),
            "/mnt/c/dev/client project",
        )
        command = build_muse_command(
            Path(r"C:\dev\client project"),
            Path(r"C:\temp\muse prompt.txt"),
            max_steps=7,
        )
        self.assertEqual(command[:5], ["wsl.exe", "-d", "Ubuntu", "--exec", "/home/goncloud/.local/bin/muse"])
        self.assertEqual(command[-2:], ["--max-model-steps", "7"])
        self.assertIn("/mnt/c/temp/muse prompt.txt", command)
        self.assertNotIn("bash", command)

    def test_parser_returns_terminal_text_model_and_session(self):
        output = "\n".join(
            [
                '{"stream":{"kind":"session","id":"session-1"},"payload_type":"run.model.configured","payload":{"model_id":"muse-spark-1.3-contributor"}}',
                '{"stream":{"kind":"session","id":"session-1"},"payload_type":"run.terminal.completed","payload":{"terminal":"completed","text":"finished"}}',
            ]
        )

        self.assertEqual(
            parse_muse_jsonl(output),
            {
                "session_id": "session-1",
                "model": "muse-spark-1.3-contributor",
                "terminal": "completed",
                "text": "finished",
            },
        )

    def test_parser_rejects_output_without_terminal_record(self):
        with self.assertRaisesRegex(MuseBridgeError, "terminal record"):
            parse_muse_jsonl('{"payload_type":"run.lifecycle.started"}')

    def test_invalid_workspace_and_step_limit_are_rejected(self):
        with self.assertRaisesRegex(MuseBridgeError, "absolute Windows path"):
            windows_path_to_wsl(Path("relative"))
        with self.assertRaisesRegex(MuseBridgeError, "max_steps"):
            build_muse_command(
                Path(r"C:\dev\repo"), Path(r"C:\temp\prompt.txt"), max_steps=0
            )

    def test_delegate_sends_arbitrary_prompt_on_stdin_not_command_line(self):
        observed = {}

        def fake_runner(command, **kwargs):
            prompt_path = Path(
                command[command.index("--prompt-file") + 1]
                .replace("/mnt/c/", "C:/")
            )
            observed.update(
                command=command,
                kwargs=kwargs,
                prompt_path=prompt_path,
                prompt_text=prompt_path.read_text(encoding="utf-8"),
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"stream":{"kind":"session","id":"s1"},"payload_type":"run.terminal.completed","payload":{"terminal":"completed","text":"done"}}\n',
                stderr="",
            )

        prompt = 'review $(danger) "quoted" and `literal`'
        result = delegate_to_muse(
            prompt,
            Path(r"C:\dev\repo"),
            actor_role="implementer",
            max_steps=3,
            runner=fake_runner,
        )

        self.assertEqual(result["text"], "done")
        self.assertEqual(result["actor_role"], "implementer")
        self.assertEqual(observed["prompt_text"], prompt)
        self.assertFalse(observed["prompt_path"].exists())
        self.assertNotIn(prompt, observed["command"])
        self.assertFalse(observed["kwargs"]["shell"])

    def test_plugin_manifest_advertises_muse_delegation_tool(self):
        manifest = yaml.safe_load(
            (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        )

        self.assertIn("goncloud_muse_delegate", manifest["provides_tools"])


if __name__ == "__main__":
    unittest.main()
