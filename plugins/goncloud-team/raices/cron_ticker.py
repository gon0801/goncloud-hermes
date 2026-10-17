"""Una vuelta del ticker de raices como script de cron de Hermes (SUA-07 H2).

El coordinador la registra en la PC (H5) como trabajo sin agente del perfil
claw; el cron de Hermes resuelve --script bajo profiles/claw, donde
configure_team copia el plugin:

  hermes -p claw cron create "* * * * *" --name ticker-raices --script
      profiles/claw/plugins/goncloud-team/raices/cron_ticker.py --no-agent

Convencion de los scripts no_agent (README): nada en la salida si la vuelta
sale bien, una linea `ERROR ticker raices: ...` y salida 1 si fallo. Con la
ruta de raices cerrada no corre y sale 0: no es un fallo, la puerta de
capacidades (H4) aun no abre. El store es %HERMES_HOME%/sistema-agentes.db y
el kanban, HERMES_KANBAN_DB o %HERMES_HOME%/kanban.db.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))


def ruta_de_raices_abierta():
    """El hueco de capacidades.py (H4): hoy la ruta de raices esta siempre
    abierta. Devuelve (abierta, causa)."""
    return True, None


def ruta_del_kanban(home):
    elegida = os.environ.get("HERMES_KANBAN_DB")
    return Path(elegida) if elegida else Path(home) / "kanban.db"


def correr(home=None, ruta_abierta=None):
    base_texto = str(home or os.environ.get("HERMES_HOME", "")).strip()
    if not base_texto:
        raise RuntimeError("HERMES_HOME no esta definido; sin el no hay raices")
    base = Path(base_texto)
    abierta, causa = (ruta_abierta or ruta_de_raices_abierta)()
    if not abierta:
        print(f"ticker-raices: ruta de raices cerrada ({causa})")
        return 0
    store = importlib.import_module("raices.store")
    ticker = importlib.import_module("raices.ticker")
    kanban = importlib.import_module("raices.kanban")
    tienda = store.Store(base / "sistema-agentes.db")
    try:
        puerto = kanban.KanbanHermes(ruta_del_kanban(base))
        try:
            ticker.vuelta(tienda, puerto)
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
