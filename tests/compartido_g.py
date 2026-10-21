"""Acceso controlado a la copia fijada de G en shared/sistema-agentes/g.

El espejo repite las rutas internas de G para que contrato.py halle
docs/evidence/agent-work/politica-calidad.json por Path(__file__).parents[2]
sin editarse.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
G_DIR = REPO / "shared/sistema-agentes/g"
G_SCRIPTS = G_DIR / "scripts"
PIN = REPO / "shared/sistema-agentes/pin.json"

if str(G_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(G_SCRIPTS))

# El importe del espejo no deja __pycache__ dentro de la copia fijada: la prueba
# del pin exige que shared/sistema-agentes/g no traiga ni un archivo de más.
_antes = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    from sistema_agentes import (
        contrato,  # noqa: F401
        derivar,  # noqa: F401
    )
finally:
    sys.dont_write_bytecode = _antes
