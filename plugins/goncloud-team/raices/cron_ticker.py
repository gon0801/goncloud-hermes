"""Una vuelta del ticker de raices como script de cron de Hermes (SUA-07 H2).

El instalador (H4b3) deja el script en <home>/scripts/ticker-raices.py y
registra el trabajo sin agente en el perfil default (la raiz del home, sin -p):

  hermes cron create "every 1m" --name ticker-raices --script
      "ticker-raices.py" --no-agent --deliver local

Va en default porque el scheduler le pone HERMES_HOME = home del perfil al
script y gateway_state.json (la puerta de H4a) vive en la raiz.

--script se resuelve contra <home del perfil>/scripts (hermes_cli/cron.py:604-618)
y el argumento es el nombre pelado.

Convencion de los scripts no_agent (README): nada en la salida si la vuelta
sale bien, una linea `ERROR ticker raices: ...` y salida 1 si fallo. Con la
ruta de raices cerrada (H4a) el cron avisa CAPACIDAD_INCIERTA una vez por
cambio de causa y sale 0: no es un fallo. Si la causa es un par (incierto o
sin certificar) la vuelta corre igual pero cerrada: bloquea y concilia sin
decidir ni despachar y sin correr la pasada del emisor; si falta una
capacidad ni siquiera abre el store. El store vive en la RAIZ del home de
Hermes (raices/hogar.py la resuelve: el home de un perfil sube a su raiz) y
el kanban, HERMES_KANBAN_DB o <raiz>/kanban.db.

Tras las vueltas del ticker va la pasada del emisor (SUA-07 H3, §3.5/§3.6):
proyecta cada raiz en <raiz>/raices/proyecciones y avisa las excepciones por
el Telegram del perfil claw, desde el MISMO estado. Sus errores van a stderr
y no tumban las vueltas ni la salida del cron. Con la puerta cerrada la
pasada no corre: el aviso CAPACIDAD_INCIERTA es el unico mensaje del estado
cerrado.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

# Los mismos parametros de la proyeccion que usan las pruebas de proyeccion
# y avisos (5 minutos): freshness.staleAfterMs y el nextCheckAt de cada fila
# caminan con el intervalo del cron, no con su cadencia de un minuto.
STALE_DESPUES_MS = 300_000
INTERVALO_MS = 300_000


def ruta_de_raices_abierta(home=None):
    capacidades = importlib.import_module("raices.capacidades")
    return capacidades.abrir_ruta(home=home)


def _aviso_por_consola(causa):
    def enviar(_texto):
        # stderr y no stdout: el stdout del no_agent se entrega como salida
        # del agente; el aviso de la puerta cerrada no es salida de agente.
        print(f"ticker-raices: CAPACIDAD_INCIERTA ({causa})", file=sys.stderr)
        return 1

    return enviar


def ruta_del_kanban(home):
    elegida = os.environ.get("HERMES_KANBAN_DB")
    return Path(elegida) if elegida else Path(home) / "kanban.db"


def _vuelta_cerrada(base, causa) -> int:
    """La vuelta con la puerta cerrada (H4a): avisa CAPACIDAD_INCIERTA una vez
    por cambio de causa y, salvo capacidad-faltante (sin write_txn no hay
    bloqueo ni conciliacion posibles y ni siquiera abre el store), corre la
    vuelta cerrada: bloqueos y conciliacion, sin decidir, despachar ni
    levantar el gate de arranque. La pasada del emisor no corre: el aviso
    CAPACIDAD_INCIERTA es el unico mensaje del estado cerrado."""
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
        print(f"ticker-raices: ruta de raices cerrada ({causa})", file=sys.stderr)
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
    print(f"ticker-raices: ruta de raices cerrada ({causa})", file=sys.stderr)
    return 0


def emisor_de_telegram(base, resolvedor=None, corredor=None):
    """El emisor de produccion del aviso de excepciones (§3.5): `hermes send
    --to telegram --json` con el cuerpo por stdin y el HERMES_HOME del perfil
    claw (el home con el Telegram del dueno, home_channel). Devuelve el
    message_id entero, o None si falla: el checkpoint del aviso (noticed_at)
    solo avanza con un id confirmado, asi que None se traduce en "no se
    confirmo" y la pasada siguiente reintenta. El ejecutable se resuelve con
    shutil.which: en Windows el CLI es hermes.CMD y CreateProcess sin shell
    no resuelve el nombre pelado (como capacidades._correr_hermes_version,
    H4a).

    El payload lo fija la fuente de Hermes (medido en la PC, hermes-agent):
    send_message_tool imprime _success("telegram", chat_id, warnings,
    message_id=str(last_msg.message_id)) con exit 0 si success o skipped sin
    error, 1 si error (tools/send_message_senders.py:305). Solo eso cuenta
    como confirmado: success is True, sin "error", y un message_id que sea
    entero (no bool) o texto de digitos ASCII, mayor que cero. Cualquier otra
    cosa — incluido {"skipped": true} — es None."""
    resolvedor = resolvedor or shutil.which
    corredor = corredor or subprocess.run
    perfil_claw = Path(base) / "profiles" / "claw"

    def enviar(texto):
        ejecutable = resolvedor("hermes")
        if not ejecutable:
            return None
        entorno = dict(os.environ)
        entorno["HERMES_HOME"] = str(perfil_claw)
        try:
            terminado = corredor(
                [ejecutable, "send", "--to", "telegram", "--json"],
                input=texto,
                capture_output=True,
                text=True,
                # F16: text=True sin encoding usa la code page local; en un
                # Windows no UTF-8 el cuerpo ("⚠️ ...") revienta encodeando
                # con UnicodeEncodeError (un ValueError que nadie atrapaba).
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env=entorno,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if terminado.returncode != 0:
            return None
        try:
            datos = json.loads(terminado.stdout or "")
        except ValueError:
            return None
        if (
            not isinstance(datos, dict)
            or datos.get("success") is not True
            or "error" in datos
        ):
            return None
        crudo = datos.get("message_id")
        if isinstance(crudo, bool):
            return None
        if isinstance(crudo, str) and not (crudo.isascii() and crudo.isdigit()):
            return None
        if not isinstance(crudo, (int, str)):
            return None
        message_id = int(crudo)
        return message_id if message_id > 0 else None

    return enviar


def _pasada_del_emisor(tienda, base, enviar):
    """La pasada tras las vueltas (§3.6): proyecta cada raiz (derivar de la
    copia fijada, I14) en <raiz>/raices/proyecciones y avisa las excepciones
    (§3.5) desde el MISMO estado. Los errores van a stderr; un fallo del
    envio o de la proyeccion no tumba las vueltas, que ya corrieron."""
    proyeccion = importlib.import_module("raices.proyeccion")
    try:
        informe = proyeccion.pasada(
            tienda,
            ahora=datetime.now(timezone.utc),
            dir_destino=base / "raices",
            enviar=enviar,
            stale_after_ms=STALE_DESPUES_MS,
            intervalo_ms=INTERVALO_MS,
            host_gateway=socket.gethostname(),
        )
    except Exception as fallo:  # noqa: BLE001 - la vuelta ya corrio; solo reportar
        print(f"ticker-raices: proyeccion/aviso fallo: {fallo}", file=sys.stderr)
        return
    for error in informe["errores"]:
        print(f"ticker-raices: {error}", file=sys.stderr)


def correr(home=None, ruta_abierta=None, enviar=None):
    hogar = importlib.import_module("raices.hogar")
    base = hogar.raiz_del_hogar(home)
    puerta = ruta_abierta or (lambda: ruta_de_raices_abierta(base))
    abierta, causa = puerta()
    if not abierta:
        return _vuelta_cerrada(base, causa)
    store = importlib.import_module("raices.store")
    ticker = importlib.import_module("raices.ticker")
    kanban = importlib.import_module("raices.kanban")
    # I07: los permisos de H viven junto al store, en la raiz (hogar.raiz_del_hogar,
    # nunca HERMES_HOME pelado); sin archivo -> None y el gate falla cerrado. Una
    # sola lectura alimenta el store y la vuelta: las mismas reglas para todos.
    permisos = hogar.leer_permisos(home)
    tienda = store.Store(base / "sistema-agentes.db", permisos_h=permisos)
    try:
        puerto = kanban.KanbanHermes(ruta_del_kanban(base))
        try:
            ticker.vuelta(tienda, puerto, permisos_h=permisos)
        finally:
            puerto.cerrar()
        _pasada_del_emisor(tienda, base, enviar or emisor_de_telegram(base))
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
