"""Acceso controlado a la copia fijada de G en shared/sistema-agentes/g.

El espejo repite las rutas internas de G para que contrato.py halle
docs/evidence/agent-work/politica-calidad.json por Path(__file__).parents[2]
sin editarse.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
G_DIR = REPO / "shared/sistema-agentes/g"
G_SCRIPTS = G_DIR / "scripts"
PIN = REPO / "shared/sistema-agentes/pin.json"

if str(G_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(G_SCRIPTS))

import sistema_agentes.contrato as contrato  # noqa: E402, F401
import sistema_agentes.derivar as derivar  # noqa: E402, F401
