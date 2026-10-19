"""Avisos a la sesión que lanzó la cadena (encargo H-avisos-a-la-sesion).

El problema medido (19-oct): el cron despertar-raices entregaba el aviso
con --deliver bot-chat:claw, que inyecta en la sesión "Bot Chat" oculta de
escritorio, NO en la conversación (p. ej. el DM de Telegram) donde claw
lanzó la cadena. La claw de esa conversación nunca se enteraba.

El mecanismo nativo de Hermes ya resuelve esto para trabajos en segundo
plano: el productor pone un evento en
``tools.process_registry.process_registry.completion_queue`` con la
``session_key`` de la sesión que lo lanzó, y el gateway lo inyecta en ESA
sesión como evento interno (``_inject_watch_notification`` /
``admit_internal_event`` en gateway/run_notifications.py).

Aquí, dentro del proceso del gateway y SIN modelo, un hilo revisa cada
pocos segundos el buzón durable <raíz>/goncloud-local/avisos/*.json. Por
cada aviso CON origin_session_key encola el evento y lo marca .entregado
(el mismo rename del despertar). Los avisos SIN origen los deja intactos:
siguen por el despertar de hoy, que a su vez ignora los que traen origen
para no duplicar.

Forma del evento: la notificación de proceso de fondo de
process_registry (``_publish_finished`` pone
``{"type": "completion", "session_id", "session_key", "command",
"exit_code", "output", "started_at", ...}`` en completion_queue), NO
``async_delegation``: esa forma exige un registro durable conocido
(claim/completion en tools.async_delegation) y un goal/results que un
aviso de cadena no tiene; la de proceso es autocontenida y el gateway la
formatea e inyecta sin buscar ningún registro propio.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

#: Cada cuántos segundos mira el buzón el hilo del gateway.
INTERVALO_SEGUNDOS = 3.0

#: Campos de ruteo que _build_process_event_source lee del evento
#: (gateway/run_notifications.py, _WATCHER_ROUTE_FIELDS).
_CAMPOS_RUTA = ("platform", "chat_type", "chat_id", "thread_id", "user_id", "user_name")

_bloqueo = threading.Lock()
_hilo: threading.Thread | None = None


def _despertar():
    return importlib.import_module("raices.despertar")


def _ruta_desde_session_key(session_key: str) -> dict:
    """platform/chat_type/chat_id/thread_id desde agent:<perfil>:<plataforma>:<tipo>:<chat>[:<hilo>].

    El gateway también lo deriva (_parse_session_key); llevarlo explícito
    deja el evento autocontenido, como el watcher de procesos de fondo.
    """
    partes = str(session_key or "").split(":")
    if len(partes) < 5 or partes[0] != "agent":
        return {}
    ruta = {
        "platform": partes[2],
        "chat_type": partes[3],
        "chat_id": partes[4],
    }
    if len(partes) > 5 and partes[5]:
        ruta["thread_id"] = partes[5]
    return ruta


def construir_evento(aviso: dict, *, nombre: str = "") -> dict | None:
    """El evento de completion_queue para un aviso, o None si no es suyo.

    None cuando el aviso no trae origin_session_key (lo drena el
    despertar) o cuando ni el despertar sabría contarlo (linea_de_aviso
    None): ese archivo no se marca ni se encola, igual que hoy.
    """
    session_key = str(aviso.get("origin_session_key") or "").strip()
    if not session_key:
        return None
    despertar = _despertar()
    linea = despertar.linea_de_aviso(aviso)
    if linea is None:
        return None
    routing = aviso.get("origin_routing")
    routing = routing if isinstance(routing, dict) else {}
    evento = {
        "type": "completion",
        # Identidad estable del productor: el nombre del aviso ya es
        # <workflow>-<run>-<outcome>, único por parada de la cadena.
        "session_id": f"goncloud-{Path(nombre).stem}" if nombre else "goncloud-aviso",
        "session_key": session_key,
        "command": f"goncloud_local_run {aviso.get('workflow_id') or ''}".strip(),
        "exit_code": 0 if aviso.get("outcome") == "delivered" else 1,
        "completion_reason": "exited",
        "output": f"{linea} revisa con goncloud_local_status y sigue.",
    }
    # Las claves de ruteo capturadas al lanzar mandan; la session_key las
    # respalda cuando la captura no las trajo.
    for campo in _CAMPOS_RUTA:
        valor = str(routing.get(campo) or "").strip()
        if valor:
            evento[campo] = valor
    for campo, valor in _ruta_desde_session_key(session_key).items():
        evento.setdefault(campo, valor)
    for campo in ("scope_id", "origin_session_id"):
        valor = str(routing.get(campo) or "").strip()
        if valor:
            evento[campo] = valor
    # started_at ancla la identidad de deduplicación del gateway
    # (_completion_delivery_identity pide session_id + started_at).
    try:
        evento["started_at"] = datetime.fromisoformat(
            str(aviso.get("fecha") or "").replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        evento["started_at"] = time.time()
    return evento


def entregar_avisos(buzon, cola) -> int:
    """Una pasada del vigilante: encola cada aviso con origen y lo marca.

    Devuelve cuántos eventos encoló. Marcar DESPUÉS de encolar, con el
    mismo rename del despertar: si el gateway se cae entre medias, la
    próxima vida lo reencola (a-lo-más-una-vez extra del propio gateway),
    pero jamás se pierde. El bloqueo serializa las pasadas de este
    proceso para que dos hilos nunca encolen el mismo archivo.
    """
    despertar = _despertar()
    entregados = 0
    with _bloqueo:
        for ruta, aviso in despertar.avisos_de_cadenas(Path(buzon)):
            evento = construir_evento(aviso, nombre=ruta.name)
            if evento is None:
                continue
            cola.put(evento)
            despertar.entregar_aviso(ruta)
            entregados += 1
    return entregados


def _cola_del_gateway():
    """La completion_queue real del gateway, o None fuera de él."""
    try:
        from tools.process_registry import process_registry

        return process_registry.completion_queue
    except Exception:
        return None


def _buzon_del_hogar() -> Path | None:
    try:
        hogar = importlib.import_module("raices.hogar")
        return hogar.raiz_del_hogar() / "goncloud-local" / "avisos"
    except Exception:
        return None


def iniciar_vigilante(
    buzon=None, *, cola=None, intervalo=INTERVALO_SEGUNDOS, detener=None
):
    """Arranca (una sola vez por proceso) el hilo que drena el buzón.

    Sin buzón resoluble o sin completion_queue (el plugin cargó fuera del
    gateway: un CLI, las pruebas) no arranca nada y devuelve None: los
    avisos quedan en el buzón para la próxima vida del gateway.
    """
    global _hilo
    if _hilo is not None and _hilo.is_alive():
        return _hilo
    if cola is None:
        cola = _cola_del_gateway()
    if cola is None:
        return None
    if buzon is None:
        buzon = _buzon_del_hogar()
    if buzon is None:
        return None
    buzon = Path(buzon)
    alto = detener if detener is not None else threading.Event()

    def vigilar():
        while not alto.is_set():
            try:
                entregar_avisos(buzon, cola)
            except Exception:
                pass  # el buzón es durable: la próxima vuelta reintenta
            alto.wait(intervalo)

    _hilo = threading.Thread(target=vigilar, name="goncloud-avisos-sesion", daemon=True)
    _hilo.start()
    return _hilo
