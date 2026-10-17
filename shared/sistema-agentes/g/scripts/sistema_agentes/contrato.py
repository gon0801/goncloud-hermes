"""Validador del contrato sistema-agentes.v1 (docs/spec/sistema-agentes.v1.md).

Forma de los datos:

- Un documento es el objeto JSON ya cargado. `cargar_json` es la única función con I/O.
- `Hallazgo(regla, ruta, detalle)`. `regla` es un código del contrato. `ruta` es la ruta JSON
  del campo, como `plan.units[2].dependsOn[0]`. `detalle` es un texto corto.
- Un veredicto es una lista de `Hallazgo`. La lista vacía significa válido.
- `Campo` describe un valor: tipo, si es requerido, si admite null, enum, mínimo, máximo,
  elementos de una lista, campos de un objeto, si el objeto es cerrado y los campos que
  dependen del valor de un hermano (`segun`).
- `ESQUEMAS` mapea cada literal `schema` a su `Campo` raíz. `validar` despacha por esa tabla.
- `TRANSICIONES` mapea cada estado de unidad a los estados que admite después.
- Una fecha es un texto ISO 8601 en UTC con sufijo `Z`. `efecto_admisible` recibe `ahora` así.

Orden del veredicto: `schema`, luego las reglas propias del esquema (para meta `id-duplicado`,
`dependencia-faltante`, `ciclo`, `criterio-faltante`, `quality-policy-unknown`; para excepción
`opcion-no-ofrecida`), luego `campo-prohibido`. Las reglas propias solo corren sobre un
documento sin hallazgos `schema`, porque leen su forma sin volver a comprobarla.
`quality-policy-unknown` compara `qualityPolicy.version` contra las `versiones` de
`docs/evidence/agent-work/politica-calidad.json`; sin esa tabla legible no hay versión
conocida, como en R sin tabla no se abre una raíz.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


class Hallazgo(NamedTuple):
    regla: str
    ruta: str
    detalle: str


class Campo(NamedTuple):
    tipo: str
    requerido: bool = True
    nulo: bool = False
    enum: tuple[str, ...] = ()
    minimo: int | None = None
    maximo: int | None = None
    no_vacia: bool = False
    elementos: Campo | None = None
    campos: Mapping[str, Campo] | None = None
    cerrado: bool = False
    segun: tuple[str, Mapping[str | None, Mapping[str, Campo]]] | None = None


META = "sistema-agentes.meta.v1"
AUTORIDAD = "sistema-agentes.autoridad.v1"
UNIDAD = "sistema-agentes.unidad.v1"
RESULTADO = "sistema-agentes.resultado.v1"
EVIDENCIA = "sistema-agentes.evidencia.v1"
CONTROL = "sistema-agentes.control.v1"
FLOTA = "sistema-agentes.flota.v1"
EXCEPCION = "sistema-agentes.excepcion.v1"
PROYECCION = "sistema-agentes.proyeccion.v1"

MODOS = ("design", "read", "implement", "publish")

TRANSICIONES: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"admitted", "cancelled"}),
    "admitted": frozenset({"running", "cancelled"}),
    "running": frozenset({"reported", "failed", "cancelled"}),
    "reported": frozenset({"accepted", "failed", "cancelled"}),
    "accepted": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

_NOMBRES_PROHIBIDOS = {
    nombre.lower(): nombre
    for nombre in (
        "key",
        "token",
        "secret",
        "session",
        "signature",
        "credential",
        "authorization",
        "cookie",
        "jwt",
        "password",
        "passwd",
        "apiKey",
        "idToken",
        "accessToken",
        "authToken",
        "clientSecret",
        "privateKey",
        "keyMaterial",
        "refreshToken",
        "bearerToken",
        "appSecret",
        "secretValue",
        "rawSecret",
        "secretInput",
        "apiToken",
    )
}
_SUFIJO_PROHIBIDO = re.compile(r"[_-](?:key|token|secret|password|passwd)$", re.IGNORECASE)

_FECHA = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REPO = re.compile(r"[^/\s]+/[^/\s]+")


def _instante(texto: str) -> datetime:
    partes = _FECHA.fullmatch(texto)
    if partes is None:
        raise ValueError(f"no es una fecha ISO 8601 en UTC con Z: {texto!r}")
    *enteros, fraccion = partes.groups()
    micros = int((fraccion or "")[:6].ljust(6, "0"))
    return datetime(*(int(parte) for parte in enteros), micros, tzinfo=timezone.utc)


def _es_fecha(valor: str) -> bool:
    try:
        _instante(valor)
    except ValueError:
        return False
    return True


_ESCALARES: Mapping[str, tuple[type, str, Callable[..., bool]]] = {
    "texto": (str, "texto", lambda valor: True),
    "id": (str, "cadena no vacía", lambda valor: valor != ""),
    "fecha": (str, "fecha ISO 8601 en UTC con Z", _es_fecha),
    "digest": (str, "sha256:<64 hex>", lambda valor: _DIGEST.fullmatch(valor) is not None),
    "repo": (str, "owner/nombre", lambda valor: _REPO.fullmatch(valor) is not None),
    "booleano": (bool, "booleano", lambda valor: True),
    # bool hereda de int en Python. Un `true` de JSON no cuenta como entero.
    "entero": (int, "entero", lambda valor: not isinstance(valor, bool)),
}


def _uno_de(*valores: str) -> Campo:
    return Campo("texto", enum=valores)


def _entero(minimo: int | None = None, maximo: int | None = None) -> Campo:
    return Campo("entero", minimo=minimo, maximo=maximo)


def _lista(elementos: Campo | None = None, no_vacia: bool = False) -> Campo:
    return Campo("lista", elementos=elementos, no_vacia=no_vacia)


def _objeto(
    campos: Mapping[str, Campo],
    cerrado: bool = False,
    segun: tuple[str, Mapping[str | None, Mapping[str, Campo]]] | None = None,
) -> Campo:
    return Campo("objeto", campos=campos, cerrado=cerrado, segun=segun)


def _o_null(campo: Campo) -> Campo:
    return campo._replace(nulo=True)


TEXTO = Campo("texto")
ID = Campo("id")
FECHA = Campo("fecha")
DIGEST = Campo("digest")
BOOLEANO = Campo("booleano")
DESDE_UNO = _entero(minimo=1)
REFERENCIA = _objeto({"ref": TEXTO, "digest": DIGEST})
# El contrato escribe `"principal": [...]` sin fijar el tipo de sus elementos.
PRINCIPAL = _lista()
VERIFICADOR = _uno_de("reviewer", "verifier", "ci", "owner")
MODO = _uno_de(*MODOS)
ESTADO_DE_CONTROL = _uno_de("active", "paused", "cancelled")

# Las diez llaves de ManagedBudgetProfile en R 4acb881e015a,
# src/agents/tasks/managed-task.types.ts:198-209. El contrato las cita sin listarlas.
_LLAVES_DE_PRESUPUESTO = (
    "maxConcurrentTasks",
    "maxDepth",
    "maxChildren",
    "maxModelCalls",
    "maxInputTokens",
    "maxOutputTokens",
    "maxCacheReadTokens",
    "maxContextTokens",
    "maxTreeTokens",
    "maxAutomaticRecoveryCalls",
)

_AUTORIDAD = _objeto(
    {
        "schema": _uno_de(AUTORIDAD),
        "authorityRef": TEXTO,
        "grantedBy": TEXTO,
        "grantedAt": FECHA,
        "actions": _lista(
            _uno_de(
                "read",
                "implement",
                "review",
                "push",
                "merge",
                "deploy",
                "send-mail",
                "publish",
                "create-repo",
            )
        ),
        "repos": _lista(Campo("repo")),
        "accounts": _lista(TEXTO),
        "destinations": _lista(TEXTO),
        "deadline": _o_null(FECHA),
        "mode": MODO,
    }
)

_UNIDAD_DE_PLAN = _objeto(
    {
        "unitId": ID,
        "title": TEXTO,
        "dependsOn": _lista(ID),
        # La lista vacía no es `schema`. El contrato le da su código, `criterio-faltante`.
        "criteria": _lista(_objeto({"criterionId": ID, "text": TEXTO, "verifier": VERIFICADOR})),
        "scope": _objeto({"repos": _lista(TEXTO), "mode": MODO}),
    }
)

_META = _objeto(
    {
        "schema": _uno_de(META),
        "rootId": ID,
        "requester": _objeto({"principal": PRINCIPAL, "agentId": TEXTO}),
        "authority": _AUTORIDAD,
        "budget": _objeto({llave: _entero(minimo=0) for llave in _LLAVES_DE_PRESUPUESTO}),
        "plan": _objeto(
            {
                "revision": DESDE_UNO,
                "planDigest": DIGEST,
                "requestDigest": DIGEST,
                "units": _lista(_UNIDAD_DE_PLAN),
                "assumptions": _lista(TEXTO),
            }
        ),
        "controlEpoch": DESDE_UNO,
        "control": _objeto(
            {"state": ESTADO_DE_CONTROL, "epoch": _entero(), "reason": _o_null(TEXTO)}
        ),
        "qualityPolicy": _objeto({"version": TEXTO}),
        "createdAt": FECHA,
    }
)

_UNIDAD = _objeto(
    {
        "schema": _uno_de(UNIDAD),
        "rootId": ID,
        "planRevision": DESDE_UNO,
        "unitId": ID,
        "attempt": DESDE_UNO,
        "taskId": _o_null(TEXTO),
        "assignment": _objeto(
            {
                "target": _objeto({}),
                "instructionRef": REFERENCIA,
                "resultContract": TEXTO,
                "continuation": _objeto({}),
            }
        ),
        "state": _uno_de(*TRANSICIONES),
        "controlEpoch": DESDE_UNO,
    }
)

_PAYLOAD_DE_REVIEW = _objeto(
    {"verdict": _uno_de("approved", "changes")},
    segun=(
        "verdict",
        {"approved": {"evidenceRef": REFERENCIA}, "changes": {"findingsRef": REFERENCIA}},
    ),
)

_RESULTADO = _objeto(
    {
        "schema": _uno_de(RESULTADO),
        "rootId": ID,
        "unitId": ID,
        "attempt": DESDE_UNO,
        "resultContract": _uno_de("review.v1", "ready.v1"),
        "resultDigest": DIGEST,
        "reportedAt": FECHA,
        "reportedBy": _objeto({"kind": _uno_de("native", "cli"), "taskId": TEXTO}),
    },
    segun=(
        "resultContract",
        {
            "review.v1": {"payload": _PAYLOAD_DE_REVIEW},
            "ready.v1": {"payload": _objeto({"evidenceRef": REFERENCIA})},
        },
    ),
)

_EVIDENCIA = _objeto(
    {
        "schema": _uno_de(EVIDENCIA),
        "evidenceId": ID,
        "rootId": ID,
        "unitId": ID,
        "criterionId": ID,
        "planRevision": DESDE_UNO,
        "artifactRef": REFERENCIA,
        "environment": _objeto({"host": TEXTO, "versions": Campo("mapa", elementos=TEXTO)}),
        "verifier": _objeto({"principal": PRINCIPAL, "role": VERIFICADOR}),
        "procedure": TEXTO,
        "outcome": _uno_de("passed", "failed", "unknown"),
        "at": FECHA,
        "expiresAt": _o_null(FECHA),
    }
)

_RECIBO = _objeto(
    {
        "controlEpoch": DESDE_UNO,
        "state": ESTADO_DE_CONTROL,
        "replayed": BOOLEANO,
        "committedEffects": _lista(
            _objeto(
                {
                    "effectId": ID,
                    "destination": TEXTO,
                    "state": _uno_de("started", "uncertain"),
                    "cancellable": BOOLEANO,
                }
            )
        ),
    }
)

_CONTROL = _objeto(
    {
        "schema": _uno_de(CONTROL),
        "rootId": ID,
        "command": _uno_de("pause", "resume", "cancel"),
        "commandId": ID,
        "expectedControlEpoch": DESDE_UNO,
        "reason": _o_null(TEXTO),
        "issuedAt": FECHA,
        # El contrato no dice si una orden sin recibo es válida. Aquí no se exige.
        "receipt": _RECIBO._replace(requerido=False),
    }
)

_FLOTA = _objeto(
    {
        "schema": _uno_de(FLOTA),
        "rootId": ID,
        "unitId": ID,
        "attempt": DESDE_UNO,
        "specialist": TEXTO,
        "host": TEXTO,
        "executor": _uno_de("native", "cli", "opencode"),
        "accountRef": _o_null(TEXTO),
        "model": _objeto({"id": _o_null(TEXTO)}, segun=("id", {None: {"reason": TEXTO}})),
        "generation": DESDE_UNO,
        "permissions": _lista(TEXTO),
        "attestations": _lista(
            _objeto({"what": _uno_de("poteto", "profile", "tools"), "digest": DIGEST})
        ),
        # Opcional desde SUA-03: cada hijo de `task` de una CLI con el modelo que corrió.
        "children": _lista(_objeto({"agent": TEXTO, "model": TEXTO}, cerrado=True))._replace(
            requerido=False
        ),
    }
)

_EXCEPCION = _objeto(
    {
        "schema": _uno_de(EXCEPCION),
        "exceptionId": ID,
        "rootId": ID,
        "unitId": _o_null(ID),
        "type": _uno_de(
            "SIN_REVISOR",
            "SIN_CUOTA",
            "AUTH_VENCIDA",
            "CI_ROJO",
            "BOT_CALLADO",
            "CONFIRMAR_PLAN",
            "NECESITA_DAVID",
            "BLOQUEANTE_REPETIDO",
            "DEPLOY_FALLO",
            "CIERRE_ROJO",
            "INESPERADO",
            "EVIDENCIA_INCIERTA",
            "CAPACIDAD_INCIERTA",
        ),
        "options": _lista(_objeto({"optionId": TEXTO, "text": TEXTO}), no_vacia=True),
        "evidenceRef": _o_null(REFERENCIA),
        "recipient": _objeto({"channel": TEXTO, "principal": PRINCIPAL}),
        "raisedAt": FECHA,
        "renoticeAfterMs": _entero(minimo=3_600_000),
        "response": _o_null(_objeto({"optionId": TEXTO, "by": PRINCIPAL, "at": FECHA})),
    }
)

_PORCENTAJE = _o_null(_entero(minimo=0, maximo=100))

_PROYECCION = _objeto(
    {
        "schema": _uno_de(PROYECCION),
        "rootId": ID,
        "planRevision": DESDE_UNO,
        "controlEpoch": DESDE_UNO,
        "generatedAt": FECHA,
        "progress": _objeto(
            {"declared": _PORCENTAJE, "observed": _PORCENTAJE, "accepted": _PORCENTAJE},
            cerrado=True,
        ),
        "units": _lista(
            _objeto(
                {
                    "unitId": ID,
                    "state": TEXTO,
                    "host": TEXTO,
                    "executor": TEXTO,
                    "modelId": _o_null(TEXTO),
                    "nextCheckAt": FECHA,
                    "error": _o_null(TEXTO),
                },
                cerrado=True,
            )
        ),
        "freshness": _objeto({"staleAfterMs": _entero()}, cerrado=True),
    },
    cerrado=True,
)

ESQUEMAS: Mapping[str, Campo] = {
    META: _META,
    AUTORIDAD: _AUTORIDAD,
    UNIDAD: _UNIDAD,
    RESULTADO: _RESULTADO,
    EVIDENCIA: _EVIDENCIA,
    CONTROL: _CONTROL,
    FLOTA: _FLOTA,
    EXCEPCION: _EXCEPCION,
    PROYECCION: _PROYECCION,
}


_AUSENTE = object()


def _roto(ruta: str, detalle: str) -> Hallazgo:
    return Hallazgo("schema", ruta, detalle)


def _hijo(ruta: str, nombre: str) -> str:
    return f"{ruta}.{nombre}" if ruta else nombre


def _forma_de_escalar(campo: Campo, valor: object, ruta: str) -> list[Hallazgo]:
    tipo, descripcion, cumple = _ESCALARES[campo.tipo]
    if not isinstance(valor, tipo) or not cumple(valor):
        return [_roto(ruta, f"se esperaba {descripcion}")]
    if campo.enum and valor not in campo.enum:
        return [_roto(ruta, f"fuera del enum: {valor!r}")]
    if campo.minimo is not None and valor < campo.minimo:
        return [_roto(ruta, f"menor que el mínimo {campo.minimo}: {valor}")]
    if campo.maximo is not None and valor > campo.maximo:
        return [_roto(ruta, f"mayor que el máximo {campo.maximo}: {valor}")]
    return []


def _forma_de_objeto(campo: Campo, valor: object, ruta: str) -> list[Hallazgo]:
    if not isinstance(valor, dict):
        return [_roto(ruta, "se esperaba objeto")]
    campos = dict(campo.campos or {})
    if campo.segun is not None:
        hermano, variantes = campo.segun
        elegido = valor.get(hermano, _AUSENTE)
        if not isinstance(elegido, (list, dict)):
            campos.update(variantes.get(elegido, {}))
    hallazgos = []
    for nombre, descrito in campos.items():
        if nombre in valor:
            hallazgos += _forma(descrito, valor[nombre], _hijo(ruta, nombre))
        elif descrito.requerido:
            hallazgos.append(_roto(_hijo(ruta, nombre), "falta el campo"))
    if campo.cerrado:
        hallazgos += [
            _roto(_hijo(ruta, nombre), "campo fuera de la tabla")
            for nombre in valor
            if nombre not in campos
        ]
    return hallazgos


def _forma_de_lista(campo: Campo, valor: object, ruta: str) -> list[Hallazgo]:
    if not isinstance(valor, list):
        return [_roto(ruta, "se esperaba lista")]
    if campo.no_vacia and not valor:
        return [_roto(ruta, "la lista no puede ir vacía")]
    if campo.elementos is None:
        return []
    return [
        hallazgo
        for indice, elemento in enumerate(valor)
        for hallazgo in _forma(campo.elementos, elemento, f"{ruta}[{indice}]")
    ]


def _forma_de_mapa(campo: Campo, valor: object, ruta: str) -> list[Hallazgo]:
    if not isinstance(valor, dict):
        return [_roto(ruta, "se esperaba objeto")]
    return [
        hallazgo
        for nombre, elemento in valor.items()
        for hallazgo in _forma(campo.elementos, elemento, _hijo(ruta, nombre))
    ]


_COMPUESTOS: Mapping[str, Callable[[Campo, object, str], list[Hallazgo]]] = {
    "objeto": _forma_de_objeto,
    "lista": _forma_de_lista,
    "mapa": _forma_de_mapa,
}


def _forma(campo: Campo, valor: object, ruta: str) -> list[Hallazgo]:
    if valor is None:
        return [] if campo.nulo else [_roto(ruta, "null no permitido")]
    return _COMPUESTOS.get(campo.tipo, _forma_de_escalar)(campo, valor, ruta)


def nombre_de_campo_prohibido(nombre: str) -> str | None:
    """Devuelve por qué la redacción de R taparía el campo, o None si el nombre pasa."""
    exacto = _NOMBRES_PROHIBIDOS.get(nombre.lower())
    if exacto is not None:
        return f"nombre exacto {exacto}"
    sufijo = _SUFIJO_PROHIBIDO.search(nombre)
    if sufijo is not None:
        return f"sufijo {sufijo.group().lower()}"
    return None


def _campos_prohibidos(valor: object, ruta: str) -> Iterator[Hallazgo]:
    if isinstance(valor, dict):
        for nombre, hijo in valor.items():
            razon = nombre_de_campo_prohibido(nombre)
            if razon is not None:
                yield Hallazgo("campo-prohibido", _hijo(ruta, nombre), razon)
            yield from _campos_prohibidos(hijo, _hijo(ruta, nombre))
    elif isinstance(valor, list):
        for indice, hijo in enumerate(valor):
            yield from _campos_prohibidos(hijo, f"{ruta}[{indice}]")


def _ids_duplicados(unidades: list[dict]) -> Iterator[Hallazgo]:
    por_nombre = {
        "unitId": [
            (f"plan.units[{i}].unitId", unidad["unitId"]) for i, unidad in enumerate(unidades)
        ],
        "criterionId": [
            (f"plan.units[{i}].criteria[{j}].criterionId", criterio["criterionId"])
            for i, unidad in enumerate(unidades)
            for j, criterio in enumerate(unidad["criteria"])
        ],
    }
    for nombre, identificados in por_nombre.items():
        vistos = set()
        for ruta, identificador in identificados:
            if identificador in vistos:
                yield Hallazgo("id-duplicado", ruta, f"{nombre} repetido: {identificador}")
            vistos.add(identificador)


def _dependencias_faltantes(unidades: list[dict]) -> Iterator[Hallazgo]:
    existentes = {unidad["unitId"] for unidad in unidades}
    for i, unidad in enumerate(unidades):
        for k, dependencia in enumerate(unidad["dependsOn"]):
            if dependencia not in existentes:
                ruta = f"plan.units[{i}].dependsOn[{k}]"
                yield Hallazgo("dependencia-faltante", ruta, f"no existe la unidad {dependencia}")


def _ciclos(unidades: list[dict]) -> Iterator[Hallazgo]:
    indice_de: dict[str, int] = {}
    for i, unidad in enumerate(unidades):
        indice_de.setdefault(unidad["unitId"], i)
    sin_visitar, en_camino, cerrada = 0, 1, 2
    estado = [sin_visitar] * len(unidades)

    # Recorrido en profundidad con pila propia. Un plan largo en cadena rebasaría el límite
    # de recursión de Python.
    for raiz in range(len(unidades)):
        if estado[raiz] != sin_visitar:
            continue
        estado[raiz] = en_camino
        camino = [raiz]
        pendientes = [iter(enumerate(unidades[raiz]["dependsOn"]))]
        while camino:
            actual = camino[-1]
            for k, dependencia in pendientes[-1]:
                destino = indice_de.get(dependencia)
                if destino is None:
                    continue
                if estado[destino] == en_camino:
                    vuelta = [*camino[camino.index(destino) :], destino]
                    yield Hallazgo(
                        "ciclo",
                        f"plan.units[{actual}].dependsOn[{k}]",
                        " -> ".join(unidades[n]["unitId"] for n in vuelta),
                    )
                elif estado[destino] == sin_visitar:
                    estado[destino] = en_camino
                    camino.append(destino)
                    pendientes.append(iter(enumerate(unidades[destino]["dependsOn"])))
                    break
            else:
                estado[actual] = cerrada
                camino.pop()
                pendientes.pop()


def _criterios_faltantes(unidades: list[dict]) -> Iterator[Hallazgo]:
    for i, unidad in enumerate(unidades):
        if not unidad["criteria"]:
            yield Hallazgo(
                "criterio-faltante", f"plan.units[{i}].criteria", "la unidad no trae criterios"
            )


_REGLAS_DE_PLAN = (_ids_duplicados, _dependencias_faltantes, _ciclos, _criterios_faltantes)


def _reglas_de_plan(meta: dict) -> list[Hallazgo]:
    unidades = meta["plan"]["units"]
    return [hallazgo for regla in _REGLAS_DE_PLAN for hallazgo in regla(unidades)]


def _respuesta_fuera_de_opciones(excepcion: dict) -> list[Hallazgo]:
    respuesta = excepcion["response"]
    ofrecidas = {opcion["optionId"] for opcion in excepcion["options"]}
    if respuesta is None or respuesta["optionId"] in ofrecidas:
        return []
    detalle = f"{respuesta['optionId']} no es una de las opciones ofrecidas"
    return [Hallazgo("opcion-no-ofrecida", "response.optionId", detalle)]


POLITICA_DE_CALIDAD = (
    Path(__file__).resolve().parents[2] / "docs/evidence/agent-work/politica-calidad.json"
)


def _politica_de_calidad_desconocida(meta: dict) -> list[Hallazgo]:
    version = meta["qualityPolicy"]["version"]
    try:
        documento = cargar_json(POLITICA_DE_CALIDAD)
    except (OSError, ValueError) as fallo:
        detalle = f"no se pudo leer politica-calidad.json: {fallo}"
    else:
        versiones = documento.get("versiones") if isinstance(documento, dict) else None
        if isinstance(versiones, dict) and version in versiones:
            return []
        detalle = f"la versión {version} no está en politica-calidad.json"
    return [Hallazgo("quality-policy-unknown", "qualityPolicy.version", detalle)]


def _reglas_de_meta(meta: dict) -> list[Hallazgo]:
    return [*_reglas_de_plan(meta), *_politica_de_calidad_desconocida(meta)]


_REGLAS_PROPIAS: Mapping[str, Callable[[dict], list[Hallazgo]]] = {
    META: _reglas_de_meta,
    EXCEPCION: _respuesta_fuera_de_opciones,
}


def _validar(documento: object, esquemas: Mapping[str, Campo]) -> list[Hallazgo]:
    literal = documento.get("schema") if isinstance(documento, dict) else None
    forma = esquemas.get(literal) if isinstance(literal, str) else None
    if forma is None:
        hallazgos = [_roto("schema", f"literal no admitido: {literal!r}")]
    else:
        hallazgos = _forma(forma, documento, "")
        if not hallazgos and literal in _REGLAS_PROPIAS:
            hallazgos = _REGLAS_PROPIAS[literal](documento)
    return [*hallazgos, *_campos_prohibidos(documento, "")]


def validar(documento: object) -> list[Hallazgo]:
    """Veredicto completo de un documento de cualquier esquema del contrato."""
    return _validar(documento, ESQUEMAS)


def validar_plan(meta: object) -> list[Hallazgo]:
    """Veredicto de una meta. Un documento de otro esquema es `schema`."""
    return _validar(meta, {META: ESQUEMAS[META]})


_RANGO_DE_MODO = {modo: rango for rango, modo in enumerate(MODOS)}


def _vencida(autoridad: Mapping, efecto: Mapping, ahora: str) -> bool:
    plazo = autoridad["deadline"]
    return plazo is not None and _instante(ahora) > _instante(plazo)


def _accion_fuera(autoridad: Mapping, efecto: Mapping, ahora: str) -> bool:
    return efecto["action"] not in autoridad["actions"]


def _repo_fuera(autoridad: Mapping, efecto: Mapping, ahora: str) -> bool:
    return efecto["repo"] not in autoridad["repos"]


def _destino_fuera(autoridad: Mapping, efecto: Mapping, ahora: str) -> bool:
    destino = efecto.get("destination")
    return destino is not None and destino not in autoridad["destinations"]


def _modo_fuera(autoridad: Mapping, efecto: Mapping, ahora: str) -> bool:
    # Un modo que el contrato no conoce queda por encima de cualquier autoridad.
    rango = _RANGO_DE_MODO.get(efecto["mode"], len(MODOS))
    return rango > _RANGO_DE_MODO[autoridad["mode"]]


_RECHAZOS: tuple[tuple[str, Callable[[Mapping, Mapping, str], bool]], ...] = (
    ("autoridad-vencida", _vencida),
    ("accion-fuera-de-autoridad", _accion_fuera),
    ("repo-fuera-de-autoridad", _repo_fuera),
    ("destino-fuera-de-autoridad", _destino_fuera),
    ("modo-fuera-de-autoridad", _modo_fuera),
)


def efecto_admisible(autoridad: Mapping, efecto: Mapping, ahora: str) -> str | None:
    """Primera razón del rechazo, o None si el efecto es admisible.

    `autoridad` ya pasó `validar`. `efecto` es `{action, repo, destination (opcional), mode}`.
    """
    for razon, rechaza in _RECHAZOS:
        if rechaza(autoridad, efecto, ahora):
            return razon
    return None


def transicion_permitida(estado_desde: str, estado_hasta: str) -> bool:
    return estado_hasta in TRANSICIONES.get(estado_desde, frozenset())


def cargar_json(ruta: str | Path) -> object:
    return json.loads(Path(ruta).read_text(encoding="utf-8"))
