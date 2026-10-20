"""Respuesta rapida de "como va", armada por codigo y de solo lectura.

Cuando David pregunta por el avance ("Como va?", "Sigue?", "Ya quedo todo?")
el gateway ofrece el gancho post_gateway_admission: este modulo decide si el
mensaje ES esa pregunta y arma el texto a partir del estado REAL de las
cadenas Goncloud, sin invocar el modelo del director.

Fuentes (todas de solo lectura; nada se crea, cierra ni relanza):
  - <raiz de Hermes>/goncloud-local/<workflow>.json  resultado/marca del driver
  - subscription-router.db (mode=ro)                 etapa, modelo, minutos
  - kanban.db (mode=ro)                              repo (contrato) y rol

Deteccion en dos escalones: una frase corta tipica ("Como va?") se resuelve
por regla; un mensaje mas largo que solo huele a avance se confirma con JEV
(Decisor.avance, umbral 0.85). Un veto de verbos de accion ("cierra",
"arregla", "hazlo") lo manda siempre al turno normal del LLM.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    from .subscriptions.redaction import redact_text
except ImportError:  # carga suelta (pruebas): el plugin esta en sys.path
    from subscriptions.redaction import redact_text

HORAS_RECIENTES = 12
TOPE_TEXTO = 300  # mas largo que esto ya es otra cosa que "como va"
TOPE_CORTA = 60
TOPE_CADENAS = 5
TOPE_ARCHIVO = 2_000_000

# Frases tipicas, ya sin acentos ni signos. El segundo valor exige "?": sin el,
# "sigue" o "ya quedo" son ordenes o afirmaciones, no preguntas.
_CORTAS = (
    (
        re.compile(
            r"(?:y )?(?:como|que tal) (?:va|van|vamos|vas|andamos|anda|sigue|"
            r"siguen|quedo|quedaron)"
            r"(?: (?:eso|esto|todo|todas|todo eso|el trabajo|la revision|"
            r"la implementacion|el pr|(?:la|las) cadenas?(?: de)?(?: \S+)?))?"
        ),
        False,
    ),
    (
        re.compile(
            r"(?:y )?(?:sigue|siguen|sigues|seguimos|continua|continuan|avanza|"
            r"avanzan|va|vamos)(?: (?:vivo|viva|eso|todo|ahi|en pie))?"
        ),
        True,
    ),
    (
        re.compile(
            r"(?:y )?ya (?:quedo|quedaron|termino|terminaron|acabo|esta|estan|"
            r"salio|va|vamos)(?: (?:todo|listo|listos|eso|ya))?"
        ),
        True,
    ),
    (
        re.compile(
            r"en que (?:va|vamos|andamos|etapa (?:va|vamos|andamos|esta)|"
            r"paso (?:va|vamos|andamos|esta))(?: todo| eso)?"
        ),
        False,
    ),
    (
        re.compile(
            r"(?:cual es )?(?:el )?(?:avance|estado|status)"
            r"(?: (?:de )?(?:todo|eso|la cadena|las cadenas))?"
        ),
        False,
    ),
)
# Huele a pregunta de avance dentro de un mensaje mas largo: JEV confirma.
_OLOR = re.compile(
    r"\b(?:como (?:va|van|vamos|andamos|sigue|siguen|quedo|quedaron)|"
    r"en que (?:va|vamos|andamos|etapa|paso)|"
    r"ya (?:quedo|termino|acabo|esta listo|va)|cuanto (?:falta|tarda|le falta))\b"
)
# Palabras que sin "?" son ordenes o charla ("sigue", "la cadena"): exigen pregunta.
_OLOR_DEBIL = re.compile(r"\b(?:sigue|siguen|avance|cadenas?)\b")
# Pide algo MAS que el estado (o no es una pregunta): turno normal, sin JEV.
_VETO = re.compile(
    r"\b(?:cierra\w*|cierre|cerrar|relanza\w*|lanza\w*|arregla\w*|arreglamos|"
    r"cancela\w*|pausa\w*|reanuda\w*|corrige\w*|corregir|haz\w*|hacer|crea\w*|"
    r"manda\w*|envia\w*|borra\w*|elimina\w*|mata\w*|deten\w*|para|parar|"
    r"reinicia\w*|mergea\w*|despliega\w*|deploy|abre|abrir|ejecuta\w*|corre|"
    r"correr|cambia\w*|agrega\w*|quita\w*|revisa\w*|explica\w*|dime|dame|"
    r"muestra\w*|por que|porque|ayudame|resuelve\w*|reintenta\w*|vuelve|"
    r"empieza\w*|inicia\w*)\b"
    r"|^(?:ok|dale|vale|listo|adelante|procede|si|opcion)\b"
)

_ROLES = {
    "scout": "exploración",
    "ingenieria": "planeación",
    "implementer": "implementación",
    "verifier": "verificación",
    "reviewer": "revisión",
    "adversary": "prueba adversaria",
    "entrega": "entrega",
}
_ACTIVAS = ("running", "pending")
_DETENIDAS = ("blocked", "paused", "waiting", "error")


def _plano(texto: str) -> str:
    """Minusculas, sin acentos ni signos de apertura, espacios colapsados."""
    descompuesto = unicodedata.normalize("NFD", str(texto or "").lower())
    sin_acentos = "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")
    return " ".join(sin_acentos.replace("¿", " ").replace("¡", " ").split())


def clasificar_texto(texto) -> str | None:
    """'regla' (frase tipica), 'consultar' (huele a avance: JEV confirma) o None."""
    plano = _plano(texto)
    if not plano or len(plano) > TOPE_TEXTO:
        return None
    pregunta = "?" in plano
    base = " ".join(re.sub(r"[?!.,;:]+", " ", plano).split())
    if not base or _VETO.search(base):
        return None
    if len(base) <= TOPE_CORTA:
        for patron, exige_pregunta in _CORTAS:
            if patron.fullmatch(base) and (pregunta or not exige_pregunta):
                return "regla"
    if _OLOR.search(base) or (pregunta and _OLOR_DEBIL.search(base)):
        return "consultar"
    return None


# --- lectura del estado real -------------------------------------------------


def _epoch(texto) -> float | None:
    try:
        momento = datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.timestamp()


def _abrir_ro(ruta):
    ruta = Path(ruta)
    if not ruta.is_file():
        return None
    return sqlite3.connect(f"{ruta.resolve().as_uri()}?mode=ro", uri=True, timeout=1)


def _abrir_seguro(ruta):
    try:
        return _abrir_ro(ruta) if ruta else None
    except sqlite3.Error:
        return None


def _marcas(carpetas, ahora: float, horas: int):
    """Resultados/marcas de driver tocados en las ultimas `horas`, uno por cadena."""
    vistas: dict = {}
    for carpeta in carpetas:
        try:
            archivos = list(Path(carpeta).glob("*.json"))
        except OSError:
            continue
        for archivo in archivos:
            try:
                estado = archivo.stat()
                if (
                    estado.st_size > TOPE_ARCHIVO
                    or ahora - estado.st_mtime > horas * 3600
                ):
                    continue
                datos = json.loads(archivo.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(datos, dict):
                continue
            cadena = str(datos.get("workflow_id") or archivo.stem)
            datos["_mtime"] = estado.st_mtime
            if cadena not in vistas or vistas[cadena]["_mtime"] < estado.st_mtime:
                vistas[cadena] = {**datos, "workflow_id": cadena}
    return list(vistas.values())


def _ultimo_intento(conn, cadena: str):
    """(etapa, adapter, modelo, status, fallo, inicio_etapa) del intento mas reciente."""
    fila = conn.execute(
        "SELECT stage_id, adapter, model, status, failure FROM subscription_attempts "
        "WHERE workflow_id = ? ORDER BY id DESC LIMIT 1",
        (cadena,),
    ).fetchone()
    if fila is None:
        return None
    inicio = conn.execute(
        "SELECT MIN(created_at) FROM subscription_attempts "
        "WHERE workflow_id = ? AND stage_id = ?",
        (cadena, fila[0]),
    ).fetchone()
    return (*fila, inicio[0] if inicio else None)


def _repo_y_rol(conn, cadena: str, etapa):
    """(repo, rol) del kanban; None donde no se sepa. Nunca lanza."""
    repo = rol = None
    try:
        raiz = conn.execute("SELECT body FROM tasks WHERE id = ?", (cadena,)).fetchone()
        cuerpo = (raiz[0] or "") if raiz else ""
        inicio = cuerpo.find("{")
        contrato = json.loads(cuerpo[inicio:]) if inicio >= 0 else {}
        origen = str(contrato.get("repository") or contrato.get("worktree") or "")
        repo = Path(origen.replace("\\", "/")).name or None
        if etapa:
            fila = conn.execute(
                "SELECT assignee FROM tasks WHERE id = ?", (etapa,)
            ).fetchone()
            rol = fila[0] if fila else None
    except (sqlite3.Error, ValueError, AttributeError):
        pass
    return repo, rol


def _una_linea(texto, tope: int = 100) -> str:
    return " ".join(redact_text(str(texto or ""), ()).split())[:tope]


def leer_cadenas(
    carpetas, db_router, kanban, ahora: float, vivo, horas=HORAS_RECIENTES
):
    """Las cadenas activas o recientes con lo que hace falta para contarlas."""
    router = _abrir_seguro(db_router)
    kanban_conn = _abrir_seguro(kanban)
    cadenas = []
    try:
        for marca in _marcas(carpetas, ahora, horas):
            cadena = marca["workflow_id"]
            salida = str(marca.get("outcome") or "")
            intento = None
            if router is not None:
                try:
                    intento = _ultimo_intento(router, cadena)
                except sqlite3.Error:
                    intento = None
            etapa = intento[0] if intento else None
            repo = rol = None
            if kanban_conn is not None:
                repo, rol = _repo_y_rol(kanban_conn, cadena, etapa)
            info = {
                "id": cadena,
                "salida": salida,
                "mtime": marca["_mtime"],
                "repo": repo,
                "rol": rol,
                "modelo": intento[2] if intento else None,
                "adapter": intento[1] if intento else None,
                "minutos": None,
                "motivo": None,
                "vivo": None,
            }
            if intento:
                desde = _epoch(intento[5])
                if desde is not None:
                    info["minutos"] = max(0, int((ahora - desde) // 60))
            if salida == "running":
                try:
                    info["vivo"] = bool(vivo(dict(marca)))
                except Exception:  # noqa: BLE001 - sin comprobar: se dice, no se inventa
                    info["vivo"] = None
            if salida in _DETENIDAS or (salida == "running" and info["vivo"] is False):
                info["motivo"] = (
                    _una_linea(
                        (intento[4] if intento else None)
                        or marca.get("motivo")
                        or marca.get("error")
                    )
                    or None
                )
            cadenas.append(info)
    finally:
        for conn in (router, kanban_conn):
            if conn is not None:
                conn.close()
    return cadenas


# --- el texto ---------------------------------------------------------------


def _hace(minutos) -> str:
    if minutos is None:
        return ""
    if minutos < 1:
        return "menos de un minuto"
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    return f"{horas} h {resto} min" if resto else f"{horas} h"


def _orden(info) -> tuple:
    grupo = (
        0 if info["salida"] in _ACTIVAS else 1 if info["salida"] in _DETENIDAS else 2
    )
    return (grupo, -info["mtime"])


def _linea(info, ahora: float) -> str:
    nombre = info["repo"] or f"cadena {info['id']}"
    rol = _ROLES.get(info["rol"] or "", info["rol"] or "")
    etapa = f"la etapa de {rol}" if rol else "una etapa"
    modelo = f" con {info['modelo']}" if info["modelo"] else ""
    tiempo = (
        f" desde hace {_hace(info['minutos'])}" if info["minutos"] is not None else ""
    )
    salida = info["salida"]
    if salida == "running":
        if info["vivo"] is None:
            return (
                f"• {nombre}: trabajando en {etapa}{modelo}{tiempo}. "
                "No pude comprobar si el proceso sigue vivo."
            )
        if info["vivo"]:
            return f"• {nombre}: trabajando en {etapa}{modelo}{tiempo}. El proceso sigue vivo."
        motivo = f" Motivo: {info['motivo']}." if info["motivo"] else ""
        return (
            f"• {nombre}: está trabada en {etapa}{modelo}{tiempo}: el proceso "
            f"ya no está vivo.{motivo}"
        )
    if salida in _DETENIDAS:
        estado = "en pausa" if salida in ("paused", "waiting") else "trabada"
        motivo = f" Motivo: {info['motivo']}." if info["motivo"] else ""
        return f"• {nombre}: {estado} en {etapa}{modelo}{tiempo}.{motivo}"
    if salida == "delivered":
        hace = _hace(max(0, int((ahora - info["mtime"]) // 60)))
        return f"• {nombre}: entregada hace {hace}."
    return f"• {nombre}: en curso en {etapa}{modelo}{tiempo}."


def armar_respuesta(cadenas, ahora: float) -> str:
    """El mensaje para David: corto, en español y sin tecnicismos."""
    if not cadenas:
        return f"No hay cadenas activas ni recientes (últimas {HORAS_RECIENTES} h)."
    ordenadas = sorted(cadenas, key=_orden)
    lineas = [_linea(info, ahora) for info in ordenadas[:TOPE_CADENAS]]
    if len(ordenadas) > TOPE_CADENAS:
        lineas.append(f"… y {len(ordenadas) - TOPE_CADENAS} más.")
    return "Así van las cadenas:\n" + "\n".join(lineas)
