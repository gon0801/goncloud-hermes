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
cada aviso CON origin_session_key primero lo reclama renombrándolo a
.encolado (fuera del glob *.json) y solo quien gana ese renombre encola
el evento: el gateway carga una copia del plugin por perfil, así que
varios vigilantes pueden mirar el mismo buzón en el mismo proceso.
Al arrancar, el vigilante re-encola los .encolado que
quedaron — la cola vive en memoria y un reinicio la vacía — y un
.encolado con más de 24 h pasa a .entregado. Es al-menos-una-vez: tras
un reinicio puede llegar un aviso repetido, nunca uno perdido. Los
avisos SIN origen los deja intactos: siguen por el despertar de hoy, que
a su vez ignora los que traen origen para no duplicar.

Forma del evento (B1, contra el gateway v0.22.0 de la PC): type
``watch_match`` con ``pattern``/``output``/``session_id``/``command``/
``session_key`` y las claves de ruta. NO ``completion``: el watcher del
gateway (_async_delegation_watcher → _drain_watch_notifications) drena
la cola con _drain_gateway_watch_events (gateway/run.py:3053-3073), que
SOLO conserva watch_match/watch_disabled/watch_overflow_*/heartbeat y
las "completion" las saca y las tira ("handled by the watcher task") —
ese task es el watcher de procesos de la sesión, no un despertar; el
aviso nunca llegaba a claw. watch_match sí: run.py:3023-3050
(_format_gateway_process_notification, rama watch_match) lo formatea y
_inject_watch_notification lo inyecta como turno interno, re-encolando
si la inyección falla. NO watch_disabled: diagnostic_process_event
(agent/notification_presentation.py:24-28) lo marca diagnóstico y la
presentación cambia. NO async_delegation: exige un registro durable
conocido que un aviso de cadena no tiene.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import threading
import time
import types
from pathlib import Path

# B3-3: este módulo también se importa como módulo de primer nivel
# (sys.path del plugin), y ese __name__ queda fuera del árbol
# hermes_plugins.* que Hermes sí escribe en sus .log. Nombre fijo.
logger = logging.getLogger("hermes_plugins.goncloud_team.avisos_sesion")

_PLUGIN = Path(__file__).resolve().parent
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

#: Cada cuántos segundos mira el buzón el hilo del gateway.
INTERVALO_SEGUNDOS = 3.0

#: Cuánto vive un .encolado antes de darse por entregado: el gateway tuvo
#: un día entero (y al menos un arranque, que lo re-encola) para inyectarlo.
ENCOLADO_MAX_SEGUNDOS = 24 * 3600

#: Campos de ruteo que _build_process_event_source lee del evento
#: (gateway/run_notifications.py, _WATCHER_ROUTE_FIELDS).
_CAMPOS_RUTA = ("platform", "chat_type", "chat_id", "thread_id", "user_id", "user_name")


def _centinela():
    """El estado compartido por TODAS las copias del módulo en el proceso.

    El gateway carga una copia del plugin por perfil (módulo distinto,
    globals distintos), así que el candado, el hilo y los encolados
    propios viven en un módulo centinela en sys.modules, común a todas.
    Se crea con setdefault: la primera copia que llega manda, y su
    candado de arranque queda guardado en el propio centinela.
    """
    existente = sys.modules.get("goncloud_avisos_vigilante")
    if existente is not None and hasattr(existente, "bloqueo"):
        for atributo, fabrica in (
            ("candado_arranque", threading.Lock),
            ("encolados_propios", set),
            ("reencolados", set),
            ("vigilantes", dict),
        ):
            if not hasattr(existente, atributo):
                setattr(existente, atributo, fabrica())
        if not hasattr(existente, "hilo"):
            existente.hilo = None
        return existente
    nuevo = types.ModuleType("goncloud_avisos_vigilante")
    nuevo.bloqueo = threading.Lock()
    nuevo.candado_arranque = threading.Lock()
    # Alias por si otra copia busca el candado con otro nombre usual.
    nuevo.lock = nuevo.bloqueo
    nuevo.hilo = None
    nuevo.encolados_propios = set()
    nuevo.reencolados = set()
    nuevo.vigilantes = {}
    return sys.modules.setdefault("goncloud_avisos_vigilante", nuevo)


