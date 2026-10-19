#!/usr/bin/env python3
"""Contrato CI-una-vez-por-bloque del workflow sistema-agentes.yml.

Orden del dueno 2026-10-18 (patron portado de goncloud-Orbit, PR 477; el
mismo modelo que quality.yml de G): la bateria corre UNA vez por bloque.

(1) En un PR en draft no corre nada; `ready_for_review` (y opened,
    synchronize u reopened de un PR que NO es draft) dispara la bateria UNA
    vez sobre el SHA final. El push queda limitado a la rama por defecto:
    con "**" cada push a una rama de PR pagaria la bateria de nuevo.
(2) Un solo job: la condicion del propio job ES la exigencia del gate. En
    draft el job se salta entero y no deja check que pueda contar como verde
    de la bateria; al pasar a ready el check nace sobre el SHA final.
(3) En push a la rama por defecto, si el arbol empujado es identico al de la
    cabeza del PR y esa cabeza dejo `espejo-de-g` en success, el veredicto
    se reusa (.github/scripts/reusar_completa.sh); ante cualquier duda se
    corre (fail-closed hacia CORRER, nunca hacia saltar).

Las aserciones leen el YAML por lineas, sin parser (el workflow es texto
versionado que este repo controla) y sin lineas de comentario: un comentario
dentro del propio job no puede satisfacer el contrato.

La prueba del script corre con un doble de gh, sin red y SIN heredar las
variables GIT_*: un hook de git exporta GIT_DIR/GIT_INDEX_FILE/GIT_WORK_TREE
y, heredadas por los subprocess, el git del repo temporal pegaria contra el
repo REAL (incidente 2026-10-18 en Orbit: GIT_DIR del hook pre-push
reinicializo el repo y le puso core.bare=true).
"""

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "sistema-agentes.yml"
SCRIPT = REPO / ".github" / "scripts" / "reusar_completa.sh"
RAMA_POR_DEFECTO = "feat/hermes-team"

DOBLE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    args = sys.argv[1:]
    assert args[0] == "api", args
    url = args[1]
    escena = json.loads(os.environ["ESCENA"])
    if url.endswith("/pulls"):
        clave = "pulls"
    elif url.endswith("/check-runs"):
        clave = "check-runs"
    else:
        clave = "arbol"
    accion = escena[clave]
    if accion == "FALLO":
        sys.exit(1)
    if not isinstance(accion, str):
        accion = json.dumps(accion)
    sys.stdout.write(accion)
    """
)


def bloque(texto: str, cabecera: str) -> str:
    """Bloque desde la cabecera exacta hasta la siguiente clave de su nivel.

    `cabecera` es la linea completa de la clave (por ejemplo `"on:"` a nivel
    raiz o `"  espejo-de-g:"` a nivel de jobs). Se excluyen las lineas de
    comentario: un comentario no debe poder satisfacer aserciones.
    """
    lineas: list[str] = []
    dentro = False
    for linea in texto.splitlines():
        if linea.rstrip() == cabecera:
            dentro = True
            continue
        if not dentro:
            continue
        if cabecera.startswith("  "):
            # Clave de job: termina en la siguiente clave de igual nivel.
            if re.match(r"^  [A-Za-z_][A-Za-z0-9_-]*:\s*$", linea):
                break
        elif linea and not linea.startswith((" ", "#", "-")):
            # Clave raiz: termina en la siguiente clave de top-nivel.
            break
        if linea.strip() and not linea.lstrip().startswith("#"):
            lineas.append(linea)
    return "\n".join(lineas)


def env_sin_git() -> dict[str, str]:
    """Entorno del proceso SIN variables GIT_*.

    El hook pre-push corre las pruebas con GIT_DIR, GIT_INDEX_FILE, etc.
    puestos por git; heredadas en un subprocess, `git init/add/commit`
    pegarian contra el repo apuntado por el entorno y no contra el repo
    temporal (incidente 2026-10-18: goncloud-Orbit reinicializado y con
    core.bare=true).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(raiz: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(raiz), *args],
        check=True,
        capture_output=True,
        env=env_sin_git(),
    )
    return res.stdout.decode().strip()


def repo_temporal(raiz: Path) -> tuple[str, str]:
    """Repo de juguete con un commit; devuelve (sha, arbol) sin tocar GIT_*."""
    raiz.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(raiz)],
        check=True,
        capture_output=True,
        env=env_sin_git(),
    )
    (raiz / "archivo.txt").write_text("contenido\n", encoding="utf-8")
    git(raiz, "add", ".")
    git(raiz, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "uno")
    return git(raiz, "rev-parse", "HEAD"), git(raiz, "rev-parse", "HEAD^{tree}")


