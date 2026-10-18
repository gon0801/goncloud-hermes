#!/usr/bin/env python3
"""El instalador y la reversa de la ruta de raíces de Hermes (SUA-07 H4b3).

--aplicar deja andando DOS trabajos sin agente en el perfil DEFAULT, cada
uno con su envoltorio que delega en el módulo del plugin:

- ticker-raices (raices/cron_ticker.py) con entrega local: la vuelta que
  concilia, decide y despacha.
- despertar-raices (raices/despertar.py, H6b) con entrega bot-chat:claw: el
  stdout de la corrida entra como un turno en el chat "Bot Chat" de claw y
  stdout vacío es corrida silenciosa. Despierta al director con las
  necesidades de las raíces vivas.

En Hermes el perfil default ES la raíz del home (medido en la Gon-pc:
profiles/default no existe y los scripts de sus crones viven en
%HERMES_HOME%\\scripts, junto a backup-claw.sh; el help de `hermes cron
create` dice "--script Path to a script under ~/.hermes/scripts/"), y el
CLI resuelve el perfil ACTIVO pegajoso por encima de HERMES_HOME (medido
en la Gon-pc: sin -p el cron cayó en claw), así que toda llamada lleva
"-p default" aunque HERMES_HOME apunte a la raíz, donde gateway_state.json
(vivo la puerta de H4a) espera estar. El instalador nunca crea
profiles/default ni toca scripts ajenos de scripts/.

Los crons del perfil se leen de <home>/cron/jobs.json en solo lectura, la
misma fuente que load_jobs (cron/jobs.py de Hermes): `hermes cron list`
solo imprime una tabla para humanos (sin --json; medido en la Gon-pc con
Hermes 0.21.5) y `cron remove` recibe job_id, no nombre. El instalador no
importa el paquete `cron` de Hermes: sus constantes de ruta se fijan al
importar con el HERMES_HOME del proceso. jobs.json ausente es un perfil
sin crons; ilegible es fail-closed (código 1, nada escrito).

El id de cada cron creado se guarda en raices/instalacion.json (versión 2,
una fila por cron): sale de releer jobs.json por nombre tras el create (la
línea "Created job: <id>" del CLI es el respaldo). --revertir borra con
`cron remove <job_id>` por cron: manda el id guardado si sigue vivo en
jobs.json, y el nombre solo respalda cuando no hay id guardado (si un cron
ajeno se llama igual, jamás se toca). Si el id guardado ya no vive en el
almacén y no hay id por nombre, el job ya no está: ya retirado, se quita
solo lo nuestro (envoltorios y estado) y se sale 0 avisándolo; llamar
remove con un id obsoleto fallaría y atascaría la reversa para siempre.
Un remove intentado y fallido sigue cerrando la ruta con código 1 sin
borrar nada (F2 de #28). Un estado de la versión 1 (solo ticker-raices)
se revierte igual: la migración lo lee tal cual.

--revertir quita SOLO lo instalado aquí (crons, envoltorios, archivo de
estado) y deja la ruta cerrada: sistema-agentes.db, kanban.db,
permisos.json y las proyecciones son datos del operador y jamás se tocan.
--check no escribe nada y no invoca a hermes.

La plantilla de permisos.json (solo cuando el archivo no existe: nunca se
pisan datos del operador) niega TODO: acciones, toolsets y comandos
vacíos. El operador la abre a mano; H no tiene otra fuente para esos
datos.

Los flags del CLI se confirman en la PC en H5; el create es el que fija
el plan: hermes -p default cron create "every 1m" --name <nombre>
--script <nombre> --no-agent --deliver <local|bot-chat:claw>.
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
from typing import NamedTuple


class CronDeRaices(NamedTuple):
    nombre: str
    script: str
    deliver: str
    modulo: str

    @property
    def envoltorio(self) -> str:
        return f"scripts/{self.script}"


CRONES = (
    CronDeRaices("ticker-raices", "ticker-raices.py", "local", "cron_ticker"),
    CronDeRaices(
        "despertar-raices", "despertar-raices.py", "bot-chat:claw", "despertar"
    ),
)
POR_NOMBRE = {cron.nombre: cron for cron in CRONES}
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
"""El {nombre} como script del cron del perfil default.