_CENTINELA = _centinela()
_bloqueo = _CENTINELA.bloqueo
_hilo: threading.Thread | None = None
#: Nombres (.json) de los .encolado que ESTE proceso puso en la cola: son
#: los únicos cuyo drenaje puede certificarse mirando la cola (B2).
#: Es el set del centinela: compartido por todas las copias del módulo.
_encolados_propios: set = _CENTINELA.encolados_propios
#: (buzón, nombre) ya re-encolados por este proceso en este arranque.
_reencolados: set = _CENTINELA.reencolados


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


def _clave_para_log(session_key: str) -> str:
    """La session_key para el log sin el chat id (cr 4320005117)."""
    ruta = _ruta_desde_session_key(session_key)
    if not ruta:
        return "***" if str(session_key or "").strip() else ""
    return f"{ruta['platform']}:{ruta['chat_type']}:***"


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
        # B1: watch_match es el tipo que el watcher del gateway conserva
        # al drenar la cola y formatea en un turno interno; "completion"
        # se sacaba y se tiraba (ver docstring del módulo).
        "type": "watch_match",
        "pattern": f"cadena {aviso.get('outcome') or ''}".strip(),
        # Identidad estable del productor: el nombre del aviso ya es
        # <workflow>-<run>-<outcome>, único por parada de la cadena.
        "session_id": f"goncloud-{Path(nombre).stem}" if nombre else "goncloud-aviso",
        "session_key": session_key,
        "command": f"goncloud_local_run {aviso.get('workflow_id') or ''}".strip(),
        "output": f"{linea} revisa con goncloud_local_status y sigue.",
    }
    if nombre:
        # B2: el id con el que este proceso reconoce SU evento en la cola
        # (el nombre .json, estable tras el renombre a .encolado).
        evento["goncloud_aviso"] = Path(nombre).with_suffix(".json").name
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
    return evento


def _renombrar(ruta: Path, sufijo: str) -> None:
    """El rename del buzón (os.replace, como entregar_aviso del despertar)."""
    try:
        os.replace(ruta, ruta.with_suffix(sufijo))
    except OSError:
        pass  # otra pasada ya lo movió


def _reclamar(ruta: Path) -> Path | None:
    """Reclama el aviso renombrándolo a .encolado ANTES de encolar.

    Solo el hilo cuyo os.replace funciona es dueño del aviso; los demás
    reciben None y se saltan el archivo. Si el proceso muere entre el
    reclamo y el put, el .encolado se re-encola al arrancar.
    """
    destino = ruta.with_suffix(".encolado")
    try:
        os.replace(ruta, destino)
    except (FileNotFoundError, OSError):
        return None  # otro vigilante ya lo tomó
    return destino


def _soltar_reclamo(reclamado: Path) -> None:
    """Devuelve un reclamo a .json: no era nuestro o no se pudo encolar."""
    try:
        os.replace(reclamado, reclamado.with_suffix(".json"))
    except OSError:
        pass


def entregar_avisos(buzon, cola) -> int:
    """Una pasada del vigilante: reclama cada aviso y solo el dueño encola.

    Devuelve cuántos eventos encoló. El reclamo (.json → .encolado) va
    ANTES del put (F2 y duplicado en vivo 2026-10-19): con varias copias
    del plugin en el mismo proceso, encolar antes de renombrar dejaba
    que dos hilos encolaran el mismo archivo. .entregado aquí mentiría
    — el evento solo vive en la cola en memoria; si el proceso muere
    tras el reclamo, el próximo arranque re-encola el .encolado.
    """
    despertar = _despertar()
    encolados = 0
    with _bloqueo:
        for ruta, aviso in despertar.avisos_de_cadenas(Path(buzon)):
            if not str(aviso.get("origin_session_key") or "").strip():
                continue  # sin origen: lo deja intacto para el despertar
            reclamado = _reclamar(ruta)
            if reclamado is None:
                continue  # otro vigilante ya lo tomó
            try:
                evento = construir_evento(aviso, nombre=ruta.name)
                if evento is None:
                    _soltar_reclamo(reclamado)
                    continue
                cola.put(evento)
            except Exception:
                # Sin esto el .encolado quedaba sin reintento hasta el
                # proximo arranque: vuelve a .json y la siguiente pasada
                # (3 s) lo recoge.
                logger.exception("aviso %s no encolado; vuelve a .json", ruta.name)
                _soltar_reclamo(reclamado)
                continue
            if evento.get("goncloud_aviso"):
                _encolados_propios.add(evento["goncloud_aviso"])
            logger.info(
                "aviso encolado %s -> %s",
                ruta.name,
                _clave_para_log(evento.get("session_key", "")),
            )
            encolados += 1
    return encolados


