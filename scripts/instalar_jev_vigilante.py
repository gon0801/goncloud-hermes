#!/usr/bin/env python3
"""Instala y revierte el cron del vigilante de JEV (encargo H-jev-hermes).

Un trabajo sin agente, cada 5 minutos, en el perfil DEFAULT (la raiz del
home) con entrega local: raices/jev_vigilante.py. Usa las mismas piezas que
instalar_raices.py (envoltorio en scripts/, id leido de cron/jobs.json,
toda llamada al CLI con "-p default") y su propio archivo de estado,
raices/instalacion-jev.json, asi el instalador de raices y su reversa no se
enteran ni se tocan. Es un instalador aparte porque aquel fija "every 1m"
para todos sus crones.

--check no escribe ni invoca a hermes. --aplicar es idempotente: si el job ya
existe no lo vuelve a crear. --revertir quita solo el job, el envoltorio y el
estado de este instalador. El gateway no se toca: el cron lo corre el
scheduler, no hay que reiniciar nada.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent


def _cargar_instalador_de_raices():
    spec = importlib.util.spec_from_file_location(
        "instalar_raices_base", AQUI / "instalar_raices.py"
    )
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


base = _cargar_instalador_de_raices()
CRON = base.CronDeRaices("jev-vigilante", "jev-vigilante.py", "local", "jev_vigilante")
FRECUENCIA = "every 5m"
ESTADO = "raices/instalacion-jev.json"


def _id_guardado(home: Path):
    try:
        datos = json.loads((home / ESTADO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cron_id = datos.get("cron_id") if isinstance(datos, dict) else None
    return cron_id if isinstance(cron_id, str) else None


def chequear(home: Path, plugin: Path) -> int:
    jobs = base._leer_jobs(home)
    if jobs is None:
        print(base._jobs_ilegibles(home) + "; nada que hacer")
        return 1
    print(
        f"cron {CRON.nombre}: "
        + (
            "presente"
            if base._hay_cron(jobs, CRON.nombre)
            else f"se crearia en el perfil default ({FRECUENCIA}, --no-agent, --deliver local)"
        )
    )
    print(
        f"envoltorio {CRON.envoltorio}: "
        + (
            "presente"
            if (home / CRON.envoltorio).exists()
            else f"se escribiria delegando en {plugin}"
        )
    )
    return 0


def aplicar(home: Path, plugin: Path, corredor) -> int:
    jobs = base._leer_jobs(home)
    if jobs is None:
        print(base._jobs_ilegibles(home) + "; nada que hacer")
        return 1
    base._escribir_envoltorio(home, CRON, plugin)
    cron_id = base._id_del_cron(jobs, CRON.nombre)
    if not base._hay_cron(jobs, CRON.nombre):
        codigo, salida = corredor(
            [
                "cron",
                "create",
                FRECUENCIA,
                "--name",
                CRON.nombre,
                "--script",
                CRON.script,
                "--no-agent",
                "--deliver",
                CRON.deliver,
            ]
        )
        if codigo != 0:
            # Sin job no hay quien use el envoltorio: se desenlaza y se dice.
            (home / CRON.envoltorio).unlink(missing_ok=True)
            print(
                f"instalar-jev: hermes rechazo el cron (codigo {codigo}): {salida.strip()}"
            )
            return 1
        tras = base._leer_jobs(home)
        cron_id = base._id_del_cron(tras, CRON.nombre) if tras is not None else None
        cron_id = cron_id or base._id_desde_salida(salida)
    estado = home / ESTADO
    estado.parent.mkdir(parents=True, exist_ok=True)
    estado.write_text(
        json.dumps({"version": 1, "cron": CRON.nombre, "cron_id": cron_id}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(
        f"instalar-jev: cron {CRON.nombre} ({FRECUENCIA}) en default, envoltorio en scripts/"
    )
    return 0


def revertir(home: Path, corredor) -> int:
    estado = home / ESTADO
    if not estado.exists():
        print("instalar-jev: sin instalacion registrada; no se toca nada")
        return 0
    jobs = base._leer_jobs(home)
    if jobs is None:
        print(base._jobs_ilegibles(home) + "; no se toca nada")
        return 1
    guardado = _id_guardado(home)
    vivos = {j.get("id") for j in jobs if isinstance(j.get("id"), str)}
    por_remove = guardado if guardado in vivos else None
    if por_remove is None and guardado is None:
        por_remove = base._id_del_cron(jobs, CRON.nombre)  # el nombre respalda sin id
    if por_remove is not None:
        codigo, salida = corredor(["cron", "remove", por_remove])
        if codigo != 0:
            print(f"instalar-jev: hermes rechazo el remove: {salida.strip()}")
            return 1
    (home / CRON.envoltorio).unlink(missing_ok=True)
    estado.unlink()
    print("instalar-jev: cron del vigilante retirado; datos de jev intactos")
    return 0


def main(argv=None, corredor=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modo = parser.add_mutually_exclusive_group(required=True)
    modo.add_argument("--check", action="store_true")
    modo.add_argument("--aplicar", action="store_true")
    modo.add_argument("--revertir", action="store_true")
    parser.add_argument("--home", type=Path, required=True, help="HERMES_HOME objetivo")
    parser.add_argument(
        "--plugin",
        type=Path,
        default=AQUI.parent / "plugins" / "goncloud-team",
        help="ruta del plugin goncloud-team con raices/",
    )
    args = parser.parse_args(argv)
    if corredor is None:

        def corredor(llamada):
            return base.correr_por_subprocess(llamada, home=args.home)

    if args.check:
        return chequear(args.home, args.plugin)
    if args.aplicar:
        return aplicar(args.home, args.plugin, corredor)
    return revertir(args.home, corredor)


if __name__ == "__main__":
    sys.exit(main())
