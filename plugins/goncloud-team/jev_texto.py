"""Texto limpio para JEV (cleanPrompt de OpenClaw, adaptado al gateway de Hermes).

El `user_message` que llega a los ganchos trae, ademas de lo que escribio
David, los envoltorios del gateway: la cita del mensaje respondido
(`[Replying to: "..."]`), las notas de media (`[The user sent ...]`), las notas
del sistema (`[System note: ...]`), el remitente (`[Nombre]`), encabezados de
Telegram, bloques de metadatos con valla ``` y, de vuelta, las lineas que
nosotros mismos inyectamos (`JEV skill_select:`, `Jev sugiere:`). Si JEV
clasifica con eso encima, rutea el envoltorio y no el mensaje. `limpiar`
devuelve solo el mensaje actual; texto vacio si no queda nada.
"""

from __future__ import annotations

import re

MIN_CHARS = 15  # debajo de esto no se gasta una consulta (ahorro)

_FLAGS = re.IGNORECASE | re.MULTILINE

# Encabezado de metadatos seguido de una valla ```json ... ```
_BLOQUE_VALLADO = re.compile(
    r"^[^\n]*(?:\(untrusted metadata\)|conversation info|sender|replied message|"
    r"forwarded message|chat history|metadata|context)[^\n]*:[ \t]*\n"
    r"```[a-z]*\n.*?\n```[ \t]*\n?",
    _FLAGS | re.DOTALL,
)
_NOTA_MEDIA = re.compile(r"\[The user sent\b(.*?)\]\s*$", _FLAGS | re.DOTALL)
_TRANSCRIPCION = re.compile(r'(?:said|transcript)[^"]*"(.*)"\s*$', re.DOTALL | re.I)
_NOTA_SISTEMA = re.compile(r"\[System note:.*?\]", re.DOTALL | re.I)
_CITA = re.compile(
    r"^[ \t]*\[(?:Replying to|Reply to|Forwarded from|Forwarded)\b.*\][ \t]*$", _FLAGS
)
_ENCABEZADO_TELEGRAM = re.compile(r"^[ \t]*\[Telegram\b[^\]\n]*\][ \t]*", _FLAGS)
_LINEA_NUESTRA = re.compile(
    r"^[ \t]*(?:JEV skill_select|JEV:|Jev sugiere|Jev cierre|Jev advierte)\b.*$",
    _FLAGS,
)
_LINEA_SISTEMA = re.compile(r"^[ \t]*System:.*$", _FLAGS)
_LINEA_FECHA = re.compile(
    r"^[ \t]*(?:\[|\()?(?:current (?:date|time)|fecha|date|time)\b.*$"
    r"|^[ \t]*\[?(?:[A-Za-z]{3},?[ \t]+)?\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?(?:[ \t]?[A-Za-z]{2,5}|[+-]\d{2}:?\d{2}|Z)?\]?[ \t]*$",
    _FLAGS,
)
_REMITENTE = re.compile(r"\A\[[^\]\n]{1,60}\][ \t]+(?=\S)")


def _nota_media(coincidencia: re.Match) -> str:
    """Una nota de media se va, salvo la transcripcion: esa SI es el mensaje."""
    transcripcion = _TRANSCRIPCION.search(coincidencia.group(1))
    return transcripcion.group(1).strip() if transcripcion else ""


def limpiar(texto) -> str:
    """Solo el mensaje actual del usuario, sin envoltorios del gateway."""
    if not isinstance(texto, str):
        return ""
    limpio = texto.replace("\r\n", "\n")
    limpio = _BLOQUE_VALLADO.sub("", limpio)
    limpio = _NOTA_SISTEMA.sub("", limpio)
    limpio = _NOTA_MEDIA.sub(_nota_media, limpio)
    for patron in (_CITA, _LINEA_NUESTRA, _LINEA_SISTEMA, _LINEA_FECHA):
        limpio = patron.sub("", limpio)
    limpio = _ENCABEZADO_TELEGRAM.sub("", limpio)
    limpio = re.sub(r"\n{2,}", "\n", limpio).strip()
    limpio = _REMITENTE.sub("", limpio, count=1)
    return "\n".join(linea.rstrip() for linea in limpio.splitlines()).strip()


def es_corto(texto: str) -> bool:
    """Debajo de MIN_CHARS no vale una consulta a JEV."""
    return len(texto.strip()) < MIN_CHARS
