"""El despertar del dueño: el cron despertar-raices (SUA-07 H6b).

El instalador deja el script en <home>/scripts/desperar-raices.py y registra
el trabajo sin agente en el perfil default con entrega al chat del director:

  hermes -p default cron create "every 1m" --name despertar-raices --script
      "despertar-raices.py" --no-agent --deliver bot-chat:claw

Con --deliver bot-chat:claw el stdout de la corrida entra como un turno en
el chat "Bot Chat" de claw y stdout vacío es corrida silenciosa
(scheduler_delivery._deliver_to_bot_chat de Hermes 0.21.5). En cada vuelta:
para cada raíz viva (control active, no terminal) con necesidades no vacías,
imprime un bloque corto si el needs_digest cambió desde el último aviso o si
pasó RENOTICE_MS desde él; el asiento (avisos_de_necesisidades) ocurre en la
misma transacción del store que decide imprimir. El texto lleva el rootId,
las necesidades (kind, unitId, reason/criteria, attempt) y la línea que le
dice a claw qué hacer: nada de briefs ni secretos. Cero conexiones abiertas
al salir.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))


def _linea_de(necesidad) -> str:
    if necesidad.get("kind") == "verify":
        criterios = ",".join(str(c) for c in necesidad.get("criteria") or ())
        return (
            f"- verify unidad={necesidad.get('unitId')}"
            f" intento={necesidad.get('attempt')} criterios={criterios}"
        )
    return (
        f"- assignment unidad={necesidad.get('unitId')} razon={necesidad.get('reason')}"
    )


def bloque_de_aviso(root_id: str, necesidades: list) -> str:
    """El texto del aviso: ids y razones, nunca contenido de briefs."""
    lineas = [f"raiz {root_id} necesita:"]
    lineas.extend(_linea_de(n) for n in necesidades)
    lineas.append(
        "Usa la skill raices y contesta con goncloud_root_read,"
        " goncloud_root_instruction_put y goncloud_root_reconcile."
    )
    return "\n".join(lineas)


def correr(home=None, escribir=print, reloj=None) -> int:
    hogar = importlib.import_module("raices.hogar")
    store = importlib.import_module("raices.store")
    base = hogar.raiz_del_hogar(home)
    tienda = (
        store.Store(base / "sistema-agentes.db", reloj=reloj)
        if reloj is not None
        else store.Store(base / "sistema-agentes.db")
    )
    try:
        pendientes = tienda.avisos_de_necesidades()
    finally:
        tienda.cerrar()
    for root_id, necesidades in pendientes:
        escribir(bloque_de_aviso(root_id, necesidades))
    return 0


def main() -> int:
    try:
        return correr()
    except Exception as exc:
        print(f"ERROR despertar raices: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
