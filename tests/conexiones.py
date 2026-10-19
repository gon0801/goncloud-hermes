"""Rastrea las conexiones sqlite3 que una prueba deja abiertas.

En Windows un archivo abierto no se borra (WinError 32) y el checkpoint WAL
tardio de una conexion viva deja archivos que reaparecen al vaciar el
temporal (WinError 145). El rastreo no depende de la plataforma: detecta la
conexion sin cerrar aunque el borrado del temporal no falle en la Mac.
"""

import os
import sqlite3
from unittest import mock

# JEV apagado en la suite: el ticker bajo prueba no consulta TypeSafe de
# verdad ni anexa al registro del dueño. Igual que en goncloud_test_support.
os.environ["GONCLOUD_JEV_ROUTER_CLASIFICACION"] = "0"
os.environ["GONCLOUD_JEV_MUSE_BRIDGE_CLASIFICACION"] = "0"
os.environ["GONCLOUD_JEV_TICKER_INCIDENCIA"] = "0"


class RastroDeConexiones:
    def __init__(self):
        self.conexiones = []
        self._parche = None

    def instalar(self):
        original = sqlite3.connect

        def conectar(*argumentos, **nombrados):
            conexion = original(*argumentos, **nombrados)
            self.conexiones.append(conexion)
            return conexion

        self._parche = mock.patch("sqlite3.connect", conectar)
        self._parche.start()

    def desinstalar(self):
        if self._parche is not None:
            self._parche.stop()
            self._parche = None

    def abiertas(self):
        vivas = []
        for conexion in self.conexiones:
            try:
                conexion.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                # Cerrada ("Cannot operate on a closed database") o nacida en
                # otro hilo con check_same_thread=True (no se puede sondear
                # desde aqui): en los dos casos este rastro no la cuenta.
                continue
            vivas.append(conexion)
        return vivas


def _exigir_sin_abiertas(rastro, detalle):
    sobrantes = rastro.abiertas()
    assert not sobrantes, f"conexiones sqlite sin cerrar{detalle}: {sobrantes}"


def rastrear_conexiones(caso, detalle=""):
    """Registra el rastro en una prueba. Los cleanups corren en orden inverso:
    la asercion se anota primero para que corra al final, cuando todas las
    conexiones ya deberian estar cerradas."""
    rastro = RastroDeConexiones()
    rastro.instalar()
    caso.addCleanup(rastro.desinstalar)
    caso.addCleanup(_exigir_sin_abiertas, rastro, detalle)
    return rastro
