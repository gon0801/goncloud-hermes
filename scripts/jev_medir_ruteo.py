"""Mide el ruteo de JEV contra los mensajes etiquetados de David.

    python3 scripts/jev_medir_ruteo.py [--fixture RUTA]

Corre Decisor.rutear sobre tests/fixtures/jev_ruteo_etiquetado.jsonl y
reporta, por mensaje, el destino esperado y el obtenido con su confianza,
mas cuantos aciertan y cuantos quedan >= UMBRAL. Sin llave en el repo: la
llave se resuelve con Decisor.resuelva (scope, entorno o .env del perfil),
igual que en el gancho. Es medicion, no prueba unitaria: gasta llamadas
reales a TypeSafe.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "goncloud-team"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

import jev_decisiones  # noqa: E402

FIXTURE_POR_OMISION = ROOT / "tests" / "fixtures" / "jev_ruteo_etiquetado.jsonl"


def cargar_casos(ruta) -> list[dict]:
    casos = []
    for linea in Path(ruta).read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        dato = json.loads(linea)
        mensaje = dato.get("mensaje") or dato.get("texto") or dato.get("message")
        esperado = (
            dato.get("esperado")
            or dato.get("destino")
            or dato.get("correcto")
            or dato.get("ruta")
        )
        casos.append({"mensaje": str(mensaje or ""), "esperado": str(esperado or "")})
    return casos


def medir(decisor, casos: list[dict]) -> dict:
    """Rutea cada caso con `decisor` y resume aciertos y >= UMBRAL."""
    clave = secretos = None
    resuelva = getattr(decisor, "resuelva", None)
    if callable(resuelva):
        try:
            clave, secretos = resuelva()
        except Exception:
            clave = secretos = None
    filas = []
    for caso in casos:
        try:
            if clave is None and secretos is None:
                decision = decisor.rutear(caso["mensaje"])
            else:
                decision = decisor.rutear(
                    caso["mensaje"], clave=clave, secretos=secretos
                )
            obtenido = decision.opcion
            confianza = decision.confianza
            error = decision.error
        except Exception as exc:  # la medicion reporta, no explota
            obtenido, confianza, error = None, None, str(exc)[:120]
        filas.append(
            {
                "mensaje": caso["mensaje"],
                "esperado": caso["esperado"],
                "obtenido": obtenido,
                "confianza": confianza,
                "acierto": obtenido is not None and obtenido == caso["esperado"],
                "sobre_umbral": confianza is not None
                and confianza >= jev_decisiones.UMBRAL,
                "error": error,
            }
        )
    return {
        "filas": filas,
        "total": len(filas),
        "aciertos": sum(1 for f in filas if f["acierto"]),
        "sobre_umbral": sum(1 for f in filas if f["sobre_umbral"]),
    }


def formatear(resultado: dict) -> str:
    lineas = []
    for fila in resultado["filas"]:
        confianza = f"{fila['confianza']:.2f}" if fila["confianza"] is not None else "?"
        marca = "ok" if fila["acierto"] else "FALLA"
        detalle = fila["obtenido"] or f"error: {fila['error']}"
        lineas.append(
            f"[{marca}] esperado={fila['esperado']} obtenido={detalle} "
            f"confianza={confianza} :: {fila['mensaje']}"
        )
    lineas.append(
        f"aciertos: {resultado['aciertos']}/{resultado['total']} | "
        f">= {jev_decisiones.UMBRAL:g}: {resultado['sobre_umbral']}/{resultado['total']}"
    )
    return "\n".join(lineas)


def main(argv=None, decisor=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixture", type=Path, default=FIXTURE_POR_OMISION)
    args = parser.parse_args(argv)
    casos = cargar_casos(args.fixture)
    resultado = medir(decisor or jev_decisiones.Decisor(), casos)
    print(formatear(resultado))
    return 0


if __name__ == "__main__":
    sys.exit(main())