def correr_reuso(raiz: Path, escena: dict) -> tuple[subprocess.CompletedProcess, str]:
    """Corre el script REAL con el doble de gh en el PATH; (proc, salida)."""
    doble = raiz / "doble"
    doble.mkdir(exist_ok=True)
    gh = doble / "gh"
    gh.write_text(DOBLE_GH, encoding="utf-8")
    gh.chmod(0o755)
    salida = raiz / "salida.txt"
    env = env_sin_git() | {
        "PATH": f"{doble}:{os.environ['PATH']}",
        "ESCENA": json.dumps(escena),
        "GITHUB_OUTPUT": str(salida),
        "GH_REPO": "dueno/hermes",
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        cwd=str(raiz),
        env=env,
        text=True,
        timeout=60,
        check=False,  # el rc es el dato bajo prueba, no un fallo del proceso
    )
    return proc, salida.read_text(encoding="utf-8")


class ContratoDisparadores(unittest.TestCase):
    def setUp(self) -> None:
        self.texto = WORKFLOW.read_text(encoding="utf-8")
        self.disparadores = bloque(self.texto, "on:")
        self.job = bloque(self.texto, "  espejo-de-g:")

    def test_la_bateria_corre_una_vez_al_pasar_a_ready(self) -> None:
        """(1) draft sin bateria; ready dispara una vez; push a la default."""
        self.assertIn(
            "types: [opened, synchronize, reopened, ready_for_review]",
            self.disparadores,
            "pull_request debe dispararse en ready_for_review: es el paso "
            "donde la bateria corre UNA vez sobre el SHA final (tras el APROBADO)",
        )
        self.assertIn(
            f"- {RAMA_POR_DEFECTO}",
            self.disparadores,
            "push debe quedar limitado a la rama por defecto",
        )
        self.assertNotIn(
            '"**"',
            self.disparadores,
            "push en '**' pagaria la bateria en cada push a cada rama de PR",
        )
        self.assertIn(
            "(github.event_name == 'pull_request' && !github.event.pull_request.draft)",
            self.job,
            "el job debe saltarse entero en draft: en draft no corre la bateria (1)",
        )
        self.assertIn(
            "github.event_name == 'push'",
            self.job,
            "el job debe seguir corriendo en push a la rama por defecto",
        )
        self.assertIn(
            "github.event_name == 'workflow_dispatch'",
            self.job,
            "dispatch manual sigue existiendo para casos raros",
        )

    def test_permisos_minimos_sin_tokens_nuevos(self) -> None:
        """(4) Solo lectura de contenido, PRs y check runs; github.token."""
        permisos = bloque(self.texto, "permissions:")
        self.assertIn("contents: read", permisos, "permisos: contents: read es el piso")
        self.assertIn(
            "pull-requests: read",
            permisos,
            "el reuso necesita leer el PR asociado al commit empujado",
        )
        self.assertIn(
            "checks: read",
            permisos,
            "el reuso necesita leer los check runs de la cabeza del PR",
        )


class ContratoReuso(unittest.TestCase):
    def setUp(self) -> None:
        self.texto = WORKFLOW.read_text(encoding="utf-8")
        self.job = bloque(self.texto, "  espejo-de-g:")

    def test_el_paso_de_reuso_es_solo_push_y_usa_github_token(self) -> None:
        """(3) El reuso vive en un paso propio, solo push, sin tokens nuevos."""
        self.assertIn(
            "id: reuso",
            self.job,
            "la bateria debe tener el paso de reuso por arbol (id: reuso)",
        )
        self.assertIn(
            "if: github.event_name == 'push'",
            self.job,
            "el reuso SOLO aplica en push a la rama por defecto",
        )
        self.assertIn(
            "GH_TOKEN: ${{ github.token }}",
            self.job,
            "el reuso usa el token del propio workflow, sin tokens nuevos",
        )
        self.assertIn(
            "run: bash .github/scripts/reusar_completa.sh",
            self.job,
            "el reuso debe invocar el script de reuso",
        )

    def test_la_bateria_queda_detras_del_veredicto_del_reuso(self) -> None:
        """Los tres pasos de la bateria cuelgan de steps.reuso.outputs.reusar."""
        condicion = "if: steps.reuso.outputs.reusar != 'true'"
        self.assertEqual(
            self.job.count(condicion),
            3,
            "setup-python, las pruebas unittest y el pin byte a byte deben "
            "quedar detras del veredicto del reuso (3 pasos condicionados)",
        )

    def test_script_falla_cerrado_hacia_correr(self) -> None:
        """El default es NO reusar y el fallback es CORRER, jamas saltar."""
        texto = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "reusar=false",
            texto,
            "el default del reuso es NO reusar: sin veredicto confirmado se corre",
        )
        self.assertIn(
            "se corre la bateria completa",
            texto,
            "el texto del fallback (correr, no saltar) debe estar fijado",
        )
        self.assertIn(
            'select(.name == "espejo-de-g")',
            texto,
            "el reuso mira el check run del propio job de la bateria",
        )


