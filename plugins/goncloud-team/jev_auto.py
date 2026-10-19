"""Consulta automatica a JEV antes de cargar una skill (encargo H-jev-auto r1).

claw tenia la regla de consultar mcp__jev_skill_router__skill_select antes de
cargar una skill, pero nada lo obligaba a cumplirla ni avisaba cuando se la
saltaba. La plataforma ahora consulta sola: en pre_llm_call, SOLO para el
director y SOLO en chats con persona (telegram y cli), corre la seleccion del
router en un subproceso y devuelve una linea corta como contexto del turno;
transform_llm_output agrega el pie "JEV: ..." al final de la respuesta de ese
mismo turno.

La via al router es la de su mcp.json: la funcion suggest del plugin
jev-skill-router-mcp instalado (fijado por director-plugins.json y
patches/claw: este modulo jamas lo parcha ni lo reimplementa), lanzada por
`uv run --project <plugin>` o, sin uv, por el Python del proceso con
<plugin>/src en PYTHONPATH. El texto del mensaje viaja por stdin (nunca por
argv, que queda visible en la lista de procesos) y la respuesta del hijo es
un JSON minimo: sugerencia, decision y nada mas.

La llave JEV_TYPESAFE_API_KEY se resuelve en tiempo de ejecucion como hace
el parche de jev (agent.secret_scope.get_secret con os.environ de respaldo)
y SOLO viaja al entorno del hijo: jamas a argv, logs ni registro. El texto
que sale hacia TypeSafe va REDACTADO con subscriptions/redaction.redact_text
(el mismo filtro de evidencias del plugin) contra los valores sensibles
REALES: los del entorno del hijo, TODO el scope del perfil
(agent.secret_scope.current_secret_scope, Hermes 0.22) y la llave de
TypeSafe resuelta; y acotado a TOPE_MENSAJE caracteres. David autorizo
mandar datos del trabajo a TypeSafe, nunca credenciales. El turno es
fail-open: si el router no responde dentro de TOPE_S, no esta instalado o
falla, sigue sin el y el contexto lo dice. El registro JSONL en
<home de claw>/jev/consultas.jsonl deja una linea por consulta sin el texto
del mensaje (a lo mas su largo y un sha256 corto) y sin la llave.

Encargo H-jev-hermes (el esquema de OpenClaw, por codigo y no por SOUL): en el
mismo gancho, y en paralelo a skill_select, JEV rutea el mensaje (claw directo,
un especialista o la cadena de ingenieria) y la linea "Jev sugiere: X (conf)"
entra al contexto; las tarjetas de especialista y las etapas de cadena
Goncloud que terminaron desde el turno anterior (incluido blocked) traen su
"Jev cierre: X (conf)". Confianza >= 0.85 se sigue, debajo decide claw; JEV
con tope de 2 s y fail-open (jev_decisiones.py). pre_llm_call y
post_llm_call tambien anotan el seguimiento (mensajes sin respuesta,
promesas "te aviso") que lee el vigilante sin modelo
(raices/jev_vigilante.py).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import jev_estado
    from .jev_decisiones import TOPE_JEV_S, Decisor, linea_cierre, linea_ruteo
    from .subscriptions.redaction import redact_text, sensitive_values
except ImportError:  # carga suelta (pruebas): el plugin esta en sys.path
    import jev_estado
    from jev_decisiones import TOPE_JEV_S, Decisor, linea_cierre, linea_ruteo
    from subscriptions.redaction import redact_text, sensitive_values

DIRECTOR = "claw"
PLATAFORMAS = frozenset({"telegram", "cli"})
TOPE_S = 8.0
TOPE_MENSAJE = 4000
ROUTER = "jev-skill-router-mcp"
VAR_VISIBLE = "GONCLOUD_JEV_AUTO_VISIBLE"
CLAVE_VISIBLE = "jev_auto_visible"
APAGADORES = frozenset({"0", "false", "no", "off"})

# Lo que corre dentro del hijo (venv de uv o PYTHONPATH del respaldo): una
# sola consulta, salida JSON minima sin roster ni cuerpos de skills.
_HIJO = """
import json, sys
try:
    texto = sys.stdin.buffer.read().decode("utf-8")
    from jev_skill_router.mcp_server import load_roster, suggest
    datos = suggest(texto, load_roster())
    print(json.dumps({
        "suggested": datos.get("suggested"),
        "decision": datos.get("decision"),
    }))
