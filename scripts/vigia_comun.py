"""Lo que comparten los vigías (``vigila_modelos.py``, ``vigila_tareas.py``)."""

import os
import time


def encola(directorio, texto, clave):
    """Deja ``<ms>-<clave>.txt`` en la cola del motor, ya completo: el motor nunca lee a medias."""
    directorio.mkdir(parents=True, exist_ok=True)
    nombre = f"{int(time.time() * 1000)}-{clave}.txt"
    oculto = directorio / f".{nombre}.tmp"
    oculto.write_text(texto, encoding="utf-8")
    os.replace(oculto, directorio / nombre)
