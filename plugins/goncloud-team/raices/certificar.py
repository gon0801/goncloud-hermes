#!/usr/bin/env python3
"""Certifica el par (version, code_sha) de Hermes vivo en esta PC (SUA-07 H4a).

Nunca toca el HERMES_HOME original: copia a un directororio temporal, corre
la bateria del kanban real contra esa copia y, si pasa (ultima linea OK
exacta y al menos tres pruebas), imprime la candidata para
certificadas.json. Con --escribir la agrega al archivo del plugin tras
validar la forma y rechazar duplicados; el par entra al repo por PR.

Uso: python certificar.py [--escribir] [--home RUTA]
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

capacidades = importlib.import_module("raices.capacidades")
store = importlib.import_module("raices.store")

PRUEBAS = "tests.test_sistema_agentes_kanban_real"
_RESUMEN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)


def _texto_de_version() -> str | None:
    try:
        terminado = subprocess.run(
            ["hermes", "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if terminado.returncode != 0:
        return None
    return terminado.stdout.strip() or None


def _leer_par_vivo():
    """El par segun los lectores reales de la puerta, con el texto crudo de
    --version para la evidencia."""
    hechos = capacidades.hechos_vivos()
    abierta, causa = capacidades.decidir(hechos)
    par = None
    if hechos.version is not None and hechos.gateway is not None:
        par = {
            "version": hechos.version.version,
            "code_sha": hechos.gateway.code_sha,
            "texto_version": _texto_de_version(),
        }
    return abierta, causa, par


def _copiar_home(home, destino):
    shutil.copytree(home, destino)


def _correr_pruebas(home_copia, repo, python=sys.executable) -> str:
    entorno = dict(os.environ)
    entorno.update(
        {
            "HERMES_HOME": str(home_copia),
            "HERMES_KANBAN_DB": str(Path(home_copia) / "kanban.db"),
            "PYTHONPATH": f"{repo}/plugins/goncloud-team:{repo}/tests",
        }
    )
    terminado = subprocess.run(
        [python, "-m", "unittest", PRUEBAS, "-v"],
        cwd=str(repo),
        env=entorno,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return terminado.stdout


def _paso_la_bateria(salida) -> str | None:
    """La ultima linea OK exacta (nada de skipped) y al menos tres pruebas:
    la certificacion es de la PC, no del doble de la Mac."""
    if not isinstance(salida, str):
        return None
    lineas = [linea for linea in salida.strip().splitlines() if linea.strip()]
    if not lineas or lineas[-1] != "OK":
        return None
    total = 0
    for hallado in _RESUMEN.finditer(salida):
        total = max(total, int(hallado.group(1)))
    return lineas[-1] if total >= 3 else None


def _escribir(ruta: Path, lista: list) -> None:
    temporal = ruta.with_name(f".{ruta.name}.{os.getpid()}.tmp")
    temporal.write_text(
        json.dumps(lista, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    os.replace(temporal, ruta)


def main(
    argv=None,
    *,
    leer_par=_leer_par_vivo,
    copiar_home=_copiar_home,
    correr_pruebas=_correr_pruebas,
    ruta_certificados=None,
) -> int:
    analizador = argparse.ArgumentParser(
        description="Certifica el par (version, code_sha) de Hermes vivo."
    )
    analizador.add_argument(
        "--escribir",
        action="store_true",
        help="agrega la candidata a certificadas.json (sin esta bandera solo imprime)",
    )
    analizador.add_argument(
        "--home", default=None, help="HERMES_HOME a copiar (por defecto, el del env)"
    )
    argumentos = analizador.parse_args(argv)
    home = argumentos.home or os.environ.get("HERMES_HOME", "")

    abierta, causa, par = leer_par()
    if abierta:
        print("el par vivo ya esta certificado")
        return 0
    if causa != "version-no-certificada" or not isinstance(par, dict):
        print(causa)
        return 1

    with tempfile.TemporaryDirectory() as temporal:
        copia = Path(temporal) / "hermes"
        copiar_home(home, copia)
        salida = correr_pruebas(copia, REPO)
    resumen = _paso_la_bateria(salida)
    if resumen is None:
        print(salida)
        return 1

    candidata = {
        "version": par["version"],
        "code_sha": par["code_sha"],
        "certificadoEn": store.ahora_utc(),
        "evidencia": {
            "pruebas": PRUEBAS,
            "resumen": resumen,
            "hermesVersion": par.get("texto_version"),
            "corridoEn": store.ahora_utc(),
        },
    }
    print(json.dumps(candidata, indent=2, ensure_ascii=False))
    if not argumentos.escribir:
        return 0

    ruta = (
        Path(ruta_certificados)
        if ruta_certificados
        else (capacidades.RUTA_CERTIFICADAS)
    )
    nuevas = list(capacidades.leer_certificadas(ruta)) + [candidata]
    errores = capacidades.validar_certificadas(nuevas)
    if errores:
        for error in errores:
            print(f"error: {error}")
        return 1
    _escribir(ruta, nuevas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
