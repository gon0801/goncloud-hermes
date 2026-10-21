"""La raiz del home de Hermes, para quien cuelga archivos de ella (JEV).

El gateway no define HERMES_HOME en el entorno: resuelve el home de cada
perfil con un override de contexto, y el script de un cron recibe el
HERMES_HOME de SU perfil. Este modulo es el unico lugar que decide la raiz
(un home de perfil sube a ella), para que todos los perfiles abran la misma
ruta.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path


def _constants_de_hermes():
    try:
        return importlib.import_module("hermes_constants")
    except ImportError:
        return None


def raiz_del_hogar(home=None) -> Path:
    """La raiz del home de Hermes: un home de perfil sube a su raiz.

    Con hermes_constants (la PC): get_default_hermes_root sobre
    get_hermes_home (el override de contexto del gateway cuenta, no solo el
    entorno). Sin el (la Mac, pruebas): HERMES_HOME del entorno, y vacio es
    error claro.
    """
    constants = _constants_de_hermes()
    if constants is not None:
        hogar = str(home or constants.get_hermes_home() or "").strip()
        if not hogar:
            raise ValueError("hermes home is not set")
        return Path(constants.get_default_hermes_root(home=hogar))
    texto = str(home or os.environ.get("HERMES_HOME", "")).strip()
    if not texto:
        raise ValueError("HERMES_HOME is not set")
    base = Path(texto)
    if base.parent.name == "profiles":
        return base.parent.parent
    return base
