"""The installed tools stay executable by the user's own (filtered) token.

``test_configuration.py`` needs the Hermes runtime; this file imports only the
script, so the ACL decision runs on any machine.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "configure_team", ROOT / "scripts/configure_team.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
USER = module.WindowsUser(SID, ["Gon\\ehven", "WORKGROUP\\ehven"])
PYTHON_DIR = (
    "C:\\Users\\ehven\\AppData\\Local\\hermes\\tools\\python-3.14.7+20260901-win32-x64"
)
#: Real ``icacls`` output of the python tool after the elevated Hermes install
#: (2026-10-15): the gateway task got "Access is denied" on python.exe.
PROTECTED = (
    f"{PYTHON_DIR} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
    + " " * 75
    + "BUILTIN\\Administrators:(OI)(CI)(F)\n"
    + " " * 75
    + "OWNER RIGHTS:(OI)(CI)(F)\n"
    + "\n"
    + "Successfully processed 1 files; Failed processing 0 files\n"
)
#: The parent ``tools`` directory, inherited from the profile: fine as it was.
INHERITED = (
    "C:\\Users\\ehven\\AppData\\Local\\hermes\\tools Gon\\CodexSandboxUsers:(I)(OI)(CI)(M)\n"
    "                                            NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)\n"
    "                                            BUILTIN\\Administrators:(I)(OI)(CI)(F)\n"
    "                                            Gon\\ehven:(I)(OI)(CI)(F)\n"
    "\n"
    "Successfully processed 1 files; Failed processing 0 files\n"
)


def with_entry(entry):
    return PROTECTED.replace("\n\nSuccessfully", f"\n{' ' * 75}{entry}\n\nSuccessfully")


def completed(args, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


class AclDecisionTests(unittest.TestCase):
    def test_the_protected_install_acl_lacks_the_user(self):
        aces = module.parse_icacls(PROTECTED)

        self.assertEqual(
            aces,
            [
                module.Ace(
                    f"{PYTHON_DIR} NT AUTHORITY\\SYSTEM", frozenset({"OI", "CI", "F"})
                ),
                module.Ace("BUILTIN\\Administrators", frozenset({"OI", "CI", "F"})),
                module.Ace("OWNER RIGHTS", frozenset({"OI", "CI", "F"})),
            ],
        )
        self.assertFalse(module.user_can_execute(aces, USER))

    def test_the_live_grant_by_sid_makes_the_tool_executable(self):
        after = with_entry(f"*{SID}:(OI)(CI)(RX)")

        self.assertTrue(module.user_can_execute(module.parse_icacls(after), USER))

    def test_a_grant_by_account_name_counts_in_any_of_its_forms(self):
        for entry in (
            "Gon\\ehven:(OI)(CI)(RX)",
            "WORKGROUP\\ehven:(OI)(CI)(RX)",
            f"{SID}:(OI)(CI)(F)",
        ):
            with self.subTest(entry=entry):
                aces = module.parse_icacls(with_entry(entry))
                self.assertTrue(module.user_can_execute(aces, USER))

    def test_the_inherited_parent_acl_is_enough(self):
        aces = module.parse_icacls(INHERITED)

        self.assertTrue(module.user_can_execute(aces, USER))

    def test_public_principals_with_execute_count(self):
        for entry in (
            "BUILTIN\\Users:(I)(OI)(CI)(RX)",
            "Everyone:(OI)(CI)(M)",
            "NT AUTHORITY\\Authenticated Users:(OI)(CI)(R,X)",
        ):
            with self.subTest(entry=entry):
                aces = module.parse_icacls(with_entry(entry))
                self.assertTrue(module.user_can_execute(aces, USER))

    def test_other_accounts_denies_and_narrow_grants_do_not_count(self):
        for entry in (
            "Gon\\CodexSandboxUsers:(OI)(CI)(F)",
            "Gon\\ehven:(DENY)(OI)(CI)(RX)",
            "Gon\\ehven:(RX)",
            "Gon\\ehven:(OI)(CI)(NP)(RX)",
            "Gon\\ehven:(OI)(CI)(R)",
            "Gon\\ehven:(OI)(CI)(W)",
        ):
            with self.subTest(entry=entry):
                aces = module.parse_icacls(with_entry(entry))
                self.assertFalse(module.user_can_execute(aces, USER))


class ToolsDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.temporary)
        self.home = Path(self.temporary.name)
        self.tools = self.home / "tools"
        for name in ("python-3.14.7+20260901-win32-x64", "git", "node"):
            (self.tools / name).mkdir(parents=True)
        (self.tools / "stray.txt").write_text("not a tool", encoding="utf-8")
        self.calls = []

    def fake_run(self, *args):
        self.calls.append(list(args))
        if args[0] == "whoami":
            return completed(args, f'"Gon\\ehven","{SID}"\n')
        if args[0] == "icacls" and args[2:3] == ("/grant",):
            return completed(args, "processed file: " + args[1] + "\n")
        if args[0] == "icacls":
            name = Path(args[1]).name
            if name.startswith("python"):
                return completed(args, PROTECTED)
            if name == "git":
                return completed(args, "", 5, args[1] + ": Access is denied.\n")
            return completed(args, INHERITED)
        raise AssertionError(args)

    def test_only_tools_the_user_cannot_execute_are_reported(self):
        lacking = module.tools_without_user_access(self.tools, USER, self.fake_run)

        self.assertEqual(
            lacking,
            [self.tools / "git", self.tools / "python-3.14.7+20260901-win32-x64"],
        )
        self.assertEqual(
            self.calls,
            [
                ["icacls", str(self.tools / "git")],
                ["icacls", str(self.tools / "node")],
                ["icacls", str(self.tools / "python-3.14.7+20260901-win32-x64")],
            ],
        )

    def test_the_grant_is_by_sid_recursive_and_read_execute_only(self):
        module.grant_user_execute(self.tools / "git", USER, self.fake_run)

        self.assertEqual(
            self.calls,
            [
                [
                    "icacls",
                    str(self.tools / "git"),
                    "/grant",
                    f"*{SID}:(OI)(CI)RX",
                    "/T",
                    "/C",
                    "/Q",
                ]
            ],
        )

    def test_a_refused_grant_is_an_error_naming_the_directory(self):
        def refused(*args):
            return completed(args, "", 5, "Access is denied.\n")

        with self.assertRaisesRegex(ValueError, r"git.*elevated.*Access is denied"):
            module.grant_user_execute(self.tools / "git", USER, refused)

    def test_the_current_user_is_read_from_whoami_and_the_environment(self):
        with mock.patch.dict(
            os.environ, {"COMPUTERNAME": "GON", "USERDOMAIN": "WORKGROUP"}
        ):
            user = module.current_windows_user(self.fake_run)

        self.assertEqual(
            user,
            module.WindowsUser(SID, ["GON\\ehven", "Gon\\ehven", "WORKGROUP\\ehven"]),
        )
        self.assertEqual(self.calls, [["whoami", "/user", "/fo", "csv", "/nh"]])

    def test_check_fails_naming_every_tool_the_user_cannot_execute(self):
        with (
            mock.patch.object(module.os, "name", "nt"),
            self.assertRaisesRegex(ValueError, "Hermes_Gateway") as raised,
        ):
            module.ensure_tools_executable(
                self.home, check_only=True, run=self.fake_run
            )

        message = str(raised.exception)
        self.assertIn(str(self.tools / "git"), message)
        self.assertIn(str(self.tools / "python-3.14.7+20260901-win32-x64"), message)
        self.assertNotIn(str(self.tools / "node"), message)
        self.assertNotIn("/grant", str(self.calls))

    def test_apply_grants_each_lacking_tool_and_nothing_else(self):
        with mock.patch.object(module.os, "name", "nt"):
            granted = module.ensure_tools_executable(
                self.home, check_only=False, run=self.fake_run
            )

        self.assertEqual(
            granted,
            [self.tools / "git", self.tools / "python-3.14.7+20260901-win32-x64"],
        )
        grants = [call for call in self.calls if "/grant" in call]
        self.assertEqual(
            grants,
            [
                [
                    "icacls",
                    str(self.tools / name),
                    "/grant",
                    f"*{SID}:(OI)(CI)RX",
                    "/T",
                    "/C",
                    "/Q",
                ]
                for name in ("git", "python-3.14.7+20260901-win32-x64")
            ],
        )

    def test_outside_windows_nothing_runs(self):
        with mock.patch.object(module.os, "name", "posix"):
            granted = module.ensure_tools_executable(
                self.home, check_only=True, run=self.fake_run
            )

        self.assertEqual(granted, [])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