except Exception as exc:
    print(json.dumps({"error": str(exc)[:200]}))
"""

_popen = subprocess.Popen  # costura de prueba


def _acotar(texto, tope: int = 80) -> str:
    """Un motivo de una linea y de largo acotado, para contexto y registro."""
    limpio = " ".join(str(texto).split())
    return limpio[:tope]


def _hogar_perfil() -> Path:
    """El home del perfil claw: constants, entorno, ruta del plugin, ~/.hermes."""
    with contextlib.suppress(Exception):
        import hermes_constants

        getter = getattr(hermes_constants, "get_hermes_home", None)
        casa = str((getter() if getter else "") or "").strip()
        if casa:
            return Path(casa)
    casa = os.environ.get("HERMES_HOME", "").strip()
    if casa:
        return Path(casa)
    candidata = Path(__file__).resolve().parents[2]  # <perfil>/plugins/<plugin>/
    if (candidata / "plugins").is_dir():
        return candidata
    return Path.home() / ".hermes"


def _raiz_del_hogar(casa: Path) -> Path:
    """La raiz de Hermes (donde vive kanban.db): la decide raices/hogar.py."""
    try:
        from .raices import hogar
    except ImportError:
        from raices import hogar
    return hogar.raiz_del_hogar(casa)


def _clave_typesafe() -> str:
    """La llave del router: scope del perfil primero, entorno de respaldo."""
    try:
        from agent.secret_scope import get_secret

        clave = str(get_secret("JEV_TYPESAFE_API_KEY", "") or "").strip()
    except Exception:
        clave = ""
    return clave or os.environ.get("JEV_TYPESAFE_API_KEY", "").strip()


def _secretos_del_scope() -> tuple[str, ...]:
    """Los values() del scope del perfil (agent.secret_scope, Hermes 0.22).

    En el gateway multiplexado TODAS las llaves del perfil viven ahi, no en
    os.environ, y cualquiera pegada en el chat tampoco debe salir a
    TypeSafe. Los valores solo se usan para tachar: jamas a argv, logs ni
    registro.
    """
    try:
        from agent.secret_scope import current_secret_scope

        alcance = current_secret_scope()
    except Exception:
        return ()
    if alcance is None:
        return ()
    try:
        return tuple(
            valor for valor in alcance.values() if isinstance(valor, str) and valor
        )
    except Exception:
        return ()


def _banderas_del_grupo() -> dict:
    """Grupo de proceso propio para poder matar el arbol uv + python."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _matar_arbol(proceso) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proceso.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proceso.pid), signal.SIGKILL)
    except Exception:
        pass
    with contextlib.suppress(Exception):
        proceso.kill()


def _comando_y_ambiente(raiz: Path, hogar, clave: str):
    """El lanzamiento de su mcp.json (`uv run --project`) y el env del hijo."""
    env = dict(os.environ)
    env.pop("JEV_TYPESAFE_API_KEY", None)
    if clave:
        env["JEV_TYPESAFE_API_KEY"] = clave
    if hogar is not None:
        # El roster del router: mismo orden de preferencia que su parche
        # 0002-skills-root, pero resuelto aqui para no depender del gateway.
        env["HERMES_HOME"] = str(hogar)
        env["JEV_SKILLS_ROOT"] = str(Path(hogar) / "skills")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--project", str(raiz), "python", "-c", _HIJO], env
    origen = str(raiz / "src")
    previo = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{origen}{os.pathsep}{previo}" if previo else origen
    return [sys.executable, "-c", _HIJO], env


