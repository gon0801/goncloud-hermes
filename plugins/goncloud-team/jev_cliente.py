"""Cliente JEV (TypeSafe System One) para decisiones atómicas en el código.

La forma de la petición es la del plugin jev fijado en el repo
(hermes-jev-plugin @ b3d29f7, client.py): POST
https://api.typesafe.ai/v1/systemone con {"state", "model", "questions"},
modelo "jev-latest", y respuestas en "answers": noul -> {"noul": p},
choice -> {"choice", "probabilities", "confidence"}, score ->
{"score", "legend", "confidence"}.

Regla de la capa: cualquier error, tope o respuesta ilegible es "sin
respuesta" (None) y gana la regla que ya tenía el código. La llave vive
solo en memoria: se resuelve por agent.secret_scope.get_secret (parche
patches/claw/jev/0001-secret-scope.patch) con os.environ de respaldo, y
nunca se registra ni se imprime.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

try:  # Cargado con el directorio del plugin en sys.path.
    from subscriptions.redaction import redact_data, redact_text
except ImportError:  # Cargado como paquete de Hermes.
    from .subscriptions.redaction import redact_data, redact_text  # type: ignore

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_UMBRAL = 0.85
_MAX_STATE_CHARS = 8000

PUNTO_ROUTER_CLASIFICACION = "router_clasificacion"
PUNTO_MUSE_BRIDGE_CLASIFICACION = "muse_bridge_clasificacion"
PUNTO_TICKER_INCIDENCIA = "ticker_incidencia"

#: La clasificación de una falla de vendor, compartida por el router de
#: suscripciones y por muse_bridge: las opciones son los FailureKind.
PREGUNTA_CLASIFICACION_FALLA = (
    "Classify this subscription vendor failure into exactly one kind. "
    "A short throttle that clears in seconds is rate_limit; a spent plan "
    "window that returns hours later is quota; rejected credentials are "
    "auth; a failure of the task itself with a healthy account is "
    "task_failure."
)
OPCIONES_CLASIFICACION_FALLA = {
    "auth": "credentials rejected, expired or not logged in",
    "quota": "the plan usage window is spent for hours or days",
    "rate_limit": "a short throttle; the same route works again in seconds",
    "model_unavailable": "the model is disabled, unknown or not enabled for the plan",
    "timeout": "the vendor did not answer before its deadline",
    "transport": "a connection or network failure before an answer",
    "invalid_request": "the request itself was malformed or refused as invalid",
    "invalid_terminal": "the vendor answered outside the terminal contract",
    "task_failure": "the task failed; the account and route are healthy",
}


class JevError(Exception):
    """La llamada a TypeSafe no produjo una respuesta usable."""


@dataclass(frozen=True)
class RespuestaRuta:
    opcion: str
    probabilidad: float
    confianza: float | None = None


@dataclass(frozen=True)
class RespuestaScore:
    valor: float
    confianza: float | None = None
    leyenda: Any = None


@dataclass(frozen=True)
class JevConfig:
    """Umbral, tope y interruptores por punto de decisión."""

    umbral: float = DEFAULT_UMBRAL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    modelo: str = DEFAULT_MODEL
    puntos: Mapping[str, bool] = field(default_factory=dict)
    registro_path: Path | None = None

    def punto_activo(self, punto: str) -> bool:
        return bool(self.puntos.get(punto, True))


def _es_probabilidad(valor: Any) -> bool:
    return (
        isinstance(valor, (int, float))
        and not isinstance(valor, bool)
        and math.isfinite(float(valor))
        and 0.0 <= float(valor) <= 1.0
    )


def obtener_llave(getter: Callable | None = None) -> str:
    """La llave de TypeSafe, solo en memoria: secret_scope, luego entorno."""
    if getter is None:
        try:
            from agent.secret_scope import get_secret

            getter = get_secret
        except Exception:
            getter = None
    if getter is not None:
        try:
            try:
                valor = getter("TYPESAFE_API_KEY", "")
            except TypeError:
                valor = getter("TYPESAFE_API_KEY")
            valor = str(valor or "").strip()
            if valor:
                return valor
        except Exception:
            pass
    valor = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if valor:
        return valor
    raise JevError("TYPESAFE_API_KEY no está disponible")


def default_registro_path(env: Mapping[str, str] | None = None) -> Path:
    """<home del perfil o del root>/jev/decisiones.jsonl."""
    entorno = os.environ if env is None else env
    configurado = str(entorno.get("GONCLOUD_JEV_REGISTRO", "")).strip()
    if configurado:
        return Path(configurado)
    home = str(entorno.get("HERMES_HOME", "")).strip()
    if home:
        return Path(home) / "jev" / "decisiones.jsonl"
    return Path.home() / ".goncloud" / "jev" / "decisiones.jsonl"


def _valor_fuente(fuente: Any, clave: str, defecto: Any) -> Any:
    if fuente is None:
        return defecto
    if isinstance(fuente, Mapping):
        return fuente.get(clave, defecto)
    get_config = getattr(fuente, "get_config", None)
    if callable(get_config):
        try:
            return get_config(clave, default=defecto)
        except Exception:
            return defecto
    return getattr(fuente, clave, defecto)


def _a_bool(valor: Any, defecto: bool) -> bool:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        texto = valor.strip().lower()
        if texto in ("1", "true", "yes", "on", "si", "sí"):
            return True
        if texto in ("0", "false", "no", "off"):
            return False
    return defecto


def _a_float(valor: Any, defecto: float) -> float:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return defecto
    return numero if math.isfinite(numero) else defecto


def config_jev(fuente: Any = None, env: Mapping[str, str] | None = None) -> JevConfig:
    """Config del plugin (config_schema de plugin.yaml) con respaldo de entorno."""
    entorno = os.environ if env is None else env
    umbral = _a_float(
        _valor_fuente(fuente, "jev_umbral", DEFAULT_UMBRAL), DEFAULT_UMBRAL
    )
    if "GONCLOUD_JEV_UMBRAL" in entorno:
        umbral = _a_float(entorno["GONCLOUD_JEV_UMBRAL"], umbral)
    umbral = min(max(umbral, 0.0), 1.0)
    timeout = _a_float(
        _valor_fuente(fuente, "jev_timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        DEFAULT_TIMEOUT_SECONDS,
    )
    if "GONCLOUD_JEV_TIMEOUT_SEGUNDOS" in entorno:
        timeout = _a_float(entorno["GONCLOUD_JEV_TIMEOUT_SEGUNDOS"], timeout)
    timeout = min(max(timeout, 0.1), 30.0)

    def interruptor(clave_fuente: str, clave_env: str) -> bool:
        activo = _a_bool(_valor_fuente(fuente, clave_fuente, True), True)
        if clave_env in entorno:
            activo = _a_bool(entorno[clave_env], activo)
        return activo

    registro = _valor_fuente(fuente, "jev_registro", None)
    if "GONCLOUD_JEV_REGISTRO" in entorno:
        registro = entorno["GONCLOUD_JEV_REGISTRO"]
    return JevConfig(
        umbral=umbral,
        timeout_seconds=timeout,
        puntos={
            PUNTO_ROUTER_CLASIFICACION: interruptor(
                "jev_router_clasificacion", "GONCLOUD_JEV_ROUTER_CLASIFICACION"
            ),
            PUNTO_MUSE_BRIDGE_CLASIFICACION: interruptor(
                "jev_muse_bridge_clasificacion",
                "GONCLOUD_JEV_MUSE_BRIDGE_CLASIFICACION",
            ),
            PUNTO_TICKER_INCIDENCIA: interruptor(
                "jev_ticker_incidencia", "GONCLOUD_JEV_TICKER_INCIDENCIA"
            ),
        },
        registro_path=Path(str(registro)) if registro else None,
    )


def registrar_decision(config: JevConfig | None, entrada: Mapping[str, Any]) -> None:
    """Una línea JSONL por decisión; nunca levanta ni deja secretos."""
    try:
        ruta = (config.registro_path if config else None) or default_registro_path()
        ruta.parent.mkdir(parents=True, exist_ok=True)
        linea = json.dumps(redact_data(dict(entrada)), ensure_ascii=False)
        with ruta.open("a", encoding="utf-8") as archivo:
            archivo.write(linea + "\n")
    except Exception:
        pass


def _transporte_urllib(url: str, payload: dict, headers: dict, timeout: float):
    datos = json.dumps(payload).encode("utf-8")
    peticion = urllib.request.Request(url, data=datos, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            cuerpo = respuesta.read(1_000_000).decode("utf-8", "replace")
            return respuesta.status, json.loads(cuerpo) if cuerpo else {}
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (TimeoutError, urllib.error.URLError, OSError, ValueError) as error:
        raise JevError(f"TypeSafe no respondió: {type(error).__name__}") from None


class ClienteJev:
    """jev_check / jev_route / jev_score sobre la API real; None = sin respuesta."""

    def __init__(
        self,
        *,
        transporte: Callable | None = None,
        obtener_llave: Callable | None = None,
        config: JevConfig | None = None,
    ) -> None:
        self._transporte = transporte or _transporte_urllib
        self._obtener_llave = obtener_llave
        self.config = config or JevConfig()
        self.ultimo_error: str | None = None

    def _llave(self) -> str:
        if self._obtener_llave is not None:
            return str(self._obtener_llave() or "").strip()
        return obtener_llave()

    def _llamar(self, estado: str, preguntas: dict) -> dict | None:
        inicio = time.monotonic()
        self.ultimo_error = None
        try:
            llave = self._llave()
            if not llave:
                self.ultimo_error = "sin_llave"
                return None
            estado_limpio = redact_text(str(estado or ""))[-_MAX_STATE_CHARS:]
            payload = {
                "state": estado_limpio,
                "model": self.config.modelo,
                "questions": preguntas,
            }
            headers = {
                "Authorization": f"Bearer {llave}",
                "Content-Type": "application/json",
            }
            timeout = max(
                0.1, self.config.timeout_seconds - (time.monotonic() - inicio)
            )
            status, cuerpo = self._transporte(API_URL, payload, headers, timeout)
            if status != 200 or not isinstance(cuerpo, dict):
                self.ultimo_error = f"http_{status}"
                return None
            answers = cuerpo.get("answers")
            if not isinstance(answers, dict):
                self.ultimo_error = "sin_answers"
                return None
            return answers
        except Exception as error:  # noqa: BLE001 - sin respuesta, gana la regla
            self.ultimo_error = type(error).__name__
            return None

    def check(self, estado: str, pregunta: str) -> float | None:
        """jev_check (noul): probabilidad de sí, o None sin respuesta."""
        answers = self._llamar(
            estado, {"jev_check": {"type": "noul", "instructions": str(pregunta)}}
        )
        if not answers:
            return None
        valor = (answers.get("jev_check") or {}).get("noul")
        return float(valor) if _es_probabilidad(valor) else None

    def route(
        self, estado: str, pregunta: str, opciones: Mapping[str, str]
    ) -> RespuestaRuta | None:
        """jev_route (choice): opción elegida y su probabilidad, o None."""
        if not opciones or len(opciones) < 2:
            return None
        answers = self._llamar(
            estado,
            {
                "jev_route": {
                    "type": "choice",
                    "instructions": str(pregunta),
                    "criteria": {str(k): str(v) for k, v in opciones.items()},
                }
            },
        )
        if not answers:
            return None
        respuesta = answers.get("jev_route") or {}
        opcion = respuesta.get("choice")
        probabilidades = respuesta.get("probabilities") or {}
        if not isinstance(opcion, str) or opcion not in opciones:
            return None
        probabilidad = probabilidades.get(opcion)
        if not _es_probabilidad(probabilidad):
            confianza = respuesta.get("confidence")
            probabilidad = confianza if _es_probabilidad(confianza) else None
        if probabilidad is None:
            return None
        confianza = respuesta.get("confidence")
        return RespuestaRuta(
            opcion=opcion,
            probabilidad=float(probabilidad),
            confianza=float(confianza) if _es_probabilidad(confianza) else None,
        )

    def score(
        self, estado: str, pregunta: str, niveles: list[str]
    ) -> RespuestaScore | None:
        """jev_score: valor en la rúbrica ordenada, o None sin respuesta."""
        if not niveles or len(niveles) < 2:
            return None
        answers = self._llamar(
            estado,
            {
                "jev_score": {
                    "type": "score",
                    "instructions": str(pregunta),
                    "criteria": [str(nivel) for nivel in niveles],
                }
            },
        )
        if not answers:
            return None
        respuesta = answers.get("jev_score") or {}
        valor = respuesta.get("score")
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return None
        confianza = respuesta.get("confidence")
        return RespuestaScore(
            valor=float(valor),
            confianza=float(confianza) if _es_probabilidad(confianza) else None,
            leyenda=respuesta.get("legend"),
        )


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reclasificar_falla(
    cliente: ClienteJev | None,
    config: JevConfig | None,
    *,
    punto: str,
    regla: str,
    estado: str,
    contexto: Mapping[str, Any] | None = None,
) -> str:
    """La re-clasificación compartida (router y muse_bridge).

    Con la probabilidad de la opción >= umbral manda JEV; abajo, sin
    respuesta, con error o con el punto apagado gana ``regla`` (la
    heurística de hoy). Toda consulta queda registrada.
    """
    if cliente is None or config is None or not config.punto_activo(punto):
        return regla
    inicio = time.monotonic()
    error = None
    respuesta = None
    try:
        respuesta = cliente.route(
            estado, PREGUNTA_CLASIFICACION_FALLA, OPCIONES_CLASIFICACION_FALLA
        )
    except Exception as exc:  # noqa: BLE001 - JEV nunca rompe al llamador
        error = type(exc).__name__
    ms = round((time.monotonic() - inicio) * 1000)
    if respuesta is None and error is None:
        error = getattr(cliente, "ultimo_error", None) or "sin_respuesta"
    opcion = getattr(respuesta, "opcion", None)
    probabilidad = getattr(respuesta, "probabilidad", None)
    actuo = "regla"
    salida = regla
    if (
        isinstance(opcion, str)
        and opcion in OPCIONES_CLASIFICACION_FALLA
        and _es_probabilidad(probabilidad)
        and float(probabilidad) >= config.umbral
    ):
        actuo = "jev"
        salida = opcion
    registrar_decision(
        config,
        {
            "at": ahora_iso(),
            "punto": punto,
            "pregunta": "choice",
            "respuesta": opcion if isinstance(opcion, str) else None,
            "probabilidad": (
                float(probabilidad) if _es_probabilidad(probabilidad) else None
            ),
            "umbral": config.umbral,
            "actuo": actuo,
            "resultado_regla": regla,
            "ms": ms,
            "error": error,
            **dict(contexto or {}),
        },
    )
    return salida


def confirmar(
    cliente: ClienteJev | None,
    config: JevConfig | None,
    *,
    punto: str,
    estado: str,
    pregunta: str,
    regla: str,
    contexto: Mapping[str, Any] | None = None,
) -> bool | None:
    """jev_check con umbral: True/False solo cuando JEV manda; None = la regla.

    La respuesta "no" manda con la misma fuerza: probabilidad <= 1-umbral.
    En la zona ambigua, sin respuesta, con error o con el punto apagado
    devuelve None y el llamador aplica su regla de hoy.
    """
    if cliente is None or config is None or not config.punto_activo(punto):
        return None
    inicio = time.monotonic()
    error = None
    probabilidad = None
    try:
        probabilidad = cliente.check(estado, pregunta)
    except Exception as exc:  # noqa: BLE001 - JEV nunca rompe al llamador
        error = type(exc).__name__
    ms = round((time.monotonic() - inicio) * 1000)
    if probabilidad is None and error is None:
        error = getattr(cliente, "ultimo_error", None) or "sin_respuesta"
    actuo = "regla"
    decision: bool | None = None
    if _es_probabilidad(probabilidad):
        if float(probabilidad) >= config.umbral:
            decision, actuo = True, "jev"
        elif float(probabilidad) <= 1.0 - config.umbral:
            decision, actuo = False, "jev"
    registrar_decision(
        config,
        {
            "at": ahora_iso(),
            "punto": punto,
            "pregunta": "noul",
            "respuesta": decision,
            "probabilidad": (
                float(probabilidad) if _es_probabilidad(probabilidad) else None
            ),
            "umbral": config.umbral,
            "actuo": actuo,
            "resultado_regla": regla,
            "ms": ms,
            "error": error,
            **dict(contexto or {}),
        },
    )
    return decision
