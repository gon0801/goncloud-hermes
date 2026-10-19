"""Texto limpio para JEV (cleanPrompt de OpenClaw, adaptado al gateway de Hermes).

El `user_message` que llega a los ganchos trae, ademas de lo que escribio
David, los envoltorios del gateway: la cita del mensaje respondido
(`[Replying to: "..."]`), las notas de media (`[The user sent ...]`), las notas
del sistema (`[System note: ...]`), el remitente (`[Nombre]`), encabezados de
Telegram, bloques de metadatos con valla ``` y, de vuelta, las lineas que
nosotros mismos inyectamos (`JEV skill_select:`, `Jev sugiere:`). Cada uno se
quita anclado a su formato exacto, nunca por palabra suelta. Si JEV clasifica
con eso encima, rutea el envoltorio y no el mensaje. `limpiar` devuelve solo el
mensaje actual; si quitar los envoltorios lo dejara vacio, devuelve el texto
original (un mensaje real jamas se pierde).
"""

from __future__ import annotations

import re

MIN_CHARS = 15  # debajo de esto no se gasta una consulta (ahorro)

_FLAGS = re.IGNORECASE | re.MULTILINE

# Encabezado EXACTO de metadatos del gateway ("... (untrusted metadata):")
# seguido de una valla ```json ... ```. Nunca por palabra suelta: un "Te paso el
# contexto:" de David con un bloque de codigo es mensaje, no envoltorio.
_BLOQUE_VALLADO = re.compile(
    r"^[ \t]*(?:Conversation info|Sender|Replied message|Forwarded message"
    r"(?: context)?|Chat history(?: since last reply)?)[^\n]{0,40}"
    r"\(untrusted metadata\):[ \t]*\n```[a-z]*\n.*?\n```[ \t]*\n?",
    _FLAGS | re.DOTALL,
)
_NOTA_MEDIA = re.compile(r"\[The user sent\b(.*?)\]\s*$", _FLAGS | re.DOTALL)
_TRANSCRIPCION = re.compile(r'(?:said|transcript)[^"]*"(.*)"\s*$', re.DOTALL | re.I)
_NOTA_SISTEMA = re.compile(r"\[System note:.*?\]", re.DOTALL | re.I)
# [Replying to: "..."] con la cita posiblemente multilinea; cierra en `"]`.
_CITA = re.compile(
    r'^[ \t]*\[Replying to:[ \t]*".*?"\][ \t]*$\n?', _FLAGS | re.DOTALL
)
_ENCABEZADO_TELEGRAM = re.compile(r"^[ \t]*\[Telegram\b[^\]\n]*\][ \t]*", _FLAGS)
_LINEA_NUESTRA = re.compile(
    r"^[ \t]*(?:JEV skill_select(?::| no respondi)|JEV:|"
    r"Jev (?:sugiere|cierre|advierte):)[^\n]*$",
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
    original = limpio = texto.replace("\r\n", "\n")
    limpio = _BLOQUE_VALLADO.sub("", limpio)
    limpio = _NOTA_SISTEMA.sub("", limpio)
    limpio = _NOTA_MEDIA.sub(_nota_media, limpio)
    for patron in (_CITA, _LINEA_NUESTRA):
        limpio = patron.sub("", limpio)
    limpio = _ENCABEZADO_TELEGRAM.sub("", limpio)
    limpio = re.sub(r"\n{2,}", "\n", limpio).strip()
    limpio = _REMITENTE.sub("", limpio, count=1)
    limpio = "\n".join(linea.rstrip() for linea in limpio.splitlines()).strip()
    # Jamas se vacia un mensaje real: sin nada que clasificar, el original.
    return limpio or original.strip()


def es_corto(texto: str) -> bool:
    """Debajo de MIN_CHARS no vale una consulta a JEV."""
    return len(texto.strip()) < MIN_CHARS
