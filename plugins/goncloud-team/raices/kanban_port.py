"""La frontera con el kanban y su doble en memoria (SUA-07 H1).

El adaptador real sobre hermes_cli.kanban_db es H2: aquí solo la interfaz, la
primitiva de despacho y el doble. Invariante de la frontera: las escrituras del kanban
solo existen dentro de un write_txn exclusivo, y la única forma de crear una tarjeta
es buscar_o_crear con la clave de idempotencia, dentro de esa transacción. Así, dos
tickers con la misma intención producen exactamente una tarjeta (I03), y una caída
tras el commit (ACK perdido) la encuentra la vuelta siguiente (I04).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from typing import NamedTuple, Protocol


def clave_de_tarjeta(root_id: str, unit_id: str, attempt: int) -> str:
    """La clave de idempotencia de una tarjeta de intento. Única fuente: la compone,
    nunca se parsea; la conciliación la deriva desde los intentos."""
    return f"sa:{root_id}:{unit_id}:{attempt}"


class Tarjeta(NamedTuple):
    id: str
    clave: str
    estado: str
    resultado: dict | None


class SpecDeTarjeta(NamedTuple):
    titulo: str
    cuerpo: str
    asignado_a: str


class Escritura(Protocol):
    """Las operaciones de escritura del kanban, válidas solo dentro de write_txn."""

    def buscar_o_crear(self, clave: str, spec: SpecDeTarjeta) -> Tarjeta: ...

    def vincular(self, id_tarjeta: str, id_padre: str) -> None: ...

    def completar(self, id_tarjeta: str, resultado: dict | None) -> None: ...

    def bloquear(self, id_tarjeta: str, motivo: str) -> None: ...


class KanbanPort(Protocol):
    def write_txn(self) -> object: ...

    def leer_estado(self, clave: str) -> Tarjeta | None: ...


def asegurar_tarjeta(puerto, clave: str, spec: SpecDeTarjeta) -> Tarjeta:
    """La primitiva de despacho: UN write_txn con buscar-o-crear adentro. El
    invariante de I03 vive aquí: quien llama no puede despachar con dos transacciones
    ni saltarse la búsqueda."""
    with puerto.write_txn() as escritura:
        return escritura.buscar_o_crear(clave, spec)


class PuntoDeCaida(Enum):
    ABRIR_TXN = "abrir_txn"
    ANTES_DE_BUSCAR = "antes_de_buscar"
    DESPUES_DE_CREAR = "despues_de_crear"
    DESPUES_DE_TXN = "despues_de_txn"


class CaidaInyectada(RuntimeError):
    def __init__(self, punto: PuntoDeCaida):
        self.punto = punto
        super().__init__(f"caída inyectada en {punto.value}")


class _EscrituraDoble:
    def __init__(self, doble: DobleKanban, overlay: dict):
        self._doble = doble
        self._overlay = overlay

    def buscar_o_crear(self, clave: str, spec: SpecDeTarjeta) -> Tarjeta:
        self._doble._disparar(PuntoDeCaida.ANTES_DE_BUSCAR)
        hallada = self._overlay.get(clave) or self._doble._tarjetas.get(clave)
        if hallada is not None:
            return hallada
        nueva = self._doble._crear(clave, spec)
        self._overlay[clave] = nueva
        self._doble._disparar(PuntoDeCaida.DESPUES_DE_CREAR)
        return nueva

    def vincular(self, id_tarjeta: str, id_padre: str) -> None:
        self._doble._vinculos.add((id_tarjeta, id_padre))

    def completar(self, id_tarjeta: str, resultado: dict | None) -> None:
        self._mutar(id_tarjeta, "completada", resultado)

    def bloquear(self, id_tarjeta: str, motivo: str) -> None:
        self._doble._bloqueos[id_tarjeta] = motivo
        self._mutar(id_tarjeta, "bloqueada", None)

    def _mutar(self, id_tarjeta: str, estado: str, resultado: dict | None) -> None:
        for origen in (self._overlay, self._doble._tarjetas):
            for clave, tarjeta in origen.items():
                if tarjeta.id == id_tarjeta:
                    origen[clave] = tarjeta._replace(estado=estado, resultado=resultado)
                    return
        raise ValueError(f"tarjeta desconocida: {id_tarjeta}")


class DobleKanban:
    """El doble en memoria con la misma interfaz que el adaptador de H2. `creaciones`
    audita las tarjetas PERSISTIDAS: se anota al confirmar la transacción, así una
    caída dentro de la txn no deja cuenta ni tarjeta."""

    def __init__(self) -> None:
        self._tarjetas: dict = {}
        self._bloqueos: dict = {}
        self._vinculos = set()
        self._caidas = set()
        self._lock = threading.Lock()
        self._contador = 0
        self.creaciones: list = []
        self.creaciones_llamadas: list = []

    def caer_en(self, punto: PuntoDeCaida) -> None:
        self._caidas.add(punto)

    def quitar_caida(self, punto: PuntoDeCaida) -> None:
        self._caidas.discard(punto)

    def _disparar(self, punto: PuntoDeCaida) -> None:
        if punto in self._caidas:
            raise CaidaInyectada(punto)

    def _crear(self, clave: str, spec: SpecDeTarjeta) -> Tarjeta:
        self._contador += 1
        self.creaciones_llamadas.append(clave)
        return Tarjeta(f"t-{self._contador}", clave, "pendiente", None)

    @contextmanager
    def write_txn(self) -> Iterator[_EscrituraDoble]:
        self._disparar(PuntoDeCaida.ABRIR_TXN)
        with self._lock:
            overlay: dict = {}
            yield _EscrituraDoble(self, overlay)
            self._asentar(overlay)
        self._disparar(PuntoDeCaida.DESPUES_DE_TXN)

    def _asentar(self, overlay: dict) -> None:
        previas = set(self._tarjetas)
        self._tarjetas.update(overlay)
        self.creaciones.extend(clave for clave in overlay if clave not in previas)

    def leer_estado(self, clave: str) -> Tarjeta | None:
        return self._tarjetas.get(clave)

    def por_clave(self, clave: str) -> Tarjeta | None:
        return self.leer_estado(clave)

    def _avanzar(self, clave: str, estado: str, resultado: dict | None) -> None:
        tarjeta = self._tarjetas.get(clave)
        if tarjeta is None:
            raise ValueError(f"tarjeta desconocida: {clave}")
        self._tarjetas[clave] = tarjeta._replace(estado=estado, resultado=resultado)

    def crear_corriendo(self, clave: str) -> None:
        self._avanzar(clave, "corriendo", None)

    def crear_hecha(self, clave: str, resultado: dict) -> None:
        self._avanzar(clave, "completada", resultado)

    def crear_bloqueada(self, clave: str, motivo: str) -> None:
        self._bloqueos[clave] = motivo
        self._avanzar(clave, "bloqueada", None)
