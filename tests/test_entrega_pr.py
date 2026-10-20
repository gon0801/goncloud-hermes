"""La publicacion de la entrega: push de la rama y PR por la API (H-entrega-pr).

Git real sin red — un origin pelado local y el worktree de la cadena — y
dobles para la credencial y la API: la entrega empuja la rama y abre el PR
con head/base correctos; un PR abierto de la rama se reutiliza; sin
credencial la rama queda subida y el motivo viaja; y el token jamas sale en
resultado, aviso ni motivo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
PLUGIN = RAIZ / "plugins/goncloud-team"
sys.path.insert(0, str(RAIZ / "tests"))
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

import entrega_pr  # noqa: E402

TOKEN = "ghp_Abc123Def456Ghi789"
URL_PR = "https://github.com/acme/repo/pull/9"


def git(*args, cwd):
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


class _TableroGit(unittest.TestCase):
    """Un origen pelado local y el worktree de la cadena t_x con un commit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.raiz = Path(self.tmp.name)
        self.origen = self.raiz / "origin.git"
        self.repo = self.raiz / "repo"
        self.worktree = self.raiz / "wt" / "repo-t_x"
        git("init", "--bare", "-b", "main", "origin.git", cwd=self.raiz)
        git("clone", "-q", "origin.git", "repo", cwd=self.raiz)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@ejemplo.invalid",
            "commit",
            "-qm",
            "base",
            cwd=self.repo,
        )
        git("push", "-q", "origin", "main", cwd=self.repo)
        # Un clon normal de un repo con contenido deja origin/HEAD apuntando
        # a la rama por defecto; aqui lo fijamos a mano porque nacio vacio.
        git("remote", "set-head", "origin", "-a", cwd=self.repo)
        git(
            "worktree",
            "add",
            "-q",
            "-b",
            "cadena/t_x",
            str(self.worktree),
            cwd=self.repo,
        )
        (self.worktree / "cambio.txt").write_text("cambio\n", encoding="utf-8")
        git("add", ".", cwd=self.worktree)
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@ejemplo.invalid",
            "commit",
            "-qm",
            "cambio de la cadena",
            cwd=self.worktree,
        )

    def api_falsa(self, respuestas):
        """Un doble de la API: (codigo, cuerpo) por (metodo, ruta sin query)."""
        hechas = []

        def falso(metodo, ruta, token, payload=None):
            hechas.append(
                {"metodo": metodo, "ruta": ruta, "token": token, "payload": payload}
            )
            return respuestas.get((metodo, ruta.split("?")[0]), (200, {}))

        return falso, hechas

    def publicar(self, api, **cambios):
        credencial = cambios.pop("credencial", TOKEN)
        with (
            mock.patch.object(entrega_pr, "_credencial", return_value=credencial),
            mock.patch.object(entrega_pr, "_api", api),
            mock.patch.object(
                entrega_pr, "_dueño_y_repo", return_value=("acme", "repo")
            ),
        ):
            return entrega_pr.publicar(
                "t_x",
                worktree=cambios.pop("worktree", self.worktree),
                summary=cambios.pop("summary", "panel arreglado"),
                **cambios,
            )

    def la_rama_esta_en_el_origen(self):
        hecho = subprocess.run(
            [
                "git",
                "-C",
                str(self.origen),
                "rev-parse",
                "--verify",
                "refs/heads/cadena/t_x",
            ],
            capture_output=True,
        )
        return hecho.returncode == 0


