"""Las herramientas goncloud_root_* del contrato, solo para el perfil claw (H2).

submit abre la raiz (el store valida el documento contra la copia fijada),
read devuelve el snapshot de la raiz (la proyeccion de derivar llega en H3),
control entrega el recibo de control.v1 con replay y resolve guarda la
respuesta del dueno en la excepcion. Los nombres de campo son los del contrato.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

store = importlib.import_module("raices.store")
hogar = importlib.import_module("raices.hogar")
instrucciones = importlib.import_module("raices.instrucciones")

PERFIL_AUTORIZADO = "claw"
TOPE_DE_INSTRUCCION = 32768


def _exigir_claw(perfil):
    if perfil != PERFIL_AUTORIZADO:
        raise PermissionError(
            f"only the claw profile may manage raices (got {perfil!r})"
        )


def ruta_del_store(home=None):
    """F6: el store vive en la RAIZ del home de Hermes (raices/hogar), la
    misma ruta para el perfil claw del gateway y el cron del perfil default."""
    return hogar.raiz_del_hogar(home) / "sistema-agentes.db"


def abrir_tienda(home=None):
    """El store con los permisos de H de la misma fuente que el cron
    (<raiz>/raices/permisos.json via hogar, con los toolsets intersectados
    contra el config real; I07/H4b3): el resume recontrasta lo que va a correr
    con esos permisos, no con un None que niega todo."""
    return store.Store(ruta_del_store(home), permisos_h=hogar.leer_permisos(home))


def _de_ok(respuesta, documento):
    if hasattr(respuesta, "codigo"):
        return {"error": respuesta.codigo, "detalle": respuesta.detalle or {}}
    return {**documento(respuesta.valor), "replayed": bool(respuesta.replayed)}


def enviar_raiz(argumentos, perfil, home=None, ruta_abierta=None):
    _exigir_claw(perfil)
    plan = (argumentos or {}).get("plan")
    if not isinstance(plan, dict):
        return {
            "error": "documento-invalido",
            "detalle": {"regla": "el cuerpo trae el documento meta.v1 en plan"},
        }
    # submit crea trabajo nuevo y la puerta de capacidades (H4a) lo niega con
    # la ruta cerrada, sin abrir la tienda; el dueño sigue leyendo y frenando
    # con read, control y resolve. La puerta mira el gateway_state.json de la
    # RAIZ (hogar.raiz_del_hogar: el home de un perfil sube, nunca HERMES_HOME
    # pelado; cierra el F8 del bot).
    capacidades = importlib.import_module("raices.capacidades")
    raiz = hogar.raiz_del_hogar(home)
    puerta = ruta_abierta or (lambda: capacidades.abrir_ruta(home=raiz))
    abierta, causa = puerta()
    if not abierta:
        return {"error": "ruta-cerrada", "detalle": {"causa": causa}}
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
        if snapshot is not None:
            snapshot["necesidades"] = tienda.necesidades_de(root_id)
    finally:
        tienda.cerrar()
    if snapshot is None:
        return {"error": "raiz-desconocida", "detalle": {"rootId": root_id}}
    permisos = hogar.leer_permisos(home) or {}
    cobertura = permisos.get("cobertura")
    if not isinstance(cobertura, dict):
        cobertura = {}
    for unidad in snapshot["unidades"]:
        assignment = unidad.get("assignment") or {}
        if assignment.get("resultContract") != "review.v1":
            continue
        target = assignment.get("target") or {}
        specialist = str(
            target.get("agentId") or target.get("adapterId") or "sin-perfil"
        )
        unidad["cobertura"] = cobertura.get(specialist, "unknown")
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
    root_id = datos.get("rootId")
    if not isinstance(root_id, str) or not root_id:
        return {"error": "root-id-requerido", "detalle": {"exceptionId": exception_id}}
    tienda = abrir_tienda(home)
    try:
        respuesta = tienda.responder_excepcion(
            root_id,
            exception_id,
            str(datos.get("optionId", "")),
            datos.get("by"),
        )
    finally:
        tienda.cerrar()
    return _de_ok(respuesta, lambda _valor: {"exceptionId": exception_id})


def guardar_instruccion(argumentos, perfil, home=None):
    """El texto del brief entra al store content-addressed; nunca viaja
    dentro de la asignación (fixture G)."""
    _exigir_claw(perfil)
    texto = (argumentos or {}).get("text")
    if not isinstance(texto, str) or not texto or len(texto) > TOPE_DE_INSTRUCCION:
        return {
            "error": "documento-invalido",
            "detalle": {"regla": "text: texto de 1 a 32768 caracteres"},
        }
    ref, digest = instrucciones.guardar(
        hogar.raiz_del_hogar(home) / "raices" / "instrucciones", texto
    )
    return {"ref": ref, "digest": digest}


def _asignacion_cruda(item):
    return (
        isinstance(item, dict)
        and isinstance(item.get("unitId"), str)
        and item["unitId"] != ""
        and isinstance(item.get("assignment"), dict)
        and isinstance(item.get("relay"), bool)
    )


def reconciliar_raiz(argumentos, perfil, home=None):
    _exigir_claw(perfil)
    datos = argumentos or {}
    root_id = datos.get("rootId")
    crudos = datos.get("asignaciones")
    if (
        not isinstance(root_id, str)
        or not root_id
        or not isinstance(crudos, list)
        or not all(_asignacion_cruda(item) for item in crudos)
    ):
        return {
            "error": "documento-invalido",
            "detalle": {"regla": "asignaciones: lista de {unitId, assignment, relay}"},
        }
    tienda = abrir_tienda(home)
    try:
        respuesta = tienda.reconciliar_asignaciones(root_id, crudos)
    finally:
        tienda.cerrar()
    if hasattr(respuesta, "codigo"):
        return {"error": respuesta.codigo, "detalle": respuesta.detalle or {}}
    recibo = respuesta.valor
    return {
        "rootId": recibo.root_id,
        "planRevision": recibo.plan_revision,
        "asignaciones": recibo.asignaciones,
        "replayed": bool(recibo.asignaciones)
        and all(item["replayed"] for item in recibo.asignaciones),
        "necesidades": recibo.necesidades,
    }
