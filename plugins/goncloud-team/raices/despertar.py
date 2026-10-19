"""El despertar del dueño: el cron despertar-raices (SUA-07 H6b).

El instalador deja el script en <home>/scripts/despertar-raices.py y registra
el trabajo sin agente en el perfil default con entrega al chat del director:

  hermes -p default cron create "every 1m" --name despertar-raices --script
      "despertar-raices.py" --no-agent --deliver bot-chat:claw

Con --deliver bot-chat:claw el stdout de la corrida entra como un turno en
el chat "Bot Chat" de claw y stdout vacío es corrida silenciosa
(scheduler_delivery._deliver_to_bot_chat de Hermes 0.21.5). En cada vuelta:
para cada raíz viva (control active, no terminal) con necesidades no vacías,
imprime un bloque corto si el needs_digest cambió desde el último aviso o si
pasó RENOTICE_MS desde él; el asiento (avisos_de_necesidades) ocurre en la
misma transacción del store que decide imprimir. El texto lleva el rootId,
las necesidades (kind, unitId, reason/criteria, attempt) y la línea que le
dice a claw qué hacer: nada de briefs ni secretos. Cero conexiones abiertas
al salir.

Encargo H-cadenas-avisan: antes de las raíces drena también el buzón
durable de las cadenas locales. Cada corrida que el driver de
goncloud_local_run deja parada (delivered, blocked, waiting, paused, error)
o deja corriendo sin terminar (running: el cierre por task_failure, live
review F3) escribió un evento en <raiz>/goncloud-local/avisos/
<workflow_id>-<run_id>-<outcome>.json, en la raíz del home de Hermes para
que escritor (el perfil que lanzó la cadena) y lector (este cron, en el
perfil default) se encuentren. Por cada aviso no entregado imprime UNA
línea corta en español y lo renombra a .entregado para no repetirlo; lo
ilegible o desconocido no se imprime ni se marca. Sin avisos, stdout vacío:
cero tokens, claw no se despierta.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

#: La línea es un titular, no un reporte: el motivo queda acotado.
AVISO_MOTIVO_CHARS = 160


def _redacta(texto) -> str:
    """Redacción en el borde de lectura (cr 4312376359): el aviso pudo
    escribirlo otra mano, así que el repo se redacta otra vez aquí, y todo
    blanco de control o relleno de redacción queda como un solo espacio.
    """
    redaccion = importlib.import_module("subscriptions.redaction")
    return " ".join(redaccion.redact_text(str(texto or "")).split())


def _motivo_corto(motivo) -> str:
    """El motivo acotado y sin secretos: misma redacción que el buzón.

    Una sola línea prometida (cr 4312376374): el resumen de una etapa
    bloqueada puede traer salto de línea (parse_terminal_envelope no exige
    una línea), y ningún blanco de control sobrevive al titular.
    """
    texto = _redacta(motivo).rstrip(".")
    if len(texto) > AVISO_MOTIVO_CHARS:
        texto = texto[: AVISO_MOTIVO_CHARS - 1].rstrip() + "…"
    return texto


def linea_de_aviso(aviso: dict) -> str | None:
    """La línea corta para claw; None si el aviso no se sabe contar.

    Los tres formatos del encargo: entregada, se trabó en <etapa>, falló.
    Paused, waiting y running tienen su propia línea: la cadena paró igual
    o quedó corriendo sin terminar, y claw debe saberlo, con el mismo techo
    de motivo.
    """
    cadena = str(aviso.get("workflow_id") or "").strip()
    if not cadena:
        return None
    repo = _redacta(aviso.get("repo"))
    lugar = f"{cadena} ({repo})" if repo else cadena
    motivo = _motivo_corto(aviso.get("motivo"))
    detalle = f": {motivo}" if motivo else ""
    outcome = aviso.get("outcome")
    if outcome == "delivered":
        # H-entrega-pr: el detalle trae el link del PR o el motivo de por qué
        # no se abrió — sin él, claw se enteraba de la entrega pero no del PR.
        return f"Cadena {lugar} entregada{detalle}."
    if outcome == "blocked":
        etapa = str(aviso.get("etapa") or "").strip()
        traba = f"se trabó en {etapa}" if etapa else "se trabó"
        return f"Cadena {lugar} {traba}{detalle}."
    if outcome == "error":
        return f"Cadena {lugar} falló{detalle}."
    if outcome == "paused":
        return f"Cadena {lugar} quedó en pausa{detalle}."
    if outcome == "waiting":
        return f"Cadena {lugar} quedó esperando{detalle}."
    if outcome == "running":
        # El cierre por task_failure (live review F3): el proceso del driver
        # terminó con la etapa viva y nada más lo anuncia.
        return f"Cadena {lugar} quedó corriendo sin terminar{detalle}."
    return None


def avisos_de_cadenas(buzon: Path) -> list:
    """Los avisos no entregados del buzón, en orden de nombre.

    Un archivo ilegible se queda para otra vuelta y jamás se imprime: el
    stdout vacío es lo único que mantiene a claw dormido.
    """
    if not buzon.is_dir():
        return []
    avisos = []
    for ruta in sorted(buzon.glob("*.json")):
        try:
            aviso = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(aviso, dict):
            avisos.append((ruta, aviso))
    return avisos


def entregar_aviso(ruta: Path) -> None:
    """Marca el aviso entregado: renombrado, la próxima vuelta no lo lee.

    os.replace y no rename: si ya queda un .entregado de una vuelta
    anterior (lead r3: el evento nuevo del mismo arranque roto no frena),
    hay que pisarlo — con rename en Windows el nombre ocupado falla, el
    aviso nuevo queda sin marcar y la próxima vuelta lo repite.
    """
    try:
        os.replace(ruta, ruta.with_suffix(".entregado"))
    except OSError:
        pass  # otra vuelta ya lo marcó


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
    # El buzón de cadenas va primero: no abre el store ni necesita la ruta
    # de raíces viva, y una cadena que terminó se avisa aunque el store esté
    # en crisis. Imprimir antes de marcar: una caída a media vuelta repite
    # una línea antes que perder un aviso.
    for ruta, aviso in avisos_de_cadenas(base / "goncloud-local" / "avisos"):
        linea = linea_de_aviso(aviso)
        if linea:
            escribir(linea)
            entregar_aviso(ruta)
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
