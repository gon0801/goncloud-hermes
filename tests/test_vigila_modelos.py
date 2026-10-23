import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import claw_models  # noqa: E402
import configure_team  # noqa: E402
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
    "profile_inheritance": {"claw": "dirigir", "mail": "simples", "default": "simples"},
}
RUTEO_CLAW = {"agents": {"main": {"row": "dirigir"}, "mail": {"row": "simples"}}}


def claw_en_vivo(main_fallbacks=None, opus="high"):
    return {
        "agents": {
            "defaults": {
                "model": {
                    "primary": "anthropic/claude-haiku-5-5",
                    "fallbacks": [
                        "opencode-go-2/deepseek-v4.1-flash",
                        "zai/glm-5.3-flash",
                        "xai/grok-4.7",
                    ],
                },
                "models": {
                    "opencode-go-2/deepseek-v4.1-flash": {"params": {"thinking": "off"}}
                },
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
    vivo = _hermes_en_vivo(opus)
    vivo["default"] = vivo["mail"]
    return vivo


def _hermes_en_vivo(opus):
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


def _json(nombre):
    return json.loads((ROOT / nombre).read_text(encoding="utf-8"))


class ConfigRealTests(unittest.TestCase):
    """La config de hoy: lo que la tabla del repo manda, sin claves ni máquinas."""

    def setUp(self):
        self.tabla = _json("model-routing.source.json")
        self.ruteo = _json("claw-routing.json")
        self.politica = _json("tareas-programadas.json")["modelo_propio"]
        self.director = _json("team.json")["director"]
        con_esfuerzo = {
            **self.ruteo,
            "agents": {
                a: {**d, "thinking": True} for a, d in self.ruteo["agents"].items()
            },
        }
        self.config = claw_models.desired_claw(self.tabla, con_esfuerzo)
        self.jobs = [
            {"name": k.split(":", 1)[1], "enabled": True, "payload": {"model": m}}
            for k, m in self.politica.items()
            if k.startswith("claw:")
        ]
        self.hermes = self._hermes()

    def _hermes(self):
        perfiles = {}
        for perfil, fila in self.tabla["profile_inheritance"].items():
            cadena = [claw_models.split_label(e) for e in self.tabla["chains"][fila]]
            modelos = [m.split("/", 1)[1] for m, _ in cadena]
            perfiles[perfil] = {
                "model": {"default": modelos[0]},
                "fallback_providers": [{"model": m} for m in modelos[1:]],
                "agent": {
                    "reasoning_overrides": {
                        m: e for m, (_, e) in zip(modelos, cadena) if e
                    }
                },
                "cron": [],
            }
        perfiles[self.director]["auxiliary"] = copy.deepcopy(
            configure_team._DIRECTOR_VISION_AUXILIARY
        )
        for k, m in self.politica.items():
            if k.startswith("hermes:"):
                perfil, nombre = k.split(":", 1)[1].split("/", 1)
                perfiles[perfil]["cron"].append(
                    {"name": nombre, "enabled": True, "model": m}
                )
        return perfiles

    def corre(self, config=None, jobs=None, hermes=None):
        with tempfile.TemporaryDirectory() as avisos:
            codigo = module.main(
                ["--raiz", str(ROOT), "--aviso", avisos],
                vivo=lambda: (
                    (config or self.config, self.jobs if jobs is None else jobs),
                    hermes or self.hermes,
                    [],
                ),
            )
            return codigo, [
                p.read_text(encoding="utf-8") for p in Path(avisos).iterdir()
            ]

    def test_el_por_omision_sale_de_la_fila_default_de_la_tabla(self):
        self.assertEqual(
            self.config["agents"]["defaults"]["model"]["primary"],
            "anthropic/claude-haiku-5-5",
        )
        cadena = [m for m, _ in module.esperado(self.tabla, self.ruteo)[0]["defaults"]]
        self.assertEqual(cadena[0], "claude-haiku-5-5")
        self.assertEqual(len(cadena), 4)
        cambiada = copy.deepcopy(self.tabla)
        cambiada["profile_inheritance"]["default"] = "dirigir"
        nueva = claw_models.desired_claw(cambiada, self.ruteo)["agents"]["defaults"]
        self.assertEqual(nueva["model"]["primary"], "anthropic/claude-opus-5-5")

    def test_claw_llama_a_deepseek_por_la_segunda_cuenta(self):
        deepseek = "opencode-go-2/deepseek-v4.1-flash"
        defaults = self.config["agents"]["defaults"]["model"]
        self.assertEqual(defaults["primary"], "anthropic/claude-haiku-5-5")
        self.assertEqual(
            defaults["fallbacks"],
            [deepseek, "zai/glm-5.3-flash", "xai/grok-4.7"],
        )
        operativos = [
            a for a, d in self.ruteo["agents"].items() if d["row"] == "operativo"
        ]
        self.assertTrue(operativos)
        for agente in operativos:
            self.assertIn(
                deepseek, self.config["agents"]["entries"][agente]["model"]["fallbacks"]
            )

    def test_una_fila_con_deepseek_repetido_no_lleva_doble_sufijo(self):
        fila = [
            "opencode-go/deepseek-v4.1-flash (off)",
            "opencode-go/deepseek-v4.1-flash (off)",
            "xai/grok-4.7 (low)",
        ]
        agente = claw_models.desired_agent(fila, self.ruteo, thinking=False)
        ids = [agente["model"]["primary"], *agente["model"]["fallbacks"]]
        self.assertNotIn("opencode-go-2-2", "".join(ids))
        self.assertEqual(ids[0], "opencode-go-2/deepseek-v4.1-flash")

    def test_hermes_no_cambia_con_el_override_de_claw(self):
        sin = {**self.ruteo, "id_overrides": {}}
        self.assertEqual(
            module.esperado(self.tabla, self.ruteo)[1],
            module.esperado(self.tabla, sin)[1],
        )

    def test_todo_coincide_y_no_avisa(self):
        self.assertEqual(self.corre(), (0, []))

    def test_el_por_omision_en_opus_avisa(self):
        config = copy.deepcopy(self.config)
        config["agents"]["defaults"]["model"] = {
            "primary": "anthropic/claude-opus-5-5",
            "fallbacks": [],
        }
        codigo, avisos = self.corre(config=config)
        self.assertEqual(codigo, 1)
        self.assertIn("el modelo por omisión de claw", avisos[0])

    def test_un_cron_con_modelo_fuera_de_la_politica_avisa(self):
        jobs = copy.deepcopy(self.jobs)
        jobs[0]["payload"]["model"] = "anthropic/claude-opus-5-5"
        codigo, avisos = self.corre(jobs=jobs)
        self.assertEqual(codigo, 1)
        self.assertIn(f"la tarea {jobs[0]['name']}", avisos[0])

    def test_un_cron_de_hermes_fuera_de_la_politica_avisa(self):
        hermes = copy.deepcopy(self.hermes)
        job = next(j for c in hermes.values() for j in c["cron"])
        job["model"] = "claude-opus-5-5"
        codigo, avisos = self.corre(hermes=hermes)
        self.assertEqual(codigo, 1)
        self.assertIn(f"la tarea {job['name']}", avisos[0])

    def test_la_vision_terminando_en_opus_avisa(self):
        hermes = copy.deepcopy(self.hermes)
        hermes[self.director]["auxiliary"]["vision"]["fallback_chain"][-1] = {
            "provider": "anthropic",
            "model": "claude-opus-5-5",
        }
        codigo, avisos = self.corre(hermes=hermes)
        self.assertEqual(codigo, 1)
        self.assertIn("la visión del director", avisos[0])
        self.assertLessEqual(len(avisos[0].splitlines()), 8)

    def test_seco_imprime_y_no_encola(self):
        config = copy.deepcopy(self.config)
        del config["agents"]["defaults"]
        with tempfile.TemporaryDirectory() as avisos:
            codigo = module.main(
                ["--raiz", str(ROOT), "--aviso", avisos, "--seco"],
                vivo=lambda: ((config, self.jobs), self.hermes, []),
            )
            self.assertEqual((codigo, list(Path(avisos).iterdir())), (1, []))

    def test_sin_tablas_sale_con_2(self):
        with tempfile.TemporaryDirectory() as vacia:
            self.assertEqual(
                module.main(["--raiz", vacia], vivo=lambda: (None, None, [])), 2
            )


if __name__ == "__main__":
    unittest.main()
