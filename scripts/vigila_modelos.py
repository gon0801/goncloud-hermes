"""Revisa una vez al día que claw (OpenClaw, Mini) y Hermes (PC) sigan la tabla de modelos.

La tabla del propietario es ``model-routing.source.json``: cada perfil de Hermes compila su carril
(``profile_inheritance``) y cada agente de claw el suyo (``claw-routing.json``). Este script lee la
configuración real de las dos máquinas y compara, agente por agente, la cadena ordenada de
(modelo, esfuerzo). No usa ningún modelo de IA y nunca escribe en claw ni en Hermes.

    python scripts/vigila_modelos.py                  # corre en la Mac: ssh a la Mini y a la PC
    python scripts/vigila_modelos.py --aviso ~/.motor/avisos/pendientes

Con diferencias sale con 1 y, con ``--aviso``, deja un aviso en la cola del motor de la Mac, que lo
manda por Telegram. Sin diferencias no avisa nada.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from claw_models import split_label

ROOT = Path(__file__).resolve().parents[1]
MINI = "gon@100.73.187.5"
PC = "gwpc"
TOPE_NOMBRES = 6

_LEE_HERMES = r"""
import json, os, yaml
base = os.path.join(os.environ["LOCALAPPDATA"], "hermes")
out = {"default": os.path.join(base, "config.yaml")}
pdir = os.path.join(base, "profiles")
for n in os.listdir(pdir):
    if os.path.isfile(os.path.join(pdir, n, "config.yaml")):
        out[n] = os.path.join(pdir, n, "config.yaml")
res = {}
for n, p in out.items():
    cfg = yaml.safe_load(open(p, encoding="utf-8")) or {}
    res[n] = {k: cfg.get(k) for k in ("model", "fallback_providers", "agent")}
print(json.dumps(res))
"""
_PYTHON_HERMES = r"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"


def _modelo(ref):
    return ref.split("/", 1)[-1]


def cadena_de_tabla(etiquetas):
    """``["anthropic/claude-opus-5-5 (high)", ...]`` -> ``[("claude-opus-5-5", "high"), ...]``."""
    out = []
    for etiqueta in etiquetas:
        ref, esfuerzo = split_label(etiqueta)
        out.append((_modelo(ref), esfuerzo or "off"))
    return out


def esperado(tabla, ruteo_claw):
    claw = {
        agente: cadena_de_tabla(tabla["chains"][datos["row"]])
        for agente, datos in ruteo_claw["agents"].items()
    }
    hermes = {
        perfil: cadena_de_tabla(tabla["chains"][carril])
        for perfil, carril in tabla["profile_inheritance"].items()
    }
    return claw, hermes


def vivo_claw(config):
    agentes = config["agents"]
    globales = agentes.get("defaults", {}).get("models") or {}
    out = {}
    for agente, entrada in agentes.get("entries", {}).items():
        modelo = entrada.get("model")
        if not isinstance(modelo, dict):
            continue
        propios = entrada.get("models") or {}
        cadena = []
        for ref in [modelo.get("primary"), *(modelo.get("fallbacks") or [])]:
            params = (propios.get(ref) or globales.get(ref) or {}).get("params") or {}
            cadena.append((_modelo(ref), params.get("thinking") or "off"))
        out[agente] = cadena
    return out


def vivo_hermes(perfiles):
    out = {}
    for perfil, cfg in perfiles.items():
        modelo = cfg.get("model")
        if not isinstance(modelo, dict):
            continue
        esfuerzos = ((cfg.get("agent") or {}).get("reasoning_overrides")) or {}
        ids = [modelo.get("default") or modelo.get("model")]
        ids += [fb.get("model") for fb in cfg.get("fallback_providers") or []]
        out[perfil] = [(m, esfuerzos.get(m, "off")) for m in ids]
    return out


def diferencias(quiere, tiene):
    """Agentes cuya cadena real no es la de la tabla, o que no existen."""
    return sorted(a for a, cadena in quiere.items() if tiene.get(a) != cadena)


def texto_aviso(claw_mal, hermes_mal, sin_leer):
    def nombres(lista):
        cola = (
            f" y {len(lista) - TOPE_NOMBRES} más" if len(lista) > TOPE_NOMBRES else ""
        )
        return ", ".join(lista[:TOPE_NOMBRES]) + cola

    lineas = ["En la Mac · Modelos de los agentes:"]
    for quien in sin_leer:
        lineas.append(f"Hoy no pude revisar {quien}: su máquina no contestó.")
    if claw_mal:
        lineas.append(f"claw ya no sigue tu lista de modelos en: {nombres(claw_mal)}.")
    if hermes_mal:
        lineas.append(
            f"Hermes ya no sigue tu lista de modelos en: {nombres(hermes_mal)}."
        )
    lineas.append("Pídele a tu asistente que lo revise; los agentes siguen trabajando.")
    return "\n".join(lineas)


def encola(directorio, texto):
    directorio.mkdir(parents=True, exist_ok=True)
    tmp = directorio / ".vigila-modelos.tmp"
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, directorio / f"{int(time.time() * 1000)}-vigila-modelos.txt")


def _corre(argv, entrada=None):
    r = subprocess.run(
        argv, input=entrada, capture_output=True, text=True, timeout=120, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:] or f"salió con {r.returncode}")
    return r.stdout


def _tabla_de_origin(nombre):
    rama = _corre(
        ["git", "-C", str(ROOT), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    ).strip()
    return json.loads(_corre(["git", "-C", str(ROOT), "show", f"{rama}:{nombre}"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aviso", type=Path, help="carpeta de avisos pendientes del motor")
    args = ap.parse_args(argv)

    subprocess.run(
        ["git", "-C", str(ROOT), "fetch", "-q", "origin"], check=False, timeout=120
    )
    tabla = _tabla_de_origin("model-routing.source.json")
    quiere_claw, quiere_hermes = esperado(tabla, _tabla_de_origin("claw-routing.json"))

    sin_leer, claw_mal, hermes_mal = [], [], []
    claw, hermes = {}, {}
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    try:
        claw = vivo_claw(
            json.loads(_corre([*ssh, MINI, "cat ~/.openclaw/openclaw.json"]))
        )
        claw_mal = diferencias(quiere_claw, claw)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"claw: no pude leer la Mini: {e}")
        sin_leer.append("claw")
    try:
        hermes = vivo_hermes(
            json.loads(_corre([*ssh, PC, f"{_PYTHON_HERMES} -"], _LEE_HERMES))
        )
        hermes_mal = diferencias(quiere_hermes, hermes)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"Hermes: no pude leer la PC: {e}")
        sin_leer.append("Hermes")

    for quien, mal, quiere, tiene in (
        ("claw", claw_mal, quiere_claw, claw),
        ("hermes", hermes_mal, quiere_hermes, hermes),
    ):
        for a in mal:
            print(f"{quien} {a}: tiene {tiene.get(a)} y la tabla quiere {quiere[a]}")
    if not (sin_leer or claw_mal or hermes_mal):
        print(
            f"{time.strftime('%F %T')} ok: claw {len(quiere_claw)} y Hermes {len(quiere_hermes)} siguen la tabla"
        )
        return 0
    if args.aviso:
        encola(args.aviso.expanduser(), texto_aviso(claw_mal, hermes_mal, sin_leer))
    return 1


if __name__ == "__main__":
    sys.exit(main())