class PublicacionTests(_TableroGit):
    def test_la_entrega_empuja_la_rama_y_abre_el_pr(self):
        falso, hechas = self.api_falsa(
            {
                ("GET", "/repos/acme/repo"): (
                    200,
                    {"private": True, "default_branch": "main"},
                ),
                ("GET", "/repos/acme/repo/pulls"): (200, []),
                ("POST", "/repos/acme/repo/pulls"): (201, {"html_url": URL_PR}),
            }
        )

        resultado = self.publicar(falso)

        self.assertEqual(resultado, {"pull_request": URL_PR})
        # El push fue real: la rama de la cadena vive en el origen.
        self.assertTrue(self.la_rama_esta_en_el_origen())
        get, post = hechas[0], hechas[-1]
        self.assertEqual(get["metodo"], "GET")
        self.assertTrue(get["ruta"].startswith("/repos/acme/repo/pulls?"))
        self.assertIn("head=acme:cadena/t_x", get["ruta"])
        self.assertIn("state=open", get["ruta"])
        self.assertEqual(post["metodo"], "POST")
        self.assertEqual(post["payload"]["head"], "cadena/t_x")
        # La base sale del origin/HEAD del repo local: main, donde nacio.
        self.assertEqual(post["payload"]["base"], "main")
        # F5: titulo neutral; el repo es privado, asi que el resumen entra.
        self.assertEqual(post["payload"]["title"], "Cadena t_x: entrega")
        self.assertIn("panel arreglado", post["payload"]["body"])
        self.assertIn("Cadena t_x", post["payload"]["body"])
        self.assertIn("rama cadena/t_x", post["payload"]["body"])

    def test_la_entrega_con_repo_publico_no_publica_resumen_ni_instruccion(self):
        # F5: si el repo no es privado (o no se sabe), el cuerpo solo lleva
        # id de cadena y rama — jamas el resumen del trabajo ni la
        # instruccion, que son privados.
        falso, hechas = self.api_falsa(
            {
                ("GET", "/repos/acme/repo"): (
                    200,
                    {"private": False, "default_branch": "main"},
                ),
                ("GET", "/repos/acme/repo/pulls"): (200, []),
                ("POST", "/repos/acme/repo/pulls"): (201, {"html_url": URL_PR}),
            }
        )

        resultado = self.publicar(falso, summary="resumen privado del trabajo")

        self.assertEqual(resultado, {"pull_request": URL_PR})
        payload = hechas[-1]["payload"]
        self.assertNotIn("resumen privado", payload["body"])
        self.assertNotIn("Arregla el panel", payload["body"])
        self.assertIn("Cadena t_x", payload["body"])
        self.assertIn("rama cadena/t_x", payload["body"])

    def test_la_entrega_empezando_sin_repo_conocido_trata_todo_como_publico(self):
        # F5: si el GET del repo no responde, no se sabe -> publico.
        falso, hechas = self.api_falsa(
            {
                ("GET", "/repos/acme/repo/pulls"): (200, []),
                ("POST", "/repos/acme/repo/pulls"): (201, {"html_url": URL_PR}),
            }
        )

        resultado = self.publicar(falso, summary="resumen privado del trabajo")

        self.assertEqual(resultado, {"pull_request": URL_PR})
        self.assertNotIn("resumen privado", hechas[-1]["payload"]["body"])

    def test_la_base_guardada_en_el_contrato_gana_sobre_origin_head(self):
        # H-entrega-pr r2: la rama guardada al crear el worktree es la base;
        # el origin/HEAD local (main) solo es respaldo de cadenas viejas.
        falso, hechas = self.api_falsa(
            {
                ("GET", "/repos/acme/repo/pulls"): (200, []),
                ("POST", "/repos/acme/repo/pulls"): (201, {"html_url": URL_PR}),
            }
        )

        resultado = self.publicar(falso, base="feat/x")

        self.assertEqual(resultado, {"pull_request": URL_PR})
        self.assertEqual(hechas[-1]["payload"]["base"], "feat/x")

    def test_un_pr_abierto_de_la_rama_se_reutiliza(self):
        viejo = "https://github.com/acme/repo/pull/4"
        falso, hechas = self.api_falsa(
            {("GET", "/repos/acme/repo/pulls"): (200, [{"html_url": viejo}])}
        )

        resultado = self.publicar(falso)

        self.assertEqual(resultado, {"pull_request": viejo})
        self.assertEqual([hecha["metodo"] for hecha in hechas], ["GET"])

    def test_sin_credencial_la_rama_queda_subida_y_el_motivo_viaja(self):
        falso, _ = self.api_falsa({})

        with mock.patch.object(
            entrega_pr, "_api", side_effect=AssertionError("sin credencial no hay API")
        ):
            resultado = self.publicar(falso, credencial=None)

        self.assertEqual(
            resultado,
            {"pr_no_abierto": "sin credencial de GitHub para https://github.com"},
        )
        self.assertTrue(self.la_rama_esta_en_el_origen())

    def test_un_push_rechazado_deja_motivo_corto_y_sin_api(self):
        # El origin ya tiene cadena/t_x apuntando a historia ajena: el push
        # sale no-fast-forward y el motivo corto viaja sin llamar la API.
        otro = self.raiz / "otro"
        git("init", "-q", "-b", "main", "otro", cwd=self.raiz)
        (otro / "ajeno.txt").write_text("x\n", encoding="utf-8")
        git("add", ".", cwd=otro)
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@ejemplo.invalid",
            "commit",
            "-qm",
            "ajeno",
            cwd=otro,
        )
        subprocess.run(
            ["git", "-C", str(otro), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Los objetos del sha ajeno tienen que vivir en el origen.
        git("push", "-q", str(self.origen), "main:refs/heads/cadena/t_x", cwd=otro)

        falso, _ = self.api_falsa({})
        with mock.patch.object(
            entrega_pr, "_api", side_effect=AssertionError("sin push no hay API")
        ):
            resultado = self.publicar(falso)

        self.assertIn("push rechazado", resultado["pr_no_abierto"])

    def test_un_cuerpo_de_error_ilegible_no_escapa_de_publicar(self):
        # H56 F7: todos los demás tests mockean _api entero; aquí urlopen
        # lanza un HTTPError cuyo read() se corta (IncompleteRead) y
        # publicar debe devolver el motivo, no propagar la excepción.
        import http.client
        import urllib.error

        error = urllib.error.HTTPError(
            "https://api.github.com/repos/acme/repo/pulls",
            500,
            "fallo",
            {},
            None,
        )

        def read_que_se_corta(*args, **kwargs):
            raise http.client.IncompleteRead(b"parcial")

        error.read = read_que_se_corta

        def urlopen_que_falla(pedido, timeout=None):
            raise error

        with (
            mock.patch.object(entrega_pr, "_credencial", return_value=TOKEN),
            mock.patch.object(
                entrega_pr, "_dueño_y_repo", return_value=("acme", "repo")
            ),
            mock.patch.object(entrega_pr.urllib.request, "urlopen", urlopen_que_falla),
        ):
            resultado = entrega_pr.publicar(
                "t_x", worktree=self.worktree, summary="panel arreglado"
            )

        self.assertIn("pr_no_abierto", resultado)
        self.assertIn("500", resultado["pr_no_abierto"])
        self.assertTrue(self.la_rama_esta_en_el_origen())

    def test_el_token_nunca_sale_en_el_resultado(self):
        falso, _ = self.api_falsa(
            {
                ("GET", "/repos/acme/repo/pulls"): (200, []),
                ("POST", "/repos/acme/repo/pulls"): (
                    422,
                    {"message": f"el head {TOKEN} ya tiene un PR"},
                ),
            }
        )

        resultado = self.publicar(falso)

        self.assertNotIn(TOKEN, json.dumps(resultado))
        self.assertIn("[redacted]", resultado["pr_no_abierto"])
        self.assertIn("API de GitHub 422", resultado["pr_no_abierto"])


@unittest.skipIf(os.name == "nt", "los dobles de git son scripts sh")
class CredencialTests(unittest.TestCase):
    """F4: _credencial con un doble de git — sin credencial -> None."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.binario = Path(self.tmp.name) / "bin"
        self.binario.mkdir()
        self.shim = self.binario / "git"
        self.shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.shim.chmod(0o755)

    def correr_con(self, guion):
        self.shim.write_text(guion, encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"PATH": f"{self.binario}:{os.environ.get('PATH', '')}"}
        ):
            return entrega_pr._credencial()

    def test_un_git_que_falla_no_da_credencial(self):
        self.assertIsNone(self.correr_con("#!/bin/sh\nexit 1\n"))

    def test_un_llenado_sin_password_no_da_credencial(self):
        self.assertIsNone(
            self.correr_con(
                "#!/bin/sh\necho 'protocol=https'\necho 'host=github.com'\n"
            )
        )


class DueñoYRepoTests(unittest.TestCase):
    def test_el_owner_y_el_repo_se_deducen_de_urls_https_y_ssh(self):
        deducir = entrega_pr._dueño_y_repo
        self.assertEqual(deducir("https://github.com/acme/repo.git"), ("acme", "repo"))
        self.assertEqual(deducir("https://github.com/acme/repo"), ("acme", "repo"))
        self.assertEqual(
            deducir("https://token@github.com/acme/repo"), ("acme", "repo")
        )
        self.assertEqual(deducir("git@github.com:acme/repo.git"), ("acme", "repo"))
        self.assertEqual(deducir("ssh://git@github.com/acme/repo"), ("acme", "repo"))
        self.assertIsNone(deducir("https://gitlab.com/acme/repo.git"))
        self.assertIsNone(deducir("/una/ruta/local/origin.git"))


if __name__ == "__main__":
    unittest.main()
