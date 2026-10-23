"""Vigía de tareas programadas: avisa cuando una tarea falla o deja de correr.

Cada máquina (Mac, Mini, PC) corre su propia copia y lee solo lo suyo: LaunchAgents, los crons de
claw (Mini) y los de Hermes (PC). No usa ningún modelo de IA y nunca escribe en las tareas.

    python scripts/vigila_tareas.py --maquina mac --seco

La política (modelos acordados, ventana de reinicio, límites, nombres llanos) vive en
``tareas-programadas.json``, en la raíz de la copia instalada.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time as reloj
from xml.parsers.expat import ExpatError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from vigia_comun import encola

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


def _lista_de(item):
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
    "excepciones_ventana": (_mapa(_texto), "un objeto de texto a texto"),
    "apagadas_a_proposito": (_mapa(_texto), "un objeto de texto a texto"),
    "launchd": {
        "prefijos": (_lista_de(_texto), "una lista de texto"),
        "salidas_ok": (
            _mapa(_lista_de(_entero)),
            "un objeto de texto a lista de enteros",
        ),
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
    excepciones_ventana: dict
    apagadas_a_proposito: dict
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
        ventana = datos["ventana_reinicio"]
        try:
            ZoneInfo(ventana["zona"])
        except ZoneInfoNotFoundError as e:
            if available_timezones():
                raise PoliticaError(
                    f"ventana_reinicio.zona: no conozco la zona {ventana['zona']}"
                ) from e
        except ValueError as e:
            raise PoliticaError(
                f"ventana_reinicio.zona: no conozco la zona {ventana['zona']}"
            ) from e
        if ventana["desde"] >= ventana["hasta"]:
            raise PoliticaError("ventana_reinicio.desde: debe ser menor que hasta")
        return cls(
            modelo_propio=datos["modelo_propio"],
            ventana=Ventana(**datos["ventana_reinicio"]),
            excepciones_ventana=datos["excepciones_ventana"],
            apagadas_a_proposito=datos["apagadas_a_proposito"],
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
    if fila["label"] not in pol.apagadas_a_proposito and (
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
    if job["name"] not in pol.apagadas_a_proposito and _atrasada(proxima, ahora_ms):
        fallas.append(Falla.NO_CORRE)
    fallas += _modelo_propio(
        f"claw:{job['name']}", (job.get("payload") or {}).get("model"), pol
    )
    if (
        proxima is not None
        and (job.get("schedule") or {}).get("kind") != "every"
        and job["name"] not in pol.excepciones_ventana
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
    if job["name"] not in pol.apagadas_a_proposito and _atrasada(
        _iso_ms(job.get("next_run_at")), ahora_ms
    ):
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
    motivo: str = ""


MAQUINAS = {"mac": "la Mac", "mini": "la Mini", "pc": "la PC"}
CIERRE = "Pídele a tu asistente que lo revise; lo demás sigue trabajando."
MAX_LINEAS = 8
MESES = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre"
).split()
FECHA = re.compile(r"\d{4}-\d{2}-\d{2}")


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


def _lineas_de(hallazgos, politica):
    cuerpo = []
    for falla in Falla:
        nombres = []
        for h in hallazgos:
            nombre = _nombre_llano(h.tarea, politica)
            if h.falla is falla and nombre not in nombres:
                nombres.append(nombre)
        if nombres:
            cuerpo.append(_linea(nombres, falla))
    return cuerpo


def _lineas_arreglados(arreglados, politica):
    nombres = []
    for h in arreglados:
        nombre = _nombre_llano(h.tarea, politica)
        if nombre not in nombres:
            nombres.append(nombre)
    if len(nombres) > 3:
        return [f"• {len(nombres)} tareas: ya se arreglaron."]
    return [f"• {n}: ya se arregló." for n in nombres]


def _fecha_llana(fecha):
    _, mes, dia = fecha.split("-")
    return f"{int(dia)} de {MESES[int(mes) - 1]}"


def texto_aviso(hallazgos, sin_leer, politica, manana, arreglados=(), pendientes=None):
    """El aviso para David, o ``None`` si no hay nada que decir.

    ``hallazgos`` son los que se avisan por primera vez, ``arreglados`` los que ya no
    aparecen y ``pendientes`` (``{fecha del primer aviso: hallazgos}``) el recordatorio del día.
    Solo usa la máquina, la tarea y la falla: ``Hallazgo.detalle`` (rutas, ids, errores)
    se queda en el registro.
    """
    entran = [h for h in hallazgos if h.falla.urgente or manana]
    pendientes = pendientes or {}
    todos = [*entran, *arreglados, *(h for hs in pendientes.values() for h in hs)]
    if not todos and not sin_leer:
        return None
    cuerpo = _lineas_de(entran, politica) + _lineas_arreglados(arreglados, politica)
    for fecha, hs in sorted(pendientes.items()):
        cuerpo += [
            f"Sigue pendiente desde {_fecha_llana(fecha)}:",
            *_lineas_de(hs, politica),
        ]
    no_pude = (
        [f"• No pude revisar {_lista([s.que for s in sin_leer])}."] if sin_leer else []
    )
    cupo = MAX_LINEAS - 2 - len(no_pude)
    if len(cuerpo) > cupo:
        sobran = len(cuerpo) - (cupo - 1)
        cuerpo = [*cuerpo[: cupo - 1], f"• Y {sobran} avisos más."]
    cuerpo += no_pude
    maquina = MAQUINAS[(todos or sin_leer)[0].maquina]
    return "\n".join([f"En {maquina} · Tareas programadas:", *cuerpo, CIERRE])


def clave_de(h):
    return f"{h.maquina}|{h.falla.name}|{h.tarea}"


def _clave_valida(clave):
    partes = clave.split("|", 2) if isinstance(clave, str) else []
    return len(partes) == 3 and partes[1] in Falla.__members__


def _hallazgo_de(clave):
    maquina, falla, tarea = clave.split("|", 2)
    return Hallazgo(maquina, tarea, Falla[falla], "")


def _ruta_del_estado():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or r"C:\Users\ehven\AppData\Local"
        return Path(base) / "goncloud-vigia-estado" / "estado.json"
    return Path.home() / ".local" / "state" / "goncloud-vigia" / "estado.json"


def _fecha_valida(valor):
    if not (isinstance(valor, str) and FECHA.fullmatch(valor)):
        return False
    try:
        datetime.strptime(valor, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def lee_estado(ruta):
    """Lo que ya se avisó: ``{"hallazgos": {clave: fecha}, "recordatorio": fecha}``.

    Un archivo que falta o no se entiende cuenta como vacío.
    """
    vacio = {"hallazgos": {}, "recordatorio": ""}
    try:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        avisados = datos["hallazgos"]
        recordatorio = datos.get("recordatorio", "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return vacio
    if not isinstance(avisados, dict):
        return vacio
    return {
        "hallazgos": {
            k: v for k, v in avisados.items() if _clave_valida(k) and _fecha_valida(v)
        },
        "recordatorio": recordatorio if isinstance(recordatorio, str) else "",
    }


def guarda_estado(ruta, estado):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_name(f".{ruta.name}.tmp")
    temporal.write_text(json.dumps(estado, indent=2, ensure_ascii=False), "utf-8")
    os.replace(temporal, ruta)


def concilia(estado, hallazgos, manana, hoy, completo=True):
    """Compara lo que hay con lo ya avisado: ``(nuevos, arreglados, pendientes, estado nuevo)``.

    Con una lectura incompleta no se sabe qué se arregló: lo avisado que no salió
    se queda en el estado y no se declara arreglado ni se recuerda.

    Un hallazgo no urgente que todavía no toca avisar (por la tarde) no entra al estado.
    """
    actuales = {clave_de(h): h for h in hallazgos}
    avisados = estado["hallazgos"]
    nuevos = [
        h
        for k, h in actuales.items()
        if k not in avisados and (h.falla.urgente or manana)
    ]
    arreglados = (
        [_hallazgo_de(k) for k in avisados if k not in actuales] if completo else []
    )
    quedan = {
        k: fecha for k, fecha in avisados.items() if k in actuales or not completo
    }
    pendientes = {}
    if manana and estado["recordatorio"] != hoy:
        for k, fecha in quedan.items():
            if fecha != hoy and k in actuales:
                pendientes.setdefault(fecha, []).append(actuales[k])
    nuevo = {
        "hallazgos": {**quedan, **{clave_de(h): hoy for h in nuevos}},
        "recordatorio": hoy if pendientes else estado["recordatorio"],
    }
    return nuevos, arreglados, pendientes, nuevo


@dataclass(frozen=True)
class Lector:
    """Una lectura de la máquina: ``leer(politica, ahora_ms)`` devuelve sus ``Hallazgo``."""

    que: str
    leer: object


def _corre(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    return r


def _campo(texto, nombre):
    m = re.search(rf"^\s*{nombre} = (.*)$", texto, re.MULTILINE)
    return m.group(1).strip() if m else None


def _codigo_de_salida(crudo):
    m = re.match(r"-?\d+", crudo or "")
    return int(m.group()) if m else None


def lee_launchd(prefijos, carpeta=None):
    carpeta = carpeta or Path.home() / "Library" / "LaunchAgents"
    filas = []
    for ruta in sorted(carpeta.glob("*.plist")):
        try:
            plist = plistlib.loads(ruta.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
            plist = None
        label = plist.get("Label", ruta.stem) if plist else ruta.stem
        if not label.startswith(tuple(prefijos)):
            continue
        if plist is None:
            r = _corre(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
            filas.append(
                {
                    "label": label,
                    "periodico": False,
                    "keepalive": False,
                    "cargado": False,
                    "state": _campo(r.stdout, "state"),
                    "runs": None,
                    "last_exit_code": None,
                    "rutas_que_faltan": [str(ruta)],
                }
            )
            continue
        r = _corre(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
        args = plist.get("ProgramArguments") or [plist.get("Program", "")]
        runs = _campo(r.stdout, "runs")
        filas.append(
            {
                "label": label,
                "periodico": "StartCalendarInterval" in plist
                or "StartInterval" in plist,
                "keepalive": bool(plist.get("KeepAlive")),
                "cargado": r.returncode == 0,
                "state": _campo(r.stdout, "state"),
                "runs": int(runs) if runs and runs.isdigit() else None,
                "last_exit_code": _codigo_de_salida(_campo(r.stdout, "last exit code")),
                "rutas_que_faltan": [
                    a
                    for a in args
                    if a.startswith("/") and ":" not in a and not os.path.exists(a)
                ],
            }
        )
    return filas


def hallazgos_launchd(filas, maquina, pol):
    out = []
    for fila in filas:
        for falla in juzga_launchd(fila, pol):
            detalle = (
                f"cargado={fila['cargado']} state={fila['state']} "
                f"last_exit_code={fila['last_exit_code']} "
                f"rutas_que_faltan={fila['rutas_que_faltan']}"
            )
            out.append(Hallazgo(maquina, fila["label"], falla, detalle))
    return out


def _openclaw():
    return shutil.which("openclaw") or str(
        Path.home() / ".openclaw" / "bin" / "openclaw"
    )


def lee_cron_claw():
    """Todos los crons de claw, página por página (``hasMore`` y ``nextOffset``)."""
    jobs, offset = [], 0
    while True:
        params = json.dumps({"includeDisabled": True, "limit": 100, "offset": offset})
        r = _corre(
            [_openclaw(), "gateway", "call", "cron.list", "--params", params, "--json"]
        )
        if r.returncode != 0:
            raise RuntimeError(
                r.stderr.strip()[-200:] or f"openclaw salió con {r.returncode}"
            )
        pagina = json.loads(r.stdout)
        jobs += pagina["jobs"]
        if not pagina.get("hasMore"):
            return jobs
        offset = pagina["nextOffset"]


def hallazgos_claw(jobs, maquina, pol, ahora_ms):
    out = []
    for job in jobs:
        for falla in juzga_claw(job, pol, ahora_ms):
            detalle = f"id={job.get('id')} lastRunError={job.get('lastRunError')}"
            out.append(Hallazgo(maquina, job["name"], falla, detalle))
    return out


def _carpeta_hermes():
    base = os.environ.get("LOCALAPPDATA") or r"C:\Users\ehven\AppData\Local"
    return Path(base) / "hermes" / "profiles"


def _ultima_salida(carpeta):
    salidas = (
        sorted(p for p in carpeta.glob("*") if p.is_file()) if carpeta.is_dir() else []
    )
    return (
        salidas[-1].read_text(encoding="utf-8", errors="replace") if salidas else None
    )


def lee_hermes(carpeta=None):
    """``[(perfil, trabajo, última salida o None)]`` de cada perfil con ``cron/jobs.json``."""
    carpeta = carpeta or _carpeta_hermes()
    out = []
    for perfil in sorted(p for p in carpeta.iterdir() if p.is_dir()):
        ruta = perfil / "cron" / "jobs.json"
        if not ruta.is_file():
            continue
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        for job in datos["jobs"] if isinstance(datos, dict) else datos:
            salida = _ultima_salida(perfil / "cron" / "output" / str(job.get("id")))
            out.append((perfil.name, job, salida))
    return out


def hallazgos_hermes(trabajos, maquina, pol, ahora_ms):
    out = []
    for perfil, job, salida in trabajos:
        for falla in juzga_hermes(perfil, job, pol, ahora_ms, salida):
            detalle = (
                f"perfil={perfil} id={job.get('id')} last_status={job.get('last_status')} "
                f"failure_streak={job.get('failure_streak')} "
                f"last_error={job.get('last_error')} "
                f"last_delivery_error={job.get('last_delivery_error')}"
            )
            out.append(Hallazgo(maquina, job["name"], falla, detalle))
    return out


def _mb_de_carpeta(carpeta):
    r = _corre(["du", "-sk", str(carpeta)])
    if r.returncode != 0 or not r.stdout.split():
        raise RuntimeError(f"no pude medir {carpeta}")
    return int(r.stdout.split()[0]) / 1024


def hallazgos_limites(maquina, pol, ahora_ms, log=None, perfil=None):
    home = Path.home()
    log = log or home / "Library" / "Logs" / "openclaw" / "gateway.log"
    perfil = perfil or home / ".openclaw" / "browser" / "claw" / "user-data"
    rss = memoria_alta_mb(log.read_text(encoding="utf-8", errors="replace"), ahora_ms)
    mb = _mb_de_carpeta(perfil)
    return [
        Hallazgo(
            maquina,
            "claw",
            falla,
            f"rss_max_24h_mb={rss:.0f} perfil_chrome_mb={mb:.0f}",
        )
        for falla in juzga_limites(rss, mb, pol)
    ]


def lectores_de(maquina):
    if maquina == "mac":
        return [
            Lector(
                "las tareas de launchd",
                lambda pol, ahora: hallazgos_launchd(
                    lee_launchd(pol.prefijos), "mac", pol
                ),
            )
        ]
    if maquina == "mini":
        return [
            Lector(
                "las tareas de launchd",
                lambda pol, ahora: hallazgos_launchd(
                    lee_launchd(pol.prefijos), "mini", pol
                ),
            ),
            Lector(
                "las tareas programadas de claw",
                lambda pol, ahora: hallazgos_claw(lee_cron_claw(), "mini", pol, ahora),
            ),
            Lector(
                "la memoria de claw",
                lambda pol, ahora: hallazgos_limites("mini", pol, ahora),
            ),
        ]
    return [
        Lector(
            "las tareas programadas de Hermes",
            lambda pol, ahora: hallazgos_hermes(lee_hermes(), "pc", pol, ahora),
        )
    ]


def _ruta_del_registro():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or r"C:\Users\ehven\AppData\Local"
        return Path(base) / "goncloud-vigia" / "vigia.log"
    return Path.home() / "Library" / "Logs" / "goncloud-vigia.log"


def _linea_de_detalle(h):
    return f"{h.maquina} {h.tarea} {h.falla.name}: {h.detalle}"


def _deja_en_el_registro(ruta, hallazgos, sin_leer, ahora):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    marca = reloj.strftime("%F %T", reloj.localtime(ahora))
    lineas = [f"{marca} {_linea_de_detalle(h)}" for h in hallazgos]
    lineas += [
        f"{marca} {s.maquina} no pude revisar {s.que}: {s.motivo}" for s in sin_leer
    ]
    with ruta.open("a", encoding="utf-8") as f:
        f.write("\n".join(lineas) + "\n")


def main(argv=None, lectores=None, ahora=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--maquina", required=True, choices=sorted(MAQUINAS))
    ap.add_argument(
        "--raiz", type=Path, default=RAIZ, help="carpeta de la copia instalada"
    )
    ap.add_argument(
        "--seco", action="store_true", help="imprime los hallazgos y no encola"
    )
    ap.add_argument("--aviso", type=Path, default=Path("~/.motor/avisos/pendientes"))
    ap.add_argument("--registro", type=Path, default=None)
    ap.add_argument(
        "--estado", type=Path, default=None, help="archivo de lo que ya se avisó"
    )
    args = ap.parse_args(argv)
    ahora = reloj.time() if ahora is None else ahora
    try:
        pol = Politica.carga(args.raiz / "tareas-programadas.json")
    except PoliticaError as e:
        print(f"vigila_tareas: {e}", file=sys.stderr)
        return 2

    hallazgos, sin_leer = [], []
    for lector in lectores or lectores_de(args.maquina):
        try:
            hallazgos += lector.leer(pol, int(ahora * 1000))
        except Exception as e:  # noqa: BLE001
            sin_leer.append(SinLeer(args.maquina, lector.que, str(e)[-300:]))
    urgente = any(h.falla.urgente for h in hallazgos)

    if args.seco:
        for h in hallazgos:
            print(_linea_de_detalle(h))
        for s in sin_leer:
            print(f"{s.maquina} no pude revisar {s.que}: {s.motivo}")
        return 1 if urgente else 0

    manana = reloj.localtime(ahora).tm_hour < 12
    hoy = reloj.strftime("%Y-%m-%d", reloj.localtime(ahora))
    ruta_estado = args.estado or _ruta_del_estado()
    nuevos, arreglados, pendientes, estado = concilia(
        lee_estado(ruta_estado), hallazgos, manana, hoy, not sin_leer
    )
    arreglados = [h for h in arreglados if h.tarea not in pol.apagadas_a_proposito]
    aviso = texto_aviso(nuevos, sin_leer, pol, manana, arreglados, pendientes)
    try:
        if aviso:
            encola(args.aviso.expanduser(), aviso, "vigila-tareas")
        guarda_estado(ruta_estado, estado)
        if hallazgos or sin_leer:
            _deja_en_el_registro(
                args.registro or _ruta_del_registro(), hallazgos, sin_leer, ahora
            )
    except OSError as e:
        print(f"vigila_tareas: no pude dejar el aviso: {e}", file=sys.stderr)
        return 2
    return 1 if urgente else 0


if __name__ == "__main__":
    sys.exit(main())
