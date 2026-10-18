"""El store de instrucciones de las raíces (SUA-07 H6a): content-addressed.

Dueño exclusivo de <raiz>/raices/instrucciones/. El texto de un brief nunca
viaja dentro de la asignación (fixture G): viaja su nombre, artifact:<sha256>,
y el digest se verifica en cada lectura, como el artifact store de R. La
escritura es atómica (.pending-<uuid> + link) e idempotente por contenido;
después del link el archivo queda de solo lectura.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

REF = re.compile(r"^artifact:([0-9a-f]{64})$")


def guardar(dir_instrucciones: Path, texto: str) -> tuple[str, str]:
    """("artifact:<hex64>", "sha256:<hex64>"); el nombre ES el contenido."""
    hex64 = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    dir_instrucciones = Path(dir_instrucciones)
    dir_instrucciones.mkdir(parents=True, exist_ok=True)
    final = dir_instrucciones / hex64
    pendiente = dir_instrucciones / f".pending-{uuid.uuid4().hex}"
    try:
        with open(pendiente, "x", encoding="utf-8") as archivo:
            archivo.write(texto)
            archivo.flush()
            os.fsync(archivo.fileno())
        try:
            os.link(pendiente, final)
        except FileExistsError:
            pass
        os.chmod(final, 0o444)
    finally:
        pendiente.unlink(missing_ok=True)
    return f"artifact:{hex64}", f"sha256:{hex64}"


def resolver(
    dir_instrucciones: Path, ref: str, digest: str
) -> tuple[str | None, str | None]:
    """(texto, None) si el ref matchea, el archivo existe y su sha256 es el
    hex del ref y del digest; si no, (None, motivo)."""
    hallado = REF.fullmatch(ref) if isinstance(ref, str) else None
    if hallado is None:
        return None, "ref-invalida"
    try:
        texto = (Path(dir_instrucciones) / hallado.group(1)).read_text(encoding="utf-8")
    except OSError:
        return None, "instruccion-ausente"
    real = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    if real != hallado.group(1) or f"sha256:{real}" != digest:
        return None, "digest-distinto"
    return texto, None
