"""La puerta de capacidades de la ruta de raíces (SUA-07 H4a).

Sin caché ni estado persistido: cada llamada relee el par (la versión del
CLI y <home>/gateway_state.json), sondea las capacidades del hermes_cli
vivo y decide. La causa sale en un orden fijo, la capacidad que falta
primero y el par sin certificar al final, para que el aviso del cron apunte
a un solo problema por vez. Ningún lector lanza: un mundo ilegible cierra
la ruta.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

CAPACIDADES_REQUERIDAS = (
    "create_task",
    "link_tasks",
    "complete_task",
    "block_task",
    "write_txn",
)

# Dónde vive cada capacidad: las cuatro operaciones en hermes_cli.kanban_db
# y la transacción exclusiva en hermes_cli.kanban_db_connect.
_MODULO_DE = {
    "create_task": "hermes_cli.kanban_db",
    "link_tasks": "hermes_cli.kanban_db",
    "complete_task": "hermes_cli.kanban_db",
    "block_task": "hermes_cli.kanban_db",
    "write_txn": "hermes_cli.kanban_db_connect",
}

RUTA_CERTIFICADAS = Path(__file__).with_name("certificadas.json")

_FORMA_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_FORMA_SHA = re.compile(r"^[0-9a-f]{40}$")
_FORMA_INSTANTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# El tramo "+N.g<sha>" manda cuando existe. Si el CLI es de release, la
# versión es "v<x.y.z>" y el sufijo es el hex tras "· local " (7 a 40 hex);
# ese hex cruza contra code_sha igual que el de "+N.g". El hex de
# "· upstream" es el commit de upstream, no el que corre: nunca cuenta como
# sufijo. Sin ninguno de los dos no hay comprobación cruzada contra
# code_sha: un texto así no parsea a par vivo y la puerta queda cerrada
# por par-incierto.
_VERSION_CLI = re.compile(r"Hermes Agent v(\d+\.\d+\.\d+)\+\d+\.g([0-9a-f]+)")
_VERSION_CLI_RELEASE = re.compile(
    r"Hermes Agent v(\d+\.\d+\.\d+)\b.*?· local ([0-9a-f]{7,40})(?![0-9a-f])"
)


class VersionCLI(NamedTuple):
    version: str
    sufijo: str


class ParGateway(NamedTuple):
    kind: str
    gateway_state: str
    code_version: str
    code_sha: str
    hermes_home: str
    updated_at: str


class Hechos(NamedTuple):
    version: VersionCLI | None
    gateway: ParGateway | None
    certificadas: tuple
    capacidades: frozenset
    home: str


def _correr_hermes_version(resolvedor=None) -> str | None:
    """El texto de `--version` vía la RUTA que devuelve shutil.which: en
    Windows el CLI es hermes.CMD y CreateProcess sin shell no resuelve el
    nombre pelado (WinError 2). Sin ruta resoluble no se invoca nada y no
    hay texto (fail-closed, igual que un CLI roto)."""
    ejecutable = (resolvedor or shutil.which)("hermes")
    if not ejecutable:
        return None
    try:
        terminado = subprocess.run(
            [ejecutable, "--version"],
            capture_output=True,
            text=True,
            # text=True sin encoding usa la code page local: en un Windows no
            # UTF-8 el texto de --version revienta decodeando (mojibake) y la
            # lectura de la version se pierde por un detalle de codificacion.
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if terminado.returncode != 0:
        return None
    return terminado.stdout or None


def leer_version(runner=None) -> VersionCLI | None:
    """La versión del CLI vivo según `runner` (la salida de --version). Sin
    coincidencia no hay versión, y sin versión no hay par que certificar."""
    try:
        texto = (runner or _correr_hermes_version)()
    except Exception:
        return None
    if not isinstance(texto, str):
        return None
    hallado = _VERSION_CLI.search(texto) or _VERSION_CLI_RELEASE.search(texto)
    if hallado is None:
        return None
    return VersionCLI(hallado.group(1), hallado.group(2))


def leer_gateway(home) -> ParGateway | None:
    """Solo <home>/gateway_state.json: por construcción el par vivo está en la
    raíz del home, nunca en perfiles ni snapshots; leer otro archivo sería
    creerle a un par que no está corriendo. No valida semántica: eso es de
    decidir."""
    try:
        datos = json.loads(Path(home, "gateway_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(datos, dict):
        return None
    return ParGateway(
        datos.get("kind"),
        datos.get("gateway_state"),
        datos.get("code_version"),
        datos.get("code_sha"),
        datos.get("hermes_home"),
        datos.get("updated_at"),
    )


def leer_certificadas(ruta=None) -> tuple:
    """La lista de pares certificados; ilegible o no-lista es (): lo que no
    se puede leer no certifica nada."""
    camino = Path(ruta) if ruta is not None else RUTA_CERTIFICADAS
    try:
        datos = json.loads(camino.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    return tuple(datos) if isinstance(datos, list) else ()


def sondear_capacidades(importador=None) -> frozenset:
    """Los nombres requeridos que el hermes_cli vivo expone hoy. Sin un
    módulo no hay sondeo parcial: las cinco capacidades vienen de los dos
    módulos o la puerta no afirma nada."""
    cargar = importador or importlib.import_module
    try:
        modulos = {nombre: cargar(nombre) for nombre in set(_MODULO_DE.values())}
    except Exception:
        return frozenset()
    return frozenset(
        capacidad
        for capacidad in CAPACIDADES_REQUERIDAS
        if hasattr(modulos[_MODULO_DE[capacidad]], capacidad)
    )


def _resuelto(ruta) -> str:
    return os.path.realpath(str(ruta))


def _causa_de_par(hechos) -> str | None:
    version = hechos.version
    gateway = hechos.gateway
    if not isinstance(version, VersionCLI) or not isinstance(gateway, ParGateway):
        return "par-incierto"
    if gateway.kind != "hermes-gateway":
        return "par-incierto"
    if gateway.gateway_state != "running":
        return "par-incierto"
    if not isinstance(gateway.code_sha, str) or not _FORMA_SHA.match(gateway.code_sha):
        return "par-incierto"
    if gateway.code_version != version.version:
        return "par-incierto"
    if not gateway.code_sha.startswith(version.sufijo):
        return "par-incierto"
    if _resuelto(gateway.hermes_home) != _resuelto(hechos.home):
        return "par-incierto"
    if not gateway.updated_at:
        return "par-incierto"
    return None


def _certificado(hechos) -> bool:
    """La comparación de code_sha en minúsculas: el par certificado es el par
    vivo, no su ortografía."""
    version = hechos.version.version
    sha = hechos.gateway.code_sha.lower()
    entradas = hechos.certificadas
    if not isinstance(entradas, (list, tuple)):
        return False
    for entrada in entradas:
        if not isinstance(entrada, dict):
            continue
        if (
            entrada.get("version") == version
            and str(entrada.get("code_sha", "")).lower() == sha
        ):
            return True
    return False


def decidir(hechos) -> tuple[bool, str | None]:
    """Pura y total: con hechos malformados cierra. El orden de causas es
    fijo porque el aviso del cron nombra una sola por vez.

    La versión sin certificar ya no cierra (encargo H punto 3): abre con
    aviso — (True, "version-no-certificada") — y los consumidores avisan
    sin bloquear. Las causas de capacidad y de par incierto siguen
    cerrando."""
    vivas = getattr(hechos, "capacidades", None)
    if not isinstance(vivas, (set, frozenset)):
        vivas = frozenset()
    for capacidad in CAPACIDADES_REQUERIDAS:
        if capacidad not in vivas:
            return False, f"capacidad-faltante:{capacidad}"
    try:
        causa = _causa_de_par(hechos)
    except Exception:
        return False, "par-incierto"
    if causa is not None:
        return False, causa
    if not _certificado(hechos):
        return True, "version-no-certificada"
    return True, None


def hechos_vivos(
    home=None, runner=None, importador=None, ruta_certificados=None
) -> Hechos:
    """Junta los lectores del mundo; ninguno tumba la llamada."""
    casa = home if home is not None else os.environ.get("HERMES_HOME", "")
    return Hechos(
        leer_version(runner),
        leer_gateway(casa),
        leer_certificadas(ruta_certificados),
        sondear_capacidades(importador),
        casa,
    )


def abrir_ruta(home=None, runner=None, importador=None, ruta_certificados=None):
    """La puerta: hechos vivos y decisión. (bool, causa|None)."""
    return decidir(hechos_vivos(home, runner, importador, ruta_certificados))


def validar_certificadas(lista) -> list:
    """Los errores de forma de la lista de pares certificados; [] si vale.
    Un solo punto para la prueba del repo y certificar.py, así el archivo y
    su escritura no pueden discrepar sobre qué es una entrada válida."""
    if not isinstance(lista, list):
        return ["la lista certificada no es una lista"]
    errores: list = []
    vistos: set = set()
    for posicion, entrada in enumerate(lista):
        if not isinstance(entrada, dict):
            errores.append(f"entrada {posicion}: no es un objeto")
            continue
        version = entrada.get("version")
        if not isinstance(version, str) or not _FORMA_VERSION.match(version):
            errores.append(f"entrada {posicion}: version no es semver X.Y.Z")
        sha = entrada.get("code_sha")
        if not isinstance(sha, str) or not _FORMA_SHA.match(sha):
            errores.append(f"entrada {posicion}: code_sha no son 40 hex minúsculas")
        instante = entrada.get("certificadoEn")
        if not isinstance(instante, str) or not _FORMA_INSTANTE.match(instante):
            errores.append(f"entrada {posicion}: certificadoEn no es ISO 8601 Z")
        if not entrada.get("evidencia"):
            errores.append(f"entrada {posicion}: falta la evidencia")
        if isinstance(version, str) and isinstance(sha, str):
            clave = (version, sha.lower())
            if clave in vistos:
                errores.append(f"entrada {posicion}: par (version, code_sha) duplicado")
            vistos.add(clave)
    return errores
