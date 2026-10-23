"""Vigía de tareas programadas: avisa cuando una tarea falla o deja de correr.

Cada máquina (Mac, Mini, PC) corre su propia copia y lee solo lo suyo: LaunchAgents, los crons de
claw (Mini) y los de Hermes (PC). No usa ningún modelo de IA y nunca escribe en las tareas.

    python scripts/vigila_tareas.py --maquina mac --seco

La política (modelos acordados, ventana de reinicio, límites, nombres llanos) vive en
``tareas-programadas.json``, en la raíz de la copia instalada.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re

RAIZ = Path(__file__).resolve().parents[1]
HORA = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


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
