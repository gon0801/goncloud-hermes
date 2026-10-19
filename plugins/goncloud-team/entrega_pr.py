"""Publica la entrega de la cadena: push de la rama y PR por la API.

Decision de David (2026-10-19): "Hermes que lo haga en la PC". La etapa
entrega no puede publicar (su sandbox bloquea GitHub y en la PC no hay gh);
claw abria los PR a mano. Ahora el driver —codigo del plugin, fuera del
sandbox del modelo— lo hace cuando la cadena termina delivered:

1. En el worktree de la cadena: `git push origin cadena/<id>` con las
   credenciales que la PC ya tiene (Git Credential Manager o SSH).
2. El PR por la API de GitHub (POST /repos/{owner}/{repo}/pulls), base = la
   rama de la que nacio la cadena, head = cadena/<id>, titulo neutral
   ("Cadena <id>: entrega") y cuerpo con id y rama — el resumen de la
   entrega solo entra si el repo es privado; la instruccion JAMAS sale a
   GitHub. El token se pide en el momento con `git credential fill` para
   https://github.com (si el remoto es SSH, se deduce owner/repo de la URL
   y la credencial https se pide igual), vive en memoria y JAMAS se
   escribe a disco, log, resultado ni aviso.
3. Fail-open: si algo falla (sin remoto GitHub, sin credencial, push
   rechazado, API 4xx), la cadena NO falla — ya esta entregada — y el
   motivo corto y redactado viaja al aviso.
4. Idempotente: si ya hay un PR abierto de la rama, se reutiliza su URL.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

try:
    from .subscriptions.redaction import SENSITIVE_TEXT
except ImportError:  # carga suelta (pruebas): el plugin esta en sys.path
    from subscriptions.redaction import SENSITIVE_TEXT

#: Un motivo de una linea, acotado: viaja en el aviso, nunca un volcado.
_TOPE_MOTIVO = 200

#: owner/repo desde la URL del remoto, https o SSH, con o sin .git.
_GITHUB = re.compile(
    r"^(?:https?://(?:[^/@]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def _sanitizar(texto: str) -> str:
    """Nada con forma de credencial sale de este modulo, ni en un motivo."""
    return SENSITIVE_TEXT.sub("[redacted]", str(texto or ""))


def _corto(texto: str, tope: int = _TOPE_MOTIVO) -> str:
    return " ".join(_sanitizar(texto).split())[:tope]


def _salida_git(worktree, *args) -> str | None:
    try:
        hecho = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if hecho.returncode != 0:
        return None
    return hecho.stdout.strip() or None


def _dueño_y_repo(url: str):
    """(owner, repo) de una URL de GitHub; None si el remoto no es GitHub."""
    coincidencia = _GITHUB.match((url or "").strip())
    if coincidencia is None:
        return None
    return coincidencia.group(1), coincidencia.group(2)


def _subir(worktree, rama: str) -> tuple[bool, str]:
    """git push origin <rama>; (ok, detalle corto y redactado)."""
    try:
        hecho = subprocess.run(
            ["git", "-C", str(worktree), "push", "origin", rama],
            capture_output=True,
            text=True,
            timeout=600,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, _corto(f"git push no corrio: {exc}")
    if hecho.returncode == 0:
        return True, ""
    detalle = _corto(hecho.stderr or hecho.stdout)
    return False, detalle or f"git push salio con codigo {hecho.returncode}"


def _credencial() -> str | None:
    """El password para https://github.com del almacen de la PC, en memoria.

    `git credential fill` pregunta al almacen configurado de la PC (Git
    Credential Manager, osxkeychain, ...). El token vive solo en esta
    variable: jamas a disco, log, resultado ni aviso. El entorno se arma
    POR LLAMADA (F4): sin GIT_ASKPASS — "echo" en POSIX fabricaria una
    credencial con el texto del prompt — y con GIT_TERMINAL_PROMPT=0 para
    que sin almacen el fill falle limpio.
    """
    try:
        llenado = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if llenado.returncode != 0:
        return None
    for linea in llenado.stdout.splitlines():
        if linea.startswith("password="):
            return linea[len("password=") :].strip() or None
    return None


def _api(metodo: str, ruta: str, token: str, payload=None) -> tuple[int | None, dict]:
    """Una llamada a la API de GitHub; (codigo, cuerpo). Sin reintentos."""
    pedido = urllib.request.Request(
        "https://api.github.com" + ruta,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=metodo,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "goncloud-hermes",
        },
    )
    try:
        with urllib.request.urlopen(pedido, timeout=30) as respuesta:
            bruto = respuesta.read().decode("utf-8", "replace")
        try:
            return respuesta.status, json.loads(bruto or "{}")
        except (TypeError, ValueError):
            return respuesta.status, {}
    except urllib.error.HTTPError as exc:
        try:
            datos = json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except (
            ValueError,
            http.client.HTTPException,
            OSError,
        ):  # respuesta truncada o cortada a medias: ni el cuerpo escapa
            datos = {}
        if not isinstance(datos, dict):
            datos = {}
        return exc.code, datos
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None, {}


def _motivo_api(codigo, cuerpo: dict) -> str:
    mensaje = _corto(str((cuerpo or {}).get("message") or ""), 120)
    if codigo is None:
        return "la API de GitHub no respondio"
    return _corto(f"API de GitHub {codigo}: {mensaje}")


def _rama_base_local(worktree) -> str | None:
    """La rama de la que nacio la cadena, segun origin/HEAD del repo local."""
    salida = _salida_git(worktree, "symbolic-ref", "refs/remotes/origin/HEAD")
    if salida and salida.startswith("refs/remotes/origin/"):
        return salida[len("refs/remotes/origin/") :] or None
    return None


def publicar(workflow_id: str, *, worktree, summary: str = "", base=None):
    """Sube `cadena/<id>` y abre el PR; ({"pull_request": url}) o el motivo.

    `base` es la rama guardada en el contrato (de donde nacio la cadena,
    capturada al crear el worktree); sin ella —cadenas viejas— se deduce del
    origin/HEAD local y, por ultimo, de la rama por defecto del remoto.
    F5 (privacidad): el titulo es neutral y la instruccion JAMAS sale a
    GitHub; el resumen de la entrega solo entra si el repo es privado.
    Nunca lanza y nunca devuelve la credencial: lo unico que sale de aqui
    es la URL del PR o un motivo corto y redactado.
    """
    rama = f"cadena/{workflow_id}"
    remoto = _salida_git(worktree, "remote", "get-url", "origin")
    if remoto is None:
        return {"pr_no_abierto": "sin remoto origin en el worktree de la cadena"}
    partes = _dueño_y_repo(remoto)
    if partes is None:
        return {"pr_no_abierto": "el remoto origin no es GitHub"}
    dueño, nombre = partes
    ok, detalle = _subir(worktree, rama)
    if not ok:
        return {"pr_no_abierto": _corto(f"push rechazado: {detalle}")}
    token = _credencial()
    if token is None:
        return {"pr_no_abierto": "sin credencial de GitHub para https://github.com"}

    # Idempotente: un PR abierto de la misma rama se reutiliza.
    codigo, cuerpo = _api(
        "GET",
        f"/repos/{dueño}/{nombre}/pulls?head={dueño}:{rama}&state=open",
        token,
    )
    if codigo == 200 and isinstance(cuerpo, list) and cuerpo:
        url = (cuerpo[0] or {}).get("html_url")
        if url:
            return {"pull_request": url}
    if codigo != 200:
        return {"pr_no_abierto": _motivo_api(codigo, cuerpo)}

    # F5 (privacidad): el repo se consulta siempre — `private` decide si el
    # resumen del trabajo puede salir a GitHub, y default_branch es el
    # respaldo de la base. Si no se sabe, se trata como público.
    codigo, cuerpo = _api("GET", f"/repos/{dueño}/{nombre}", token)
    privado = codigo == 200 and isinstance(cuerpo, dict) and bool(cuerpo.get("private"))

    # La base guardada en el contrato manda; origin/HEAD y la rama por
    # defecto del remoto son el respaldo de las cadenas viejas.
    base = base or _rama_base_local(worktree)
    if not base and codigo == 200 and isinstance(cuerpo, dict):
        base = cuerpo.get("default_branch")
    if not base:
        return {"pr_no_abierto": "sin rama base identificable"}

    # F5: el titulo es neutral y la instruccion JAMAS viaja a GitHub; el
    # resumen de la entrega solo entra si el repo es privado.
    titulo = f"Cadena {workflow_id}: entrega"
    cuerpo_pr = _corto(f"Cadena {workflow_id} (rama {rama}).", 2000)
    if privado and (summary or "").strip():
        cuerpo_pr += "\n\n" + _corto(summary, 1800)
    codigo, cuerpo = _api(
        "POST",
        f"/repos/{dueño}/{nombre}/pulls",
        token,
        {"title": titulo, "head": rama, "base": base, "body": cuerpo_pr},
    )
    url = cuerpo.get("html_url") if isinstance(cuerpo, dict) else None
    if codigo in (200, 201) and url:
        return {"pull_request": url}
    return {"pr_no_abierto": _motivo_api(codigo, cuerpo)}
