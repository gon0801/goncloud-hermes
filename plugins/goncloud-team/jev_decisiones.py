"""Decisiones chicas de JEV por codigo: ruteo y cierre.

El esquema de OpenClaw (jev-auto) replicado en Hermes sin depender de lo que
el modelo se acuerde de hacer: la plataforma pregunta sola. Dos preguntas
de opcion multiple (type choice de System One), cada una con su criterio:

  ruteo      un mensaje de David -> claw directo | especialista | ingenieria
  cierre     una tarjeta o etapa terminada      -> quedo | corregir | bloqueado

Reglas que viven aqui y no en el SOUL:
  - Umbral: confianza >= UMBRAL se sigue; debajo decide claw.
  - Timeout de TOPE_JEV_S (2 s) duro: la llamada corre en un hilo y el que
    llama nunca espera mas. Sin reintentos. Si JEV falla, tarda o no esta,
    la Decision trae el error y nada se bloquea.
  - Cada decision deja una linea en <home de claw>/jev/decisiones.jsonl:
    entrada resumida (largo, hash, input de 200 caracteres tachado y rawLen
    -el largo antes de limpiar el texto-), salida, confianza, latencia y error.

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
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from .subscriptions.redaction import redact_text
except ImportError:  # carga suelta (pruebas): el plugin esta en sys.path
    from subscriptions.redaction import redact_text

UMBRAL = 0.85
TOPE_JEV_S = 2.0
TOPE_ESTADO = 3000
MODELO = "jev-latest"

# Las descripciones llevan las palabras reales de David: "flex para hoy" subio
# de 0.42 a 0.66 hacia amazon en OpenClaw solo por nombrar Seller Flex.
ESPECIALISTAS = {
    "amazon": "ordenes Amazon MX y US: Seller Flex, flex para hoy, recolecciones, "
    "guias y envios; sin personalizadas",
    "pers": "solo personalizadas nuevas (pedidos personalizados que llegan por "
    "Amazon o MeLi): llegó una personalizada, personalizada nueva",
    "meli": "MeLi / Mercado Libre: ventas, publicaciones, preguntas y envios de MeLi; "
    "preguntas pendientes de MeLi, responder preguntas de compradores",
    "mail": "Gmail / correo: leer, clasificar y redactar; no le escribe a David",
    "limpia": "barrer promos de Gmail: promos, newsletters, archivar o limpiar el correo",
    "odoo": "Odoo: cajas, pedidos, inventario y cotizaciones",
    "arras": "arras: el mercado de arras",
    "gonserver": "el server por SSH: revisar el servidor goncloud, contenedores, "
    "logs, reinicios",
    "skillverif": "la receta de como se prueba el repo",
    "tinkabot": "plugin / API: envolver una API en plugin",
    "eggbot": "bot nuevo: disenar un bot nuevo",
}
RUTAS = {
    "claw": "claw solo cuando el mensaje no nombra un sistema de especialista "
    "ni pide cambiar codigo: saludos, cómo va algo ya en curso, decisiones "
    "sobre trabajo existente; si nombra MeLi, Amazon, Odoo, Gmail, el server, "
    "una app o un repo, no es claw",
    **{
        nombre: f"especialista {nombre}: {texto}"
        for nombre, texto in ESPECIALISTAS.items()
    },
    "ingenieria": "cadena de ingenieria: cambiar, arreglar o construir codigo "
    "de un repositorio (scout, implementacion, verificacion, review); bugs y "
    "errores del bridge, de OpenClaw o de un plugin; agregar o cambiar algo "
    "en una app o sitio: un botón, una pantalla, una función "
    "(p. ej. la app de fotos, Orbit)",
}
# Tanda 2: en cualquier perfil JEV clasifica el encargo y lo cierra.
TIPOS = {
    "codigo": "cambiar, arreglar o construir codigo de un repositorio",
    "diagnostico": "averiguar por que algo falla o se comporta raro",
    "operacion": "ejecutar una tarea operativa: ordenes, envios, correo, server",
    "comunicacion": "redactar, responder o avisar a alguien",
    "investigacion": "buscar, comparar o leer para saber algo que no se sabe",
    "consulta": "pregunta puntual o estado de algo que ya se sabe",
}
MODOS = {
    "directo": "se puede hacer ya, sin preguntar ni investigar antes",
    "investigar": "primero hay que revisar archivos, datos o la web antes de actuar",
    "aclarar": "falta un dato o una decision de David: hay que preguntarle antes",
}
FINES = {
    "quedo": "el encargo quedo resuelto con lo que dice la respuesta",
    "seguir": "la respuesta no termina: queda trabajo por hacer en el mismo encargo",
    "bloqueado": "falta algo externo (acceso, decision de David, dato); no se puede avanzar solo",
}
BUCLE = {
    "bucle": "se repite la misma llamada sin avanzar: un bucle",
    "avanza": "la repeticion es normal: cada llamada trae algo distinto",
}
AVANCES = {
    "avance": "David solo pregunta como va, si sigue o si ya quedo el trabajo en "
    "curso (las cadenas); no pide hacer, cambiar ni explicar nada",
    "otra": "pide algo mas que el estado: hacer, cerrar, arreglar, explicar, "
    "decidir, o habla de otro tema",
}
UMBRAL_BUCLE = 0.9
N_EJEMPLOS = 15
TOPE_CORRECCIONES = 200  # lineas del archivo; pasado esto se rota
TOPE_CORRECCIONES_TRAS_ROTAR = 100
TOPE_INPUT = 200
CIERRES = {
    "quedo": "el resultado resuelve lo pedido y la evidencia lo respalda; se da por cerrado",
    "corregir": "el resultado no resuelve lo pedido o le falta evidencia; hay que reintentar o corregir",
    "bloqueado": "falta algo externo (acceso, decision de David, dato); nadie puede avanzar solo",
}
# Cierre con codigo primero: el desenlace acota las opciones antes de
# preguntar; un completed jamas puede salir "bloqueado".
CIERRE_POR_DESENLACE = {
    "completed": ("quedo", "corregir"),
    "blocked": ("corregir", "bloqueado"),
}
FIN_POR_DESENLACE = {
    "completed": ("quedo", "seguir"),
    "blocked": ("seguir", "bloqueado"),
}

_clientes: dict = {}
_cliente_candado = threading.Lock()


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


def _valores_del_env(hogar: Path) -> tuple[str, ...]:
    """Los values del .env del perfil, solo para tachar.

    Ahi viven credenciales que no llegan ni al entorno del proceso ni al
    scope del gateway: si salen en un mensaje, salen sin tachar.
    """
    try:
        lineas = (hogar / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    valores = []
    for linea in lineas:
        if "=" not in linea or linea.lstrip().startswith("#"):
            continue
        valor = linea.partition("=")[2].strip().strip("\"'")
        if valor:
            valores.append(valor)
    return tuple(valores)


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
    ruta = hogar / "plugins" / "jev" / "client.py"
    if not ruta.is_file():
        # Los perfiles que no son claw no instalan el plugin jev: leen el
        # client.py de claw, solo lectura (jamas se parcha ni se copia).
        ruta = _ruta_cliente_de_claw(hogar) or ruta
    if not ruta.is_file():
        raise RuntimeError("plugin jev no instalado")
    # Una copia del modulo POR HOGAR: la llave se inyecta en el modulo, y dos
    # perfiles con llaves distintas no deben pisarse en el gateway.
    clave_cache = (str(hogar), str(ruta))
    with _cliente_candado:
        modulo = _clientes.get(clave_cache)
        if modulo is None:
            spec = importlib.util.spec_from_file_location("goncloud_jev_client", ruta)
            modulo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modulo)
            _clientes[clave_cache] = modulo
    return modulo


def _ruta_cliente_de_claw(hogar: Path) -> Path | None:
    try:
        try:
            from .raices import hogar as raices_hogar
        except ImportError:
            from raices import hogar as raices_hogar
        ruta = (
            raices_hogar.raiz_del_hogar(hogar)
            / "profiles"
            / "claw"
            / "plugins"
            / "jev"
            / "client.py"
        )
    except Exception:
        return None
    return ruta if ruta.is_file() else None


def llamada_real(hogar: Path, clave: str | None = None):
    """La llamada de produccion: (state, preguntas, timeout) -> respuesta.

    Con `clave` ya resuelta (en el hilo del gancho) no la vuelve a buscar."""

    def llamar(state, preguntas, timeout):
        cliente = _cargar_cliente(hogar)
        resuelta = clave if clave is not None else clave_typesafe(hogar)
        if resuelta:
            # Solo en NUESTRA copia del modulo.
            cliente._get_key = lambda: resuelta
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
    valor = float(bruta)
    if not math.isfinite(valor):
        # NaN/inf no es certeza (json.loads acepta NaN): sin decision.
        return None
    return max(0.0, min(1.0, valor))


class Decisor:
    """Las dos preguntas sobre un mismo cliente, registro y tope de 2 s."""

    def __init__(self, hogar=None, llamar=None, tope_s: float = TOPE_JEV_S):
        self.hogar = _hogar_de(hogar)
        self._llamar = llamar
        self.tope_s = tope_s

    def resuelva(self) -> tuple[str, tuple[str, ...]]:
        """(clave, secretos) resueltos EN el hilo que llama.

        El scope del gateway y el .env no viajan a los hilos nuevos: la
        resolucion vive en el hilo del gancho y a los hilos de decision ya
        solo llegan los valores resueltos (para el cliente y para tachar).
        """
        return clave_typesafe(self.hogar), (
            *secretos_del_scope(),
            *_valores_del_env(self.hogar),
        )

    def _entrada_texto(self, texto: str, crudo: int | None, clave, secretos) -> dict:
        """Entrada del registro: largo, hash, input (200 chars, tachado) y rawLen."""
        visto = redact_text(texto, (*(secretos or ()), clave or ""))[:TOPE_INPUT]
        return {
            "len": len(texto),
            "sha256": hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12],
            "input": " ".join(visto.split()),
            "rawLen": len(texto) if crudo is None else int(crudo),
        }

    def rutear(self, texto: str, clave=None, secretos=None, crudo=None) -> Decision:
        entrada = self._entrada_texto(texto, crudo, clave, secretos)
        estado = "Mensaje de David al director (claw):\n" + texto
        return self._decidir(
            "ruteo",
            estado,
            "A quien le toca este mensaje? Si pide varias cosas a la vez, "
            "elige claw para que el decida el reparto." + ejemplos_de(self.hogar),
            RUTAS,
            entrada,
            clave=clave,
            secretos=secretos,
        )

    def avance(self, texto: str, clave=None, secretos=None, crudo=None) -> Decision:
        """avance | otra: solo 'avance' con confianza >= UMBRAL responde sin LLM."""
        return self._decidir(
            "avance",
            "Mensaje de David al director (claw):\n" + texto,
            "Este mensaje solo pregunta por el avance del trabajo en curso, "
            "o pide algo mas? Ante la duda, otra.",
            AVANCES,
            self._entrada_texto(texto, crudo, clave, secretos),
            clave=clave,
            secretos=secretos,
        )

    def clasificar(
        self, texto: str, clave=None, secretos=None, crudo=None
    ) -> tuple[Decision, Decision]:
        """(tipo, modo) del encargo en UNA llamada con dos preguntas."""
        resultado = self._consultar(
            {
                "tipo": ("Que tipo de encargo es este?", TIPOS),
                "modo": ("Como conviene atacarlo?", MODOS),
            },
            "Encargo recibido:\n" + texto,
            self._entrada_texto(texto, crudo, clave, secretos),
            clave,
            secretos,
        )
        return resultado["tipo"], resultado["modo"]

    def fin(
        self, ficha: str, entrada: dict, clave=None, secretos=None, desenlace=None
    ) -> Decision:
        """Al terminar un turno o etapa: quedo | seguir | bloqueado.

        Con `desenlace` el codigo acota primero: completed solo elige entre
        quedo/seguir y blocked solo entre seguir/bloqueado.
        """
        permitidas = FIN_POR_DESENLACE.get(desenlace)
        opciones = FINES if permitidas is None else {k: FINES[k] for k in permitidas}
        return self._decidir(
            "fin",
            ficha,
            "Como termino este encargo, segun lo que respondio?",
            opciones,
            entrada,
            clave=clave,
            secretos=secretos,
        )

    def bucle(self, ficha: str, entrada: dict, clave=None, secretos=None) -> Decision:
        return self._decidir(
            "bucle",
            ficha,
            "Esta herramienta se llamo de nuevo justo despues de si misma. "
            "Es un bucle sin avance o una repeticion normal?",
            BUCLE,
            entrada,
            clave=clave,
            secretos=secretos,
        )

    def cerrar(
        self, ficha: str, entrada: dict, clave=None, secretos=None, desenlace=None
    ) -> Decision:
        """Cierre de tarjeta: con `desenlace` el codigo acota primero.

        completed solo elige entre quedo/corregir (nunca bloqueado) y
        blocked solo entre corregir/bloqueado.
        """
        permitidas = CIERRE_POR_DESENLACE.get(desenlace)
        opciones = (
            CIERRES if permitidas is None else {k: CIERRES[k] for k in permitidas}
        )
        return self._decidir(
            "cierre",
            ficha,
            "Como queda esta tarjeta o etapa, segun lo que reporto?",
            opciones,
            entrada,
            clave=clave,
            secretos=secretos,
        )

    def _decidir(
        self,
        nombre,
        estado,
        instrucciones,
        opciones,
        entrada,
        clave=None,
        secretos=None,
    ) -> Decision:
        return self._consultar(
            {nombre: (instrucciones, opciones)}, estado, entrada, clave, secretos
        )[nombre]

    def _consultar(
        self, definidas: dict, estado, entrada, clave=None, secretos=None
    ) -> dict:
        """Una llamada a JEV con una o varias preguntas de opcion; una Decision
        y una linea de registro por pregunta."""
        inicio = time.monotonic()
        preguntas = {
            nombre: {
                "type": "choice",
                "instructions": instrucciones,
                "criteria": dict(opciones),
            }
            for nombre, (instrucciones, opciones) in definidas.items()
        }

        def llamada():
            if clave is None or secretos is None:
                clave_resuelta, secretos_resueltos = self.resuelva()
            else:
                clave_resuelta, secretos_resueltos = clave, secretos
            llamar = self._llamar or llamada_real(self.hogar, clave_resuelta)
            # Se tacha la llave resuelta y los secretos, no solo el scope.
            texto = redact_text(estado, (*secretos_resueltos, clave_resuelta))[
                :TOPE_ESTADO
            ]
            return llamar(texto, preguntas, self.tope_s)

        respuesta, error_llamada = _en_hilo(llamada, self.tope_s)
        ms = int((time.monotonic() - inicio) * 1000)
        decisiones = {}
        for nombre, (_, opciones) in definidas.items():
            decision = Decision(pregunta=nombre)
            error = error_llamada
            if error is None:
                respuestas = (
                    respuesta.get("answers") if isinstance(respuesta, dict) else None
                )
                elegida = (
                    respuestas.get(nombre) if isinstance(respuestas, dict) else None
                )
                opcion = elegida.get("choice") if isinstance(elegida, dict) else None
                if opcion in opciones:
                    decision.opcion = opcion
                    decision.confianza = _confianza(elegida)
                    if decision.confianza is None:
                        error = "respuesta sin confianza"
                else:
                    error = "respuesta sin opcion valida"
            decision.error = error
            decision.ms = ms
            self._registrar(decision, entrada)
            decisiones[nombre] = decision
        return decisiones

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


def linea_clasificacion(tipo: Decision, modo: Decision) -> str:
    """'Jev sugiere' de un perfil que no es el director: tipo y modo."""
    partes = []
    for etiqueta, decision in (("tipo", tipo), ("modo", modo)):
        if decision.error or decision.opcion is None:
            continue
        marca = "" if decision.sigue else f", bajo el umbral {UMBRAL:g}"
        partes.append(
            f"{etiqueta}={decision.opcion} ({_porcentaje(decision.confianza)}{marca})"
        )
    if not partes:
        return "Jev sugiere: no respondió, decide tú"
    return "Jev sugiere: " + ", ".join(partes)


def linea_bucle(herramienta: str, decision: Decision) -> str | None:
    """La advertencia del siguiente turno: solo con bucle y confianza >= 0.9."""
    if (
        decision.error
        or decision.opcion != "bucle"
        or (decision.confianza or 0.0) < UMBRAL_BUCLE
    ):
        return None
    return (
        f"Jev advierte: {herramienta} se repitio sin avanzar "
        f"({_porcentaje(decision.confianza)}); cambia de enfoque o pregunta"
    )


# --- Correcciones: JEV sugirio X y el director hizo Y ----------------------


def _ruta_correcciones(hogar) -> Path:
    return Path(hogar) / "jev" / "correcciones.jsonl"


def guardar_correccion(
    hogar,
    texto: str,
    sugerido: str,
    correcto: str,
    secretos=(),
    *,
    confirmada: bool = False,
) -> bool:
    """Una linea {texto, sugerido, correcto}; el texto va acotado y tachado.

    ``confirmada`` es la compuerta del pendiente del reviewer (H-jev-tanda2):
    la deteccion automatica (despacho distinto, trabajo sin despachar) da
    falsos positivos, asi que registra con confirmada=False y SOLO la señal
    explicita de David ("eso era para X") guarda confirmada=True — y solo
    las confirmadas se inyectan como ejemplos.
    """
    try:
        visto = " ".join(redact_text(texto, tuple(secretos))[:TOPE_INPUT].split())
        linea = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "texto": visto,
            "sugerido": sugerido,
            "correcto": correcto,
            "confirmada": confirmada,
        }
        ruta = _ruta_correcciones(hogar)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with ruta.open("a", encoding="utf-8", newline="\n") as archivo:
            archivo.write(json.dumps(linea, ensure_ascii=False, sort_keys=True) + "\n")
        _acotar_archivo(ruta)
        return True
    except Exception:
        return False  # aprender es un apoyo: jamas rompe el turno


def _acotar_archivo(ruta: Path) -> None:
    """El archivo no crece sin fin: pasado el tope se queda con las ultimas."""
    with contextlib.suppress(Exception):
        lineas = ruta.read_text(encoding="utf-8").splitlines()
        if len(lineas) > TOPE_CORRECCIONES:
            temporal = ruta.with_suffix(".tmp")
            temporal.write_text(
                "\n".join(lineas[-TOPE_CORRECCIONES_TRAS_ROTAR:]) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporal, ruta)


def leer_correcciones(hogar, n: int | None = N_EJEMPLOS) -> list:
    try:
        lineas = _ruta_correcciones(hogar).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    salida = []
    for linea in lineas:
        try:
            dato = json.loads(linea)
        except ValueError:
            continue
        if (
            isinstance(dato, dict)
            and isinstance(dato.get("texto"), str)
            and dato.get("correcto") in RUTAS
            and dato.get("sugerido") in RUTAS
        ):
            salida.append(dato)
    return salida if n is None else salida[-n:]


# La senal explicita de David: el mensaje ENTERO es la confirmacion, anclado
# de punta a punta para que un encargo que la menciona de paso no confirme
# nada. El destino tiene que ser una ruta real.
_CONFIRMACION = re.compile(
    r"^\s*(?:no[,.]?\s+)?eso\s+(?:era\s+para|va\s+con|iba\s+con|era\s+con)"
    r"\s+([a-záéíóúñ]+)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def confirmacion_explicita(texto) -> str | None:
    """Destino de "eso era para X" / "no, eso va con X"; None si no es senal."""
    if not isinstance(texto, str):
        return None
    coincidencia = _CONFIRMACION.match(texto)
    if not coincidencia:
        return None
    destino = "".join(
        letra
        for letra in unicodedata.normalize("NFD", coincidencia.group(1).lower())
        if unicodedata.category(letra) != "Mn"
    )
    return destino if destino in RUTAS else None


def ejemplos_de(hogar) -> str:
    """Las ultimas 15 correcciones CONFIRMADAS como ejemplos del ruteo.

    Compuerta del reviewer: la deteccion automatica registra correcciones
    que pueden ser falsas; sin la marca confirmada (senal explicita de
    David) ninguna entra al prompt. Las lineas viejas sin marca tampoco.
    """
    corregidas = [
        c for c in leer_correcciones(hogar, n=None) if c.get("confirmada") is True
    ][-N_EJEMPLOS:]
    if not corregidas:
        return ""
    lineas = [
        f'- "{c["texto"][:120]}" -> {c["correcto"]} (no {c["sugerido"]})'
        for c in corregidas
    ]
    return "\nEjemplos de correcciones reales (aprende de ellas):\n" + "\n".join(lineas)