def _ejecutar_router(texto: str, hogar, tope_s: float = TOPE_S) -> dict:
    """Una consulta al router: {estado, skill, motivo, ms}. Nunca lanza."""
    inicio = time.monotonic()

    def fallo(motivo: str) -> dict:
        return {
            "estado": "fallo",
            "skill": None,
            "motivo": motivo,
            "ms": int((time.monotonic() - inicio) * 1000),
        }

    # El home se resuelve UNA vez: con el van la ubicacion del plugin y el
    # HERMES_HOME/JEV_SKILLS_ROOT del hijo, tambien sin hogar inyectado.
    casa = Path(hogar) if hogar else _hogar_perfil()
    raiz = casa / "plugins" / ROUTER
    if not (raiz / "pyproject.toml").is_file():
        return fallo("router no instalado")
    clave = _clave_typesafe()
    url_adapter = os.environ.get("JEV_SKILL_ADAPTER_URL", "").strip()
    token_adapter = os.environ.get("JEV_SKILL_ADAPTER_TOKEN", "").strip()
    if not clave:
        if url_adapter and not token_adapter:
            return fallo("sin JEV_SKILL_ADAPTER_TOKEN")
        if not url_adapter:
            return fallo("sin JEV_TYPESAFE_API_KEY")
    comando, env = _comando_y_ambiente(raiz, casa, clave)
    # Lo que sale a TypeSafe: redactado contra los valores sensibles REALES
    # (los del entorno del hijo, TODO el scope del perfil y la llave de
    # TypeSafe que solo vive en secret_scope) y acotado. Datos del trabajo
    # si, credenciales nunca; jamas se imprimen ni registran los valores,
    # solo se usan para tachar.
    pedido = redact_text(
        texto, (*sensitive_values(env), *_secretos_del_scope(), clave)
    )[:TOPE_MENSAJE]
    try:
        proceso = _popen(
            comando,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(raiz),
            env=env,
            **_banderas_del_grupo(),
        )
    except OSError as exc:
        return fallo(_acotar(exc))
    try:
        fuera, errores = proceso.communicate(
            input=pedido.encode("utf-8"), timeout=tope_s
        )
    except subprocess.TimeoutExpired:
        _matar_arbol(proceso)
        with contextlib.suppress(Exception):
            proceso.wait(timeout=5)
        return fallo(f"vencio el tope de {int(tope_s)} s")
    except OSError as exc:
        _matar_arbol(proceso)
        return fallo(_acotar(exc))
    if proceso.returncode != 0:
        detalle = _acotar(errores.decode("utf-8", "replace"))
        return fallo(detalle or f"el router salio con codigo {proceso.returncode}")
    try:
        datos = json.loads(fuera.decode("utf-8", "replace"))
    except ValueError:
        return fallo("respuesta no JSON")
    if isinstance(datos, dict) and datos.get("error"):
        return fallo(_acotar(datos["error"]))
    sugerida = datos.get("suggested") if isinstance(datos, dict) else None
    if isinstance(datos, dict) and datos.get("decision") == "suggest" and sugerida:
        return {
            "estado": "ok",
            "skill": str(sugerida),
            "motivo": None,
            "ms": int((time.monotonic() - inicio) * 1000),
        }
    return {
        "estado": "vacio",
        "skill": None,
        "motivo": None,
        "ms": int((time.monotonic() - inicio) * 1000),
    }


def _linea_contexto(veredicto: dict) -> str:
    """La linea que entra al mensaje del usuario de ese turno."""
    estado = veredicto.get("estado")
    if estado == "ok":
        return f"JEV skill_select: {veredicto.get('skill')} (cárgala)"
    if estado == "vacio":
        return "JEV skill_select: ninguna"
    return f"JEV skill_select no respondió ({veredicto.get('motivo')}): decide tú"


def _pie_visible(veredicto: dict) -> str:
    """La linea que se agrega al final de la respuesta del mismo turno."""
    estado = veredicto.get("estado")
    if estado == "ok":
        return f"JEV: {veredicto.get('skill')}"
    if estado == "vacio":
        return "JEV: ninguna"
    return "JEV: no respondió"


def _resultado(veredicto: dict):
    if veredicto.get("estado") == "ok":
        return veredicto.get("skill")
    if veredicto.get("estado") == "vacio":
        return "ninguna"
    return None


