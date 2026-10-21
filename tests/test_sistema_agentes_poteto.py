#!/usr/bin/env python3
"""Poteto fijado en H (I08, parte Mac): la copia contra el manifiesto de G, la
adaptación Hermes, el bloque de SOUL, la generación de managed.json, la
atestación de flota y la puerta de despacho sin poteto."""

import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

from conexiones import rastrear_conexiones  # noqa: E402

poteto = importlib.import_module("poteto")

POTETO = poteto.REPO / "shared/sistema-agentes/poteto"


def canonico(documento):
    return json.dumps(
        documento, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class CopiaFijada(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.manifiesto = json.loads(
            (POTETO / "poteto.v1.json").read_text(encoding="utf-8")
        )
        self.registro = json.loads((POTETO / "hermes.json").read_text(encoding="utf-8"))

    def test_cada_archivo_del_manifiesto_esta_en_la_copia_con_su_sha(self):
        excluidos = set(self.registro["excluidos"])
        for fila in self.manifiesto["files"]:
            if fila["path"] in excluidos:
                continue
            with self.subTest(path=fila["path"]):
                contenido = (POTETO / fila["path"]).read_bytes()
                self.assertEqual(hashlib_de(contenido), fila["sha256"])

    def test_los_excluidos_estan_documentados_y_no_publicables(self):
        self.assertEqual(
            set(self.registro["excluidos"]),
            {"skills/poteto-openclaw/SKILL.md"},
        )

    def test_el_manifiesto_tiene_el_digest_que_hermes_json_registra(self):
        recomputado = "sha256:" + hashlib_de(
            canonico({k: v for k, v in self.manifiesto.items() if k != "digest"})
        )
        self.assertEqual(self.manifiesto["digest"], recomputado)
        self.assertEqual(self.registro["manifest"]["digest"], recomputado)
        self.assertEqual(self.registro["generation"], self.manifiesto["generation"])

    def test_un_byte_cambiado_en_la_copia_se_detecta(self):
        with tempfile.TemporaryDirectory() as tmp:
            copia = Path(tmp) / "poteto"
            shutil.copytree(POTETO, copia)
            poteto.comprobar_copia(copia)
            ruta = copia / "skills/poteto-mode/SKILL.md"
            contenido = ruta.read_text(encoding="utf-8")
            victima = contenido.split()[0]
            ruta.write_text(
                contenido.replace(victima, victima + "x", 1), encoding="utf-8"
            )
            with self.assertRaises(poteto.CopiaRota):
                poteto.comprobar_copia(copia)

    def test_un_manifiesto_con_otro_digest_se_detecta(self):
        with tempfile.TemporaryDirectory() as tmp:
            copia = Path(tmp) / "poteto"
            shutil.copytree(POTETO, copia)
            manifiesto = json.loads(
                (copia / "poteto.v1.json").read_text(encoding="utf-8")
            )
            manifiesto["files"].append(
                {"path": "skills/falso/SKILL.md", "sha256": "0" * 64}
            )
            manifiesto.pop("digest")
            manifiesto["digest"] = "sha256:" + hashlib_de(canonico(manifiesto))
            (copia / "poteto.v1.json").write_text(
                json.dumps(manifiesto), encoding="utf-8"
            )
            with self.assertRaises(poteto.CopiaRota):
                poteto.comprobar_copia(copia)

    def test_el_bloque_cita_generacion_y_digest_y_cabe(self):
        bloque = (POTETO / "bloque-soul-hermes.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(bloque), 1500)
        self.assertIn(f"generacion={self.manifiesto['generation']}", bloque)
        self.assertIn(self.manifiesto["digest"], bloque)
        self.assertIn("poteto-mode", bloque)

    def test_la_adaptacion_hermes_esta_registrada_y_cubre_el_metodo(self):
        adaptacion = (POTETO / "adaptacion-hermes.md").read_text(encoding="utf-8")
        self.assertEqual(
            self.registro["adaptacion"],
            "sha256:" + hashlib_de(adaptacion.encode("utf-8")),
        )
        for obligatoria in ("poteto-mode", "playbook", "permisos", "read"):
            self.assertIn(obligatoria, adaptacion)

    def test_las_skills_publicadas_son_las_del_perfil_segun_g(self):
        publicadas = poteto.skills_a_publicar("ingenieria")
        self.assertIn("skills/poteto-mode/SKILL.md", publicadas)
        self.assertTrue(
            any("/principle-" in r for r in publicadas),
            "las principles viajan con el paquete",
        )
        for skill in ("architect", "how", "tdd"):
            self.assertTrue(
                any(r.startswith(f"skills/{skill}/") for r in publicadas),
                skill,
            )
        self.assertFalse(
            any(r.startswith("skills/why/") for r in publicadas),
            "una skill que el perfil no requiere no se publica",
        )
        propias = poteto.skills_a_publicar("reviewer")
        self.assertTrue(any(r.startswith("skills/blast-radius/") for r in propias))

    def test_todos_los_perfiles_de_team_json_estan_en_especialidades(self):
        # F10: un perfil de team.json ausente de ESPECIALIDADES publica
        # poteto-mode y principle-* pero ninguna skill de especialidad, sin
        # que quede dicho si es a propósito. G no trae perfiles.v1.json en el
        # espejo, así que la decisión es explícita: () cuenta como decisión.
        team = json.loads((REPO / "team.json").read_text(encoding="utf-8"))
        faltan = set(team["profiles"]) - set(poteto.ESPECIALIDADES)
        self.assertEqual(
            faltan,
            set(),
            "todo perfil de team.json necesita una entrada explícita en "
            "ESPECIALIDADES (aunque sea ())",
        )


def hashlib_de(datos: bytes) -> str:
    import hashlib

    return hashlib.sha256(datos).hexdigest()


class GeneracionDelDespliegue(unittest.TestCase):
    def test_un_despliegue_que_cambia_archivos_sube_generation(self):
        self.assertEqual(poteto.siguiente_generation(None, {"a": "1"}), 1)
        self.assertEqual(poteto.siguiente_generation({"generation": 3}, {"a": "2"}), 4)
        self.assertEqual(
            poteto.siguiente_generation(
                {"generation": 3, "files": {"a": "1"}}, {"a": "1"}
            ),
            3,
            "sin cambios no sube",
        )


class Sonda(unittest.TestCase):
    """La sonda de poteto por perfil: estructura verificada aquí; la tarea de
    comportamiento representativa la corre el coordinador en la PC."""

    def perfil_desplegado(self, tmp):
        home = Path(tmp)
        copia = home / "shared/sistema-agentes/poteto"
        shutil.copytree(POTETO, copia)
        bloque = (copia / "bloque-soul-hermes.md").read_text(encoding="utf-8")
        (home / "SOUL.md").write_text("alma local\n\n" + bloque, encoding="utf-8")
        for ruta in poteto.skills_a_publicar("codex"):
            origen = POTETO / ruta
            final = home / ruta
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(origen, final)
        # La adaptación de Hermes que configure_team publica por perfil.
        adaptacion = home / "skills/poteto-hermes/SKILL.md"
        adaptacion.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(POTETO / "adaptacion-hermes.md", adaptacion)
        (home / "goncloud-managed.json").write_text(
            json.dumps(
                {
                    "source_revision": "abc",
                    "generation": 2,
                    "poteto": {"digest": poteto.digest_de_manifiesto()},
                }
            ),
            encoding="utf-8",
        )
        return home

    def test_un_perfil_bien_desplegado_no_da_fallas(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            self.assertEqual(poteto.sonar(Path(tmp), "codex"), [])

    def test_un_soul_sin_el_bloque_da_falla(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            (Path(tmp) / "SOUL.md").write_text("alma local\n", encoding="utf-8")
            fallas = poteto.sonar(Path(tmp), "codex")
        self.assertTrue(any("SOUL" in f for f in fallas), fallas)

    def test_una_skill_que_no_coincide_da_falla(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            ruta = Path(tmp) / "skills/poteto-mode/SKILL.md"
            ruta.write_text(ruta.read_text(encoding="utf-8") + "x", encoding="utf-8")
            fallas = poteto.sonar(Path(tmp), "codex")
        self.assertTrue(any("skills" in f for f in fallas), fallas)

    def test_la_adaptacion_de_hermes_ausente_da_falla(self):
        # CR3: configure_team publica skills/poteto-hermes/SKILL.md por
        # perfil; sin ella el hijo pierde la adaptación aunque las skills de
        # la copia fijada estén intactas.
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            (Path(tmp) / "skills/poteto-hermes/SKILL.md").unlink()
            fallas = poteto.sonar(Path(tmp), "codex")
        self.assertTrue(
            any("adaptacion" in f or "adaptación" in f for f in fallas), fallas
        )

    def test_la_adaptacion_de_hermes_distinta_da_falla(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.perfil_desplegado(tmp)
            ruta = Path(tmp) / "skills/poteto-hermes/SKILL.md"
            ruta.write_text(
                ruta.read_text(encoding="utf-8") + "\nedicion local\n",
                encoding="utf-8",
            )
            fallas = poteto.sonar(Path(tmp), "codex")
        self.assertTrue(
            any("adaptacion" in f or "adaptación" in f for f in fallas), fallas
        )


if __name__ == "__main__":
    unittest.main()
