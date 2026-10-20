from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import goncloud_test_support  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402


class WindowsDeletePendingError(OSError):
    """Windows reports a nonempty directory while deletions are still pending."""

    def __init__(self, code: int) -> None:
        super().__init__("The directory is not empty")
        self.winerror = code


class FlakyDirectory:
    def __init__(self, failures: int, code: int = 145) -> None:
        self.remaining = failures
        self.code = code
        self.calls = 0

    def cleanup(self) -> None:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise WindowsDeletePendingError(self.code)


class CleanupTempDirectoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        name = patch.object(goncloud_test_support.os, "name", "nt")
        name.start()
        self.addCleanup(name.stop)
        sleep = patch.object(goncloud_test_support.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_transient_delete_pending_is_retried_until_it_succeeds(self) -> None:
        directory = FlakyDirectory(failures=3)

        cleanup_temp_directory(directory)

        self.assertEqual(directory.calls, 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_persistent_delete_pending_still_fails_the_test(self) -> None:
        directory = FlakyDirectory(failures=999)

        with self.assertRaises(OSError) as raised:
            cleanup_temp_directory(directory)

        self.assertEqual(raised.exception.winerror, 145)
        self.assertEqual(directory.calls, 20)

    def test_unrelated_windows_error_is_never_retried(self) -> None:
        directory = FlakyDirectory(failures=1, code=5)

        with self.assertRaises(OSError):
            cleanup_temp_directory(directory)

        self.assertEqual(directory.calls, 1)
        self.sleep.assert_not_called()

    def test_posix_failures_are_never_retried(self) -> None:
        directory = FlakyDirectory(failures=1)

        with patch.object(goncloud_test_support.os, "name", "posix"):
            with self.assertRaises(OSError):
                cleanup_temp_directory(directory)

        self.assertEqual(directory.calls, 1)
        self.sleep.assert_not_called()


def unguarded_temp_cleanups(source: str) -> list[str]:
    """Return registrations that hand a raw TemporaryDirectory to addCleanup.

    Those bypass `cleanup_temp_directory`, so Windows delete-pending windows
    turn into WinError 145 teardown errors that fail an otherwise green test.
    Cubre atributos (self.tmp), variables locales (temporal, otro) y el
    registro de clase (addClassCleanup): la forma cruda es la misma y el
    reintento se salta igual (F6 de #47).
    """
    pattern = re.compile(r"(?:addCleanup|addClassCleanup)\(\s*[\w.]+\.cleanup\s*\)")
    return pattern.findall(source)


class NoUnguardedTempCleanupTestCase(unittest.TestCase):
    def test_every_test_module_retries_windows_delete_pending(self) -> None:
        offenders = {}
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue
            found = unguarded_temp_cleanups(path.read_text(encoding="utf-8"))
            if found:
                offenders[path.name] = found

        self.assertEqual(
            offenders,
            {},
            "use addCleanup(cleanup_temp_directory, self.tmp) instead",
        )

    def test_the_guard_detects_a_raw_registration(self) -> None:
        source = "        self.addCleanup(self.tmp" + ".cleanup)\n"

        self.assertEqual(len(unguarded_temp_cleanups(source)), 1)

    def test_the_guard_detects_local_variables_and_class_registrations(self) -> None:
        # F6 de #47: la forma con variable local (temporal.cleanup) y la de
        # clase (addClassCleanup) saltan el mismo reintento de WinError 145.
        for source in (
            "        self.addCleanup(temporal" + ".cleanup)\n",
            "        cls.addClassCleanup(tmp" + ".cleanup)\n",
        ):
            with self.subTest(source=source.strip()):
                self.assertEqual(len(unguarded_temp_cleanups(source)), 1)
        self.assertEqual(
            unguarded_temp_cleanups(
                "        self.addCleanup(cleanup_temp_directory, temporal)\n"
            ),
            [],
        )

    def test_cleanup_temp_directory_receives_the_directory_not_the_method(self) -> None:
        """cleanup_temp_directory llama .cleanup() sobre lo que recibe.

        Si le pasan el metodo bound (cleanup_temp_directory, self.tmp.cleanup)
        el teardown revienta con AttributeError en cada prueba: asi entraron
        los 440 errores de la mitad sistema en #47 r3.
        """
        misuse = {}
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue
            found = re.compile(
                r"cleanup_temp_directory[,(]\s*[\w.]*\.cleanup\b"
            ).findall(path.read_text(encoding="utf-8"))
            if found:
                misuse[path.name] = found

        self.assertEqual(
            misuse,
            {},
            "pasa el TemporaryDirectory a cleanup_temp_directory, no su .cleanup",
        )


if __name__ == "__main__":
    unittest.main()
