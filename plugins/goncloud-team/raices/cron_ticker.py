"""Una vuelta del ticker de raices como script de cron de Hermes (SUA-07 H2).

El coordinador la registra en la PC (H5) como trabajo sin agente del perfil
claw; el cron de Hermes resuelve --script bajo profiles/claw, donde
configure_team copia el plugin:

  hermes -p claw cron create "* * * * *" --name ticker-raices --script
      profiles/claw/plugins/goncloud-team/raices/cron_ticker.py --no-agent

Convencion de los scripts no_agent (README): nada en la salida si la vuelta
sale bien, una linea `ERROR ticker raices: ...` y salida 1 si fallo. Con la
ruta de raices cerrada (H4a) el cron avisa CAPACIDAD_INCIERTA una vez por
cambio de causa y sale 0: no es un fallo. Si la causa es un par (incierto o
sin certificar) la vuelta corre igual pero cerrada: bloquea y concilia sin
decidir ni despachar; si falta una capacidad ni siquiera abre el store. El
store es %HERMES_HOME%/sistema-agentes.db y el kanban, HERMES_KANBAN_DB o
%HERMES_HOME%/kanban.db.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))


def ruta_de_raices_abierta(home=None):
    capacidades = importlib.import_module("raices.capacidades")
    return capacidades.abrir_ruta(home=home)


def _aviso_por_consola(causa):
    def enviar(_texto):
        print(f"ticker-raices: CAPACIDAD_INCIERTA ({causa})")
        return 1

    return enviar


def leer_permisos(home):
    """Los permisos de H para el gate de arranque (I07). Viven como datos en
    %HERMES_HOME%/raices/permisos.json; sin archivo legible devuelven None y
    el ticker niega todo (falla cerrada)."""
    try:
        texto = (Path(home) / "raices" / "permisos.json").read_text(encoding="utf-8")
        datos = json.loads(texto)
    except (OSError, ValueError):
        return None
    return datos if isinstance(datos, dict) else None


def ruta_del_kanban(home):
    elegida = os.environ.get("HERMES_KANBAN_DB")
    return Path(elegida) if elegida else Path(home) / "kanban.db"


def _vuelta_cerrada(base, causa) -> int:
    proyeccion = importlib.import_module("raices.proyeccion")
    proyeccion.avisar_capacidad_incierta(
        base / "raices" / "capacidad-incierta.json",
        causa,
        datetime.now(timezone.utc),
        _aviso_por_consola(causa),
    )
    if causa.startswith("capacidad-faltante"):
        # Sin write_txn del kanban no hay bloqueo ni conciliacion posible:
        # salir temprano ni siquiera abre el store.
        print(f"ticker-raices: ruta de raices cerrada ({causa})")
        return 0
    store = importlib.import_module("raices.store")
    ticker = importlib.import_module("raices.ticker")
    kanban = importlib.import_module("raices.kanban")
    tienda = store.Store(base / "sistema-agentes.db")
    try:
        puerto = kanban.KanbanHermes(ruta_del_kanban(base))
        try:
            ticker.vuelta(tienda, puerto, ruta_abierta=False)
        finally:
            puerto.cerrar()
    finally:
        tienda.cerrar()
    print(f"ticker-raices: ruta de raices cerrada ({causa})")
    return 0


def correr(home=None, ruta_abierta=None, lector_de_permisos=None):
    base_texto = str(home or os.environ.get("HERMES_HOME", "")).strip()
    if not base_texto:
        raise RuntimeError("HERMES_HOME no esta definido; sin el no hay raices")
    base = Path(base_texto)
    puerta = ruta_abierta or (lambda: ruta_de_raices_abierta(base))
    abierta, causa = puerta()
    if not abierta:
        return _vuelta_cerrada(base, causa)
    store = importlib.import_module("raices.store")
    ticker = importlib.import_module("raices.ticker")
    kanban = importlib.import_module("raices.kanban")
    permisos = (lector_de_permisos or leer_permisos)(base)
    tienda = store.Store(base / "sistema-agentes.db")
    try:
        puerto = kanban.KanbanHermes(ruta_del_kanban(base))
        try:
            ticker.vuelta(tienda, puerto, permisos_h=permisos)
        finally:
            puerto.cerrar()
    finally:
        tienda.cerrar()
    return 0


def main():
    try:
        return correr()
    except Exception as exc:
        print(f"ERROR ticker raices: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
