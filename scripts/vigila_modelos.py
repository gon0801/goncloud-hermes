"""Revisa una vez al día que claw (OpenClaw, Mini) y Hermes (PC) sigan la tabla de modelos.

La tabla del propietario es ``model-routing.source.json``: cada perfil de Hermes compila su carril
(``profile_inheritance``) y cada agente de claw el suyo (``claw-routing.json``). Este script lee la
configuración real de las dos máquinas y compara, agente por agente, la cadena ordenada de
(modelo, esfuerzo). Revisa también el modelo por omisión de claw, los crons con modelo propio
(``tareas-programadas.json``) y la cadena de visión del director de Hermes. No usa ningún modelo
de IA y nunca escribe en claw ni en Hermes.

    python scripts/vigila_modelos.py                  # corre en la Mac: ssh a la Mini y a la PC
    python scripts/vigila_modelos.py --aviso ~/.motor/avisos/pendientes
    python scripts/vigila_modelos.py --raiz . --seco  # tablas de una carpeta; imprime y no encola

Sale con 0 si todo coincide, 1 si hay diferencias (con ``--aviso`` deja un aviso en la cola del
motor de la Mac, que lo manda por Telegram) y 2 si no pudo correr. Sin diferencias no avisa nada.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from claw_models import split_label
from configure_team import _DIRECTOR_VISION_AUXILIARY
from vigia_comun import encola

ROOT = Path(__file__).resolve().parents[1]
MINI = "gon@100.73.187.5"
PC = "gwpc"
TOPE_NOMBRES = 6
DEFAULTS = "defaults"

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
    res[n]["auxiliary"] = cfg.get("auxiliary")
    jobs = os.path.join(os.path.dirname(p), "cron", "jobs.json")
    datos = json.load(open(jobs, encoding="utf-8")) if os.path.isfile(jobs) else []
    if isinstance(datos, dict):
        datos = datos.get("jobs") or []
    res[n]["cron"] = [
        {k: j.get(k) for k in ("name", "model", "enabled")} for j in datos
    ]
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
    fila_defecto = tabla["profile_inheritance"].get("default")
    if fila_defecto is not None:
        # El por omisión de claw lleva solo la cadena de modelos, sin esfuerzo.
        claw[DEFAULTS] = [
            (modelo, "off")
            for modelo, _ in cadena_de_tabla(tabla["chains"][fila_defecto])
        ]
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
    defecto = agentes.get("defaults", {}).get("model")
    if isinstance(defecto, str):
        defecto = {"primary": defecto}
    if isinstance(defecto, dict) and defecto.get("primary"):
        refs = [defecto["primary"], *(defecto.get("fallbacks") or [])]
        out[DEFAULTS] = [(_modelo(ref), "off") for ref in refs]
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


def crons_claw(jobs):
    """``{"claw:<tarea>": modelo}`` de los crons activos de claw con modelo propio."""
    return {
        f"claw:{job['name']}": job["payload"]["model"]
        for job in jobs
        if job.get("enabled") and (job.get("payload") or {}).get("model")
    }


def crons_hermes(perfiles):
    """``{"hermes:<perfil>/<tarea>": modelo}`` de los crons activos de Hermes con modelo propio."""
    return {
        f"hermes:{perfil}/{job['name']}": job["model"]
        for perfil, cfg in perfiles.items()
        for job in cfg.get("cron") or []
        if job.get("enabled") and job.get("model")
    }


def tareas_fuera_de_politica(modelo_propio, vivos):
    """Crons cuyo modelo propio no es el de la política (o que la política no conoce)."""
    return sorted(k for k, modelo in vivos.items() if modelo_propio.get(k) != modelo)


def _cadena_vision(auxiliary):
    vision = (auxiliary or {}).get("vision") or {}
    pasos = [vision, *(vision.get("fallback_chain") or [])]
    return [(p.get("provider"), p.get("model")) for p in pasos if p]


def vision_esperada():
    return _cadena_vision(_DIRECTOR_VISION_AUXILIARY)


def vision_mal(perfiles, director):
    return (
        _cadena_vision((perfiles.get(director) or {}).get("auxiliary"))
        != vision_esperada()
    )


def _nombre_de_tarea(clave):
    return clave.split(":", 1)[1].split("/", 1)[-1]


def texto_aviso(claw_mal, hermes_mal, sin_leer, tareas_mal=(), vision=False):
    claw_mal = [
        "el modelo por omisión de claw" if a == DEFAULTS else a for a in claw_mal
    ]

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
    if tareas_mal:
        lineas.append(
            "Tareas programadas con un modelo que no es el de tu política: "
            + nombres([f"la tarea {_nombre_de_tarea(t)}" for t in tareas_mal])
            + "."
        )
    if vision:
        lineas.append("Hermes: la visión del director ya no es la cadena de tu lista.")
    lineas.append("Pídele a tu asistente que lo revise; los agentes siguen trabajando.")
    return "\n".join(lineas)


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


_CRON_CLAW = (
    'openclaw="$(command -v openclaw || echo ~/.openclaw/bin/openclaw)"; '
    '"$openclaw" gateway call cron.list --params \'{"includeDisabled": true, "limit": 100, "offset": %d}\' --json'
)


def _jobs_claw(ssh):
    jobs, offset = [], 0
    while True:
        pagina = json.loads(_corre([*ssh, MINI, _CRON_CLAW % offset]))
        jobs += pagina["jobs"]
        if not pagina.get("hasMore"):
            return jobs
        offset = pagina["nextOffset"]


def lee_vivo():
    """``(claw, hermes, sin_leer)``; una máquina que no contesta queda en ``None`` y en ``sin_leer``."""
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    claw, hermes, sin_leer = None, None, []
    try:
        config = json.loads(_corre([*ssh, MINI, "cat ~/.openclaw/openclaw.json"]))
        claw = (config, _jobs_claw(ssh))
    except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        print(f"claw: no pude leer la Mini: {e}")
        sin_leer.append("claw")
    try:
        hermes = json.loads(_corre([*ssh, PC, f"{_PYTHON_HERMES} -"], _LEE_HERMES))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"Hermes: no pude leer la PC: {e}")
        sin_leer.append("Hermes")
    return claw, hermes, sin_leer


def _lee_tablas(raiz):
    def lee(nombre):
        if raiz is None:
            subprocess.run(
                ["git", "-C", str(ROOT), "fetch", "-q", "origin"],
                check=False,
                timeout=120,
            )
            return _tabla_de_origin(nombre)
        return json.loads((raiz / nombre).read_text(encoding="utf-8"))

    return {
        n: lee(n)
        for n in (
            "model-routing.source.json",
            "claw-routing.json",
            "tareas-programadas.json",
            "team.json",
        )
    }


def main(argv=None, vivo=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aviso", type=Path, help="carpeta de avisos pendientes del motor")
    ap.add_argument(
        "--raiz", type=Path, help="lee las tablas de esta carpeta en vez de git show"
    )
    ap.add_argument("--seco", action="store_true", help="imprime y no encola")
    args = ap.parse_args(argv)

    try:
        tablas = _lee_tablas(args.raiz)
        tabla = tablas["model-routing.source.json"]
        quiere_claw, quiere_hermes = esperado(tabla, tablas["claw-routing.json"])
        modelo_propio = tablas["tareas-programadas.json"]["modelo_propio"]
        director = tablas["team.json"]["director"]
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as e:
        print(f"vigila_modelos: no pude leer las tablas: {e}", file=sys.stderr)
        return 2

    claw_vivo, hermes_vivo, sin_leer = (vivo or lee_vivo)()
    claw, hermes = {}, {}
    claw_mal, hermes_mal, tareas_mal, vision = [], [], [], False
    if claw_vivo is not None:
        config, jobs = claw_vivo
        claw = vivo_claw(config)
        claw_mal = diferencias(quiere_claw, claw)
        tareas_mal += tareas_fuera_de_politica(modelo_propio, crons_claw(jobs))
    if hermes_vivo is not None:
        hermes = vivo_hermes(hermes_vivo)
        hermes_mal = diferencias(quiere_hermes, hermes)
        tareas_mal += tareas_fuera_de_politica(modelo_propio, crons_hermes(hermes_vivo))
        vision = vision_mal(hermes_vivo, director)

    for quien, mal, quiere, tiene in (
        ("claw", claw_mal, quiere_claw, claw),
        ("hermes", hermes_mal, quiere_hermes, hermes),
    ):
        for a in mal:
            print(f"{quien} {a}: tiene {tiene.get(a)} y la tabla quiere {quiere[a]}")
    for t in tareas_mal:
        print(f"la tarea {t}: su modelo no es el de tareas-programadas.json")
    if vision:
        print(f"la visión del director {director}: la tabla quiere {vision_esperada()}")
    if not (sin_leer or claw_mal or hermes_mal or tareas_mal or vision):
        print(
            f"{time.strftime('%F %T')} ok: claw {len(quiere_claw)} y Hermes {len(quiere_hermes)} siguen la tabla"
        )
        return 0
    if args.aviso and not args.seco:
        encola(
            args.aviso.expanduser(),
            texto_aviso(claw_mal, hermes_mal, sin_leer, tareas_mal, vision),
            "vigila-modelos",
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