Lo escribe scripts/instalar_raices.py; delega en el {modulo} del
plugin goncloud-team. La ruta del plugin queda fija aquí abajo porque el
instalador corre desde el checkout que la tiene.
"""
import sys

PLUGIN = {plugin!r}
if PLUGIN not in sys.path:
    sys.path.insert(0, PLUGIN)

from raices import {modulo}

raise SystemExit({modulo}.main())
'''


def correr_por_subprocess(argv, home=None, resolvedor=None):
    """El CLI por la RUTA que devuelve el resolvedor (shutil.which por
    omision, como capacidades._correr_hermes_version): en Windows el CLI es
    hermes.CMD y CreateProcess sin shell no resuelve el nombre pelado
    (WinError 2). Sin ruta resoluble no se invoca nada: error claro y codigo
    1, nunca FileNotFoundError. Toda llamada lleva "-p default" (el CLI
    resuelve el perfil ACTIVO pegajoso por encima de HERMES_HOME: medido
    en la Gon-pc, sin -p el cron cayó en claw) y HERMES_HOME=<home> en el
    entorno, y decodifica utf-8
    con replace (la code page local revienta en un Windows no UTF-8). Un
    CLI colgado (timeout) no tumba al instalador con un traceback: sale
    codigo != 0 con la causa, para que aplicar desenlace el envoltorio y
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
            [ejecutable, "-p", "default", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=entorno,
        )
    except subprocess.TimeoutExpired:
        return 1, f"timeout: hermes no contesto en 60 segundos ({' '.join(argv)})"
    # La causa puede venir por stdout o por stderr (a veces ruido en uno y
    # causa en el otro): la salida es ambas, sin tapar ninguna.
    return resultado.returncode, "\n".join(
        p for p in (resultado.stdout, resultado.stderr) if p
    )


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


def _hay_cron(jobs, nombre: str) -> bool:
    return any(entrada.get("name") == nombre for entrada in jobs)


def _id_del_cron(jobs, nombre: str):
    """El id del job con el nombre del cron (los registros sin id legible
    no cuentan: no hay remove que puisse con ellos)."""
    for entrada in jobs:
        if entrada.get("name") == nombre and isinstance(entrada.get("id"), str):
            return entrada["id"]
    return None


def _id_desde_salida(salida):
    hallado = _FORMA_DE_JOB_CREADO.search(salida or "")
    return hallado.group(1) if hallado else None


def _crons_del_estado(estado: Path) -> dict:
    """{nombre: cron_id|None} según instalacion.json. La versión 1 (un solo
    cron con las llaves planas) se lee tal cual: la reversa de una
    instalación vieja no necesita reaplicar."""
    try:
        datos = json.loads(estado.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(datos, dict):
        return {}
    if isinstance(datos.get("crons"), list):
        leidos = {}
        for entrada in datos["crons"]:
            if isinstance(entrada, dict) and isinstance(entrada.get("cron"), str):
                cron_id = entrada.get("cron_id")
                leidos[entrada["cron"]] = cron_id if isinstance(cron_id, str) else None
        return leidos
    cron_id = datos.get("cron_id")
    return {
        datos.get("cron", NOMBRE_TICKER): cron_id if isinstance(cron_id, str) else None
    }


NOMBRE_TICKER = "ticker-raices"


def _avisar_sin_id(nombre: str) -> None:
    print(
        f"instalar-raices: aviso: no pude fijar el id de "
        f"{nombre}; --revertir lo buscará por nombre"
    )


def _escribir_envoltorio(home: Path, cron: CronDeRaices, plugin: Path) -> None:
    destino = home / cron.envoltorio
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        ENVOLTORIO_TEXTO.format(
            nombre=cron.nombre, modulo=cron.modulo, plugin=str(plugin.resolve())
        ),
        encoding="utf-8",
    )


