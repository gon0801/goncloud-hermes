"""Doble de hermes_constants: el home de Hermes y su raiz (SUA-07 H2, F6).

Mide la referencia v0.22.0 (e06df9c) en la Gon-pc: get_hermes_home() es el override de
contexto -> env HERMES_HOME -> default de la plataforma. El default de la
plataforma no esta doblado: sin override y sin env devuelve "" y el
resolvedor (raices/hogar.py) falla con error claro, igual que sin el modulo.
get_default_hermes_root(home=...) sube de <raiz>/profiles/<x> a <raiz>; una
raiz es su propia raiz.
"""

import os
from pathlib import Path

_OVERRIDE = None


def set_override(home):
    global _OVERRIDE
    _OVERRIDE = home


def get_hermes_home():
    if _OVERRIDE is not None:
        return _OVERRIDE
    return os.environ.get("HERMES_HOME", "")


def get_default_hermes_root(home=None):
    hogar = Path(home if home is not None else get_hermes_home())
    if hogar.parent.name == "profiles":
        return hogar.parent.parent
    return hogar
