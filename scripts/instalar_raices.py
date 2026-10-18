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

Los crons del perfil se leen de <home>/cron/jobs.json en solo lectura, la
misma fuente que load_jobs (cron/jobs.py de Hermes): `hermes cron list`
solo imprime una tabla para humanos (sin --json; medido en la Gon-pc con
Hermes 0.21.5) y `cron remove` recibe job_id, no nombre. El instalador no
importa el paquete `cron` de Hermes: sus constantes de ruta se fijan al
importar con el HERMES_HOME del proceso. jobs.json ausente es un perfil
sin crons; ilegible es fail-closed (código 1, nada escrito).

El id del cron creado se guarda en raices/instalacion.json: sale de
releer jobs.json por nombre tras el create (la línea "Created job: <id>"
del CLI es el respaldo). --revertir borra con `cron remove <job_id>`:
manda el id guardado si sigue vivo en jobs.json, y el nombre solo
respalda cuando no hay id guardado (si un cron ajeno se llama
ticker-raices, jamás se toca). Si el id guardado ya no vive en el
almacén y no hay id por nombre, el job ya no está: ya retirado, se
quita solo lo nuestro (envoltorio y estado) y se sale 0 avisándolo;
llamar remove con un id obsoleto fallaría y atascaría la reversa para
siempre. Un remove intentado y fallido sigue cerrando la ruta con
código 1 sin borrar nada (F2 de #28).

--revertir quita SOLO lo instalado aquí (cron, envoltorio, archivo de
estado) y deja la ruta cerrada: sistema-agentes.db, kanban.db,
permisos.json y las proyecciones son datos del operador y jamás se tocan.
--check no escribe nada y no invoca a hermes.

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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NOMBRE_DEL_CRON = "ticker-raices"
ENVOLTORIO = "scripts/ticker-raices.py"
# El argumento del cron es relativo al directorio de scripts, no al home:
# Hermes resuelve --script contra <home>/scripts (hermes_cli/cron.py
# 604-618) y los crons vivos citan el nombre pelado ("model_watch.py").
SCRIPT_DEL_CRON = "ticker-raices.py"
RUTA_DE_ESTADO = "raices/instalacion.json"
# El almacén de crons del perfil (cron/jobs.py de Hermes: JOBS_FILE =
# <home>/cron/jobs.json). Lectura propia en solo lectura, nunca el paquete
# `cron` de Hermes ni la tabla humana de `cron list`.
RUTA_DE_JOBS = Path("cron") / "jobs.json"
# La línea de `hermes cron create` que anuncia el id (hermes_cli_cron.py:
# print(f"Created job: {result['job_id']}")). Respaldo de la relectura.
_FORMA_DE_JOB_CREADO = re.compile(r"Created job:\s*(\S+)")
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


def correr_por_subprocess(argv, home=None, resolvedor=None):
    """El CLI por la RUTA que devuelve el resolvedor (shutil.which por
    omision, como capacidades._correr_hermes_version): en Windows el CLI es
    hermes.CMD y CreateProcess sin shell no resuelve el nombre pelado
    (WinError 2). Sin ruta resoluble no se invoca nada: error claro y codigo
    1, nunca FileNotFoundError. Cada llamada lleva HERMES_HOME=<home> en el
    entorno (el default profile ES la raíz: sin esto el create/remove
    aterrizaría en el jobs.json del home del proceso) y decodifica utf-8
    con replace (la code page local revienta en un Windows no UTF-8). Un
    CLI colgado (timeout) no tumba al instalador con un traceback: sale
    código != 0 con la causa, para que aplicar desenlace el envoltorio y
    revertir la imprima."""
    ejecutable = (resolvedor or shutil.which)("hermes")
    if not ejecutable:
        print(
            "instalar-raices: no encontre el ejecutable de Hermes (hermes) "
            "en el PATH; nada que hacer"
        )
        return 1, ""
    entorno = os.environ.copy()
    if home is not None:
        entorno["HERMES_HOME"] = str(Path(home))
    try:
        resultado = subprocess.run(
            [ejecutable, *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=entorno,
        )
    except subprocess.TimeoutExpired:
        return 1, f"timeout: hermes no contesto en 60 segundos ({' '.join(argv)})"
    return resultado.returncode, resultado.stdout


def _leer_jobs(home: Path):
    """Los jobs del perfil desde <home>/cron/jobs.json en solo lectura
    (misma forma que load_jobs: {"jobs": [...]} o lista pelada; las
    entradas que no son objetos se saltan como en Hermes). Devuelve [] si
    el archivo no existe (perfil sin crons) y None si está ilegible:
    fail-closed, quien llama decide."""
    ruta = Path(home) / RUTA_DE_JOBS
    try:
        # utf-8-sig absorbe el BOM que el lector de Hermes también tolera.
        crudo = ruta.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError no es OSError: bytes que no son utf-8 (un
        # editor que dejó el archivo en UTF-16, por ejemplo) son un
        # almacén ilegible, no una excepción que tumbe al instalador.
        return None
    try:
        datos = json.loads(crudo)
    except ValueError:
        return None
    if isinstance(datos, dict):
        datos = datos.get("jobs")
    if not isinstance(datos, list):
        return None
    return [entrada for entrada in datos if isinstance(entrada, dict)]


def _hay_cron(jobs) -> bool:
    return any(entrada.get("name") == NOMBRE_DEL_CRON for entrada in jobs)


def _id_del_cron(jobs):
    """El id del job con el nombre del cron (los registros sin id legible
    no cuentan: no hay remove que puisse con ellos)."""
    for entrada in jobs:
        if entrada.get("name") == NOMBRE_DEL_CRON and isinstance(
            entrada.get("id"), str
        ):
            return entrada["id"]
    return None


def _id_desde_salida(salida):
    hallado = _FORMA_DE_JOB_CREADO.search(salida or "")
    return hallado.group(1) if hallado else None


def _id_del_estado(estado: Path):
    try:
        datos = json.loads(estado.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(datos, dict) and isinstance(datos.get("cron_id"), str):
        return datos["cron_id"]
    return None


def _avisar_sin_id() -> None:
    print(
        f"instalar-raices: aviso: no pude fijar el id de "
        f"{NOMBRE_DEL_CRON}; --revertir lo buscará por nombre"
    )


def _escribir_envoltorio(home: Path, plugin: Path) -> None:
    destino = home / ENVOLTORIO
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        ENVOLTORIO_TEXTO.format(plugin=str(plugin.resolve())),
        encoding="utf-8",
    )


def _registrar_estado(home: Path, con_plantilla: bool, cron_id) -> None:
    raices = home / "raices"
    raices.mkdir(exist_ok=True)
    (raices / "instalacion.json").write_text(
        json.dumps(
            {
                "version": 1,
                "cron": NOMBRE_DEL_CRON,
                "cron_id": cron_id,
                "envoltorio": ENVOLTORIO,
                "script_del_cron": SCRIPT_DEL_CRON,
                "permisos_de_plantilla": con_plantilla,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _jobs_ilegibles(home: Path) -> str:
    return f"instalar-raices: no pude leer {Path(home) / RUTA_DE_JOBS}"


def chequear(home: Path, plugin: Path) -> int:
    """R7: la lectura es de jobs.json: el corredor sobra aquí."""
    jobs = _leer_jobs(home)
    if jobs is None:
        print(_jobs_ilegibles(home) + "; nada que hacer")
        return 1
    print(
        f"cron {NOMBRE_DEL_CRON}: "
        + (
            "presente"
            if _hay_cron(jobs)
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
    jobs = _leer_jobs(home)
    if jobs is None:
        print(_jobs_ilegibles(home) + "; nada que hacer")
        return 1
    con_plantilla = False
    _escribir_envoltorio(home, plugin)
    cron_id = None
    if not _hay_cron(jobs):
        codigo, salida = corredor(
            [
                "cron",
                "create",
                "every 1m",
                "--name",
                NOMBRE_DEL_CRON,
                "--script",
                SCRIPT_DEL_CRON,
                "--no-agent",
                "--deliver",
                "local",
            ]
        )
        if codigo != 0:
            (home / ENVOLTORIO).unlink(missing_ok=True)
            print(f"instalar-raices: hermes rechazó el cron (código {codigo})")
            return 1
        # El id sale de releyendo jobs.json por nombre (la fuente viva);
        # la línea "Created job: <id>" del CLI es el respaldo.
        tras_crear = _leer_jobs(home)
        if tras_crear is not None:
            cron_id = _id_del_cron(tras_crear)
        if cron_id is None:
            cron_id = _id_desde_salida(salida)
        if cron_id is None:
            _avisar_sin_id()
    else:
        cron_id = _id_del_cron(jobs)
        if cron_id is None:
            # R3: el job ya está pero su registro no trae id textual: el
            # mismo aviso que la rama de create; nunca null en silencio.
            _avisar_sin_id()
    permisos = home / "raices" / "permisos.json"
    if not permisos.exists():
        permisos.parent.mkdir(exist_ok=True)
        permisos.write_text(
            json.dumps(PLANTILLA_DE_PERMISOS, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        con_plantilla = True
    _registrar_estado(home, con_plantilla, cron_id)
    print(
        f"instalar-raices: cron {NOMBRE_DEL_CRON} en default, envoltorio en {ENVOLTORIO}"
    )
    return 0


def _retirar_con_remove(home: Path, estado: Path, job_id: str, corredor) -> int:
    codigo, salida = corredor(["cron", "remove", job_id])
    if codigo != 0:
        # El remove SE INTENTÓ y falló: tocar nada a medias dejaría un cron
        # vivo sin estado que lo registre (igual que aplicar con create
        # rechazado). No se borra nada y el error sale impreso (F2).
        print(f"instalar-raices: hermes rechazó el remove: {salida.strip()}")
        return 1
    (home / ENVOLTORIO).unlink(missing_ok=True)
    estado.unlink()
    print(f"instalar-raices: {NOMBRE_DEL_CRON} retirado; datos del operador intactos")
    return 0


def _desenlazar_y_avisar(home: Path, estado: Path, jobs) -> int:
    # R1: el job de esta instalación ya no vive en el almacén = ya
    # retirado (el operador lo borró a mano o la store se fue). Un remove
    # con el id obsoleto fallaría y dejaría la reversa atascada en código 1
    # para siempre: no se llama, solo se quita lo nuestro.
    resto = ""
    if _hay_cron(jobs):
        resto = (
            f" (un job llamado {NOMBRE_DEL_CRON} sigue en el almacén y no se "
            "toca: no es el de esta instalación)"
        )
    print(
        f"instalar-raices: el job de esta instalación ya no está en el "
        f"almacén: ya retirado{resto}; quito envoltorio y estado"
    )
    (home / ENVOLTORIO).unlink(missing_ok=True)
    estado.unlink()
    return 0


def revertir(home: Path, corredor) -> int:
    estado = home / RUTA_DE_ESTADO
    if not estado.exists():
        print("instalar-raices: sin instalación registrada; no se toca nada")
        return 0
    jobs = _leer_jobs(home)
    if jobs is None:
        print(_jobs_ilegibles(home) + "; no se toca nada")
        return 1
    guardado = _id_del_estado(estado)
    vivos = {
        entrada.get("id") for entrada in jobs if isinstance(entrada.get("id"), str)
    }
    if guardado is not None:
        if guardado in vivos:
            # R2: el id guardado vivo manda. Si otro cron ajeno se llama
            # ticker-raices, el nombre no se toca.
            return _retirar_con_remove(home, estado, guardado, corredor)
        # R1: nuestro id ya no vive: ya retirado. El nombre queda fuera a
        # propósito (podría ser un cron ajeno, R2).
        return _desenlazar_y_avisar(home, estado, jobs)
    if _hay_cron(jobs):
        por_nombre = _id_del_cron(jobs)
        if por_nombre is None:
            # El job está en el almacén pero ni su registro ni el estado
            # traen id textual: no hay remove seguro. Fail-closed.
            print(
                f"instalar-raices: el registro de {NOMBRE_DEL_CRON} en "
                f"{Path(home) / RUTA_DE_JOBS} no trae id y el estado tampoco "
                "lo guardó; no se toca nada"
            )
            return 1
        # Sin id guardado, el nombre es el respaldo (R2).
        return _retirar_con_remove(home, estado, por_nombre, corredor)
    # R1: sin id guardado y sin el nombre en el almacén: nada que remove
    # pueda borrar; solo queda lo nuestro.
    return _desenlazar_y_avisar(home, estado, jobs)


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
    if corredor is None:

        def corredor(llamada):
            return correr_por_subprocess(llamada, home=args.home)

    if args.check:
        return chequear(args.home, args.plugin)
    if args.aplicar:
        return aplicar(args.home, args.plugin, corredor)
    return revertir(args.home, corredor)


if __name__ == "__main__":
    sys.exit(main())
