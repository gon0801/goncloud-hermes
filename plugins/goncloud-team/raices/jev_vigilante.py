"""El vigilante de JEV como script de cron de Hermes, sin agente y sin modelo.

Cada 5 minutos (scripts/instalar_jev_vigilante.py lo registra en el perfil
default, igual que el ticker de raices) revisa el trabajo de claw SIN gastar
nada: lee kanban.db y el seguimiento que anotan los ganchos de claw y arma
candidatos:

  - cadena de ingenieria Goncloud con una etapa blocked o sin avance >10 min
  - tarjeta de especialista sin avance >10 min
  - mensaje de David sin respuesta >10 min
  - promesa "te aviso" sin cumplir >30 min

Sin candidatos no hace nada mas: costo en reposo = 0. Solo con candidatos
consulta a JEV (normal / atorado / abandonado, tope de 2 s, fail-open); si
dice atorado o abandonado con confianza >= 0.8 avisa a David por el Telegram
del director (el mismo emisor `hermes send --to telegram` del ticker de
raices, con el home de claw). Un caso no se repite antes de 30 min (ni se
reconsulta a JEV antes de eso) y se avisa a lo mas tres veces. De 23:00 a
08:00 en America/Vancouver calla, salvo que haya ingenieria abierta.

Convencion de los scripts no_agent: nada en la salida si sale bien; una linea
`ERROR jev vigilante: ...` y salida 1 si fallo.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

import jev_estado  # noqa: E402
from jev_decisiones import UMBRAL_AVISO, Decisor  # noqa: E402

ZONA = "America/Vancouver"
SILENCIO_DESDE = 23
SILENCIO_HASTA = 8
MENSAJE_S = 600
PROMESA_S = jev_estado.PROMESA_S
VENTANA_S = 6 * 3600  # pasado esto el caso es historia, no un aviso util
REPETIR_S = 1800
MAX_AVISOS = 3
MAX_CONSULTAS = 5
ESTADO = "avisos.json"


def hora_local(ahora: datetime) -> datetime:
    """La hora de David. Sin tzdata cae a UTC-8 (Vancouver en invierno)."""
    try:
        from zoneinfo import ZoneInfo

        return ahora.astimezone(ZoneInfo(ZONA))
    except Exception:
        return ahora.astimezone(timezone(timedelta(hours=-8)))


def en_silencio(ahora: datetime) -> bool:
    hora = hora_local(ahora).hour
    return hora >= SILENCIO_DESDE or hora < SILENCIO_HASTA


def _leer_estado(claw: Path) -> dict:
    try:
        datos = json.loads((claw / "jev" / ESTADO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        datos = {}
    if not isinstance(datos, dict):
        datos = {}
    for clave in ("avisos", "revisados"):
        if not isinstance(datos.get(clave), dict):
            datos[clave] = {}
    return datos


def _guardar_estado(claw: Path, datos: dict, ahora: int) -> None:
    for clave in ("avisos", "revisados"):
        datos[clave] = {
            k: v
            for k, v in datos[clave].items()
            if isinstance(v, dict) and ahora - int(v.get("at", 0)) < 86400
        }
    ruta = claw / "jev" / ESTADO
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, sort_keys=True), encoding="utf-8")
    os.replace(temporal, ruta)


def candidatos_de_seguimiento(claw: Path, ahora: int) -> list[dict]:
    """Mensajes de David y promesas que los ganchos anotaron y siguen abiertos."""
    seguimiento = jev_estado.leer_seguimiento(claw)
    candidatos = []
    for llave, marca in seguimiento["mensajes"].items():
        espera = ahora - int(marca.get("at", ahora))
        if MENSAJE_S < espera < VENTANA_S:
            candidatos.append(
                {
                    "clave": f"mensaje:{llave}",
                    "tipo": "mensaje sin respuesta",
                    "ficha": (
                        f"Un mensaje de David al director lleva {espera // 60} min "
                        "sin respuesta (el turno no llego a contestar)."
                    ),
                }
            )
    for llave, marca in seguimiento["promesas"].items():
        espera = ahora - int(marca.get("at", ahora))
        if PROMESA_S < espera < VENTANA_S:
            candidatos.append(
                {
                    "clave": f"promesa:{llave}",
                    "tipo": "promesa sin cumplir",
                    "ficha": (
                        f"El director le prometio a David avisarle ('te aviso') hace "
                        f"{espera // 60} min y no ha vuelto a escribirle desde entonces."
                    ),
                }
            )
    return candidatos


def _pendiente_de_revisar(estado: dict, clave: str, ahora: int) -> bool:
    """False si ya se avisó o ya se reviso hace menos de 30 min, o se agoto el tope."""
    aviso = estado["avisos"].get(clave)
    if aviso:
        if int(aviso.get("n", 1)) >= MAX_AVISOS:
            return False
        if ahora - int(aviso.get("at", 0)) < REPETIR_S:
            return False
    revisado = estado["revisados"].get(clave)
    return not (revisado and ahora - int(revisado.get("at", 0)) < REPETIR_S)


def _texto_del_aviso(candidato: dict, decision) -> str:
    return (
        f"⚠️ Jev ve algo {decision.opcion}: {candidato['tipo']}.\n"
        f"{candidato['ficha']}\n"
        f"(confianza {decision.confianza:.2f})"
    )


def correr(home=None, ahora=None, enviar=None, decisor=None, kanban=None) -> list[str]:
    """Una vuelta. Devuelve las claves avisadas; nunca avisa dos veces lo mismo."""
    hogar = importlib.import_module("raices.hogar")
    raiz = hogar.raiz_del_hogar(home)
    claw = raiz / "profiles" / "claw"
    ahora_dt = ahora or datetime.now(timezone.utc)
    ts = int(ahora_dt.timestamp())

    candidatos: list[dict] = []
    abierta = False
    conn = jev_estado.abrir_kanban(kanban or jev_estado.ruta_del_kanban(raiz))
    if conn is not None:
        try:
            candidatos += jev_estado.candidatos_del_kanban(conn, ts)
            abierta = jev_estado.ingenieria_abierta(conn)
        finally:
            conn.close()
    candidatos += candidatos_de_seguimiento(claw, ts)
    if not candidatos:
        return []  # en reposo no se consulta nada: costo 0

    estado = _leer_estado(claw)
    candidatos = [
        c for c in candidatos if _pendiente_de_revisar(estado, c["clave"], ts)
    ]
    if not candidatos or (en_silencio(ahora_dt) and not abierta):
        return []

    decisor = decisor or Decisor(claw)
    if enviar is None:
        cron_ticker = importlib.import_module("raices.cron_ticker")
        enviar = cron_ticker.emisor_de_telegram(raiz)
    avisadas: list[str] = []
    for candidato in candidatos[:MAX_CONSULTAS]:
        clave = candidato["clave"]
        decision = decisor.vigilar(
            candidato["ficha"], {"clave": clave, "tipo": candidato["tipo"]}
        )
        if decision.error:
            continue  # JEV caido: la proxima vuelta lo intenta de nuevo
        if (
            decision.opcion in ("atorado", "abandonado")
            and (decision.confianza or 0.0) >= UMBRAL_AVISO
        ):
            if enviar(_texto_del_aviso(candidato, decision)) is not None:
                previo = estado["avisos"].get(clave, {})
                estado["avisos"][clave] = {"at": ts, "n": int(previo.get("n", 0)) + 1}
                avisadas.append(clave)
        else:
            estado["revisados"][clave] = {"at": ts}
    _guardar_estado(claw, estado, ts)
    return avisadas


def main():
    try:
        correr()
        return 0
    except Exception as exc:
        print(f"ERROR jev vigilante: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