def reencolar_encolados(buzon, cola) -> int:
    """El arranque del vigilante: los .encolado vuelven a la cola, una vez.

    La completion_queue es una queue.Queue en memoria (process_registry):
    un reinicio del gateway la vacía y los eventos encolados se pierden.
    Los archivos .encolado son la copia durable: cada arranque los
    re-encola y quedan .encolado, a la espera de su hora de .entregado.
    """
    buzon = Path(buzon)
    if not buzon.is_dir():
        return 0
    encolados = 0
    with _bloqueo:
        for ruta in sorted(buzon.glob("*.encolado")):
            try:
                aviso = json.loads(ruta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(aviso, dict):
                continue
            nombre_json = ruta.with_suffix(".json").name
            clave = (str(buzon), nombre_json)
            if nombre_json in _encolados_propios and clave in _reencolados:
                continue  # este proceso ya lo re-encoló: una sola vez
            evento = construir_evento(aviso, nombre=ruta.name)
            if evento is None:
                continue
            cola.put(evento)
            if evento.get("goncloud_aviso"):
                _encolados_propios.add(evento["goncloud_aviso"])
            _reencolados.add(clave)
            logger.info(
                "aviso re-encolado %s -> %s",
                nombre_json,
                _clave_para_log(evento.get("session_key", "")),
            )
            encolados += 1
    return encolados


def _ids_en_cola(cola):
    """Los goncloud_aviso aún en la cola, o None si no se puede saber.

    Una queue.Queue real expone .queue bajo .mutex; una cola que no los
    expone se juzga solo por empty(): vacía = todo drenado, no vacía sin
    detalle = None (no se cierra nada, manda la red de 24 h).
    """
    try:
        with cola.mutex:
            return {
                evento.get("goncloud_aviso")
                for evento in cola.queue
                if isinstance(evento, dict) and evento.get("goncloud_aviso")
            }
    except AttributeError:
        pass
    try:
        return set() if cola.empty() else None
    except Exception:
        return None


def cerrar_encolados_drenados(buzon, cola) -> int:
    """Los .encolado que ESTE proceso encoló y ya no están en cola: .entregado.

    B2: con solo la purga de 24 h, cada reinicio del gateway re-encolaba
    también los avisos YA inyectados y claw recibía otra vez el "terminó"
    de cadenas viejas. Si el id ya no está en la cola, el watcher del
    gateway lo drenó (lo inyectó o lo re-encola él mismo si falla): el
    archivo puede cerrarse. Los que este proceso no encoló no se tocan —
    su drenaje no es certificable desde aquí.
    """
    ids = _ids_en_cola(cola)
    if ids is None:
        return 0
    buzon = Path(buzon)
    if not buzon.is_dir():
        return 0
    cerrados = 0
    with _bloqueo:
        for ruta in sorted(buzon.glob("*.encolado")):
            nombre = ruta.with_suffix(".json").name
            if nombre in _encolados_propios and nombre not in ids:
                _renombrar(ruta, ".entregado")
                _encolados_propios.discard(nombre)
                cerrados += 1
    return cerrados


def purgar_encolados(buzon, *, ahora=None, max_segundos=ENCOLADO_MAX_SEGUNDOS) -> int:
    """Los .encolado viejos pasan a .entregado: ya tuvieron su día.

    La edad se mide por el mtime del archivo. Sin esto, un gateway que
    nunca logra inyectar (sesión muerta) re-encolaría el mismo aviso en
    cada arranque para siempre.
    """
    buzon = Path(buzon)
    if not buzon.is_dir():
        return 0
    ahora = time.time() if ahora is None else ahora
    cerrados = 0
    for ruta in sorted(buzon.glob("*.encolado")):
        try:
            edad = ahora - ruta.stat().st_mtime
        except OSError:
            continue
        if edad > max_segundos:
            _renombrar(ruta, ".entregado")
            _encolados_propios.discard(ruta.with_suffix(".json").name)
            cerrados += 1
    return cerrados


def es_proceso_gateway() -> bool:
    """Si ESTE proceso es el gateway vivo de Hermes (F1).

    La misma prueba de tools/process_registry._is_supervised_gateway_process:
    la marca _HERMES_GATEWAY la hereda cualquier descendiente (e importar
    gateway.run la pone), así que además se exige la propiedad del pidfile
    del gateway. Sin esa prueba, un CLI, un cron worker o las pruebas —
    que también cargan el plugin para registrar tools — arrancarían un
    vigilante que encolaría en una cola que nadie drena.
    """
    try:
        from tools.process_registry import _is_supervised_gateway_process

        return bool(_is_supervised_gateway_process())
    except Exception:
        pass
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return False
    try:
        from gateway.status import get_running_pid

        return get_running_pid(cleanup_stale=False) == os.getpid()
    except Exception:
        return False


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
    buzon=None,
    *,
    cola=None,
    intervalo=INTERVALO_SEGUNDOS,
    detener=None,
    exigir_gateway=True,
):
    """Arranca (una sola vez por proceso) el hilo que drena el buzón.

    SOLO en el proceso del gateway (F1): con exigir_gateway (el default,
    el que usa register) un CLI, un cron worker o las pruebas devuelven
    None sin arrancar nada — encolarían en una cola que nadie drena. Los
    dobles de prueba pasan exigir_gateway=False con su cola explícita.
    Sin buzón resoluble o sin completion_queue tampoco arranca: los
    avisos quedan en el buzón para la próxima vida del gateway.
    """
    global _hilo
    if exigir_gateway and not es_proceso_gateway():
        return None
    # Un vigilante por (buzón, cola), no por copia del módulo: el registro
    # vive en el centinela común y el candado de arranque serializa a las
    # copias. F3: adoptar el hilo vivo solo si vigila ESTE buzón en ESTA
    # cola; otro buzón arranca el suyo, no queda sin vigilar.
    with _CENTINELA.candado_arranque:
        if cola is None:
            cola = _cola_del_gateway()
        if cola is None:
            return None
        if buzon is None:
            buzon = _buzon_del_hogar()
        if buzon is None:
            return None
        buzon = Path(buzon)
        vigilantes = getattr(_CENTINELA, "vigilantes", None)
        if vigilantes is None:
            vigilantes = _CENTINELA.vigilantes = {}
        clave = (str(buzon), id(cola))
        vivo = vigilantes.get(clave)
        if vivo is not None and vivo.is_alive():
            _hilo = vivo
            return _hilo
        # Un hilo suelto sin clave registrada (centinela viejo) no se
        # adopta a ciegas: no hay forma de probar qué buzón vigila.
        alto = detener if detener is not None else threading.Event()

        def vigilar():
            try:
                reencolar_encolados(buzon, cola)
            except Exception:
                pass  # el buzón es durable: la vuelta normal lo recoge igual
            while not alto.is_set():
                try:
                    entregar_avisos(buzon, cola)
                    cerrar_encolados_drenados(buzon, cola)
                    purgar_encolados(buzon)
                except Exception:
                    pass  # el buzón es durable: la próxima vuelta reintenta
                alto.wait(intervalo)

        _hilo = threading.Thread(
            target=vigilar, name="goncloud-avisos-sesion", daemon=True
        )
        _CENTINELA.hilo = _hilo
        vigilantes[clave] = _hilo
        _hilo.start()
        logger.info("vigilante de avisos arrancado pid=%s buzon=%s", os.getpid(), buzon)
        return _hilo
