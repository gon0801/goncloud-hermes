"""Vigía de tareas programadas: avisa cuando una tarea falla o deja de correr.

Cada máquina (Mac, Mini, PC) corre su propia copia y lee solo lo suyo: LaunchAgents, los crons de
claw (Mini) y los de Hermes (PC). No usa ningún modelo de IA y nunca escribe en las tareas.

    python scripts/vigila_tareas.py --maquina mac --seco

La política (modelos acordados, ventana de reinicio, límites, nombres llanos) vive en
``tareas-programadas.json``, en la raíz de la copia instalada.
"""

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parents[1]
HORA = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
ATRASO_MAX_MS = 90 * 60 * 1000
DIA_MS = 24 * 3600 * 1000
MB = {
    "KIB": 1 / 1024,
    "MIB": 1,
    "GIB": 1024,
    "TIB": 1024 * 1024,
    "KB": 1 / 1000,
    "MB": 1,
    "GB": 1000,
}
MEMORIA = re.compile(
    r"^(\S+) .*memory pressure.*?\brss=([\d.]+) ?([KMGT]i?B)", re.IGNORECASE
)


class PoliticaError(ValueError):
    pass


def _numero(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _entero(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _texto(v):
    return isinstance(v, str) and bool(v)


def _hora(v):
    return isinstance(v, str) and HORA.fullmatch(v) is not None


def _lista(item):
    return lambda v: isinstance(v, list) and all(item(x) for x in v)


def _mapa(valor):
    return lambda v: (
        isinstance(v, dict) and all(_texto(k) and valor(x) for k, x in v.items())
    )


_ESQUEMA = {
    "schema_version": (_entero, "un entero"),
    "modelo_propio": (_mapa(_texto), "un objeto de texto a texto"),
    "ventana_reinicio": {
        "zona": (_texto, "texto"),
        "desde": (_hora, "una hora HH:MM"),
        "hasta": (_hora, "una hora HH:MM"),
        "motivo": (_texto, "texto"),
    },
    "launchd": {
        "prefijos": (_lista(_texto), "una lista de texto"),
        "salidas_ok": (_mapa(_lista(_entero)), "un objeto de texto a lista de enteros"),
    },
    "limites": {
        "claw_rss_mb": (_numero, "un número"),
        "chrome_perfil_mb": (_numero, "un número"),
    },
    "marca_fallo": (_texto, "texto"),
    "nombres_llanos": (_mapa(_texto), "un objeto de texto a texto"),
}
VERSION = 1


def _valida(datos, esquema, ruta=""):
    if not isinstance(datos, dict):
        raise PoliticaError(f"{ruta or 'raíz'}: esperaba un objeto")
    for clave in datos:
        if clave not in esquema:
            raise PoliticaError(f"clave desconocida: {ruta}{clave}")
    for clave, regla in esquema.items():
        if clave not in datos:
            raise PoliticaError(f"falta la clave: {ruta}{clave}")
        if isinstance(regla, dict):
            _valida(datos[clave], regla, f"{ruta}{clave}.")
            continue
        sirve, esperado = regla
        if not sirve(datos[clave]):
            raise PoliticaError(
                f"tipo equivocado en {ruta}{clave}: esperaba {esperado}"
            )


@dataclass(frozen=True)
class Ventana:
    zona: str
    desde: str
    hasta: str
    motivo: str


@dataclass(frozen=True)
class Politica:
    modelo_propio: dict
    ventana: Ventana
    prefijos: tuple
    salidas_ok: dict
    claw_rss_mb: float
    chrome_perfil_mb: float
    marca_fallo: str
    nombres_llanos: dict

    @classmethod
    def carga(cls, ruta):
        try:
            datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise PoliticaError(f"no pude leer {ruta}: {e}") from e
        _valida(datos, _ESQUEMA)
        if datos["schema_version"] != VERSION:
            raise PoliticaError(f"schema_version: esperaba {VERSION}")
        return cls(
            modelo_propio=datos["modelo_propio"],
            ventana=Ventana(**datos["ventana_reinicio"]),
            prefijos=tuple(datos["launchd"]["prefijos"]),
            salidas_ok={k: tuple(v) for k, v in datos["launchd"]["salidas_ok"].items()},
            claw_rss_mb=datos["limites"]["claw_rss_mb"],
            chrome_perfil_mb=datos["limites"]["chrome_perfil_mb"],
            marca_fallo=datos["marca_fallo"],
            nombres_llanos=datos["nombres_llanos"],
        )


class Falla(Enum):
    """El valor es ``(urgente, frase llana)``."""

    NO_CORRE = (True, "no está corriendo")
    TERMINO_MAL = (True, "su última corrida falló")
    FALLO_DECLARADO = (True, "avisó que algo falló y quedó marcada como buena")
    MODELO_PROPIO = (False, "usa un modelo distinto al acordado")
    EN_VENTANA = (False, "está programada a la hora del reinicio de claw")
    PESA_DEMASIADO = (False, "usa más memoria o espacio de lo normal")

    @property
    def urgente(self):
        return self.value[0]

    @property
    def frase(self):
        return self.value[1]


@dataclass(frozen=True)
class Hallazgo:
    maquina: str
    tarea: str
    falla: Falla
    detalle: str


def juzga_launchd(fila, pol):
    fallas = []
    if (
        fila["rutas_que_faltan"]
        or not fila["cargado"]
        or (fila["keepalive"] and fila["state"] != "running")
    ):
        fallas.append(Falla.NO_CORRE)
    codigo = fila["last_exit_code"]
    if (
        fila["periodico"]
        and codigo is not None
        and codigo not in pol.salidas_ok.get(fila["label"], (0,))
    ):
        fallas.append(Falla.TERMINO_MAL)
    return fallas


def _en_ventana(ms, ventana):
    local = datetime.fromtimestamp(ms / 1000, ZoneInfo(ventana.zona)).time()
    return (
        time.fromisoformat(ventana.desde) <= local <= time.fromisoformat(ventana.hasta)
    )


def _modelo_propio(clave, modelo, pol):
    if modelo and pol.modelo_propio.get(clave) != modelo:
        return [Falla.MODELO_PROPIO]
    return []


def _atrasada(proxima_ms, ahora_ms):
    return proxima_ms is not None and ahora_ms - proxima_ms > ATRASO_MAX_MS


def juzga_claw(job, pol, ahora_ms):
    if not job.get("enabled"):
        return []
    fallas = []
    if job.get("lastRunStatus") == "error":
        fallas.append(Falla.TERMINO_MAL)
    proxima = job.get("nextRunAtMs")
    if _atrasada(proxima, ahora_ms):
        fallas.append(Falla.NO_CORRE)
    fallas += _modelo_propio(
        f"claw:{job['name']}", job.get("payload", {}).get("model"), pol
    )
    if (
        proxima is not None
        and job.get("schedule", {}).get("kind") != "every"
        and _en_ventana(proxima, pol.ventana)
    ):
        fallas.append(Falla.EN_VENTANA)
    return fallas


def respuesta_final(texto):
    """Lo que el agente contestó al final de una salida de cron de Hermes, sin el prompt."""
    antes, marca, despues = texto.rpartition("\n## Response\n")
    return despues.strip() if marca else ""


def _iso_ms(valor):
    return int(datetime.fromisoformat(valor).timestamp() * 1000) if valor else None


def juzga_hermes(perfil, job, pol, ahora_ms, salida=None):
    if not job.get("enabled"):
        return []
    fallas = []
    estado = job.get("last_status")
    if (
        (estado is not None and estado != "ok")
        or (job.get("failure_streak") or 0) > 0
        or job.get("last_delivery_error")
    ):
        fallas.append(Falla.TERMINO_MAL)
    if (
        salida is not None
        and not job.get("no_agent")
        and respuesta_final(salida).startswith(pol.marca_fallo)
    ):
        fallas.append(Falla.FALLO_DECLARADO)
    if _atrasada(_iso_ms(job.get("next_run_at")), ahora_ms):
        fallas.append(Falla.NO_CORRE)
    fallas += _modelo_propio(f"hermes:{perfil}/{job['name']}", job.get("model"), pol)
    return fallas


def memoria_alta_mb(log, ahora_ms):
    """El mayor ``rss`` (MB) de las líneas ``memory pressure`` de las últimas 24 h."""
    mayor = 0.0
    for linea in log.splitlines():
        m = MEMORIA.match(linea)
        if not m:
            continue
        try:
            ms = _iso_ms(m.group(1))
        except ValueError:
            continue
        if ms is not None and ahora_ms - ms <= DIA_MS:
            mayor = max(mayor, float(m.group(2)) * MB[m.group(3).upper()])
    return mayor


def juzga_limites(rss_mb, perfil_mb, pol):
    if rss_mb > pol.claw_rss_mb or perfil_mb > pol.chrome_perfil_mb:
        return [Falla.PESA_DEMASIADO]
    return []


@dataclass(frozen=True)
class SinLeer:
    maquina: str
    que: str


MAQUINAS = {"mac": "la Mac", "mini": "la Mini", "pc": "la PC"}
CIERRE = "Pídele a tu asistente que lo revise; lo demás sigue trabajando."
MAX_LINEAS = 8


def _nombre_llano(tarea, pol):
    return pol.nombres_llanos.get(tarea, tarea)


def _lista(nombres):
    cola = [n[:1].lower() + n[1:] for n in nombres[1:]]
    nombres = [nombres[0], *cola]
    if len(nombres) == 1:
        return nombres[0]
    return f"{', '.join(nombres[:-1])} y {nombres[-1]}"


def _linea(nombres, falla):
    if len(nombres) > 3:
        return f"• {len(nombres)} tareas: {falla.frase}."
    return f"• {_lista(nombres)}: {falla.frase}."


def texto_aviso(hallazgos, sin_leer, politica, manana):
    """El aviso para David, o ``None`` si no hay nada que decir.

    Solo usa la máquina, la tarea y la falla: ``Hallazgo.detalle`` (rutas, ids, errores)
    se queda en el registro.
    """
    entran = [h for h in hallazgos if h.falla.urgente or manana]
    if not entran and not sin_leer:
        return None
    cuerpo = []
    for falla in Falla:
        nombres = []
        for h in entran:
            nombre = _nombre_llano(h.tarea, politica)
            if h.falla is falla and nombre not in nombres:
                nombres.append(nombre)
        if nombres:
            cuerpo.append(_linea(nombres, falla))
    if sin_leer:
        cuerpo.append(f"• No pude revisar {_lista([s.que for s in sin_leer])}.")
    if len(cuerpo) > MAX_LINEAS - 2:
        sobran = len(cuerpo) - (MAX_LINEAS - 3)
        cuerpo = [*cuerpo[: MAX_LINEAS - 3], f"• Y {sobran} avisos más."]
    maquina = MAQUINAS[(entran or sin_leer)[0].maquina]
    return "\n".join([f"En {maquina} · Tareas programadas:", *cuerpo, CIERRE])