def _registrar_estado(
    home: Path, con_plantilla: bool, ids: dict, crones=CRONES
) -> None:
    raices = home / "raices"
    raices.mkdir(exist_ok=True)
    (raices / "instalacion.json").write_text(
        json.dumps(
            {
                "version": 2,
                "crons": [
                    {
                        "cron": cron.nombre,
                        "cron_id": ids.get(cron.nombre),
                        "deliver": cron.deliver,
                        "envoltorio": cron.envoltorio,
                        "script_del_cron": cron.script,
                    }
                    for cron in crones
                ],
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
    for cron in CRONES:
        print(
            f"cron {cron.nombre}: "
            + (
                "presente"
                if _hay_cron(jobs, cron.nombre)
                else f"se crearía en el perfil default (every 1m, --no-agent,"
                f" --deliver {cron.deliver})"
            )
        )
        envoltorio = home / cron.envoltorio
        print(
            f"envoltorio {cron.envoltorio}: "
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


def _fusionar_estado(home: Path, ids_vivos: dict) -> bool:
    """F1(2): fusiona en el instalacion.json EXISTENTE los ids vivos que la
    limpieza no pudo retirar, sin perder lo demás del estado. False si el
    estado no se pudo leer (el llamador avisa; nunca se corrompe)."""
    ruta = home / RUTA_DE_ESTADO
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            raise ValueError
    except (OSError, ValueError):
        return False
    crons = datos.get("crons") if isinstance(datos.get("crons"), list) else []
    entradas = {}
    for entrada in crons:
        if isinstance(entrada, dict) and isinstance(entrada.get("cron"), str):
            entradas[entrada["cron"]] = entrada
    for nombre, cron_id in ids_vivos.items():
        cron = POR_NOMBRE.get(nombre)
        if cron is None:
            continue
        entrada = entradas.get(
            nombre,
            {
                "cron": nombre,
                "cron_id": None,
                "deliver": cron.deliver,
                "envoltorio": cron.envoltorio,
                "script_del_cron": cron.script,
            },
        )
        entrada["cron_id"] = cron_id
        entradas[nombre] = entrada
    documento = {
        "version": 2,
        "crons": [entradas[nombre] for nombre in sorted(entradas)],
        "permisos_de_plantilla": bool(datos.get("permisos_de_plantilla")),
    }
    try:
        ruta.write_text(
            json.dumps(documento, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        return False
    return True


def _limpieza_de_creacion(
    home: Path, corredor, creados: list, rechazado: str, codigo: int, salida: str
) -> int:
    """F1 (hermes#34): un create parcial no deja un cron vivo apuntando a un
    envoltorio borrado ni un desastre que --revertir no pueda retirar. Retira
    los crons creados EN ESTA corrida (los que ya estaban en jobs.json no se
    tocan) y después desenlaza SOLO los envoltorios cuyo nombre ya no tiene
    job vivo en jobs.json releído — y si la relectura devuelve None,
    fail-closed: no se desenlaza ninguno. Un cron creado sin id conserva su
    envoltorio y deja estado parcial con cron_id null (F7) para que
    --revertir lo busque por nombre; el estado previo (v1/v2) que ya existía
    no se degrada: los ids vivos que no se pudieron retirar se FUSIONAN en él
    (F1-2)."""
    print(
        f"instalar-raices: hermes rechazó el cron {rechazado} "
        f"(código {codigo}): {salida.strip()}"
    )
    quedan: list = []
    for cron in CRONES:
        for nombre, cron_id in creados:
            if nombre != cron.nombre or cron.nombre in {c.nombre for c, _ in quedan}:
                continue
            if cron_id is None:
                quedan.append((cron, None))
                continue
            codigo_rm, salida_rm = corredor(["cron", "remove", cron_id])
            if codigo_rm != 0:
                quedan.append((cron, cron_id))
                print(
                    f"instalar-raices: la limpieza del cron {nombre} falló "
                    f"(código {codigo_rm}): {salida_rm.strip()}; quedó vivo y "
                    f"queda registrado en {RUTA_DE_ESTADO} para que "
                    "--revertir lo retire"
                )
    if quedan:
        ids_por_nombre = {cron.nombre: cron_id for cron, cron_id in quedan}
        if (home / RUTA_DE_ESTADO).exists():
            fusionables = {
                nombre: cron_id for nombre, cron_id in ids_por_nombre.items() if cron_id
            }
            if fusionables:
                if _fusionar_estado(home, fusionables):
                    print(
                        f"instalar-raices: el estado previo de {RUTA_DE_ESTADO} "
                        f"conserva lo suyo y fusiona el id vivo de: "
                        f"{', '.join(sorted(fusionables))}"
                    )
                else:
                    # F9 (hermes#34): el estado previo no se pudo leer ni
                    # escribir: el fallo se dice; el vivo queda sin registro
                    # y el error de arriba ya no promete lo que no pasó.
                    print(
                        f"instalar-raices: AVISO: no pude fusionar el id vivo "
                        f"de {', '.join(sorted(fusionables))} en el estado "
                        f"previo {RUTA_DE_ESTADO}: el cron quedó vivo y sin "
                        "registro; retíralo a mano con `hermes cron remove`"
                    )
            if any(cron_id is None for cron_id in ids_por_nombre.values()):
                print(
                    f"instalar-raices: AVISO: había un estado previo en "
                    f"{RUTA_DE_ESTADO}; el cron creado sin id no dejó "
                    "registro nuevo"
                )
        else:
            _registrar_estado(
                home,
                con_plantilla=False,
                ids=ids_por_nombre,
                crones=tuple(cron for cron, _ in quedan),
            )
    tras_limpiar = _leer_jobs(home)
    if tras_limpiar is None:
        # F1(1): sin almacén legible no se sabe qué está vivo: fail-closed,
        # ningún envoltorio de cron no retirado con éxito se desenlaza.
        return 1
    quedan_nombres = {cron.nombre for cron, _ in quedan}
    for cron in CRONES:
        if cron.nombre in quedan_nombres:
            continue
        if _hay_cron(tras_limpiar, cron.nombre):
            continue
        (home / cron.envoltorio).unlink(missing_ok=True)
    return 1


def aplicar(home: Path, plugin: Path, corredor) -> int:
    jobs = _leer_jobs(home)
    if jobs is None:
        print(_jobs_ilegibles(home) + "; nada que hacer")
        return 1
    con_plantilla = False
    for cron in CRONES:
        _escribir_envoltorio(home, cron, plugin)
    ids: dict = {}
    creados: list = []
    for cron in CRONES:
        if not _hay_cron(jobs, cron.nombre):
            codigo, salida = corredor(
                [
                    "cron",
                    "create",
                    "every 1m",
                    "--name",
                    cron.nombre,
                    "--script",
                    cron.script,
                    "--no-agent",
                    "--deliver",
                    cron.deliver,
                ]
            )
            if codigo != 0:
                return _limpieza_de_creacion(
                    home, corredor, creados, cron.nombre, codigo, salida
                )
            # El id sale de releyendo jobs.json por nombre (la fuente viva);
            # la línea "Created job: <id>" del CLI es el respaldo.
            tras_crear = _leer_jobs(home)
            cron_id = (
                _id_del_cron(tras_crear, cron.nombre)
                if tras_crear is not None
                else None
            )
            if cron_id is None:
                cron_id = _id_desde_salida(salida)
            if cron_id is None:
                _avisar_sin_id(cron.nombre)
            ids[cron.nombre] = cron_id
            creados.append((cron.nombre, cron_id))
        else:
            cron_id = _id_del_cron(jobs, cron.nombre)
            if cron_id is None:
                # R3: el job ya está pero su registro no trae id textual: el
                # mismo aviso que la rama de create; nunca null en silencio.
                _avisar_sin_id(cron.nombre)
            ids[cron.nombre] = cron_id
    permisos = home / "raices" / "permisos.json"
    if not permisos.exists():
        permisos.parent.mkdir(exist_ok=True)
        permisos.write_text(
            json.dumps(PLANTILLA_DE_PERMISOS, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        con_plantilla = True
    _registrar_estado(home, con_plantilla, ids)
    nombres = ", ".join(cron.nombre for cron in CRONES)
    print(f"instalar-raices: crons {nombres} en default, envoltorios en scripts/")
    return 0


def _quitar_envoltorios_y_estado(home: Path, estado: Path, jobs) -> None:
    """F8 (hermes#34): desenlaza SOLO los envoltorios cuyo nombre ya no
    tiene job vivo en los jobs releídos tras los remove — la misma
    invariante de _limpieza_de_creacion; un vivo que el estado no listaba
    se avisa por nombre y conserva su envoltorio (igual que un ajeno: R2).
    Un almacén ilegible no llega aquí: revertir falla cerrado antes."""
    for cron in CRONES:
        if _hay_cron(jobs, cron.nombre):
            print(
                f"instalar-raices: AVISO: un job llamado {cron.nombre} sigue "
                f"vivo en el almacén y el estado no lo listaba: su envoltorio "
                f"{cron.envoltorio} queda y no se toca"
            )
            continue
        (home / cron.envoltorio).unlink(missing_ok=True)
    estado.unlink()


def _retirar_con_remove(home: Path, estado: Path, job_id: str, corredor) -> int:
    codigo, salida = corredor(["cron", "remove", job_id])
    if codigo != 0:
        # El remove SE INTENTÓ y falló: tocar nada a medias dejaría un cron
        # vivo sin estado que lo registre (igual que aplicar con create
        # rechazado). No se borra nada y el error sale impreso (F2).
        print(f"instalar-raices: hermes rechazó el remove: {salida.strip()}")
        return 1
    return 0


def _avisar_ya_retirado(nombre: str, jobs) -> None:
    # R1: el job de esta instalación ya no vive en el almacén = ya
    # retirado (el operador lo borró a mano o la store se fue). Un remove
    # con el id obsoleto fallaría y dejaría la reversa atascada en código 1
    # para siempre: no se llama, solo se quita lo nuestro.
    resto = ""
    if _hay_cron(jobs, nombre):
        resto = (
            f" (un job llamado {nombre} sigue en el almacén y no se "
            "toca: no es el de esta instalación)"
        )
    print(
        f"instalar-raices: el job de {nombre} ya no está en el "
        f"almacén: ya retirado{resto}"
    )


def revertir(home: Path, corredor) -> int:
    estado = home / RUTA_DE_ESTADO
    if not estado.exists():
        print("instalar-raices: sin instalación registrada; no se toca nada")
        return 0
    jobs = _leer_jobs(home)
    if jobs is None:
        print(_jobs_ilegibles(home) + "; no se toca nada")
        return 1
    guardados = _crons_del_estado(estado)
    nombres = list(guardados) or [cron.nombre for cron in CRONES]
    vivos = {
        entrada.get("id") for entrada in jobs if isinstance(entrada.get("id"), str)
    }
    for nombre in nombres:
        guardado = guardados.get(nombre)
        if guardado is not None:
            if guardado in vivos:
                # R2: el id guardado vivo manda. Si otro cron ajeno se llama
                # igual, el nombre no se toca.
                fallo = _retirar_con_remove(home, estado, guardado, corredor)
                if fallo:
                    return fallo
                continue
            # R1: nuestro id ya no vive: ya retirado. El nombre queda fuera a
            # propósito (podría ser un cron ajeno, R2).
            _avisar_ya_retirado(nombre, jobs)
            continue
        if _hay_cron(jobs, nombre):
            por_nombre = _id_del_cron(jobs, nombre)
            if por_nombre is None:
                # El job está en el almacén pero ni su registro ni el estado
                # traen id textual: no hay remove seguro. Fail-closed.
                print(
                    f"instalar-raices: el registro de {nombre} en "
                    f"{Path(home) / RUTA_DE_JOBS} no trae id y el estado tampoco "
                    "lo guardó; no se toca nada"
                )
                return 1
            # Sin id guardado, el nombre es el respaldo (R2).
            fallo = _retirar_con_remove(home, estado, por_nombre, corredor)
            if fallo:
                return fallo
            continue
        # R1: sin id guardado y sin el nombre en el almacén: nada que remove
        # pueda borrar; solo queda lo nuestro.
        _avisar_ya_retirado(nombre, jobs)
    tras_retirar = _leer_jobs(home)
    if tras_retirar is None:
        # F8: sin almacén legible tras los remove no se sabe qué sigue
        # vivo: fail-closed, ningún envoltorio se desenlaza y el estado
        # queda para una reversa futura.
        print(_jobs_ilegibles(home) + "; no se desenlaza ningún envoltorio")
        return 1
    _quitar_envoltorios_y_estado(home, estado, tras_retirar)
    print("instalar-raices: crones de raíces retirados; datos del operador intactos")
    return 0


def main(argv=None, corredor=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modo = parser.add_mutually_exclusive_group(required=True)
    modo.add_argument("--check", action="store_true", help="no escribe; dice qué haría")
    modo.add_argument(
        "--aplicar", action="store_true", help="instala crons y envoltorios"
    )
    modo.add_argument("--revertir", action="store_true", help="quita solo lo instalado")
    parser.add_argument("--home", type=Path, required=True, help="HERMES_HOME objetivo")
    parser.add_argument(
        "--plugin",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "plugins" / "goncloud-team",
        help="ruta del plugin goncloud-team con raices/",
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
