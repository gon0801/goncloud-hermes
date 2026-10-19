"""El ejecutor de tarjetas de raíz: un hijo CLI por tarjeta de equipo (SUA-07 H7).

El despachador nativo solo corre kanban.dispatch_profiles y a propósito (el
diseño §3.4: "las etapas de la cadena las ejecuta workflow.py con el router de
suscripciones"); las tarjetas de los perfiles de EQUIPO nadie las ejecutaba. El
diseño I08 lo pide: "poteto completo llega a cada ruta viva de H (turno de
claw, hijo CLI por tarjeta)". Este módulo es ese hijo.

DECISIÓN DE CAPA (con líneas): WorkflowService.subscription_run NO sirve para
una tarjeta de raíz. Antes de construir rutas de vendor llama a
WorkflowService._subscription_assignment_snapshot (workflow.py:697-727), que
exige `self._require_workflow_root(conn, workflow_id)` — una raíz de la cadena
GONCLOUD_ENGINEERING_V1 con assignee claw — y
`_require_subscription_membership` (workflow.py:784-795), que rechaza con
"stage identity mismatch" toda tarjeta que no sea etapa de ese workflow. Una
tarjeta de raíz no es etapa de ninguna cadena: subscription_run la niega
SIEMPRE. El ejecutor usa la capa de debajo, la misma que subscription_run
enchufa tras su autorización (workflow.py:635-650): SubscriptionRouter.run +
compile_profile_routes + InvocationRequest + sanitize_result. La autorización
aquí es doble: el reclamo con TTL que el ticker tomó en sistema-agentes.db
(store.reclamar_lanzamiento) y el lease propio del router (router.py:233-236,
claim_stage). El lease da además la recuperación I04 gratis: si el hijo muere
tras un run exitoso pero antes de completar la tarjeta, el relanzamiento recibe
el resultado grabado (router.py:267-269, latest_success sin result_transform).

La instrucción es el brief referenciado por `instructionRef` — el texto que el
ticker ya verificó contra su digest antes de crear la tarjeta
(ticker.py:144-160) — resuelto de nuevo del store content-addressed por este
hijo, más el formato del sobre terminal de LocalOrchestrator._instruction
(local_orchestrator.py:761-784) sin el bloque de deberes de etapa ni la rama
del reviewer: una raíz es la unidad completa del perfil (los deberes los
aporta el skill de su perfil) y el veredicto de los criterios lo emiten los
claims de claw, no el hijo. El outcome vuelve al mismo canal que los
especialistas nativos: la tarjeta se COMPLETA con el result JSON
que ticker._documento_de_resultado (ticker.py:335-371) pasa tal cual a
reportar_resultado — la evidencia (summary + evidence del sobre) viaja en el
store content-addressed de instrucciones (instrucciones.guardar), así claw la
verifica después por digest. Blocked, sobre inválido, router en fallo, router
sin cadena para el perfil, instrucción ilegible o workspace que no resuelve:
block_task con el motivo acotado (motivo_de_outcome) — NUNCA quedan
pendientes quemando la reserva de una llamada por vuelta; el ticker ya lo
trata como transporte caído (ticker.py:305-310) y decidir escala. Y si el
hijo murió sin llegar a cerrar la tarjeta, el ticker lee su archivo de
resultado (<raiz>/raices/ejecutor/<id>.json) en la vuelta siguiente y cierra
con la misma causa (ticker._cerrar_hijo_terminal).

Un rol de solo lectura (_READ_ONLY_STAGES de la cadena: scout, ingenieria —
live 2026-09-28) que modifique el workspace se trata igual que en la cadena:
huella antes/después y sobre bloqueado _edited_by_read_only_stage
(local_orchestrator.py:41-80, resume():723-745). El importe de
local_orchestrator es PEREZOSO (dentro de correr_intento) porque trae
hermes_cli: el resto del módulo queda importable en la Mac sin él.

PRESUPUESTO (I20): la reserva de UNA llamada (budget.maxModelCalls) la hace el
ticker al reclamar (store.reclamar_lanzamiento), no este hijo. El router no
reporta tokens (subscriptions/types.py: InvocationResult no trae usage), así
que la cuenta es de llamadas: una reservada por lanzamiento — el suelo cierto;
si el router empieza a reportar usage, el tope por tokens se añade ahí.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

instrucciones = importlib.import_module("raices.instrucciones")

PLUGIN = Path(__file__).resolve().parents[1]
#: El runtime de Hermes (el directorio que contiene el paquete hermes_cli), que
#: el hijo necesita para hermes_bootstrap y kanban_db. En producción se deriva
#: del hermes_cli vivo (la misma forma del driver de la cadena,
#: local_orchestrator.py:469); el fallback sirve donde hermes_cli no está.
HERMES_POR_OMISION = PLUGIN.parent.parent


def _runtime_de_hermes() -> Path:
    try:
        import hermes_cli
    except ImportError:
        return HERMES_POR_OMISION
    return Path(hermes_cli.__file__).resolve().parent.parent


MAX_STEPS = 40
#: El tope de pasos por corrida: el default de InvocationRequest
#: (subscriptions/types.py:55), el mismo suelo que usa la cadena sin contrato
#: explícito. El tope de corridas lo pone el budget de la raíz, no el hijo.

_PERFILES_FUERA_DE_CADENA = frozenset({"operaciones"})
#: Perfiles de equipo que no son etapas de la cadena. parse_terminal_envelope
#: exige un rol de INITIAL_STAGES (local_orchestrator.py:179); para ellos se
#: usa la vara de "verifier", que es exactamente la misma regla del sobre con
#: review obligada a null (local_orchestrator.py:211-238).

_SIN_VEREDICTO_PROPIO = _PERFILES_FUERA_DE_CADENA | {"reviewer"}
#: Perfiles cuyo sobre se juzga con la vara de verifier AUNQUE sean etapas de
#: la cadena. reviewer lo es, pero la regla del reviewer del parser
#: (local_orchestrator.py:223-238) exige review={decision, severity} para
#: completed, y esa sentencia no existe en una raíz: en la cadena la aplica
#: WorkflowService.review, y aquí el veredicto de los criterios lo dan los
#: claims que claw sube tras verificar (SKILL.md), no el hijo. La instrucción
#: de este módulo pide review=null; juzgar al hijo con la regla del reviewer
#: haría inválido TODO sobre completed y ninguna tarjeta de reviewer
#: completaría jamás (F2).

# El hijo desacoplado: solo rutas e identificadores por argv (nada del
# assignment viaja en la línea), no imprime nada y escribe UN resultado JSON
# acotado de forma atómica. Misma forma que el driver de la cadena
# (local_orchestrator.py:87-117), con hermes_bootstrap para que el import del
# plugin muera acotado y deje resultado aunque no cargue (live 2026-10-17).
_EJECUTOR_BOOTSTRAP = (
    "import json, os, sys\n"
    "plugin, hermes, store, kanban, root, unit, attempt, out = sys.argv[1:9]\n"
    "sys.path.insert(0, hermes)\n"
    "try:\n"
    "    try:\n"
    "        import hermes_bootstrap\n"
    "    except ModuleNotFoundError as exc:\n"
    "        if exc.name != 'hermes_bootstrap':\n"
    "            raise\n"
    "    sys.path.insert(0, plugin)\n"
    "    from raices.ejecutor import correr_intento\n"
    "    result = correr_intento(store, kanban, root, unit, int(attempt))\n"
    "except Exception as fallo:\n"
    "    result = {'rootId': root, 'unitId': unit, 'attempt': attempt,\n"
    "              'outcome': 'error', 'reason': str(fallo)[:400]}\n"
    "tmp = out + '.tmp'\n"
    "with open(tmp, 'w', encoding='utf-8') as handle:\n"
    "    json.dump(result, handle, ensure_ascii=True)\n"
    "os.replace(tmp, out)\n"
)


def instruccion_de_raiz(cuerpo: str) -> str:
    """El brief de la tarjeta + el formato del sobre terminal. El formato es
    el de LocalOrchestrator._instruction (local_orchestrator.py:770-783) sin
    el bloque de deberes de etapa — una raíz es la unidad completa del perfil,
    los deberes los aporta el skill de su perfil — y sin la rama del reviewer:
    en una raíz, el veredicto de los criterios no lo emite el hijo sino los
    claims que claw sube tras verificar."""
    return (
        f"{cuerpo}\n\n"
        "This assignment is the whole unit; carry it out within your duties. "
        "Report status completed once your own duties are done, even when the "
        "goal itself is not yet met; use blocked only when you cannot do your "
        "own part.\n"
        "Return one final fenced json object, its closing fence alone on its own "
        "line, and no content after that closing fence. "
        "Exact keys: status (completed or blocked), summary (nonempty, at most "
        "400 characters), evidence (at most 8 nonempty strings of at most 120 "
        "characters), review. Use review=null. "
        "Total JSON at most 1800 UTF-8 bytes. Do not include credentials, "
        "account identities, the assignment prompt, or raw logs. Do not update "
        "Kanban or complete cards yourself."
    )


def perfil_de_sobre(perfil: str, stages_iniciales) -> str:
    """El rol con el que se juzga el sobre. Los perfiles de la cadena usan su
    rol; reviewer y los de fuera (operaciones) la vara de verifier: mismo
    sobre, review obligada a null — la razón de reviewer en _SIN_VEREDICTO_PROPIO."""
    if perfil in _SIN_VEREDICTO_PROPIO or perfil not in stages_iniciales:
        return "verifier"
    return perfil


def workspace_de(revision) -> Path | None:
    """El workspace del hijo. `inputRevision.repository` es un slug owner/nombre
    por contrato (contrato.py:112 y 137: `[^/\\s]+/[^/\\s]+` — una ruta absoluta
    con dos barras jamás pasaría la validación de la autoridad, cuyo matching
    es EXACTO, contrato.py:719-720), así que el checkout local viaja en la
    llave extra `inputRevision.workspace` (el validador de la asignación admite
    llaves fuera de la forma mínima, como ya hace inputRevision). Orden:
    workspace primero, repository después — solo una ruta absoluta existente;
    un slug no nombra un directorio: None y la tarjeta se bloquea, falla
    cerrada, nunca inventar una ruta."""
    if not isinstance(revision, dict):
        return None
    for clave in ("workspace", "repository"):
        crudo = revision.get(clave)
        if not isinstance(crudo, str) or not crudo.strip():
            continue
        ruta = Path(crudo).expanduser()
        if not ruta.is_absolute():
            continue
        try:
            resuelta = ruta.resolve(strict=True)
        except OSError:
            continue
        if resuelta.is_dir():
            return resuelta
    return None


def _perfil_de(assignment: dict) -> str:
    target = assignment.get("target") or {}
    return str(target.get("agentId") or target.get("adapterId") or "sin-perfil")


def _conectar_kanban(ruta_kanban):
    from hermes_cli.kanban_db_connect import connect

    return connect(Path(ruta_kanban))


def _bloquear(conexion, card_id, motivo) -> bool:
    from hermes_cli.kanban_db import block_task

    return bool(block_task(conexion, card_id, reason=motivo[:1800]))


def _completar(conexion, card_id, carga: dict) -> bool:
    from hermes_cli.kanban_db import complete_task

    return bool(
        complete_task(
            conexion,
            card_id,
            result=json.dumps(carga, sort_keys=True, separators=(",", ":")),
        )
    )


_MOTIVOS_TERMINALES = {
    "workspace-unavailable": "workspace unavailable; owner review required",
    "instruccion-ilegible": "instruction unreadable; owner review required",
    "sin-driver": "executor sin driver de cadena; owner review required",
    "router-no-configurado": (
        "router has no chain for the profile; owner review required"
    ),
    "tarjeta-incompletable": "executor could not complete the card",
    "error": "executor crashed before reporting a terminal outcome",
}


def motivo_de_outcome(resultado: dict) -> str:
    """El motivo acotado para block_task a partir del JSON de resultado del
    hijo. Es la MISMA voz con la que el hijo bloquea (así el ticker, cuando el
    hijo murió sin cerrar la tarjeta, escribe exactamente el motivo que el
    hijo habría escrito); la razón, si viene, acota la causa."""
    outcome = str(resultado.get("outcome") or "error")
    motivo = _MOTIVOS_TERMINALES.get(outcome, f"executor outcome: {outcome}")
    razon = resultado.get("reason")
    if isinstance(razon, str) and razon.strip():
        return f"{motivo} ({razon.strip()[:400]})"
    return motivo


def _rutas_del_router(fabrica_router, perfil):
    """(router, routes) del perfil, la capa de debajo de subscription_run
    (workflow.py:635-636). fabrica_router es la costura de prueba, el mismo
    papel que service_factory en la cadena."""
    if fabrica_router is not None:
        return fabrica_router()
    try:
        from .subscriptions.catalog import compile_profile_routes
        from .subscriptions.router import SubscriptionRouter
    except ImportError:
        from subscriptions.catalog import compile_profile_routes
        from subscriptions.router import SubscriptionRouter

    router = SubscriptionRouter()
    return router, compile_profile_routes(perfil, router.routing_source)


def _resultado_del_router(router, routes, perfil, instruccion, workspace, sha, ids):
    """Un InvocationResult con la misma higiene que subscription_run
    (workflow.py:637-650): el prompt entra como secreto de redacción y el
    pre_invoke aquí no hace falta — la autorización ya la tomó el reclamo del
    ticker y el lease del router (claim_stage) es el punto de linealización."""
    try:
        from .subscriptions.redaction import sanitize_result
        from .subscriptions.types import FailureKind, InvocationRequest
    except ImportError:
        from subscriptions.redaction import sanitize_result
        from subscriptions.types import FailureKind, InvocationRequest

    root_id, unit_id, attempt = ids
    peticion = InvocationRequest(
        workflow_id=f"sa:{root_id}"[:128],
        stage_id=f"{unit_id}:{attempt}",
        role=perfil,
        prompt=instruccion,
        workspace=workspace,
        start_revision=sha,
        current_revision=sha,
        max_steps=MAX_STEPS,
    )
    return sanitize_result(router.run(peticion, routes), (instruccion,)), FailureKind


def _cargar_sobre(local, texto, perfil, stages_iniciales):
    """El sobre terminal parseado, o None si no lo es. Mismo parser y mismos
    topes que la cadena (local_orchestrator.parse_terminal_envelope)."""
    try:
        return local.parse_terminal_envelope(
            texto, role=perfil_de_sobre(perfil, stages_iniciales)
        )
    except Exception:
        return None


def _driver_de_cadena():
    """El módulo de la cadena con las piezas que H7 reusa: el parser del
    sobre, la huella del workspace y el sobre de solo lectura. Importe
    PEREZOSO: trae hermes_cli, y el resto de este módulo vive sin él."""
    try:
        return importlib.import_module("local_orchestrator")
    except ImportError:
        return None


class _LatidoDeReclamo:
    """Renueva el reclamo del lanzamiento MIENTRAS el hijo corre (F5). El TTL
    del reclamo (store.TTL_LANZAMIENTO_S) es mayor que el lease del router
    (_STAGE_LEASE_SECONDS), pero una corrida que sola dure más que el TTL
    vencería el reclamo y la vuelta siguiente relanzaría: el lease del router
    frena el doble run con stage_busy, pero el relanzamiento ya reservó otra
    llamada que no se devuelve. El latido extiende el vencimiento cada tercio
    de TTL con UN store nuevo por latido (ninguna conexión prestada entre
    hilos) y JAMÁS tumba al hijo: un error de renovación deja que el TTL
    decida. parar() pide el alto y une el hilo."""

    def __init__(self, ruta_store, root_id, unit_id, attempt):
        store = importlib.import_module("raices.store")
        self._ruta_store = str(ruta_store)
        self._ids = (root_id, unit_id, attempt)
        self._cada_s = max(1.0, store.TTL_LANZAMIENTO_S / 3.0)
        self._hecho = threading.Event()
        self._hilo = threading.Thread(target=self._latir, daemon=True)
        self._hilo.start()

    def _latir(self):
        store = importlib.import_module("raices.store")
        while not self._hecho.wait(self._cada_s):
            try:
                tienda = store.Store(self._ruta_store)
                try:
                    tienda.renovar_lanzamiento(
                        *self._ids, ttl_s=int(store.TTL_LANZAMIENTO_S)
                    )
                finally:
                    tienda.cerrar()
            except Exception:
                pass

    def parar(self):
        self._hecho.set()
        self._hilo.join(timeout=5.0)


def correr_intento(
    ruta_store,
    ruta_kanban,
    root_id,
    unit_id,
    attempt,
    *,
    fabrica_router=None,
) -> dict:
    """Una tarjeta de raíz, de punta a punta. Devuelve el outcome acotado del
    hijo (el documento del archivo de resultado); jamás lanza excepción hacia
    el bootstrap — todo desenlace es un dict."""
    store = importlib.import_module("raices.store")
    local = _driver_de_cadena()
    if local is None:
        # Sin el driver de la cadena no hay parser de sobre ni huella: no se
        # toca la tarjeta y la vuelta siguiente reintenta tras el TTL, como
        # cualquier hijo que murió sin decir nada.
        return {
            "rootId": root_id,
            "unitId": unit_id,
            "attempt": attempt,
            "outcome": "sin-driver",
        }
    stages_iniciales = importlib.import_module("workflow").INITIAL_STAGES
    tienda = store.Store(ruta_store)
    try:
        fila = tienda._una_fila(
            "SELECT card_id, assignment_json FROM intentos WHERE root_id = ?"
            " AND unit_id = ? AND attempt = ?",
            (root_id, unit_id, attempt),
        )
    finally:
        tienda.cerrar()
    if fila is None:
        return {
            "rootId": root_id,
            "unitId": unit_id,
            "attempt": attempt,
            "outcome": "intento-desconocido",
        }
    card_id = fila["card_id"]
    if card_id is None:
        return {
            "rootId": root_id,
            "unitId": unit_id,
            "attempt": attempt,
            "outcome": "sin-tarjeta",
        }
    assignment = json.loads(fila["assignment_json"])
    perfil = _perfil_de(assignment)
    revision = assignment.get("inputRevision") or {}
    conexion = _conectar_kanban(ruta_kanban)
    latido = None
    try:
        workspace = workspace_de(revision)
        if workspace is None:
            # F1: el desenlace documentado es el bloqueo, no la espera. Una
            # tarjeta que se quedara pendiente se relanzaría en cada vuelta
            # tras el TTL, quemando la reserva de UNA llamada por lanzamiento
            # hasta vaciar el presupuesto con una causa ajena.
            resultado = {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": "workspace-unavailable",
            }
            _bloquear(conexion, card_id, motivo_de_outcome(resultado))
            return resultado
        referencia = assignment.get("instructionRef") or {}
        cuerpo, motivo_ref = instrucciones.resolver(
            Path(ruta_store).parent / "raices" / "instrucciones",
            str(referencia.get("ref") or ""),
            str(referencia.get("digest") or ""),
        )
        if cuerpo is None:
            resultado = {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": "instruccion-ilegible",
                "reason": motivo_ref,
            }
            _bloquear(conexion, card_id, motivo_de_outcome(resultado))
            return resultado
        solo_lectura = perfil in local._READ_ONLY_STAGES
        antes = local._workspace_fingerprint(workspace) if solo_lectura else None
        try:
            router, routes = _rutas_del_router(fabrica_router, perfil)
        except ValueError as fallo:
            # Sin cadena para el perfil (catalog.compile_profile_routes): fallo
            # DETERMINISTA. Dejarlo llegar al bootstrap como "error" genérico
            # sería otro relanzamiento que quema la reserva en cada vuelta.
            resultado = {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": "router-no-configurado",
                "reason": str(fallo)[:400],
            }
            _bloquear(conexion, card_id, motivo_de_outcome(resultado))
            return resultado
        latido = _LatidoDeReclamo(ruta_store, root_id, unit_id, attempt)
        resultado, FailureKind = _resultado_del_router(
            router,
            routes,
            perfil,
            instruccion_de_raiz(cuerpo),
            workspace,
            str(revision.get("sha") or ""),
            (root_id, unit_id, attempt),
        )
        if (
            resultado.adapter == "router"
            and resultado.failure is FailureKind.TASK_FAILURE
        ):
            # stage busy o reintentable: OTRO ejecutor (o el mismo, más tarde)
            # se encarga; la tarjeta no se toca (el stage_busy de la cadena es
            # "running", local_orchestrator.py:731-734).
            return {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": "busy",
            }
        sobre = (
            _cargar_sobre(local, resultado.text, perfil, stages_iniciales)
            if resultado.success is True
            else None
        )
        if sobre is not None and antes is not None:
            despues = local._workspace_fingerprint(workspace)
            if despues is not None and despues != antes:
                sobre = local._edited_by_read_only_stage(perfil, antes, despues)
        if sobre is None:
            motivo = (
                "terminal envelope invalid; owner review required"
                if resultado.success is True
                else f"router failure: {resultado.failure}"
            )
            _bloquear(conexion, card_id, motivo)
            return {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": "blocked",
                "reason": motivo,
            }
        if sobre["status"] == "blocked":
            _bloquear(
                conexion,
                card_id,
                f"unit blocked by {perfil}: {sobre['summary']}"[:1800],
            )
            return {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": "blocked",
                "reason": sobre["summary"],
            }
        ref, digest = _guardar_evidencia(ruta_store, root_id, sobre)
        carga = {
            "resultContract": str(assignment.get("resultContract") or "review.v1"),
            "resultDigest": digest,
            "verdict": "approved",
            # La REFERENCIA del artefacto en la forma del contrato
            # (_PAYLOAD_DE_REVIEW exige {ref, digest}, contrato.py:280-286):
            # es lo que ticker._documento_de_resultado pasa tal cual al
            # reportar (ticker.py:358-364) y lo que claw puede reresolver y
            # verificar por digest en el mismo store.
            "evidenceRef": {"ref": ref, "digest": digest},
        }
        if not _completar(conexion, card_id, carga):
            return {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": "tarjeta-incompletable",
            }
        return {
            "rootId": root_id,
            "unitId": unit_id,
            "attempt": attempt,
            "outcome": "completed",
            "evidenceRef": ref,
            "resultDigest": digest,
        }
    finally:
        if latido is not None:
            latido.parar()
        try:
            conexion.close()
        except Exception:
            pass


def _guardar_evidencia(ruta_store, root_id, sobre) -> tuple[str, str]:
    """La evidencia del sobre (summary + evidence) al store content-addressed
    de instrucciones — la misma carpeta del store que abre este hijo, la misma
    que claw lee para verificar por digest (store.dir_de_instrucciones,
    store.py:1454-1455)."""
    documento = {
        "rootId": root_id,
        "summary": sobre["summary"],
        "evidence": list(sobre["evidence"]),
    }
    texto = json.dumps(documento, sort_keys=True, separators=(",", ":"))
    return instrucciones.guardar(
        Path(ruta_store).parent / "raices" / "instrucciones", texto
    )


#: Un id del contrato es solo texto no vacío (contrato.py:134): el nombre del
#: archivo de resultado lo acota a [A-Za-z0-9._-] para que un id raro no
#: escape del directorio (el chain driver exige su _WORKFLOW_ID en argv; aquí
#: el acotado va en el nombre, que es lo que toca el sistema de archivos).
_NOMBRE_SEGURO = re.compile(r"[^A-Za-z0-9._-]")


def _archivo_de_resultado(resultados: Path, root_id, unit_id, attempt) -> Path:
    limpio = _NOMBRE_SEGURO.sub("_", f"{root_id}-{unit_id}-{attempt}")
    return resultados / f"{limpio}.json"


def lanzador_de(base, ruta_kanban):
    """El lanzador de producción: un hijo Python desacoplado por tarjeta, la
    misma forma que LocalOrchestrator.launch_detached (local_orchestrator.py:449-496):
    argv con rutas e identificadores, sin salida, start_new_session para que la
    vuelta del cron no espere ni herede señales. Devuelve None si no pudo
    nacer — el ticker suelta el reclamo y la vuelta siguiente reintenta."""

    def lanzar(root_id, unit_id, attempt, card_id):
        del card_id  # el hijo relee todo de las bases; nada de datos por argv
        base_ruta = Path(base)
        resultados = base_ruta / "raices" / "ejecutor"
        resultados.mkdir(mode=0o700, parents=True, exist_ok=True)
        archivo = _archivo_de_resultado(resultados, root_id, unit_id, attempt)
        argv = [
            sys.executable,
            "-I",
            "-c",
            _EJECUTOR_BOOTSTRAP,
            str(PLUGIN),
            str(_runtime_de_hermes()),
            str(Path(base) / "sistema-agentes.db"),
            str(ruta_kanban),
            str(root_id),
            str(unit_id),
            str(attempt),
            str(archivo),
        ]
        desacople = (
            {
                "creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            }
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            proceso = subprocess.Popen(
                argv,
                cwd=str(base_ruta),
                env=dict(os.environ),
                stdin=None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **desacople,
            )
        except OSError:
            return None
        return {"pid": proceso.pid, "resultFile": str(archivo)}

    return lanzar
