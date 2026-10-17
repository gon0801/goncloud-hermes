"""La sonda de poteto por perfil (SUA-07 H4b2).

Corre en la PC sobre el %HERMES_HOME% de un perfil: audita la copia
desplegada, el bloque del SOUL, las skills publicadas y el digest que
goncloud-managed.json registra. Con --tarea imprime además el prompt de
comportamiento que el coordinador correrá con `hermes -p <perfil>`; la sonda
no ejecuta hermes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "goncloud-team"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from raices import poteto  # noqa: E402


def generacion_y_digest(copia_desplegada: Path) -> tuple[int | str, str]:
    """(generation, digest) de la copia desplegada; sin copia desplegada se
    cita la fijada en el repo, que es la que el despliegue debe alcanzar."""
    copia = copia_desplegada if copia_desplegada.is_dir() else poteto.COPIA
    manifiesto = poteto.leer_manifiesto(copia)
    return manifiesto["generation"], poteto.digest_de_manifiesto(manifiesto)


def prompt_de(perfil: str, tarea: str, generacion, digest: str) -> str:
    return (
        f"Tarea: {tarea}\n\n"
        "Antes de usar cualquier otra herramienta, carga la skill `poteto-mode` "
        "con la herramienta `skill` y sigue su playbook. La adaptación de este "
        "runtime es `skills/poteto-hermes/SKILL.md`: lo que poteto nombra, "
        "ella lo traduce. El despliegue fijado de este perfil es "
        f"generacion={generacion} digest={digest}; si lo desplegado no cuadra "
        "con ese digest, repórtalo y no procedas."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home", type=Path, required=True, help="HERMES_HOME del perfil"
    )
    parser.add_argument("--perfil", required=True, help="id del perfil despachado")
    parser.add_argument(
        "--tarea",
        help="texto de la tarea: imprime el prompt que el coordinador correra",
    )
    args = parser.parse_args()
    fallas = poteto.sonar(args.home, args.perfil)
    for falla in fallas:
        print(falla)
    if fallas:
        return 1
    generacion, digest = generacion_y_digest(
        args.home / "shared" / "sistema-agentes" / "poteto"
    )
    print(f"sonda poteto OK: {args.perfil} generacion={generacion} digest={digest}")
    if args.tarea:
        print()
        print(f"Prompt para hermes -p {args.perfil}:")
        print(prompt_de(args.perfil, args.tarea, generacion, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
