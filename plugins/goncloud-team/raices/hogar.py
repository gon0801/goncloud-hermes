"""La raiz del home de Hermes: donde vive el store compartido (SUA-07 H2, F6).

El gateway no define HERMES_HOME en el entorno: resuelve el home de cada
perfil con un override de contexto, y el script del cron recibe el
HERMES_HOME de SU perfil. Si el store colgara del home de cada uno, el
submit del perfil claw y el ticker del perfil default abririan bases
distintas y la ruta de raices quedaria inerte. Este modulo es el unico
lugar que decide: el store (y lo que venga a colgar de el, como los
permisos y las proyecciones de H3/H4) vive en la RAIZ del home de Hermes,
una sola ruta para todos los perfiles.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))


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
            raise ValueError("hermes home is not set; no raices store to open")
        return Path(constants.get_default_hermes_root(home=hogar))
    texto = str(home or os.environ.get("HERMES_HOME", "")).strip()
    if not texto:
        raise ValueError("HERMES_HOME is not set; no raices store to open")
    base = Path(texto)
    if base.parent.name == "profiles":
        return base.parent.parent
    return base
