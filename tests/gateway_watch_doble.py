#!/usr/bin/env python3
"""Doble de contrato del gateway Hermes que corre en la PC (B1, r4).

Copia TEXTUAL de dos funciones de gateway/run.py de Hermes v0.22.0,
commit e06df9c (artifacts/hermes-agent-pc-1019/gateway/run.py):

- _format_gateway_process_notification: líneas 3023-3050 (solo se
  conserva íntegra la rama watch_match que este contrato ejerce; las
  ramas watch_disabled/overflow y async_delegation/heartbeat quedan
  fuera porque el aviso nunca emite esos tipos).
- _drain_gateway_watch_events: líneas 3053-3073, íntegra.

Si Hermes cambia estas funciones en la PC, esta copia se actualiza a
mano y la prueba de contrato dice si el evento de avisos_sesion sigue
pasando el drain y el formato reales.
"""


def _format_gateway_process_notification(evt: dict) -> str | None:
    """Format a watch pattern event from completion_queue into a [IMPORTANT:] message."""
    evt_type = evt.get("type", "completion")
    _sid = evt.get("session_id", "unknown")
    _cmd = evt.get("command", "unknown")

    if evt_type == "watch_match":
        _pat = evt.get("pattern", "?")
        _out = evt.get("output", "")
        _sup = evt.get("suppressed", 0)
        text = (
            f"[IMPORTANT: Background process {_sid} matched "
            f'watch pattern "{_pat}".\n'
            f"Command: {_cmd}\nMatched output:\n{_out}"
        )
        if _sup:
            text += f"\n({_sup} earlier matches were suppressed by rate limit)"
        text += "]"
        return text

    return None


def _drain_gateway_watch_events(completion_queue) -> list[dict]:
    """Drain gateway-owned watch events without spinning on requeued events.
    Foreign events requeued inside ``while not queue.empty()`` never terminate: detach, then requeue."""
    watch_events: list[dict] = []
    requeue: list[dict] = []
    while not completion_queue.empty():
        try:
            evt = completion_queue.get_nowait()
        except Exception:
            break
        evt_type = evt.get("type", "completion")
        if evt_type in {
            "watch_match",
            "watch_disabled",
            "watch_overflow_tripped",
            "watch_overflow_released",
            "heartbeat",
        }:
            watch_events.append(evt)
        elif evt_type == "async_delegation":
            requeue.append(evt)
        # else: process completion events are handled by the watcher task
    for evt in requeue:
        completion_queue.put(evt)
    return watch_events