class JevAuto:
    """Los dos ganchos: consultar en pre_llm_call, pie en transform_llm_output."""

    def __init__(
        self, perfil, hogar=None, corredor=None, visible=None, decisor=None, kanban=None
    ):
        self.perfil = str(perfil or "")
        self.hogar = hogar
        self.corredor = corredor or _ejecutar_router
        self.visible = visible  # None: decide la variable de entorno por turno
        self.decisor = decisor  # None: solo skill_select, sin ruteo ni cierre
        self.kanban = kanban  # ruta del kanban.db; None: la de la raiz del home
        self._pendiente: dict = {}  # (session_id, turn_id) -> veredicto
        self._pieados: dict = {}  # turnos que ya llevaron el pie
        self._lineas: dict = {}  # (session_id, turn_id) -> lineas de Jev

    def pre_llm_call(self, **kwargs):
        """UNA consulta por turno de David al director, en chat con persona."""
        if self.perfil != DIRECTOR:
            return None
        plataforma = str(kwargs.get("platform") or "").strip().lower()
        if plataforma not in PLATAFORMAS:
            return None
        mensaje = kwargs.get("user_message")
        if not isinstance(mensaje, str) or not mensaje.strip():
            return None
        llave = (str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or ""))
        previo = self._pendiente.get(llave)
        if previo is not None:
            # F3: una sola consulta por turno; el mismo contexto vuelve sin
            # otro subproceso ni otra linea de registro.
            return {"context": self._contexto(llave, previo)}
        texto = mensaje.strip()
        if self.decisor is not None and plataforma == "telegram":
            jev_estado.anotar_mensaje(self._casa(), "|".join(llave), int(time.time()))
        hilos, casillas = self._lanzar_decisiones(texto)
        veredicto = self.consultar(texto)
        for hilo in hilos:
            hilo.join(TOPE_JEV_S + 1)
        self._registrar(veredicto, texto, kwargs, plataforma)
        self._pendiente[llave] = veredicto
        # Orden fijo: primero el ruteo, luego los cierres, sin importar quien acabe antes.
        self._lineas[llave] = [linea for casilla in casillas for linea in casilla]
        self._podar()
        return {"context": self._contexto(llave, veredicto)}

    def post_llm_call(self, **kwargs):
        """Salio la respuesta: el mensaje se atendio; ¿prometio un aviso?"""
        if self.perfil != DIRECTOR or self.decisor is None:
            return None
        plataforma = str(kwargs.get("platform") or "").strip().lower()
        if plataforma not in PLATAFORMAS:
            return None
        llave = (str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or ""))
        jev_estado.cerrar_turno(
            self._casa(),
            "|".join(llave),
            kwargs.get("assistant_response"),
            int(time.time()),
        )
        return None

    def _casa(self) -> Path:
        return Path(self.hogar) if self.hogar else _hogar_perfil()

    def _contexto(self, llave, veredicto) -> str:
        return "\n".join([_linea_contexto(veredicto), *self._lineas.get(llave, [])])

    def _lanzar_decisiones(self, texto: str):
        """Ruteo y cierres de JEV en hilos paralelos al skill_select.

        Devuelve (hilos, casillas): cada hilo llena su casilla con a lo mas
        una linea. Un fallo de cualquiera es fail-open: el turno sigue con
        lo que haya. Sin decisor no hay nada que lanzar.
        """
        if self.decisor is None:
            return [], []
        hilos: list = []
        casillas: list = []

        def lanzar(tarea):
            casilla: list = []
            casillas.append(casilla)

            def corre():
                try:
                    linea = tarea()
                except Exception:
                    linea = None
                if linea:
                    casilla.append(linea)

            hilo = threading.Thread(target=corre, name="jev-turno", daemon=True)
            hilos.append(hilo)
            hilo.start()

        lanzar(lambda: linea_ruteo(self.decisor.rutear(texto)))
        for cierre in self._cierres_nuevos():
            lanzar(lambda c=cierre: self._linea_de_cierre(c))
        return hilos, casillas

    def _linea_de_cierre(self, cierre: dict):
        decision = self.decisor.cerrar(
            jev_estado.ficha_de_cierre(cierre),
            {
                "task_id": cierre["task_id"],
                "assignee": cierre["assignee"],
                "desenlace": cierre["desenlace"],
            },
        )
        return linea_cierre(
            f"{cierre['task_id']} ({cierre['assignee']}, {cierre['desenlace']})",
            decision,
        )

    def _cierres_nuevos(self) -> list:
        """Tarjetas de especialista y etapas Goncloud que terminaron desde el ultimo turno."""
        conn = None
        try:
            casa = self._casa()
            cursor_ruta = casa / "jev" / "cierre-cursor.json"
            ruta = self.kanban or jev_estado.ruta_del_kanban(_raiz_del_hogar(casa))
            conn = jev_estado.abrir_kanban(ruta)
            if conn is None:
                return []
            try:
                desde = int(
                    json.loads(cursor_ruta.read_text(encoding="utf-8"))["evento"]
                )
            except (OSError, ValueError, KeyError, TypeError):
                desde = None
            if desde is None:
                # Primer turno: la historia no se cierra de golpe.
                cierres, nuevo = [], jev_estado.ultimo_evento(conn)
            else:
                cierres, nuevo = jev_estado.cierres_nuevos(conn, desde)
            cursor_ruta.parent.mkdir(parents=True, exist_ok=True)
            cursor_ruta.write_text(json.dumps({"evento": nuevo}), encoding="utf-8")
            return cierres
        except Exception:
            return []  # el cierre es un apoyo: sin kanban legible el turno sigue
        finally:
            if conn is not None:
                conn.close()

    def transform_llm_output(self, **kwargs):
        """El pie de ese turno; una sola vez y solo si hubo consulta."""
        if not self._pie_encendido():
            return None
        llave = (str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or ""))
        veredicto = self._pendiente.get(llave)
        if veredicto is None or llave in self._pieados:
            return None
        respuesta = kwargs.get("response_text")
        if not isinstance(respuesta, str) or not respuesta:
            return None
        pie = _pie_visible(veredicto)
        if respuesta.rstrip().endswith(pie):
            return None
        self._pieados[llave] = True
        self._podar()
        return respuesta.rstrip() + "\n\n" + pie

    def consultar(self, texto: str) -> dict:
        """El corredor envuelto: un veredicto con forma fija, nunca una excepcion."""
        try:
            veredicto = self.corredor(texto, self.hogar)
        except Exception as exc:
            veredicto = {"estado": "fallo", "motivo": str(exc)}
        if not isinstance(veredicto, dict) or veredicto.get("estado") not in (
            "ok",
            "vacio",
            "fallo",
        ):
            veredicto = {"estado": "fallo", "motivo": "el corredor no dio veredicto"}
        if veredicto.get("estado") == "fallo":
            veredicto["motivo"] = _acotar(veredicto.get("motivo") or "motivo vacio")
        veredicto.setdefault("skill", None)
        veredicto.setdefault("motivo", None)
        veredicto.setdefault("ms", 0)
        return veredicto

    def _pie_encendido(self) -> bool:
        if isinstance(self.visible, bool):
            return self.visible
        return os.environ.get(VAR_VISIBLE, "").strip().lower() not in APAGADORES

    def _podar(self) -> None:
        """Los dos libros acotados: memoria plana, sin fuga por turnos largos."""
        while len(self._pendiente) > 16:
            self._pendiente.pop(next(iter(self._pendiente)))
        while len(self._pieados) > 16:
            self._pieados.pop(next(iter(self._pieados)))

    def _registrar(self, veredicto: dict, texto: str, kwargs: dict, plataforma: str):
        """Una linea JSONL por consulta: sin mensaje, sin llave, sin excusas."""
        entrada = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_id": str(kwargs.get("session_id") or ""),
            "turn_id": str(kwargs.get("turn_id") or ""),
            "platform": plataforma,
            "consultado": veredicto.get("estado") != "fallo",
            "resultado": _resultado(veredicto),
            "error": (
                veredicto.get("motivo") if veredicto.get("estado") == "fallo" else None
            ),
            "ms": int(veredicto.get("ms") or 0),
            "msg_len": len(texto),
            "msg_sha256": hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12],
        }
        try:
            hogar = Path(self.hogar) if self.hogar else _hogar_perfil()
            ruta = hogar / "jev" / "consultas.jsonl"
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with ruta.open("a", encoding="utf-8", newline="\n") as archivo:
                archivo.write(
                    json.dumps(entrada, ensure_ascii=False, sort_keys=True) + "\n"
                )
        except Exception:
            pass  # el registro jamas rompe el turno


def _visible_de_llave(bruto) -> bool:
    """El mismo parseo de APAGADORES, para la llave de config del plugin."""
    if isinstance(bruto, str):
        return bruto.strip().lower() not in APAGADORES
    return bool(bruto)


def registrar(ctx):
    """Registra los dos ganchos con el perfil del contexto; devuelve la instancia."""
    config = getattr(ctx, "plugin_config", None)
    visible = None
    if isinstance(config, dict) and CLAVE_VISIBLE in config:
        visible = _visible_de_llave(config[CLAVE_VISIBLE])
    perfil = str(getattr(ctx, "profile_name", "") or "")
    # El ruteo, el cierre y el seguimiento son del director: otro perfil
    # carga el plugin sin decisor y sus ganchos ni consultan ni anotan.
    decisor = Decisor() if perfil == DIRECTOR else None
    auto = JevAuto(perfil=perfil, visible=visible, decisor=decisor)
    ctx.register_hook("pre_llm_call", auto.pre_llm_call)
    ctx.register_hook("post_llm_call", auto.post_llm_call)
    ctx.register_hook("transform_llm_output", auto.transform_llm_output)
    return auto
