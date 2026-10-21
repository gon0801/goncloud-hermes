"""The documented unittest discovery command must load every local module."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DiscoveryTests(unittest.TestCase):
    def test_fresh_process_discovery_has_no_loader_errors(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import unittest; loader = unittest.TestLoader(); "
                "suite = loader.discover('tests'); "
                "print('\\n'.join(loader.errors)); "
                "raise SystemExit(bool(loader.errors) or suite.countTestCases() == 0)",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_discovered_modules_execute_with_local_fixture_imports(self):
        # Selecciona los casos de los modos pesados de fixture tras un
        # discover en proceso fresco, pero NO los ejecuta aqui dentro: esos
        # mismos casos corren en la bateria (mitades 1 y 2), y una segunda
        # corrida anidada los solapaba y heredaba sus rojos de plataforma
        # (el log de CI #47 mostro "Ran 210 tests" dentro de "Ran 840").
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent("""
                    import unittest

                    loader = unittest.TestLoader()
                    suite = loader.discover('tests')
                    if loader.errors:
                        raise AssertionError('\\n'.join(loader.errors))
                    modules = {
                        'test_subscription_configuration',
                        'test_subscription_tools',
                    }

                    def cases(node):
                        if isinstance(node, unittest.TestSuite):
                            for child in node:
                                yield from cases(child)
                        elif type(node).__module__ in modules:
                            yield node

                    selected = list(cases(suite))
                    assert {type(case).__module__ for case in selected} == modules
                    assert any(type(case).__name__ == 'LiveEvidenceTests' for case in selected)
                    raise SystemExit(0)
                """),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
