"""Mide skill_select de JEV contra los mensajes etiquetados de David.

    python3 scripts/jev_medir_skills.py [--fixture RUTA] [--umbral U]

Corre el router real sobre tests/fixtures/jev_skills_etiquetado.jsonl y
reporta, por mensaje, suggested/gate/fit y si acierta, mas una tabla por
umbral (0.3, 0.4, ... 0.9; con --umbral U, solo ese): cuantas "cárgala"
(fit >= umbral) saldrian acertadas y cuantas equivocadas. El router real
es el de jev_auto (_ejecutar_router, que lanza con _comando_y_ambiente);
la llave se resuelve con jev_decisiones.clave_typesafe(hogar), con hogar
= perfil claw (jev_auto._hogar_de_etapas). Es medicion, no prueba
unitaria: gasta llamadas reales a TypeSafe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "goncloud-team"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

import jev_auto  # noqa: E402
import jev_decisiones  # noqa: E402

FIXTURE_POR_OMISION = ROOT / "tests" / "fixtures" / "jev_skills_etiquetado.jsonl"
UMBRALES = tuple(round(0.3 + 0.1 * n, 1) for n in range(7))  # 0.3 ... 0.9
SIN_SKILL = "ninguna"


def cargar_casos(ruta) -> list[dict]:
    casos = []
    for linea in Path(ruta).read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            dato = json.loads(linea)
        except json.JSONDecodeError:
            continue  # una línea malformada no aborta la medición (H67)
        if not isinstance(dato, dict):
            continue
        mensaje = dato.get("mensaje") or dato.get("texto") or dato.get("message")
        esperado = dato.get("esperado") or dato.get("skill") or dato.get("correcto")
        if not mensaje or not esperado:
            continue
        casos.append({"mensaje": str(mensaje), "esperado": str(esperado)})
    return casos


def crear_corredor_real():
    """El router real: hogar claw, llave de jev_decisiones, _ejecutar_router."""
    hogar = jev_auto._hogar_de_etapas()
    clave = jev_decisiones.clave_typesafe(hogar)
    if clave and not os.environ.get("JEV_TYPESAFE_API_KEY", "").strip():
        # _ejecutar_router resuelve por scope/entorno: el puente es el entorno.
        os.environ["JEV_TYPESAFE_API_KEY"] = clave

    def corredor(mensaje: str) -> dict:
        return jev_auto._ejecutar_router(mensaje, hogar)

    return corredor


def _sugerida(veredicto) -> str | None:
    if not isinstance(veredicto, dict):
        return None
    if veredicto.get("estado") not in (None, "ok"):
        return None
    sugerida = veredicto.get("skill") or veredicto.get("suggested")
    return str(sugerida) if sugerida else None


def medir(corredor, casos: list[dict], umbrales=UMBRALES) -> dict:
    """Corre `corredor(mensaje)` por caso y resume aciertos y tabla por umbral."""
    filas = []
    for caso in casos:
        try:
            veredicto = corredor(caso["mensaje"])
            error = None
        except Exception as exc:  # la medicion reporta, no explota
            veredicto, error = None, str(exc)[:120]
        if isinstance(veredicto, dict) and veredicto.get("estado") == "fallo":
            error = veredicto.get("motivo") or error
        sugerida = _sugerida(veredicto)
        gate = jev_auto._probabilidad(
            veredicto.get("gate") if isinstance(veredicto, dict) else None
        )
        fit = jev_auto._probabilidad(
            veredicto.get("fit") if isinstance(veredicto, dict) else None
        )
        esperado = caso["esperado"] or SIN_SKILL
        filas.append(
            {
                "mensaje": caso["mensaje"],
                "esperado": esperado,
                "suggested": sugerida,
                "gate": gate,
                "fit": fit,
                "acierto": error is None and (sugerida or SIN_SKILL) == esperado,
                "error": error,
            }
        )
    tabla = []
    for umbral in umbrales:
        cargala = [
            fila
            for fila in filas
            if fila["suggested"] is not None
            and fila["fit"] is not None
            and fila["fit"] >= umbral
        ]
        tabla.append(
            {
                "umbral": umbral,
                "cargala_acertadas": sum(1 for fila in cargala if fila["acierto"]),
                "cargala_equivocadas": sum(
                    1 for fila in cargala if not fila["acierto"]
                ),
            }
        )
    return {
        "filas": filas,
        "total": len(filas),
        "aciertos": sum(1 for fila in filas if fila["acierto"]),
        "tabla": tabla,
    }


def _numero(valor) -> str:
    return f"{valor:.2f}" if valor is not None else "?"


def formatear(resultado: dict) -> str:
    lineas = []
    for fila in resultado["filas"]:
        marca = "ok" if fila["acierto"] else "FALLA"
        detalle = fila["suggested"] or (
            f"error: {fila['error']}" if fila["error"] else SIN_SKILL
        )
        lineas.append(
            f"[{marca}] esperado={fila['esperado']} suggested={detalle} "
            f"gate={_numero(fila['gate'])} fit={_numero(fila['fit'])} "
            f":: {fila['mensaje']}"
        )
    lineas.append(f"aciertos: {resultado['aciertos']}/{resultado['total']}")
    lineas.append("umbral | cárgala acertadas | cárgala equivocadas")
    for fila in resultado["tabla"]:
        lineas.append(
            f"{fila['umbral']:.1f} | {fila['cargala_acertadas']} | "
            f"{fila['cargala_equivocadas']}"
        )
    return "\n".join(lineas)


def main(argv=None, corredor=None, decisor=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixture", type=Path, default=FIXTURE_POR_OMISION)
    parser.add_argument("--umbral", type=float, default=None)
    args = parser.parse_args(argv)
    if args.umbral is not None and not 0.0 <= args.umbral <= 1.0:
        parser.error("--umbral debe estar entre 0 y 1")
    umbrales = (args.umbral,) if args.umbral is not None else UMBRALES
    casos = cargar_casos(args.fixture)
    resultado = medir(corredor or decisor or crear_corredor_real(), casos, umbrales)
    print(formatear(resultado))
    return 0


if __name__ == "__main__":
    sys.exit(main())
