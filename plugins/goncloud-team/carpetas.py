"""Carpetas privadas que el resto del mismo dueño puede leer.

En vivo (PC, 19-oct): desde Python 3.13, en Windows mkdir(mode=0o700)
levanta una DACL protegida (SYSTEM, Administradores y OWNER RIGHTS, sin
herencia). La carpeta goncloud-local/avisos la creó un proceso por SSH y
el cron despertar-raices — que corre con el token del gateway — la veía
VACÍA (glob se traga el PermissionError): los avisos de cadenas nunca
llegaron a claw. Se arregló a mano con icacls /reset. El helper evita el
candado: en Windows la carpeta se crea sin mode y hereda la DACL del
padre; en POSIX conserva 0o700.
"""

import os
import subprocess
from pathlib import Path


def _reparar_dacl_windows(ruta) -> None:
    """Una carpeta ya creada con la DACL rota vuelve a heredar la del padre."""
    try:
        subprocess.run(
            ["icacls", str(ruta), "/reset"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # best effort: la creación ya salió; el aviso no se frena por esto


def mkdir_privado(ruta, *, exist_ok=True):
    """Crea ruta (con padres) privada para el dueño, legible para su gateway."""
    ruta = Path(ruta)
    if os.name == "nt":
        ya_existia = ruta.is_dir()
        ruta.mkdir(parents=True, exist_ok=exist_ok)
        if ya_existia:
            # F2: exist_ok no tocaba la carpeta vieja y su DACL protegida
            # seguía dejando el buzón vacío para el cron del gateway.
            _reparar_dacl_windows(ruta)
    else:
        ruta.mkdir(mode=0o700, parents=True, exist_ok=exist_ok)
