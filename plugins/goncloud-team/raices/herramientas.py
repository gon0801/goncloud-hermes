"""Las herramientas goncloud_root_* del contrato, solo para el perfil claw (H2).

submit abre la raiz (el store valida el documento contra la copia fijada),
read devuelve el snapshot de la raiz (la proyeccion de derivar llega en H3),
control entrega el recibo de control.v1 con replay y resolve guarda la
respuesta del dueno en la excepcion. Los nombres de campo son los del contrato.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

store = importlib.import_module("raices.store")

PERFIL_AUTORIZADO = "claw"


def _exigir_claw(perfil):
    if perfil != PERFIL_AUTORIZADO:
        raise PermissionError(
            f"only the claw profile may manage raices (got {perfil!r})"
        )


def ruta_del_store(home=None):
    base = Path(home or os.environ.get("HERMES_HOME", ""))
    if not str(base):
        raise ValueError("HERMES_HOME is not set; no raices store to open")
    return base / "sistema-agentes.db"


def abrir_tienda(home=None):
    return store.Store(ruta_del_store(home))


def _de_ok(respuesta, documento):
    if hasattr(respuesta, "codigo"):
        return {"error": respuesta.codigo, "detalle": respuesta.detalle or {}}
    return {**documento(respuesta.valor), "replayed": bool(respuesta.replayed)}


def enviar_raiz(argumentos, perfil, home=None):
    _exigir_claw(perfil)
    plan = (argumentos or {}).get("plan")
    if not isinstance(plan, dict):
        return {
            "error": "documento-invalido",
            "detalle": {"regla": "el cuerpo trae el documento meta.v1 en plan"},
        }
    tienda = abrir_tienda(home)
    try:
        respuesta = tienda.abrir_raiz(plan)
    finally:
        tienda.cerrar()
    return _de_ok(
        respuesta,
        lambda recibo: {
            "rootId": recibo.root_id,
            "planRevision": recibo.plan_revision,
            "controlEpoch": recibo.control_epoch,
        },
    )


def leer_raiz(argumentos, perfil, home=None):
    _exigir_claw(perfil)
    root_id = str((argumentos or {}).get("rootId", ""))
    tienda = abrir_tienda(home)
    try:
        snapshot = tienda.leer_vista(root_id, {})
    finally:
        tienda.cerrar()
    if snapshot is None:
        return {"error": "raiz-desconocida", "detalle": {"rootId": root_id}}
    return snapshot


def controlar_raiz(argumentos, perfil, home=None):
    _exigir_claw(perfil)
    datos = dict(argumentos or {})
    comando = {
        "schema": "sistema-agentes.control.v1",
        "rootId": str(datos.get("rootId", "")),
        "command": datos.get("command"),
        "commandId": str(datos.get("commandId", "")),
        "expectedControlEpoch": datos.get("expectedControlEpoch"),
        "reason": datos.get("reason"),
        "issuedAt": store.ahora_utc(),
    }
    tienda = abrir_tienda(home)
    try:
        respuesta = tienda.control(comando)
    finally:
        tienda.cerrar()
    return _de_ok(
        respuesta,
        lambda recibo: {
            "controlEpoch": recibo.control_epoch,
            "state": recibo.state,
            "committedEffects": recibo.committed_effects,
        },
    )


def resolver_excepcion(argumentos, perfil, home=None):
    _exigir_claw(perfil)
    datos = dict(argumentos or {})
    exception_id = str(datos.get("exceptionId", ""))
    tienda = abrir_tienda(home)
    try:
        respuesta = tienda.responder_excepcion(
            datos.get("rootId"),
            exception_id,
            str(datos.get("optionId", "")),
            datos.get("by"),
        )
    finally:
        tienda.cerrar()
    return _de_ok(respuesta, lambda _valor: {"exceptionId": exception_id})