class EscenarioReuso(unittest.TestCase):
    """El script real contra un doble de gh, sin red (escenas del reuso)."""

    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.raiz = Path(tmp.name) / "repo"
        cls.sha, cls.arbol = repo_temporal(cls.raiz)

    def escena(self, pulls, arbol, check_runs) -> dict:
        return {"pulls": pulls, "arbol": arbol, "check-runs": check_runs}

    def cabeza_con_arbol(self, arbol: str) -> str:
        return json.dumps({"commit": {"tree": {"sha": arbol}}})

    def check_ok(self) -> str:
        return json.dumps(
            {
                "check_runs": [
                    {
                        "name": "espejo-de-g",
                        "conclusion": "success",
                        "html_url": "https://ci/run/1",
                    }
                ]
            }
        )

    def test_arbol_identico_y_completa_en_success_reusa(self) -> None:
        """Mismo arbol ya validado en la cabeza del PR -> reusar=true."""
        pulls = json.dumps([{"number": 7, "head": {"sha": self.sha}}])
        escena = self.escena(pulls, self.cabeza_con_arbol(self.arbol), self.check_ok())
        proc, salida = correr_reuso(self.raiz, escena)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "reusar=true",
            salida,
            f"mismo arbol ya validado debe reusarse: {proc.stderr}",
        )
        self.assertIn(self.sha, proc.stdout, "debe imprimir la cabeza reusada")

    def test_arbol_distinto_corre_la_bateria(self) -> None:
        """La base avanzo y no se mezclo -> reusar=false (corre)."""
        pulls = json.dumps([{"number": 7, "head": {"sha": self.sha}}])
        escena = self.escena(pulls, self.cabeza_con_arbol("0" * 40), self.check_ok())
        proc, salida = correr_reuso(self.raiz, escena)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "reusar=false",
            salida,
            f"arbol distinto nunca se reusa: {proc.stderr}",
        )

    def test_gh_roto_corre_la_bateria(self) -> None:
        """Red, rate limit o gh ausente -> reusar=false: fail-closed a correr."""
        escena = self.escena("FALLO", "FALLO", "FALLO")
        proc, salida = correr_reuso(self.raiz, escena)
        self.assertEqual(
            proc.returncode,
            0,
            "el script jamas termina en error: " + proc.stderr,
        )
        self.assertIn(
            "reusar=false",
            salida,
            f"consulta fallida cae a correr, jamas a saltar: {proc.stderr}",
        )

    def test_push_directo_sin_pr_corre_la_bateria(self) -> None:
        """Sin PR asociado al commit empujado -> reusar=false (corre)."""
        escena = self.escena("[]", self.cabeza_con_arbol(self.arbol), self.check_ok())
        proc, salida = correr_reuso(self.raiz, escena)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "reusar=false",
            salida,
            f"sin PR no hay veredicto que reusar: {proc.stderr}",
        )


class CentinelaGitEnv(unittest.TestCase):
    def test_el_git_env_del_hook_deja_intacto_el_repo_ajeno(self) -> None:
        """Regresion 2026-10-18: GIT_* del hook no pegan contra el repo real.

        Un repo centinela recibe el GIT_DIR/GIT_INDEX_FILE hostil en el
        entorno; tras correr TODO el escenario del reuso, el centinela debe
        quedar exactamente igual (core.bare, HEAD, indice). Si el filtro de
        GIT_* falta, la mutacion del centinela vuelve la prueba roja.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            centinela = base / "centinela"
            repo_temporal(centinela)
            estado = (
                git(centinela, "config", "core.bare"),
                git(centinela, "rev-parse", "HEAD"),
                git(centinela, "ls-files", "-s"),
            )
            escenario = base / "escenario"
            repo_temporal(escenario)
            hostil = {
                "GIT_DIR": str(centinela / ".git"),
                "GIT_INDEX_FILE": str(centinela / ".git" / "index"),
            }
            with mock.patch.dict(os.environ, hostil):
                correr_reuso(
                    escenario,
                    {"pulls": "[]", "arbol": "{}", "check-runs": "{}"},
                )
            self.assertEqual(
                (
                    git(centinela, "config", "core.bare"),
                    git(centinela, "rev-parse", "HEAD"),
                    git(centinela, "ls-files", "-s"),
                ),
                estado,
                "el repo centinela cambio: el GIT_* del hook se colo en los "
                "subprocess del escenario",
            )


if __name__ == "__main__":
    unittest.main()
