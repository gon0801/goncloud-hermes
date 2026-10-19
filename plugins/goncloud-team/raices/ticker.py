"""El ticker de las raíces: una vuelta = conciliar, decidir y despachar (SUA-07 H1).

Puede haber dos tickers a la vez: la exclusividad la dan las transacciones
(BEGIN IMMEDIATE en sistema-agentes.db y write_txn en el kanban), no la memoria.
Cada paso es idempotente por filas: una vuelta interrumpida en cualquier punto la
completa la siguiente (I04). El despacho pasa por la bandeja de salida: la intención
en sistema-agentes.db, buscar-o-crear la tarjeta en UN write_txn del kanban y, recién
entonces, marcar la intención cumplida (I03). Las raíces con control != active no
despachan: sus intenciones quedan cercadas. Con home y sin datos de permisos
de H, el despacho exige el paquete poteto del perfil (H4b2): sin él se levanta
NECESITA_DAVID con causa poteto:<perfil> y no se despacha. El hijo del
ejecutor (H7) escribe un resultado JSON acotado (<raiz>/raices/ejecutor/
<id>.json): en la vuelta siguiente el ticker lo lee y un desenlace terminal
de error cierra la tarjeta con su causa y devuelve la reserva si no hubo
llamada al modelo — un hijo muerto nunca vuelve a la cola como si nada.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import NamedTuple

compartido = importlib.import_module("raices.compartido")
decidir = importlib.import_module("raices.decidir")
kanban_port = importlib.import_module("raices.kanban_port")
store_module = importlib.import_module("raices.store")
autoridad = importlib.import_module("raices.autoridad")
flota = importlib.import_module("raices.flota")
poteto = importlib.import_module("raices.poteto")

CaidaInyectada = kanban_port.CaidaInyectada
SpecDeTarjeta = kanban_port.SpecDeTarjeta
digesto = decidir.digesto

LLAVES_DE_DECISION = (
    "consumidos",
    "claims",
    "outcomes",
    "bindings",
    "efectos",
    "contadores",
    "excepciones",
    "respuestas_aplicadas",
    "control",
)

OUTCOMES_TERMINALES = frozenset(
    {
        "error",
        "sin-driver",
        "workspace-unavailable",
        "workspace-no-autorizado",
        "instruccion-ilegible",
        "router-no-configurado",
        "tarjeta-incompletable",
    }
)
#: Desenlaces del hijo que ya no progresan por sí solos (los que bloquean la
#: tarjeta en ejecutor.correr_intento y el error del bootstrap). "busy",
#: "completed" y "blocked" NO lo son: el primero se resuelve con el lease del
#: router y los otros dos dejan la tarjeta cerrada.

OUTCOMES_SIN_LLAMADA = frozenset(
    {
        "sin-driver",
        "workspace-unavailable",
        "workspace-no-autorizado",
        "instruccion-ilegible",
        "router-no-configurado",
    }
)
#: Los terminales que ocurren ANTES de la llamada al modelo: la reserva que
#: reclamar_lanzamiento hizo no se consumió y se devuelve (store
#: .devolver_lanzamiento). "error" y "tarjeta-incompletable" pueden llegar
#: después de un run: conservan la reserva — el suelo cierto del presupuesto.


class Informe(NamedTuple):
    conciliadas: int
    aplicadas: int
    conflictos: tuple
    tarjetas_creadas: int
    estado: str | None
    lanzamientos: int = 0


def cargar_politicas() -> dict:
    """Las versiones de politica-calidad.json de la copia fijada. La I/O vive aquí
    para que decidir siga pura; el store congela la versión de cada raíz al abrirla."""
    return store_module.leer_politicas()


def perfiles_de_despacho_nativo(raiz=None) -> frozenset | None:
    """Los especialistas que el despachador nativo del gateway SÍ corre
    (team.json kanban.dispatch_profiles). A esos el ticker NUNCA les lanza el
    ejecutor: su ruta es la compuerta poteto del gateway, que los niega con
    causa poteto:<perfil> (H4b2, I08). `raiz` es la costura de prueba; sin
    ella, la raíz del repo — la misma resolución que usa poteto para su copia
    (poteto.REPO = parents[3]; el cron de producción corre desde el checkout,
    que es donde vive team.json). FAIL CLOSED (hilo de CodeRabbit 4309610867):
    un team.json ausente, ilegible o sin dispatch_profiles NO es "nadie lo
    corre el nativo" (eso lanzaría ejecutores DOBLES — hijo y despachador
    nativo — y dos reservas por tarjeta) sino "no se sabe": None y
    _despachar_ejecutores se salta ENTERA la fase de lanzamiento de la vuelta
    con el aviso en stderr; la vuelta siguiente, con el archivo bueno, sigue."""
    raiz = Path(raiz) if raiz is not None else Path(__file__).resolve().parents[3]
    try:
        team = json.loads((raiz / "team.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    perfiles = (team.get("kanban") or {}).get("dispatch_profiles")
    if not isinstance(perfiles, list):
        return None
    return frozenset(str(p) for p in perfiles)


def vuelta(
    tienda,
    puerto,
    hechos_por_raiz=None,
    permisos_h=None,
    ruta_abierta=True,
    home=None,
    lanzador=None,
) -> Informe:
    """Una vuelta completa. Una CaidaInyectada del doble kanban se propaga: simula la
    muerte del proceso, y la vuelta siguiente recupera desde las filas.
    `permisos_h` es el dict de permisos de H (I07): None o ausentes niegan todo.
    `home` es el %HERMES_HOME% de los perfiles: con él el despacho congela el
    documento flota.v1 del intento que admite (I09).
    Con `ruta_abierta=False` (H4a) la puerta de capacidades está cerrada: bloquea
    y concilia igual (un intento abierto conserva su snapshot), pero no decide, no
    despacha ni levanta el gate de arranque.
    `lanzador` (H7) es la función que nace un hijo CLI por tarjeta de un perfil
    de equipo: el cron pasa el lanzador desacoplado de producción
    (ejecutor.lanzador_de); None no lanza nada (las pruebas inyectan el suyo).
    Los gates — perfil de equipo, presupuesto maxModelCalls y reclamo atómico
    con TTL — viven en _despachar_ejecutores, DESPUÉS de la bandeja: la tarjeta
    ya existe cuando se lanza su ejecutor (I03 cierra antes)."""
    hechos_por_raiz = hechos_por_raiz or {}
    permisos = autoridad.PermisosH(permisos_h)
    conciliadas = 0
    aplicadas = 0
    conflictos: list = []
    creadas_antes = len(puerto.creaciones)
    lanzados = 0
    estado: str | None = None

    for bloqueo in tienda.bloqueos_pendientes():
        with puerto.write_txn() as escritura:
            escritura.bloquear(bloqueo["cardId"], bloqueo["motivo"])
        tienda.bloqueo_hecho(bloqueo["rootId"], bloqueo["cardId"])

    for intento in tienda.intentos_con_tarjeta():
        if _conciliar_uno(tienda, puerto, intento):
            conciliadas += 1

    if ruta_abierta:
        vistas: dict = {}
        for raiz in tienda.raices():
            root_id = raiz["rootId"]
            vista = tienda.leer_vista(
                root_id, hechos_por_raiz.get(root_id) or _hechos_vacios()
            )
            if vista is None:
                continue
            vistas[root_id] = vista
            propuesta = decidir.decidir(vista)
            tienda.asentar_necesidades(root_id, propuesta.necesidades)
            decision = propuesta.decision
            if any(len(decision[llave]) > 0 for llave in LLAVES_DE_DECISION):
                resultado = tienda.aplicar(
                    root_id, decision, _decision_id(vista, propuesta)
                )
                if hasattr(resultado, "codigo"):
                    conflictos.append((root_id, resultado.codigo))
                else:
                    aplicadas += 1
            # El aviso de las excepciones no pasa por aquí: el checkpoint
            # (noticed_at) solo avanza cuando el emisor de la pasada del cron
            # (proyeccion.avisar_excepciones) confirmó un messageId. Si la vuelta
            # marcara sin enviar, la pasada no encontraría nada debido y el aviso
            # nunca saldría (F9).
            if propuesta.estado == "done":
                estado = "done"

        for intencion in tienda.intenciones_pendientes():
            target = intencion.assignment.get("target") or {}
            specialist = str(
                target.get("agentId") or target.get("adapterId") or "sin-perfil"
            )
            vista_de = vistas.get(intencion.root_id)
            if vista_de is not None:
                razon = _negar_arranque(vista_de, permisos, intencion)
                if razon is not None:
                    _levantar_arranque(
                        tienda,
                        vista_de,
                        intencion,
                        autoridad.causa_de(razon),
                        conflictos,
                    )
                    continue
                if (
                    home is not None
                    and poteto.paquete_del_perfil(home, specialist) is None
                ):
                    # H4b2 (I08): sin el paquete poteto del perfil la ruta no
                    # se admite; la causa queda visible en read. El gate de
                    # I07 ya pasó: poteto no concede permisos, exigir el
                    # paquete no los cercena.
                    _levantar_arranque(
                        tienda, vista_de, intencion, f"poteto:{specialist}", conflictos
                    )
                    continue
            referencia = intencion.assignment.get("instructionRef") or {}
            ref = str(referencia.get("ref", ""))
            texto = tienda.texto_de_instruccion(ref, str(referencia.get("digest", "")))
            if _sin_instruccion_verificable(
                tienda, vista_de, intencion, ref, conflictos, texto
            ):
                continue
            reclamo = tienda.reclamar_intencion(
                intencion.root_id, intencion.unit_id, intencion.attempt
            )
            if hasattr(reclamo, "codigo"):
                continue
            spec = SpecDeTarjeta(
                f"{intencion.root_id}/{intencion.unit_id}#{intencion.attempt}",
                texto.valor,
                specialist,
            )
            tarjeta = kanban_port.asegurar_tarjeta(
                puerto, intencion.idempotency_key, spec
            )
            documento = flota.construir(
                intencion.root_id,
                intencion.unit_id,
                intencion.attempt,
                specialist,
                home,
                permisos_h,
            )
            tienda.admitir_intento(
                intencion.root_id,
                intencion.unit_id,
                intencion.attempt,
                tarjeta.id,
                flota=documento,
            )

    if ruta_abierta:
        lanzados = _despachar_ejecutores(tienda, puerto, lanzador, vistas, conflictos)

    return Informe(
        conciliadas,
        aplicadas,
        tuple(conflictos),
        len(puerto.creaciones) - creadas_antes,
        estado,
        lanzados,
    )


def _despachar_ejecutores(tienda, puerto, lanzador, vistas, conflictos) -> int:
    """La fase H7: por cada intento con tarjeta viva de un perfil de equipo,
    reclamar y lanzar su ejecutor DESACOPLADO. NUNCA dos para la misma
    tarjeta: el reclamo (store.reclamar_lanzamiento) es UNA transacción que
    verifica active, reserva UNA llamada del budget y toma el reclamo con TTL;
    vivo, no se lanza; vencido (el hijo murió), la poda de limpiar_lanzamientos
    lo libera y la vuelta relanza. Un especialista de dispatch_profiles no se
    lanza (lo corre el despachador nativo) y un lanzamiento que no nace suelta
    el reclamo con su incidencia launch-failed (decidir la clasifica retry).
    Antes de reclamar, el resultado que el hijo dejara escrito en la vuelta
    anterior se consume: un desenlace terminal cierra la tarjeta con su causa
    (_cerrar_hijo_terminal) en lugar de relanzar a ciegas. Y si el team.json
    no se puede leer (fail closed, 4309610867), NO se lanza nada: con la
    lista de dispatch_profiles ausente no se sabe a quién corre el despachador
    nativo y lanzar sería arriesgar el doble run y la doble reserva."""
    if lanzador is None:
        return 0
    despacho_nativo = perfiles_de_despacho_nativo()
    if despacho_nativo is None:
        print(
            "ticker-raices: team.json ilegible; no lanzo ejecutores esta vuelta",
            file=sys.stderr,
        )
        return 0
    lanzados = 0
    tienda.limpiar_lanzamientos()
    for intento in tienda.intentos_lanzables():
        perfil = str(
            (intento["assignment"].get("target") or {}).get("agentId")
            or (intento["assignment"].get("target") or {}).get("adapterId")
            or "sin-perfil"
        )
        if perfil in despacho_nativo:
            continue
        resultado = _resultado_del_hijo(tienda, intento)
        if (
            isinstance(resultado, dict)
            and str(resultado.get("outcome")) in OUTCOMES_TERMINALES
        ):
            _cerrar_hijo_terminal(tienda, puerto, intento, resultado)
            continue
        reclamo = tienda.reclamar_lanzamiento(
            intento["rootId"],
            intento["unitId"],
            intento["attempt"],
            intento["cardId"],
        )
        if hasattr(reclamo, "codigo"):
            if reclamo.codigo == "sin-cuota":
                vista_de = vistas.get(intento["rootId"])
                if vista_de is not None:
                    _levantar_sin_cuota(tienda, vista_de, intento, conflictos)
            continue
        nacido = lanzador(
            intento["rootId"],
            intento["unitId"],
            intento["attempt"],
            intento["cardId"],
        )
        if nacido is None:
            tienda.soltar_lanzamiento(
                intento["rootId"], intento["unitId"], intento["attempt"]
            )
            tienda.registrar_incidencia(
                intento["rootId"],
                intento["unitId"],
                intento["attempt"],
                f"launch:{intento['cardId']}",
                {
                    "kind": "transport-unavailable",
                    "episodeId": "launch-failed",
                },
            )
            continue
        lanzados += 1
    return lanzados


def _resultado_del_hijo(tienda, intento):
    """El JSON que el hijo de ESTE intento escribió en su vuelta anterior
    (ejecutor.lanzador_de deja <raiz>/raices/ejecutor/<id>.json, escritura
    atómica), o None si no hay archivo legible: un hijo vivo aún no lo escribe
    y uno que murió sin decir nada tampoco — ese lo recupera el TTL del
    reclamo. El nombre es determinista y lo fija el propio ejecutor."""
    ejecutor = importlib.import_module("raices.ejecutor")
    archivo = ejecutor._archivo_de_resultado(
        Path(tienda.ruta).parent / "raices" / "ejecutor",
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
    )
    try:
        carga = json.loads(archivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return carga if isinstance(carga, dict) else None


def _cerrar_hijo_terminal(tienda, puerto, intento, resultado) -> None:
    """El circuito que el archivo de resultado ya implementa (F3): un hijo que
    reportó un desenlace terminal sin dejar la tarjeta cerrada — murió justo
    después de escribir, o su block_task falló — no vuelve a la cola como si
    nada. Bloquea la tarjeta con la MISMA causa acotada del hijo
    (ejecutor.motivo_de_outcome), devuelve el reclamo y la reserva cuando el
    desenlace ocurrió ANTES de la llamada al modelo, y consume el archivo para
    no revivirlo en cada vuelta. Idempotente: el bloqueo repetido no hace nada
    y la devolución solo toca un reclamo vivo; si el consumo del archivo
    fallara, la vuelta siguiente re-bloquea y no encuentra reserva que
    devolver."""
    ejecutor = importlib.import_module("raices.ejecutor")
    with puerto.write_txn() as escritura:
        escritura.bloquear(
            intento["cardId"], ejecutor.motivo_de_outcome(resultado)[:1800]
        )
    if str(resultado.get("outcome")) in OUTCOMES_SIN_LLAMADA:
        tienda.devolver_lanzamiento(
            intento["rootId"], intento["unitId"], intento["attempt"]
        )
    _consumir_archivo_de_hijo(tienda, intento)


def _consumir_archivo_de_hijo(tienda, intento) -> None:
    """El archivo de resultado del hijo de ESTE intento, consumido (F8): la
    devolución de la reserva si el desenlace fue ANTES de la llamada al
    modelo — el hijo real ya la hizo él mismo (ejecutor._devolver_reserva);
    esta es la del camino donde la conciliación cierra el intento ANTES de
    que _despachar_ejecutores llegue a leer el archivo, más el hijo que
    murió justo antes de devolverla — y el borrado del archivo para que no
    quede huérfano en <raiz>/raices/ejecutor/. Idempotente: sin archivo no
    hay nada y la devolución solo toca un reclamo vivo."""
    ejecutor = importlib.import_module("raices.ejecutor")
    resultado = _resultado_del_hijo(tienda, intento)
    if (
        isinstance(resultado, dict)
        and str(resultado.get("outcome")) in OUTCOMES_SIN_LLAMADA
    ):
        tienda.devolver_lanzamiento(
            intento["rootId"], intento["unitId"], intento["attempt"]
        )
    archivo = ejecutor._archivo_de_resultado(
        Path(tienda.ruta).parent / "raices" / "ejecutor",
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
    )
    try:
        archivo.unlink(missing_ok=True)
    except OSError:
        pass


def _levantar_sin_cuota(tienda, vista, intento, conflictos) -> None:
    """La pregunta SIN_CUOTA cuando no hay cupo de maxModelCalls. Espejo de
    _levantar_arranque: subject attempt con la causa del presupuesto, opciones
    de la clase, dedupe por exceptionId y C2 (la decisión no se reaplica: id
    fija `dec-cuota:<exceptionId>`; en pausa ni pregunta — al reanudar
    vuelve). El dueño elige seguir (esperar cupo), relevo o cancelar."""
    borrador = decidir.borrador_excepcion(
        intento["rootId"],
        "SIN_CUOTA",
        {
            "kind": "attempt",
            "unitId": intento["unitId"],
            "attempt": intento["attempt"],
            "cause": "budget:maxModelCalls",
        },
    )
    conocidas = {e["exceptionId"] for e in vista.get("excepciones") or ()}
    if (
        borrador["exceptionId"] in conocidas
        or vista["meta"]["control"]["state"] != "active"
    ):
        return
    decision = decidir._decision_vacia(
        vista["meta"]["plan"]["revision"], vista["meta"]["controlEpoch"]
    )
    decision["excepciones"] = [borrador]
    resultado = tienda.aplicar(
        intento["rootId"],
        decision,
        f"dec-cuota:{borrador['exceptionId']}",
    )
    if hasattr(resultado, "codigo"):
        conflictos.append((intento["rootId"], resultado.codigo))


def _negar_arranque(vista, permisos, intencion) -> str | None:
    """Punto 1 de la intersección (I07): la unidad no arranca si el contrato o
    H lo niegan."""
    unidad_plan = next(
        (u for u in vista["meta"]["plan"]["units"] if u["unitId"] == intencion.unit_id),
        None,
    )
    return autoridad.negar_arranque(
        vista["meta"]["authority"],
        permisos,
        vista["ahora"],
        intencion.assignment,
        unidad_plan,
    )


def _levantar_arranque(tienda, vista, intencion, causa, conflictos) -> None:
    """La pregunta por un arranque negado. Un sujeto de INTENTO no ofrece
    `autorizar`: la concesión del dueño solo existe para efectos (F8/F15),
    igual que en R, donde permission-required sale con
    ["listo","omitir","cancelar"] (managed-root.decide.ts:550). Si la
    decisión choca, el Conflicto entra al informe (F17). La decisión NO se
    reaplica (C2): con la id fija `dec-arranque:<exceptionId>` y una época
    que cambió sería un operation-conflict en cada vuelta, y con la raíz en
    pausa un root-not-active; si la pregunta ya está en la base, no hace
    falta, y si la raíz no está active la pregunta sale al reanudar."""
    borrador = decidir.borrador_excepcion(
        intencion.root_id,
        "NECESITA_DAVID",
        {
            "kind": "attempt",
            "unitId": intencion.unit_id,
            "attempt": intencion.attempt,
            "cause": causa,
        },
        ["listo", "omitir", "cancelar"],
    )
    conocidas = {e["exceptionId"] for e in vista.get("excepciones") or ()}
    if (
        borrador["exceptionId"] in conocidas
        or vista["meta"]["control"]["state"] != "active"
    ):
        return
    decision = decidir._decision_vacia(
        vista["meta"]["plan"]["revision"], vista["meta"]["controlEpoch"]
    )
    decision["excepciones"] = [borrador]
    resultado = tienda.aplicar(
        intencion.root_id,
        decision,
        f"dec-arranque:{borrador['exceptionId']}",
    )
    if hasattr(resultado, "codigo"):
        conflictos.append((intencion.root_id, resultado.codigo))


def _sin_instruccion_verificable(
    tienda, vista_de, intencion, ref, conflictos, texto
) -> bool:
    if not hasattr(texto, "codigo"):
        return False
    if vista_de is not None:
        _levantar_arranque(
            tienda, vista_de, intencion, f"instruccion:{ref}", conflictos
        )
    return True


def _hechos_vacios() -> dict:
    return {
        "principal": [],
        "asignaciones": [],
        "claims": [],
        "efectos": [],
    }


def _decision_id(vista, propuesta) -> str:
    canonico = [
        vista["meta"]["rootId"],
        vista["meta"]["plan"]["revision"],
        vista["meta"]["controlEpoch"],
        propuesta.decision,
    ]
    return "dec-" + digesto(canonico)[:24]


def _conciliar_uno(tienda, puerto, intento) -> bool:
    clave = kanban_port.clave_de_tarjeta(
        intento["rootId"], intento["unitId"], intento["attempt"]
    )
    tarjeta = puerto.leer_estado(clave)
    if tarjeta is None:
        return False
    ahora = tienda.reloj()
    root_id, unit_id, attempt = (
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
    )
    if tarjeta.estado == "corriendo":
        if intento["state"] == "admitted":
            tienda.transicionar_intento(
                root_id, unit_id, attempt, "running", tarjeta.id
            )
            return True
        return False
    if tarjeta.estado == "completada":
        if intento["state"] not in ("admitted", "running"):
            return False
        if intento["state"] == "admitted":
            tienda.transicionar_intento(
                root_id, unit_id, attempt, "running", tarjeta.id
            )
        _reportar(tienda, intento, tarjeta, ahora)
        return True
    if tarjeta.estado == "bloqueada" and intento["state"] in ("admitted", "running"):
        _transporte_caido(tienda, intento, tarjeta)
        return True
    if tarjeta.estado == "archivada" and intento["state"] in ("admitted", "running"):
        _transporte_caido(tienda, intento, tarjeta)
        return True
    return False


def _transporte_caido(tienda, intento, tarjeta) -> None:
    tienda.registrar_incidencia(
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
        f"tarjeta:{tarjeta.id}",
        {"kind": "recovery-transport-failure", "evidenceRef": tarjeta.id},
    )
    # F8: el hijo REAL bloquea la tarjeta él mismo y esta conciliación cierra
    # el intento ANTES de que _despachar_ejecutores llegue a su archivo —
    # consumirlo aquí (reserva de vuelta si no hubo llamada; el hijo ya la
    # devolvió, idempotente) para que no quede huérfano ni reserva quemada.
    _consumir_archivo_de_hijo(tienda, intento)


def _reportar(tienda, intento, tarjeta, ahora) -> None:
    carga = tarjeta.resultado or {}
    documento = _documento_de_resultado(carga, intento, tarjeta, ahora)
    if documento is None:
        _incidencia_invalida(tienda, intento, tarjeta, carga)
        return
    respuesta = tienda.reportar_resultado(documento)
    if hasattr(respuesta, "codigo"):
        _incidencia_invalida(tienda, intento, tarjeta, carga)


def _documento_de_resultado(carga, intento, tarjeta, ahora):
    contrato_resultado = carga.get("resultContract")
    digest = carga.get("resultDigest")
    if not isinstance(contrato_resultado, str) or not contrato_resultado:
        return None
    if not isinstance(digest, str) or not digest:
        return None
    documento = {
        "rootId": intento["rootId"],
        "unitId": intento["unitId"],
        "attempt": intento["attempt"],
        "resultContract": contrato_resultado,
        "resultDigest": digest,
        "reportedAt": ahora,
        "reportedBy": {"kind": "native", "taskId": tarjeta.id},
    }
    if contrato_resultado == "review.v1":
        veredicto = carga.get("verdict")
        if veredicto == "changes":
            hallazgo = carga.get("findingsRef") or carga.get("evidenceRef")
            if not hallazgo:
                return None
            documento["payload"] = {"verdict": "changes", "findingsRef": hallazgo}
        elif veredicto == "approved":
            if not carga.get("evidenceRef"):
                return None
            documento["payload"] = {
                "verdict": "approved",
                "evidenceRef": carga["evidenceRef"],
            }
        else:
            return None
    else:
        if not carga.get("evidenceRef"):
            return None
        documento["payload"] = {"evidenceRef": carga["evidenceRef"]}
    return documento


def _incidencia_invalida(tienda, intento, tarjeta, carga=None) -> None:
    """El episodeId es el canónico de R (managed-task.worker.ts): el digest del
    resultado si lo trajo, o el de schema-invalid sobre lo que llegó. Una
    tarjeta completada sin resultado alguno es un hijo que murió sin decir
    nada: reintenta con el mismo tope que invalid-result y el kind distingue
    la muerte en la evidencia (I18 sigue: sin resultado válido no hay
    veredicto, aceptación ni despacho de dependientes)."""
    if not carga:
        kind = "result-missing"
        identidad = digesto(["schema-invalid", "sin-resultado"])
    else:
        kind = "invalid-result"
        identidad = carga.get("resultDigest") if isinstance(carga, dict) else None
        if not isinstance(identidad, str) or not identidad:
            identidad = digesto(
                [
                    "schema-invalid",
                    carga if isinstance(carga, str) else "structured-result",
                ]
            )
    tienda.registrar_incidencia(
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
        f"{kind}:{tarjeta.id}",
        {
            "kind": kind,
            "episodeId": f"{kind}:{digesto([tarjeta.id, identidad])}",
            "evidenceRef": tarjeta.id,
        },
    )
