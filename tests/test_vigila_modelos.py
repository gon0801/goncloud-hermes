import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vigila_modelos as module  # noqa: E402

TABLA = {
    "chains": {
        "dirigir": [
            "anthropic/claude-opus-5-5 (high)",
            "anthropic/claude-sonnet-5-5 (high)",
            "zai/glm-5.3 (high)",
            "kimi/k3 (high)",
        ],
        "simples": [
            "anthropic/claude-haiku-5-5 (low)",
            "opencode-go/deepseek-v4.1-flash (off)",
            "zai/glm-5.3-flash (low)",
            "xai/grok-4.7 (low)",
        ],
    },
    "profile_inheritance": {"claw": "dirigir", "mail": "simples"},
}
RUTEO_CLAW = {"agents": {"main": {"row": "dirigir"}, "mail": {"row": "simples"}}}


def claw_en_vivo(main_fallbacks=None, opus="high"):
    return {
        "agents": {
            "defaults": {
                "models": {
                    "opencode-go-2/deepseek-v4.1-flash": {"params": {"thinking": "off"}}
                }
            },
            "entries": {
                "main": {
                    "model": {
                        "primary": "anthropic/claude-opus-5-5",
                        "fallbacks": main_fallbacks
                        or ["anthropic/claude-sonnet-5-5", "zai/glm-5.3", "kimi/k3"],
                    },
                    "models": {
                        "anthropic/claude-opus-5-5": {"params": {"thinking": opus}},
                        "anthropic/claude-sonnet-5-5": {"params": {"thinking": "high"}},
                        "zai/glm-5.3": {"params": {"thinking": "high"}},
                        "kimi/k3": {"params": {"thinking": "high"}},
                    },
                },
                "mail": {
                    "model": {
                        "primary": "anthropic/claude-haiku-5-5",
                        "fallbacks": [
                            "opencode-go-2/deepseek-v4.1-flash",
                            "zai/glm-5.3-flash",
                            "xai/grok-4.7",
                        ],
                    },
                    "models": {
                        "anthropic/claude-haiku-5-5": {"params": {"thinking": "low"}},
                        "zai/glm-5.3-flash": {"params": {"thinking": "low"}},
                        "xai/grok-4.7": {"params": {"thinking": "low"}},
                    },
                },
                "codex": {"model": None},
            },
        }
    }


def hermes_en_vivo(opus="high"):
    return {
        "claw": {
            "model": {"provider": "anthropic", "default": "claude-opus-5-5"},
            "fallback_providers": [
                {"provider": "anthropic", "model": "claude-sonnet-5-5"},
                {"provider": "zai", "model": "glm-5.3"},
                {"provider": "kimi-coding", "model": "k3"},
            ],
            "agent": {
                "reasoning_overrides": {
                    "claude-opus-5-5": opus,
                    "claude-sonnet-5-5": "high",
                    "glm-5.3": "high",
                    "k3": "high",
                }
            },
        },
        "mail": {
            "model": {"provider": "anthropic", "default": "claude-haiku-5-5"},
            "fallback_providers": [
                {"provider": "opencode-go", "model": "deepseek-v4.1-flash"},
                {"provider": "zai", "model": "glm-5.3-flash"},
                {"provider": "xai-oauth", "model": "grok-4.7"},
            ],
            "agent": {
                "reasoning_overrides": {
                    "claude-haiku-5-5": "low",
                    "glm-5.3-flash": "low",
                    "grok-4.7": "low",
                }
            },
        },
    }


class VigilaModelosTests(unittest.TestCase):
    def setUp(self):
        self.claw, self.hermes = module.esperado(TABLA, RUTEO_CLAW)

    def test_las_dos_maquinas_iguales_a_la_tabla_no_dan_diferencias(self):
        self.assertEqual(
            module.diferencias(self.claw, module.vivo_claw(claw_en_vivo())), []
        )
        self.assertEqual(
            module.diferencias(self.hermes, module.vivo_hermes(hermes_en_vivo())), []
        )

    def test_el_orden_de_los_respaldos_cuenta(self):
        # 2026-10-18: claw volvió al orden viejo, con GLM al final, y nadie se enteró en cuatro días.
        vivo = module.vivo_claw(
            claw_en_vivo(
                main_fallbacks=["anthropic/claude-sonnet-5-5", "kimi/k3", "zai/glm-5.3"]
            )
        )
        self.assertEqual(module.diferencias(self.claw, vivo), ["main"])

    def test_el_esfuerzo_cuenta(self):
        self.assertEqual(
            module.diferencias(
                self.hermes, module.vivo_hermes(hermes_en_vivo(opus="medium"))
            ),
            ["claw"],
        )
        self.assertEqual(
            module.diferencias(
                self.claw, module.vivo_claw(claw_en_vivo(opus="medium"))
            ),
            ["main"],
        )

    def test_un_agente_que_falta_es_una_diferencia(self):
        vivo = module.vivo_hermes(hermes_en_vivo())
        del vivo["mail"]
        self.assertEqual(module.diferencias(self.hermes, vivo), ["mail"])

    def test_el_aviso_es_corto_y_nombra_a_los_agentes(self):
        texto = module.texto_aviso(
            ["main", "verifier"], [f"p{i}" for i in range(9)], ["Hermes"]
        )
        self.assertLessEqual(len(texto.splitlines()), 8)
        self.assertIn("main, verifier", texto)
        self.assertIn("y 3 más", texto)
        self.assertIn("no pude revisar Hermes", texto)


if __name__ == "__main__":
    unittest.main()
