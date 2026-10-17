"""La puerta de capacidades de la ruta de raíces (SUA-07 H4a).

certificadas.json lista los pares (version, code_sha) acreditados en la PC.
La forma la valida un solo punto, validar_certificadas, que comparten la
prueba de forma sobre el archivo real del repo y certificar.py antes de
escribir.
"""

from __future__ import annotations

import re

_FORMA_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_FORMA_SHA = re.compile(r"^[0-9a-f]{40}$")
_FORMA_INSTANTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def validar_certificadas(lista) -> list:
    """Los errores de forma de la lista de pares certificados; [] si vale.
    Un solo punto para la prueba del repo y certificar.py, así el archivo y
    su escritura no pueden discrepar sobre qué es una entrada válida."""
    if not isinstance(lista, list):
        return ["la lista certificada no es una lista"]
    errores: list = []
    vistos: set = set()
    for posicion, entrada in enumerate(lista):
        if not isinstance(entrada, dict):
            errores.append(f"entrada {posicion}: no es un objeto")
            continue
        version = entrada.get("version")
        if not isinstance(version, str) or not _FORMA_VERSION.match(version):
            errores.append(f"entrada {posicion}: version no es semver X.Y.Z")
        sha = entrada.get("code_sha")
        if not isinstance(sha, str) or not _FORMA_SHA.match(sha):
            errores.append(f"entrada {posicion}: code_sha no son 40 hex minúsculas")
        instante = entrada.get("certificadoEn")
        if not isinstance(instante, str) or not _FORMA_INSTANTE.match(instante):
            errores.append(f"entrada {posicion}: certificadoEn no es ISO 8601 Z")
        if not entrada.get("evidencia"):
            errores.append(f"entrada {posicion}: falta la evidencia")
        if isinstance(version, str) and isinstance(sha, str):
            clave = (version, sha.lower())
            if clave in vistos:
                errores.append(f"entrada {posicion}: par (version, code_sha) duplicado")
            vistos.add(clave)
    return errores
