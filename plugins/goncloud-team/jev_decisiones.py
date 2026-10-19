"""Decisiones chicas de JEV por codigo: ruteo, cierre y vigilancia.

El esquema de OpenClaw (jev-auto) replicado en Hermes sin depender de lo que
el modelo se acuerde de hacer: la plataforma pregunta sola. Tres preguntas
de opcion multiple (type choice de System One), cada una con su criterio:

  ruteo      un mensaje de David -> claw directo | especialista | ingenieria
  cierre     una tarjeta o etapa terminada      -> quedo | corregir | bloqueado
  vigilancia un candidato a atorado             -> normal | atorado | abandonado

Reglas que viven aqui y no en el SOUL:
  - Umbral: confianza >= UMBRAL se sigue; debajo decide claw.
  - Timeout de TOPE_JEV_S (2 s) duro: la llamada corre en un hilo y el que
    llama nunca espera mas. Sin reintentos. Si JEV falla, tarda o no esta,
    la Decision trae el error y nada se bloquea.
  - Cada decision deja una linea en <home de claw>/jev/decisiones.jsonl:
    entrada resumida (ids y largo, jamas el texto del mensaje), salida,
    confianza, latencia y error.

El cliente HTTP es el client.py del plugin jev instalado, cargado como un
modulo privado: este archivo no lo parcha ni lo reimplementa (los plugins jev
estan fijados por director-plugins.json + patches/claw). Lo que sale a
TypeSafe va redactado contra los valores sensibles reales del scope del
perfil y la llave, y acotado.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from .subscriptions.redaction import redact_text
except ImportError:  # carga suelta (pruebas): el plugin esta en sys.path
    from subscriptions.redaction import redact_text

UMBRAL = 0.85
UMBRAL_AVISO = 0.8
TOPE_JEV_S = 2.0
TOPE_ESTADO = 3000
MODELO = "jev-latest"

ESPECIALISTAS = {
    "amazon": "ordenes Amazon MX y US, sin personalizadas",
    "pers": "solo personalizadas nuevas",
    "meli": "Mercado Libre",
    "mail": "correo; no le escribe a David",
    "limpia": "barrer promos de Gmail",
    "odoo": "cajas y pedidos",
    "arras": "mercado de arras",
    "gonserver": "el server por SSH",
    "skillverif": "la receta de como se prueba el repo",
    "tinkabot": "envolver una API en plugin",
    "eggbot": "disenar un bot nuevo",
}
RUTAS = {
    "claw": "claw lo atiende directo: conversacion, dudas, estado, decisiones, "
    "cosas chicas que no son de un especialista ni cambian codigo",
    **{
        nombre: f"especialista {nombre}: {texto}"
        for nombre, texto in ESPECIALISTAS.items()
    },
    "ingenieria": "cadena de ingenieria: cambiar, arreglar o construir codigo "
    "de un repositorio (scout, implementacion, verificacion, review)",
}
CIERRES = {
    "quedo": "el resultado resuelve lo pedido y la evidencia lo respalda; se da por cerrado",
    "corregir": "el resultado no resuelve lo pedido o le falta evidencia; hay que reintentar o corregir",
    "bloqueado": "falta algo externo (acceso, decision de David, dato); nadie puede avanzar solo",
}
VIGILANCIA = {
    "normal": "avanza o espera con motivo; no hace falta avisar a David",
    "atorado": "lleva demasiado sin avanzar y nadie lo esta destrabando; David debe enterarse",
    "abandonado": "se dejo a medias o se prometio y no se cumplio; David debe enterarse",
}

_cliente = None
_cliente_ruta = None


@dataclass
class Decision:
    """Una respuesta con forma fija; error != None significa que no hay opcion."""

    pregunta: str
    opcion: str | None = None
    confianza: float | None = None
    ms: int = 0
    error: str | None = None

    @property
    def sigue(self) -> bool:
        """Confianza sobre el umbral: se sigue; debajo decide claw."""
        return (
            self.error is None
            and self.opcion is not None
            and (self.confianza or 0.0) >= UMBRAL
        )


def _acotar(texto, tope: int = 120) -> str:
    return " ".join(str(texto).split())[:tope]


def _hogar_de(hogar) -> Path:
    if hogar:
        return Path(hogar)
    with contextlib.suppress(Exception):
        import hermes_constants

        casa = str(hermes_constants.get_hermes_home() or "").strip()
        if casa:
            return Path(casa)
    casa = os.environ.get("HERMES_HOME", "").strip()
    return Path(casa) if casa else Path.home() / ".hermes"


def secretos_del_scope() -> tuple[str, ...]:
    """Los values() del scope del perfil, solo para tachar: jamas se imprimen."""
    try:
        from agent.secret_scope import current_secret_scope

        alcance = current_secret_scope()
        if alcance is None:
            return ()
        return tuple(v for v in alcance.values() if isinstance(v, str) and v)
    except Exception:
        return ()


def _clave_en_env_del_perfil(hogar: Path) -> str:
    """TYPESAFE_API_KEY del .env del perfil (el cron no tiene el scope del gateway)."""
    try:
        for linea in (hogar / ".env").read_text(encoding="utf-8").splitlines():
            nombre, _, valor = linea.partition("=")
            if nombre.strip() == "TYPESAFE_API_KEY":
                return valor.strip().strip("\"'")
    except OSError:
        pass
    return ""


def clave_typesafe(hogar: Path) -> str:
    """Scope del perfil, luego entorno, luego el .env del perfil."""
    try:
        from agent.secret_scope import get_secret

        clave = str(get_secret("TYPESAFE_API_KEY", "") or "").strip()
    except Exception:
        clave = ""
    return (
        clave
        or os.environ.get("TYPESAFE_API_KEY", "").strip()
        or _clave_en_env_del_perfil(hogar)
    )


def _cargar_cliente(hogar: Path):
    """El client.py instalado del plugin jev, como modulo privado (sin parchar)."""
    global _cliente, _cliente_ruta
    ruta = hogar / "plugins" / "jev" / "client.py"
    if _cliente is not None and _cliente_ruta == ruta:
        return _cliente
    if not ruta.is_file():
        raise RuntimeError("plugin jev no instalado")
    spec = importlib.util.spec_from_file_location("goncloud_jev_client", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    _cliente, _cliente_ruta = modulo, ruta
    return modulo


def llamada_real(hogar: Path):
    """La llamada de produccion: (state, preguntas, timeout) -> respuesta."""

    def llamar(state, preguntas, timeout):
        cliente = _cargar_cliente(hogar)
        clave = clave_typesafe(hogar)
        if clave:
            # Solo en NUESTRA copia del modulo: el cron no tiene el scope.
            cliente._get_key = lambda: clave
        return cliente.call_system_one(
            state=state,
            questions=preguntas,
            model=MODELO,
            timeout=timeout,
            max_retries=0,
        )

    return llamar


def _en_hilo(funcion, tope_s: float):
    """(resultado, error) con tope duro; el hilo colgado queda daemon y se suelta."""
    caja: dict = {}

    def corre():
        try:
            caja["ok"] = funcion()
        except BaseException as exc:  # noqa: BLE001 - fail-open, el error se registra
            caja["error"] = exc

    hilo = threading.Thread(target=corre, name="jev-decision", daemon=True)
    hilo.start()
    hilo.join(tope_s)
    if hilo.is_alive():
        return None, f"vencio el tope de {tope_s:g} s"
    if "error" in caja:
        return None, _acotar(caja["error"] or type(caja["error"]).__name__)
    return caja.get("ok"), None


def _confianza(respuesta: dict) -> float | None:
    bruta = respuesta.get("confidence")
    if bruta is None and isinstance(respuesta.get("probabilities"), dict):
        bruta = respuesta["probabilities"].get(respuesta.get("choice"))
    if isinstance(bruta, bool) or not isinstance(bruta, (int, float)):
        return None
    return max(0.0, min(1.0, float(bruta)))


class Decisor:
    """Las tres preguntas sobre un mismo cliente, registro y tope de 2 s."""

    def __init__(self, hogar=None, llamar=None, tope_s: float = TOPE_JEV_S):
        self.hogar = _hogar_de(hogar)
        self._llamar = llamar
        self.tope_s = tope_s

    def rutear(self, texto: str) -> Decision:
        entrada = {
            "len": len(texto),
            "sha256": hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12],
        }
        estado = "Mensaje de David al director (claw):\n" + texto
        return self._decidir(
            "ruteo",
            estado,
            "A quien le toca este mensaje? Si pide varias cosas a la vez, "
            "elige claw para que el decida el reparto.",
            RUTAS,
            entrada,
        )

    def cerrar(self, ficha: str, entrada: dict) -> Decision:
        return self._decidir(
            "cierre",
            ficha,
            "Como queda esta tarjeta o etapa, segun lo que reporto?",
            CIERRES,
            entrada,
        )

    def vigilar(self, ficha: str, entrada: dict) -> Decision:
        return self._decidir(
            "vigilancia",
            ficha,
            "Este caso esta bien o David debe enterarse de que se atoro?",
            VIGILANCIA,
            entrada,
        )

    def _decidir(self, nombre, estado, instrucciones, opciones, entrada) -> Decision:
        inicio = time.monotonic()
        preguntas = {
            nombre: {
                "type": "choice",
                "instructions": instrucciones,
                "criteria": dict(opciones),
            }
        }
        decision = Decision(pregunta=nombre)

        def llamada():
            llamar = self._llamar or llamada_real(self.hogar)
            texto = redact_text(estado, tuple(secretos_del_scope()))[:TOPE_ESTADO]
            return llamar(texto, preguntas, self.tope_s)

        respuesta, error = _en_hilo(llamada, self.tope_s)
        if error is None:
            respuestas = (
                respuesta.get("answers") if isinstance(respuesta, dict) else None
            )
            elegida = respuestas.get(nombre) if isinstance(respuestas, dict) else None
            opcion = elegida.get("choice") if isinstance(elegida, dict) else None
            if opcion in opciones:
                decision.opcion = opcion
                decision.confianza = _confianza(elegida)
                if decision.confianza is None:
                    error = "respuesta sin confianza"
            else:
                error = "respuesta sin opcion valida"
        decision.error = error
        decision.ms = int((time.monotonic() - inicio) * 1000)
        self._registrar(decision, entrada)
        return decision

    def _registrar(self, decision: Decision, entrada: dict) -> None:
        linea = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pregunta": decision.pregunta,
            "entrada": entrada,
            "salida": decision.opcion,
            "confianza": decision.confianza,
            "ms": decision.ms,
            "error": decision.error,
        }
        try:
            ruta = self.hogar / "jev" / "decisiones.jsonl"
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with ruta.open("a", encoding="utf-8", newline="\n") as archivo:
                archivo.write(
                    json.dumps(linea, ensure_ascii=False, sort_keys=True) + "\n"
                )
        except Exception:
            pass  # el registro jamas rompe el turno


def _porcentaje(valor: float | None) -> str:
    return f"{valor:.2f}" if valor is not None else "?"


def linea_ruteo(decision: Decision) -> str:
    if decision.error or decision.opcion is None:
        return "Jev sugiere: no respondió, decide tú"
    base = f"Jev sugiere: {decision.opcion} ({_porcentaje(decision.confianza)})"
    if decision.sigue:
        return base
    return f"{base}, bajo el umbral {UMBRAL:g}: decide tú"


def linea_cierre(referencia: str, decision: Decision) -> str | None:
    """Sin respuesta no hay linea: el cierre es un apoyo, no un aviso de falla."""
    if decision.error or decision.opcion is None:
        return None
    base = f"Jev cierre: {referencia} {decision.opcion} ({_porcentaje(decision.confianza)})"
    if decision.sigue:
        return base
    return f"{base}, bajo el umbral {UMBRAL:g}: decide tú"
