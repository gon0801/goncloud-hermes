#!/usr/bin/env python3
"""Verifica la copia fijada de G contra su fuente, byte a byte.

Es el script que la CI de H llama (workflow sistema-agentes) y el que se corre en local
antes de mover el pin. La fuente es `--fuente`:

- un directorio con las rutas de G dentro (el espejo mismo, o un checkout de G), o
- una URL base tipo https://raw.githubusercontent.com/gon0801/goncloud-openclaw/<commit>/
  de la que se baja cada archivo de pin.json.

Para cada archivo de pin.json compara el sha256 de la fuente con el fijado. Sale 0 si todo
coincide y 1 si algo difiere o falta. No revisa archivos de más: eso lo hace la prueba del
espejo (tests/test_sistema_agentes_pin.py), porque un checkout completo de G trae miles.
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlparse

REPO = Path(__file__).resolve().parents[1]
PIN = REPO / "shared/sistema-agentes/pin.json"


def bytes_de_fuente(fuente, ruta):
    if isinstance(fuente, str):
        with urllib.request.urlopen(urljoin(fuente, ruta), timeout=60) as respuesta:
            return respuesta.read()
    archivo = fuente / ruta
    if not archivo.is_file():
        raise FileNotFoundError(str(archivo))
    return archivo.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fuente", required=True, help="directorio raíz de G o URL base raw"
    )
    parser.add_argument("--pin", type=Path, default=PIN, help="pin.json a verificar")
    argumentos = parser.parse_args()

    if "://" in argumentos.fuente:
        partes = urlparse(argumentos.fuente)
        fuente = partes._replace(
            path=partes.path if partes.path.endswith("/") else partes.path + "/"
        ).geturl()
    else:
        fuente = Path(argumentos.fuente).resolve()

    pin = json.loads(argumentos.pin.read_text(encoding="utf-8"))
    fallidos = []
    for ruta, fijado in pin["files"].items():
        try:
            contenido = bytes_de_fuente(fuente, ruta)
        except (OSError, URLError) as fallo:
            print(f"FALTA   {ruta}: {fallo}")
            fallidos.append(ruta)
            continue
        digest = "sha256:" + hashlib.sha256(contenido).hexdigest()
        if digest != fijado:
            print(f"DIFIERE {ruta}: fuente {digest} != pin {fijado}")
            fallidos.append(ruta)
        else:
            print(f"OK      {ruta}")

    total = len(pin["files"])
    print(f"{total - len(fallidos)}/{total} archivos coinciden con {argumentos.fuente}")
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main())
