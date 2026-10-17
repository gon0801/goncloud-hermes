#!/usr/bin/env python3
"""El instalador y la reversa de la ruta de raíces de Hermes (SUA-07 H4b3).

--aplicar deja el ticker andando en el perfil DEFAULT: el envoltorio que
delega en raices/cron_ticker.py del plugin en <home>/scripts/ticker-raices.py
y el cron "ticker-raices" cada minuto, sin agente y con entrega local. En
Hermes el perfil default ES la raíz del home (medido en la Gon-pc:
profiles/default no existe y los scripts de sus crones viven en
%HERMES_HOME%\\scripts, junto a backup-claw.sh; el help de `hermes cron
create` dice "--script Path to a script under ~/.hermes/scripts/"), y el
cron sin -p corre con el HERMES_HOME de esa raíz, donde gateway_state.json
(vivo la puerta de H4a) espera estar. El instalador nunca crea
profiles/default ni toca scripts ajenos de scripts/.

--revertir quita SOLO lo instalado aquí (cron, envoltorio, archivo de
estado) y deja la ruta cerrada: sistema-agentes.db, kanban.db,
permisos.json y las proyecciones son datos del operador y jamás se tocan.
--check no escribe nada; dice qué haría.

La plantilla de permisos.json (solo cuando el archivo no existe: nunca se
pisan datos del operador) niega TODO: acciones, toolsets y comandos
vacíos. El operador la abre a mano; H no tiene otra fuente para esos
datos.

Los flags del CLI se confirman en la PC en H5; el create es el que fija
el plan: hermes cron create "every 1m" --name ticker-raices --script
<nombre> --no-agent --deliver local (sin -p = default).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

NOMBRE_DEL_CRON = "ticker-raices"
ENVOLTORIO = "scripts/ticker-raices.py"
RUTA_DE_ESTADO = "raices/instalacion.json"
PLANTILLA_DE_PERMISOS = {
    "acciones": [],
    "toolsets": {},
    "comandos": [],
    "cobertura": {},
}

ENVOLTORIO_TEXTO = '''#!/usr/bin/env python3
"""El ticker de raíces como script del cron del perfil default.

Lo escribe scripts/instalar_raices.py; delega en el cron_ticker del
plugin goncloud-team. La ruta del plugin queda fija aquí abajo porque el
instalador corre desde el checkout que la tiene.
"""
import sys

PLUGIN = {plugin!r}
if PLUGIN not in sys.path:
    sys.path.insert(0, PLUGIN)

from raices import cron_ticker

raise SystemExit(cron_ticker.main())
'''


def correr_por_subprocess(argv):
    resultado = subprocess.run(
        ["hermes", *argv], capture_output=True, text=True, timeout=60
    )
    return resultado.returncode, resultado.stdout


def _crons(corredor):
    codigo, salida = corredor(["cron", "list", "--json"])
    if codigo != 0:
        return None
    try:
        datos = json.loads(salida)
    except ValueError:
        return None
    if not isinstance(datos, list):
        return None
    return [entrada.get("name") for entrada in datos if isinstance(entrada, dict)]


def _escribir_envoltorio(home: Path, plugin: Path) -> None:
    destino = home / ENVOLTORIO
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        ENVOLTORIO_TEXTO.format(plugin=str(plugin.resolve())),
        encoding="utf-8",
    )


def _registrar_estado(home: Path, con_plantilla: bool) -> None:
    raices = home / "raices"
    raices.mkdir(exist_ok=True)
    (raices / "instalacion.json").write_text(
        json.dumps(
            {
                "version": 1,
                "cron": NOMBRE_DEL_CRON,
                "envoltorio": ENVOLTORIO,
                "permisos_de_plantilla": con_plantilla,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def chequear(home: Path, plugin: Path, corredor) -> int:
    crons = _crons(corredor)
    if crons is None:
        print("instalar-raices: no pude leer `hermes cron list --json`; nada que hacer")
        return 1
    print(
        f"cron {NOMBRE_DEL_CRON}: "
        + (
            "presente"
            if NOMBRE_DEL_CRON in crons
            else "se crearía en el perfil default (every 1m, --no-agent, --deliver local)"
        )
    )
    envoltorio = home / ENVOLTORIO
    print(
        f"envoltorio {ENVOLTORIO}: "
        + (
            "presente"
            if envoltorio.exists()
            else f"se escribiría delegando en {plugin}"
        )
    )
    permisos = home / "raices" / "permisos.json"
    if permisos.exists():
        print("permisos.json: presente (no se toca)")
    else:
        print(
            "permisos.json: se crearía la plantilla que niega todo (el operador la abre a mano)"
        )
    estado = home / RUTA_DE_ESTADO
    print(
        f"estado {RUTA_DE_ESTADO}: "
        + ("presente" if estado.exists() else "se escribiría en --aplicar")
    )
    return 0


def aplicar(home: Path, plugin: Path, corredor) -> int:
    crons = _crons(corredor)
    if crons is None:
        print("instalar-raices: no pude leer `hermes cron list --json`; nada que hace")
        return 1
    con_plantilla = False
    if NOMBRE_DEL_CRON not in crons:
        codigo, _ = corredor(
            [
                "cron",
                "create",
                "every 1m",
                "--name",
                NOMBRE_DEL_CRON,
                "--script",
                ENVOLTORIO,
                "--no-agent",
                "--deliver",
                "local",
            ]
        )
        if codigo != 0:
            print(f"instalar-raices: hermes rechazó el cron (código {codigo})")
            return 1
    permisos = home / "raices" / "permisos.json"
    if not permisos.exists():
        permisos.parent.mkdir(exist_ok=True)
        permisos.write_text(
            json.dumps(PLANTILLA_DE_PERMISOS, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        con_plantilla = True
    _escribir_envoltorio(home, plugin)
    _registrar_estado(home, con_plantilla)
    print(
        f"instalar-raices: cron {NOMBRE_DEL_CRON} en default, envoltorio en {ENVOLTORIO}"
    )
    return 0


def revertir(home: Path, corredor) -> int:
    estado = home / RUTA_DE_ESTADO
    if not estado.exists():
        print("instalar-raices: sin instalación registrada; no se toca nada")
        return 0
    crons = _crons(corredor)
    if crons is None:
        print("instalar-raices: no pude leer `hermes cron list --json`; nada que hace")
        return 1
    if NOMBRE_DEL_CRON in crons:
        corredor(["cron", "remove", NOMBRE_DEL_CRON])
    (home / ENVOLTORIO).unlink(missing_ok=True)
    estado.unlink()
    print(f"instalar-raices: {NOMBRE_DEL_CRON} retirado; datos del operador intactos")
    return 0


def main(argv=None, corredor=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modo = parser.add_mutually_exclusive_group(required=True)
    modo.add_argument("--check", action="store_true", help="no escribe; dice qué haría")
    modo.add_argument(
        "--aplicar", action="store_true", help="instala cron y envoltorio"
    )
    modo.add_argument("--revertir", action="store_true", help="quita solo lo instalado")
    parser.add_argument("--home", type=Path, required=True, help="HERMES_HOME objetivo")
    parser.add_argument(
        "--plugin",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "plugins" / "goncloud-team",
        help="ruta del plugin goncloud-team con raices/cron_ticker.py",
    )
    args = parser.parse_args(argv)
    corredor = corredor or correr_por_subprocess
    if args.check:
        return chequear(args.home, args.plugin, corredor)
    if args.aplicar:
        return aplicar(args.home, args.plugin, corredor)
    return revertir(args.home, corredor)


if __name__ == "__main__":
    sys.exit(main())
