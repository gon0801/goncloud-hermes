"""La autoridad real de una raíz en H (I07): la intersección entre
contrato.efecto_admisible de la copia fijada y los permisos de H.

La autoridad embebida en meta NO se amplía con esto: la intersección solo
aprieta. Un efecto procede solo si el contrato lo admite y H también lo
permite; si el contrato lo niega, la razón es del contrato, y si lo admite
pero H niega, el efecto no corre y se levanta una excepción con la razón de H.
Un prompt no amplía ninguna de las dos.

Los permisos de H son datos inyectables; su lector real del config de la PC es
H2. Aquí entran como dict: acciones que la matriz de PERMISOS.md permite sin
preguntar, toolsets del perfil que corre la unidad y allowlist de comandos.
Sin datos (None) el gate falla cerrado: se niega todo con la razón propia
sin-permisos, igual que un dict vacío niega con las suyas.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping

compartido = importlib.import_module("raices.compartido")
contrato = compartido.contrato

HERRAMIENTA_POR_MODO = {
    "design": "read",
    "read": "read",
    "implement": "write",
    "publish": "write",
}


class PermisosH:
    """Los permisos de H como datos. Ninguna entrada amplía la autoridad.
    Sin datos (None) se niega todo con la razón propia sin-permisos."""

    def __init__(self, datos: dict | None = None):
        self.sin_datos = datos is None
        datos = datos or {}
        self.acciones = frozenset(datos.get("acciones") or ())
        self.toolsets = {
            perfil: frozenset(lista or ())
            for perfil, lista in (datos.get("toolsets") or {}).items()
        }
        self.comandos = frozenset(datos.get("comandos") or ())

    def niega_accion(self, accion: object) -> str | None:
        if self.sin_datos:
            return "sin-permisos"
        if accion in self.acciones:
            return None
        return "accion-preguntar"

    def niega_perfil(self, perfil: str, modo: str) -> str | None:
        if self.sin_datos:
            return "sin-permisos"
        toolsets = self.toolsets.get(perfil)
        if toolsets is None:
            return "perfil-sin-toolset"
        requerida = HERRAMIENTA_POR_MODO.get(modo, "write")
        if requerida not in toolsets:
            return "toolset-faltante"
        return None

    def niega_comando(self, comando: str) -> str | None:
        if self.sin_datos:
            return "sin-permisos"
        if comando in self.comandos:
            return None
        return "comando-fuera-de-allowlist"


def interseccion(
    autoridad_meta: Mapping, permisos: PermisosH | None, ahora: str
) -> Callable[[Mapping], str | None]:
    """El callable que decidir espera: razón del contrato, o de H, o None."""

    def negar(efecto: Mapping) -> str | None:
        razon = contrato.efecto_admisible(autoridad_meta, efecto, ahora)
        if razon is not None:
            return razon
        if permisos is None:
            return "sin-permisos"
        return permisos.niega_accion(efecto.get("action"))

    return negar


def perfil_de(assignment: Mapping) -> str:
    target = assignment.get("target") or {}
    identidad = target.get("agentId") or target.get("adapterId")
    return str(identidad) if identidad else "sin-perfil"


def efecto_de_arranque(assignment: Mapping, unidad_plan: Mapping | None) -> dict:
    scope = (unidad_plan or {}).get("scope") or {}
    return {
        "action": "implement",
        "repo": (assignment.get("inputRevision") or {}).get("repository"),
        "mode": scope.get("mode", "implement"),
    }


def negar_arranque(
    autoridad_meta: Mapping,
    permisos: PermisosH | None,
    ahora: str,
    assignment: Mapping,
    unidad_plan: Mapping | None,
) -> str | None:
    """La razón por la que H no arranca la unidad, o None si procede."""
    efecto = efecto_de_arranque(assignment, unidad_plan)
    razon = contrato.efecto_admisible(autoridad_meta, efecto, ahora)
    if razon is not None:
        return razon
    if permisos is None:
        return "sin-permisos"
    return permisos.niega_perfil(perfil_de(assignment), efecto["mode"])


def causa_de(razon: str) -> str:
    if razon in (
        "sin-permisos",
        "accion-preguntar",
        "perfil-sin-toolset",
        "toolset-faltante",
    ):
        return f"permiso-h:{razon}"
    return f"autoridad:{razon}"
