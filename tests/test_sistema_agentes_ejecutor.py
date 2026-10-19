#!/usr/bin/env python3
"""El ejecutor de tarjetas de raíz y su lanzamiento (SUA-07 H7).

Un hijo CLI por tarjeta de los perfiles de equipo, que el despachador nativo
no corre a propósito. Aquí viven: la traducción del sobre terminal al result
JSON que ticker._documento_de_resultado pasa tal cual (completed), el bloqueo
con motivo (blocked, sobre inválido, router en fallo), la guarda de solo
lectura de la cadena (_READ_ONLY_STAGES), y los gates del lanzamiento: NUNCA
dos ejecutores por tarjeta (reclamo con TTL), nada para dispatch_profiles,
nada sin cupo de maxModelCalls.
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

import hermes_falso  # noqa: E402
from conexiones import rastrear_conexiones  # noqa: E402

store = importlib.import_module("raices.store")
ticker = importlib.import_module("raices.ticker")
ejecutor = importlib.import_module("raices.ejecutor")
instrucciones = importlib.import_module("raices.instrucciones")
kanban_port = importlib.import_module("raices.kanban_port")
herramientas = importlib.import_module("raices.herramientas")
despertar = importlib.import_module("raices.despertar")
kanban = importlib.import_module("raices.kanban")

DobleKanban = kanban_port.DobleKanban
KanbanHermes = kanban.KanbanHermes
clave_de_tarjeta = kanban_port.clave_de_tarjeta

RAIZ = "root-h7"
DIGEST = "sha256:" + "a" * 64


def archivo_de_resultado(tienda, unit_id="a", attempt=1):
    """El archivo de resultado del hijo de un intento, con el mismo nombre
    determinista del lanzador de producción."""
    return ejecutor._archivo_de_resultado(
        Path(tienda.ruta).parent / "raices" / "ejecutor", RAIZ, unit_id, attempt
    )


def permisos_de_prueba(perfil="codex", herramienta="write", modo="implement"):
    return {
        "acciones": sorted({modo, "read"}),
        "toolsets": {perfil: [herramienta]},
        "comandos": ["git", "python3"],
    }


def presupuesto_base(**cambios):
    presupuesto = {
        "maxConcurrentTasks": 8,
        "maxDepth": 8,
        "maxChildren": 32,
        "maxModelCalls": 320,
        "maxInputTokens": 1000000,
        "maxOutputTokens": 100000,
        "maxCacheReadTokens": 1000000,
        "maxContextTokens": 100000,
        "maxTreeTokens": 1100000,
        "maxAutomaticRecoveryCalls": 8,
    }
    presupuesto.update(cambios)
    return presupuesto


def meta_de(budget=None, repos=("owner-01/repo-a",)):
    return {
        "schema": "sistema-agentes.meta.v1",
        "rootId": RAIZ,
        "requester": {"principal": ["david"], "agentId": "main"},
        "authority": {
            "schema": "sistema-agentes.autoridad.v1",
            "authorityRef": "run:turn-one",
            "grantedBy": "david",
            "grantedAt": "2026-10-17T00:00:00Z",
            "actions": ["read", "implement", "review"],
            "repos": list(repos),
            "accounts": [],
            "destinations": [],
            "deadline": None,
            "mode": "implement",
        },
        "budget": budget or presupuesto_base(),
        "plan": {
            "revision": 1,
            "planDigest": DIGEST,
            "requestDigest": DIGEST,
            "units": [
                {
                    "unitId": "a",
                    "title": "Unidad a",
                    "dependsOn": [],
                    "criteria": [
                        {"criterionId": "a1", "text": "a1", "verifier": "verifier"}
                    ],
                    "scope": {"repos": list(repos), "mode": "implement"},
                }
            ],
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


def asignacion(
    tienda,
    adapter="codex",
    repo="owner-01/repo-a",
    sha="0" * 40,
    workspace=None,
):
    ref, digest = instrucciones.guardar(
        tienda.dir_de_instrucciones, f"brief de {adapter}"
    )
    revision = {"kind": "code", "repository": repo, "sha": sha}
    if workspace is not None:
        # La llave extra con el checkout local: el contrato fija repository
        # como slug owner/nombre; el directorio donde corre el hijo viaja aquí.
        revision["workspace"] = str(workspace)
    return {
        "target": {"kind": "cli", "hostId": "host-01", "adapterId": adapter},
        "instructionRef": {"ref": ref, "digest": digest},
        "resultContract": "review.v1",
        "inputRevision": revision,
        "continuation": {"kind": "requester"},
    }


def hechos_de(
    tienda,
    adapter="codex",
    repo="owner-01/repo-a",
    sha="0" * 40,
    workspace=None,
):
    return {
        "principal": ["david"],
        "asignaciones": [
            {
                "unitId": "a",
                "assignment": asignacion(tienda, adapter, repo, sha, workspace),
                "relay": False,
            }
        ],
        "claims": [],
        "efectos": [],
    }


def sobre_json(status="completed", summary="Unidad lista", evidence=("hecho 1",)):
    return (
        "```json\n"
        + json.dumps(
            {
                "status": status,
                "summary": summary,
                "evidence": list(evidence),
                "review": None,
            },
            ensure_ascii=False,
        )
        + "\n```"
    )


class RouterDoble:
    """El doble del router: graba la petición y devuelve lo ensayado."""

    def __init__(self, texto=sobre_json(), success=True, adapter="doble", failure=None):
        self.respuesta = self._resultado(texto, success, adapter, failure)
        self.peticiones = []

    @staticmethod
    def _resultado(texto, success, adapter, failure):
        from subscriptions.types import FailureKind, InvocationResult

        return InvocationResult(
            success=success,
            text=texto,
            adapter=adapter,
            model="proveedor/modelo-x",
            billing_mode="subscription",
            failure=FailureKind(failure) if failure else None,
        )

    def __call__(self):
        doble = self

        class Router:
            routing_source = {}

            @staticmethod
            def run(peticion, routes):
                doble.peticiones.append(peticion)
                return doble.respuesta

        return Router(), ()

    def peticion(self):
        return self.peticiones[0] if self.peticiones else None


class Escenario(unittest.TestCase):
    def setUp(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        rastrear_conexiones(self)
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.escenarios = 0

    def repositorio(self, nombre="repo", origen="owner-01/repo-a"):
        """Un repositorio git REAL: la forma del inputRevision que la cadena
        exige ("pass the path exactly as the owner gave it") y la huella de la
        guarda de solo lectura. Con remoto origin (F9: la identidad que el
        hijo contrasta con authority.repos) a nombre de `origen`; None no
        añade remoto — el caso "checkout sin origin"."""
        ruta = Path(self.directorio.name) / f"{nombre}-{self.escenarios}"
        ruta.mkdir()
        subprocess.run(["git", "init", "-q", str(ruta)], check=True)
        subprocess.run(
            ["git", "-C", str(ruta), "config", "user.email", "p@test"], check=True
        )
        subprocess.run(["git", "-C", str(ruta), "config", "user.name", "p"], check=True)
        if origen:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(ruta),
                    "remote",
                    "add",
                    "origin",
                    f"https://github.com/{origen}.git",
                ],
                check=True,
            )
        (ruta / "a.txt").write_text("uno", encoding="utf-8")
        subprocess.run(["git", "-C", str(ruta), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(ruta), "commit", "-q", "-m", "uno"], check=True
        )
        sha = subprocess.run(
            ["git", "-C", str(ruta), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return ruta, sha

    def frescar(self, meta=None):
        self.escenarios += 1
        tienda = store.Store(self.directorio.name + f"/{self.escenarios}.db")
        self.addCleanup(tienda.cerrar)
        tienda.abrir_raiz(meta or meta_de())
        # F9: la raíz de trabajo permitida de ESTA PC de pruebas — el
        # directorio temporal que contiene el store y los checkouts. Sin
        # este archivo el default sería el padre del checkout del plugin
        # (en la PC, C:\dev) y el directorio temporal quedaría fuera.
        raices = Path(self.directorio.name) / "raices"
        raices.mkdir(exist_ok=True)
        (raices / "workspaces.json").write_text(
            json.dumps([self.directorio.name]), encoding="utf-8"
        )
        # El puerto REAL (el doble hermes_cli en proceso): el ejecutor escribe
        # la tarjeta por hermes_cli, así que la tarjeta tiene que vivir en una
        # base de kanban de verdad, no en el doble en memoria.
        self.ruta_kanban = Path(self.directorio.name) / f"kanban-{self.escenarios}.db"
        puerto = KanbanHermes(self.ruta_kanban)
        self.addCleanup(puerto.cerrar)
        return tienda, puerto

    def corer_intento(self, tienda, doble, unit_id="a", attempt=1):
        return ejecutor.correr_intento(
            str(tienda.ruta),
            str(self.ruta_kanban),
            RAIZ,
            unit_id,
            attempt,
            fabrica_router=doble,
        )

    def vuelta(self, tienda, kanban, lanzador=None, hechos=None, permisos=None):
        return ticker.vuelta(
            tienda,
            kanban,
            {RAIZ: hechos if hechos is not None else hechos_de(tienda)},
            permisos_h=permisos or permisos_de_prueba(),
            lanzador=lanzador,
        )

    def encredar(
        self,
        tienda,
        kanban,
        adapter="codex",
        repo="owner-01/repo-a",
        sha="0" * 40,
        workspace=None,
        permisos=None,
    ):
        """Una vuelta con hechos: la unidad a queda admitida con tarjeta."""
        self.vuelta(
            tienda,
            kanban,
            hechos=hechos_de(tienda, adapter, repo, sha, workspace),
            permisos=permisos,
        )
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertIsNotNone(tarjeta)
        return tarjeta


class ElResultadoDelCompletado(Escenario):
    """Puntos 1-2 del encargo: el sobre terminal de la raíz al result JSON que
    el ticker ya sabe reportar, con la evidencia verificable por digest."""

    def test_completado_deja_el_resultado_exacto_y_evidencia_verificable(self):
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        doble = RouterDoble(sobre_json(summary="Levantamiento listo"))
        assignment = tienda.intentos_lanzables()[0]["assignment"]
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "completed")
        ref, digest = resultado["evidenceRef"], resultado["resultDigest"]
        self.assertTrue(ref.startswith("artifact:"))
        # La tarjeta quedó completada con EXACTAMENTE el result del encargo:
        # la evidencia viaja como REFERENCIA {ref, digest} (la forma que
        # _documento_de_resultado pasa tal cual y el contrato valida).
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(
            tarjeta.resultado,
            {
                "resultContract": assignment["resultContract"],
                "resultDigest": digest,
                "verdict": "approved",
                "evidenceRef": {"ref": ref, "digest": digest},
            },
        )
        # La evidencia vive en el store content-addressed y su digest cuadra.
        texto, motivo = instrucciones.resolver(tienda.dir_de_instrucciones, ref, digest)
        self.assertIsNone(motivo)
        documento = json.loads(texto)
        self.assertEqual(documento["summary"], "Levantamiento listo")
        self.assertEqual(documento["evidence"], ["hecho 1"])
        # Y el ticker lo reporta tal cual (payload verdict/evidenceRef).
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertEqual([d["state"] for d in vista["unidades"]], ["admitted"])
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertEqual([d["state"] for d in vista["unidades"]], ["reported"])
        # La forma de la vista: verdict approved y la ref del artefacto.
        self.assertEqual(
            vista["resultados"][0],
            {
                "taskId": tarjeta.id,
                "contrato": "review.v1",
                "verdict": "approved",
                "kind": "produced",
                "ref": ref,
            },
        )

    def test_la_instruccion_lleva_el_brief_y_el_formato_del_sobre(self):
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        doble = RouterDoble()
        self.corer_intento(tienda, doble)
        peticion = doble.peticion()
        self.assertEqual(peticion.role, "codex")
        self.assertIn("brief de codex", peticion.prompt)
        self.assertIn("Exact keys: status", peticion.prompt)
        self.assertIn("closing fence", peticion.prompt)
        self.assertEqual(peticion.start_revision, sha)
        self.assertEqual(peticion.workspace, ruta_repo.resolve())

    def test_sin_workspace_bloquea_la_tarjeta_sin_llamar_al_router(self):
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban)  # slug sin workspace: no hay directorio
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "workspace-unavailable")
        self.assertIsNone(doble.peticion())
        # F1: BLOQUEA, como promete el docstring — nunca queda pendiente
        # quemando la reserva de una llamada por vuelta.
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")

    def test_una_tarjeta_reviewer_completa(self):
        # F2: el sobre del reviewer se juzga con la vara de verifier (review
        # obligada a null, como pide la instrucción) — con la regla del
        # reviewer del parser NINGUNA tarjeta de reviewer completaría jamás.
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar(
            tienda,
            kanban,
            adapter="reviewer",
            sha=sha,
            workspace=ruta_repo,
            permisos=permisos_de_prueba("reviewer", "write", "review"),
        )
        doble = RouterDoble(sobre_json(summary="Sin hallazgos"))
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "completed")
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "completada")
        # El hijo CORRE como reviewer; solo el sobre se juzga como verifier.
        self.assertEqual(doble.peticion().role, "reviewer")


class LosBloqueos(Escenario):
    """Blocked, sobre inválido y router en fallo: block_task con el motivo —
    el ticker ya lo trata como transporte caído."""

    def encredar_real(self, tienda, kanban):
        ruta_repo, sha = self.repositorio()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        return ruta_repo

    def test_bloqueado_del_hijo_bloquea_la_tarjeta(self):
        tienda, kanban = self.frescar()
        self.encredar_real(tienda, kanban)
        doble = RouterDoble(sobre_json(status="blocked", summary="no puedo"))
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "blocked")
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertEqual(
            [i["incident"]["kind"] for i in vista["incidencias"]],
            ["recovery-transport-failure"],
        )

    def test_sobre_invalido_bloquea_la_tarjeta(self):
        tienda, kanban = self.frescar()
        self.encredar_real(tienda, kanban)
        doble = RouterDoble("no soy un sobre json")
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "blocked")
        self.assertIn("envelope", resultado["reason"])
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")

    def test_router_en_fallo_bloquea_la_tarjeta(self):
        tienda, kanban = self.frescar()
        self.encredar_real(tienda, kanban)
        doble = RouterDoble(success=False, adapter="claude", failure="auth")
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "blocked")
        self.assertIn("router failure: auth", resultado["reason"])

    def test_instruccion_ilegible_bloquea_la_tarjeta(self):
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar_real(tienda, kanban)
        # La tarjeta ya existe con su instrucción verificada: ahora el archivo
        # desaparece y el hijo es quien se encuentra con la ausencia.
        asignacion = tienda.intentos_lanzables()[0]["assignment"]
        hex64 = asignacion["instructionRef"]["ref"].split(":", 1)[1]
        ruta_brief = Path(tienda.dir_de_instrucciones) / hex64
        os.chmod(ruta_brief, 0o644)
        ruta_brief.unlink()
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "instruccion-ilegible")
        self.assertIn("instruccion-ausente", resultado["reason"])
        self.assertIsNone(doble.peticion())
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")

    def test_router_ocupado_no_toca_la_tarjeta(self):
        tienda, kanban = self.frescar()
        self.encredar_real(tienda, kanban)
        doble = RouterDoble(success=False, adapter="router", failure="task_failure")
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "busy")
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "pendiente")


class ElWorkspaceContraLaAutoridad(Escenario):
    """F9 (SEGURIDAD): inputRevision.workspace no se confía. Un assignment
    con un repository autorizado y un workspace arbitrario pondría a un
    perfil con permiso de escritura a trabajar FUERA del repo autorizado.
    El hijo exige: checkout git con origin owner/nombre ∈ authority.repos y
    bajo una raíz de trabajo permitida (<raiz>/raices/workspaces.json);
    fuera de eso, bloquea la tarjeta SIN llamar al router y con la reserva
    de vuelta (F8: el desenlace es previo a la llamada)."""

    def test_workspace_de_otro_repo_bloquea_sin_llamar_al_router(self):
        # Checkout REAL de OTRO repo (origin otro/proyecto-b): el slug del
        # assignment (owner-01/repo-a) está autorizado, pero el directorio
        # no es ese repo — no corre.
        ruta_repo, sha = self.repositorio(origen="otro/proyecto-b")
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        reclamo = tienda.reclamar_lanzamiento(RAIZ, "a", 1, "tarjeta-x")
        self.assertFalse(hasattr(reclamo, "codigo"))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "workspace-no-autorizado")
        self.assertIn("owner-01/repo-a", resultado["reason"])
        self.assertIn("otro/proyecto-b", resultado["reason"])
        self.assertIsNone(doble.peticion())
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")
        # F8: la reserva se devolvió — el desenlace fue antes de la llamada.
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))

    def test_workspace_sin_remoto_bloquea(self):
        # Un checkout git sin origin no tiene identidad contrastable: falla
        # cerrada, nunca "confiar porque está en la raíz permitida".
        ruta_repo, sha = self.repositorio(origen=None)
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "workspace-no-autorizado")
        self.assertIn("no git origin", resultado["reason"])
        self.assertIsNone(doble.peticion())
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")

    def test_workspace_fuera_de_la_raiz_de_trabajo_bloquea(self):
        # Repo autorizado (origin correcto) PERO fuera de la raíz de trabajo
        # permitida: el workspaces.json de la PC manda aunque el repo cuadre.
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        otra_raiz = Path(self.directorio.name) / "otra-raiz-permitida"
        otra_raiz.mkdir()
        (Path(tienda.ruta).parent / "raices" / "workspaces.json").write_text(
            json.dumps([str(otra_raiz)]), encoding="utf-8"
        )
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "workspace-no-autorizado")
        self.assertIn("outside the allowed work roots", resultado["reason"])
        self.assertIsNone(doble.peticion())
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")

    def test_workspace_autorizado_completa(self):
        # El bueno: origin que corresponde al slug de la autoridad y bajo la
        # raíz permitida — el hijo corre y la tarjeta completa.
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "completed")
        self.assertEqual(doble.peticion().workspace, ruta_repo.resolve())

    def test_workspaces_json_vacio_niega_todo(self):
        # Falla cerrada: la lista explícita de la PC manda — vacía, nada
        # corre aunque el checkout sea el repo autorizado.
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        (Path(tienda.ruta).parent / "raices" / "workspaces.json").write_text(
            json.dumps([]), encoding="utf-8"
        )
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "workspace-no-autorizado")
        self.assertIsNone(doble.peticion())

    def test_el_slug_de_una_url_de_remoto(self):
        # CR 4310118486 (seguridad): la identidad es HOST + EXACTAMENTE dos
        # segmentos owner/nombre. Un host ajeno no puede hacerse pasar por
        # el repo autorizado y una ruta anidada no se trunca a los últimos
        # dos segmentos.
        resolver = ejecutor._slug_de_url
        # Las formas buenas de un remoto github.com.
        self.assertEqual(
            resolver("https://github.com/owner-01/repo-a.git"), "owner-01/repo-a"
        )
        self.assertEqual(
            resolver("https://github.com/owner-01/repo-a"), "owner-01/repo-a"
        )
        self.assertEqual(
            resolver("git@github.com:owner-01/repo-a.git"), "owner-01/repo-a"
        )
        self.assertEqual(
            resolver("ssh://git@github.com/owner-01/repo-a"), "owner-01/repo-a"
        )
        # Host ajeno: same owner/name, OTRO dueño.
        self.assertIsNone(resolver("https://evil.example/owner-01/repo-a.git"))
        self.assertIsNone(resolver("https://gitlab.com/owner-01/repo-a.git"))
        self.assertIsNone(resolver("git@evil.example:owner-01/repo-a.git"))
        # Ruta anidada: no es un slug de GitHub.
        self.assertIsNone(resolver("https://gitlab.com/attacker/owner-01/repo-a.git"))
        self.assertIsNone(resolver("https://github.com/grupo/sub/repo.git"))
        # Formas fuera del canon: un segmento, esquema raro, ssh con puerto.
        self.assertIsNone(resolver("https://github.com/solo"))
        self.assertIsNone(resolver("gopher://github.com/x/y"))
        self.assertIsNone(resolver("ssh://git@github.com:2222/owner-01/repo-a.git"))

    def test_el_origin_en_mayusculas_cuadra_con_la_autoridad(self):
        # GitHub no distingue mayúsculas: un checkout clonado de
        # https://github.com/owner-01/Repo-A.git ES el repo que la autoridad
        # lista como owner-01/repo-a.
        ruta_repo, sha = self.repositorio(origen="owner-01/Repo-A")
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        doble = RouterDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "completed")


class SoloLectura(Escenario):
    """Un scout que edita el workspace se bloquea igual que en la cadena
    (_edited_by_read_only_stage, local_orchestrator.py:66-80)."""

    def montar_scout(self):
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar(
            tienda,
            kanban,
            adapter="scout",
            sha=sha,
            workspace=ruta_repo,
            permisos=permisos_de_prueba("scout", "write", "implement"),
        )
        return ruta_repo, tienda, kanban

    def test_scout_que_edita_bloquea_la_tarjeta(self):
        ruta_repo, tienda, kanban = self.montar_scout()

        class EditorDoble(RouterDoble):
            def __call__(self):
                router, routes = super().__call__()

                class Editor:
                    @staticmethod
                    def run(peticion, rutas):
                        (ruta_repo / "b.txt").write_text("dos", encoding="utf-8")
                        return router.run(peticion, rutas)

                return Editor(), routes

        doble = EditorDoble()
        resultado = self.corer_intento(tienda, doble)
        self.assertEqual(resultado["outcome"], "blocked")
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")
        self.assertIn("changed the repository", resultado["reason"])

    def test_scout_que_no_edita_completa(self):
        _ruta_repo, tienda, kanban = self.montar_scout()
        resultado = self.corer_intento(tienda, RouterDoble())
        self.assertEqual(resultado["outcome"], "completed")


class ElLanzamientoDelTicker(Escenario):
    """Punto 3: el ticker lanza DESACOPLADO, NUNCA dos por tarjeta, nada para
    dispatch_profiles y nada sin cupo."""

    def grabador(self, nace=True):
        llamadas = []

        def lanzar(root_id, unit_id, attempt, card_id, id_lanzamiento=None):
            llamadas.append((root_id, unit_id, attempt, card_id, id_lanzamiento))
            return {"pid": 1} if nace else None

        return lanzar, llamadas

    def hijo_que_escribe(self, outcome, reason=None):
        """Un lanzador que simula al hijo REAL en lo que al F3 importa: escribe
        SU archivo de resultado (el mismo nombre determinista del lanzador de
        producción, con el lanzamiento_id que recibió — F13) y nace. El
        ticker lo consume la vuelta siguiente."""
        resultados = Path(self.directorio.name) / "raices" / "ejecutor"

        def lanzar(root_id, unit_id, attempt, card_id, id_lanzamiento=None):
            resultados.mkdir(parents=True, exist_ok=True)
            archivo = ejecutor._archivo_de_resultado(
                resultados, root_id, unit_id, attempt
            )
            documento = {
                "rootId": root_id,
                "unitId": unit_id,
                "attempt": attempt,
                "outcome": outcome,
                "lanzamientoId": id_lanzamiento,
            }
            if reason is not None:
                documento["reason"] = reason
            archivo.write_text(json.dumps(documento), encoding="utf-8")
            return {"pid": 1, "resultFile": str(archivo)}

        return lanzar

    def test_dos_vueltas_seguidas_un_solo_lanzamiento(self):
        tienda, kanban = self.frescar()
        lanzar, llamadas = self.grabador()
        primera = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(primera.lanzamientos, 1)
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        # El id del reclamo viaja al lanzador: el hijo lo usa para devolver
        # SU reclamo y ninguno posterior (F11).
        fila = tienda._una_fila("SELECT lanzamiento_id FROM lanzamientos")
        self.assertEqual(llamadas, [(RAIZ, "a", 1, tarjeta.id, fila["lanzamiento_id"])])
        segunda = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(segunda.lanzamientos, 0)
        self.assertEqual(len(llamadas), 1)
        fila = tienda._una_fila("SELECT * FROM lanzamientos")
        self.assertIsNotNone(fila)
        self.assertGreater(fila["vence_at"], fila["claimed_at"])

    def test_reclamo_vencido_relanza(self):
        tienda, kanban = self.frescar()
        lanzar, llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        tienda._conexion.execute(
            "UPDATE lanzamientos SET vence_at = '2000-01-01T00:00:00Z'"
        )
        tienda._conexion.commit()
        segunda = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(segunda.lanzamientos, 1)
        self.assertEqual(len(llamadas), 2)

    def test_especialista_de_dispatch_profiles_no_se_lanza(self):
        tienda, kanban = self.frescar()
        lanzar, llamadas = self.grabador()
        # amazon está en team.json kanban.dispatch_profiles: el despachador
        # nativo lo corre y el ejecutor NO le nace.
        self.assertIn("amazon", ticker.perfiles_de_despacho_nativo())
        permisos_amazon = permisos_de_prueba("amazon", "write", "implement")
        informe = ticker.vuelta(
            tienda,
            kanban,
            {RAIZ: hechos_de(tienda, "amazon")},
            permisos_h=permisos_amazon,
            lanzador=lanzar,
        )
        self.assertIsNotNone(kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1)))
        self.assertEqual(informe.lanzamientos, 0)
        self.assertEqual(llamadas, [])
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))

    def test_sin_presupuesto_no_lanza_y_pregunta_sin_cuota(self):
        tienda, kanban = self.frescar(meta_de(budget=presupuesto_base(maxModelCalls=0)))
        lanzar, llamadas = self.grabador()
        informe = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(informe.lanzamientos, 0)
        self.assertEqual(llamadas, [])
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        preguntas = [e for e in vista["excepciones"] if e["type"] == "SIN_CUOTA"]
        self.assertEqual(len(preguntas), 1)
        sujeto = preguntas[0]["subject"]
        self.assertEqual(sujeto["unitId"], "a")
        self.assertEqual(sujeto["cause"], "budget:maxModelCalls")
        self.assertEqual(
            [o["optionId"] for o in preguntas[0]["options"]],
            ["seguir", "relevo", "cancelar"],
        )
        # Y la pregunta no se duplica en la vuelta siguiente.
        self.vuelta(tienda, kanban, lanzador=lanzar)
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertEqual(
            len([e for e in vista["excepciones"] if e["type"] == "SIN_CUOTA"]), 1
        )

    def test_cada_lanzamiento_reserva_una_llamada(self):
        tienda, kanban = self.frescar()
        lanzar, _llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)

    def test_el_presupuesto_agotado_para_en_el_limite(self):
        # Dos unidades sin dependencias y cupo para UNA llamada: la primera
        # lanza, la segunda pregunta SIN_CUOTA y no nace.
        meta = meta_de(budget=presupuesto_base(maxModelCalls=1))
        meta["plan"]["units"].append(
            {
                "unitId": "b",
                "title": "Unidad b",
                "dependsOn": [],
                "criteria": [
                    {"criterionId": "b1", "text": "b1", "verifier": "verifier"}
                ],
                "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
            }
        )
        tienda, kanban = self.frescar(meta)
        lanzar, llamadas = self.grabador()
        hechos = hechos_de(tienda)
        hechos["asignaciones"].append(
            {
                "unitId": "b",
                "assignment": asignacion(tienda, "codex"),
                "relay": False,
            }
        )
        informe = ticker.vuelta(
            tienda,
            kanban,
            {RAIZ: hechos},
            permisos_h=permisos_de_prueba(),
            lanzador=lanzar,
        )
        self.assertEqual(informe.lanzamientos, 1)
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(llamadas[0][1], "a")
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        vista = tienda.leer_vista(RAIZ, hechos)
        preguntas = [e for e in vista["excepciones"] if e["type"] == "SIN_CUOTA"]
        self.assertEqual(len(preguntas), 1)
        self.assertEqual(preguntas[0]["subject"]["unitId"], "b")
        fila_b = tienda._una_fila("SELECT 1 FROM lanzamientos WHERE unit_id = 'b'")
        self.assertIsNone(fila_b)

    def test_un_lanzamiento_que_no_nace_suelta_reclamo_y_reserva(self):
        tienda, kanban = self.frescar()
        lanzar, _llamadas = self.grabador(nace=False)
        informe = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(informe.lanzamientos, 0)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertEqual(
            [i["incident"]["episodeId"] for i in vista["incidencias"]],
            ["launch-failed"],
        )
        # La vuelta siguiente reintenta (el intento sigue en pie).
        lanzar_bien, llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar_bien)
        self.assertEqual(len(llamadas), 1)

    def test_pausada_no_relanza_un_reclamo_vencido(self):
        tienda, kanban = self.frescar()
        lanzar, llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        tienda.control(
            {
                "schema": "sistema-agentes.control.v1",
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-pausa",
                "expectedControlEpoch": 1,
                "reason": None,
                "issuedAt": "2026-10-17T11:00:00Z",
            }
        )
        tienda._conexion.execute(
            "UPDATE lanzamientos SET vence_at = '2000-01-01T00:00:00Z'"
        )
        tienda._conexion.commit()
        informe = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(informe.lanzamientos, 0)
        self.assertEqual(len(llamadas), 1)

    def test_sin_lanzador_no_lanza_nada(self):
        tienda, kanban = self.frescar()
        informe = self.vuelta(tienda, kanban, lanzador=None)
        self.assertEqual(informe.lanzamientos, 0)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))

    def test_perfiles_de_despacho_nativo_falla_cerrado(self):
        # 4309610867: un team.json ausente, ilegible o sin dispatch_profiles
        # NO es "nadie lo corre el nativo" (lanzaría ejecutores dobles y
        # reservas dobles) sino "no se sabe": None. Solo una lista válida
        # devuelve el frozenset.
        with tempfile.TemporaryDirectory() as base:
            raiz = Path(base)
            self.assertIsNone(ticker.perfiles_de_despacho_nativo(raiz=raiz))
            (raiz / "team.json").write_text("{ no soy json", encoding="utf-8")
            self.assertIsNone(ticker.perfiles_de_despacho_nativo(raiz=raiz))
            (raiz / "team.json").write_text(
                json.dumps({"kanban": {}}), encoding="utf-8"
            )
            self.assertIsNone(ticker.perfiles_de_despacho_nativo(raiz=raiz))
            (raiz / "team.json").write_text(
                json.dumps({"kanban": {"dispatch_profiles": ["amazon"]}}),
                encoding="utf-8",
            )
            self.assertEqual(
                ticker.perfiles_de_despacho_nativo(raiz=raiz),
                frozenset({"amazon"}),
            )

    def test_team_json_ilegible_la_vuelta_no_lanza_nada(self):
        # El centinela en la vuelta: sin lista de despacho nativo, ENTERA la
        # fase de lanzamiento se salta — ni reclamo ni reserva ni hijo.
        tienda, kanban = self.frescar()
        lanzar, llamadas = self.grabador()
        centinela = mock.patch.object(
            ticker, "perfiles_de_despacho_nativo", return_value=None
        )
        centinela.start()
        self.addCleanup(centinela.stop)
        informe = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(informe.lanzamientos, 0)
        self.assertEqual(llamadas, [])
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))

    def test_hijo_que_falla_determinista_bloquea_y_devuelve_la_reserva(self):
        # F3: el resultado del hijo se lee en la vuelta siguiente. Un fallo
        # determinista ANTES de la llamada al modelo (router sin cadena para
        # el perfil) bloquea la tarjeta con su causa y devuelve reclamo y
        # reserva: el bucle de relanzar-quemando-presupuesto se corta.
        tienda, kanban = self.frescar()
        lanzar = self.hijo_que_escribe(
            "router-no-configurado",
            "No model chain configured for profile: codex",
        )
        primera = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(primera.lanzamientos, 1)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        self.assertIsNotNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        segunda = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(segunda.lanzamientos, 0)
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        # El archivo se consumió: la vuelta siguiente no lo reviviría.
        self.assertFalse(archivo_de_resultado(tienda).exists())

    def test_el_hijo_real_que_bloquea_deja_reserva_y_archivo_cuadrados(self):
        # F8 (flujo de producción): el hijo REAL además de escribir su
        # archivo BLOQUEA la tarjeta — la vuelta siguiente concilia la
        # tarjeta bloqueada y cierra el intento por la incidencia de
        # transporte ANTES de que _despachar_ejecutores lea el archivo. Sin
        # el consumo en la conciliación, la reserva quedaba quemada y el
        # archivo huérfano. Aquí el hijo bloquea él mismo y devuelve su
        # reserva (como el hijo real, F8): la conciliación la reintenta de
        # forma idempotente y consume el archivo.
        tienda, kanban = self.frescar()
        lanzar = self.hijo_que_escribe(
            "router-no-configurado",
            "No model chain configured for profile: codex",
        )
        primera = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(primera.lanzamientos, 1)
        # El hijo real: bloquea la tarjeta Y devuelve su reserva — con SU id,
        # el mismo que dejó escrito en su archivo (F13).
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        with kanban.write_txn() as escritura:
            escritura.bloquear(
                tarjeta.id,
                ejecutor.motivo_de_outcome(
                    {"outcome": "router-no-configurado", "reason": "sin cadena"}
                ),
            )
        id_del_hijo = json.loads(
            archivo_de_resultado(tienda).read_text(encoding="utf-8")
        )["lanzamientoId"]
        self.assertTrue(tienda.devolver_lanzamiento(RAIZ, "a", 1, id_del_hijo))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        # La vuelta siguiente: conciliación (tarjeta bloqueada → incidencia
        # de transporte) sin doble devolución ni archivo huérfano.
        self.vuelta(tienda, kanban, lanzador=self.grabador(nace=False)[0])
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertFalse(archivo_de_resultado(tienda).exists())
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertIn(
            "recovery-transport-failure",
            [i["incident"]["kind"] for i in vista["incidencias"]],
        )

    def test_el_archivo_tardio_no_devuelve_la_reserva_del_lanzamiento_nuevo(self):
        # F13: un hijo congelado (PC suspendida más que el TTL, sin llegar al
        # latido) despierta cuando el ticker YA relanzó. Su archivo — nombrado
        # por intento — es del lanzamiento 0: la devolución va SOLO con el
        # lanzamiento_id que el hijo dejó escrito, así que el reclamo del
        # lanzamiento 1 sobrevive y su reserva no baja.
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban)
        primero = tienda.reclamar_lanzamiento(RAIZ, "a", 1, "tarjeta-x")
        id0 = primero.valor
        tienda._conexion.execute(
            "UPDATE lanzamientos SET vence_at = '2000-01-01T00:00:00Z'"
            " WHERE lanzamiento_id = ?",
            (id0,),
        )
        tienda._conexion.commit()
        tienda.limpiar_lanzamientos()
        segundo = tienda.reclamar_lanzamiento(RAIZ, "a", 1, "tarjeta-x")
        id1 = segundo.valor
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 2)
        # El hijo del lanzamiento 0 despierta y escribe SU resultado, con SU id.
        archivo = archivo_de_resultado(tienda)
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text(
            json.dumps(
                {
                    "rootId": RAIZ,
                    "unitId": "a",
                    "attempt": 1,
                    "outcome": "workspace-unavailable",
                    "lanzamientoId": id0,
                }
            ),
            encoding="utf-8",
        )
        lanzar, _llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        # El desenlace es terminal y la tarjeta se cierra; la devolución del
        # hijo 0 NO toca el reclamo del lanzamiento 1 ni su reserva.
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 2)
        fila = tienda._una_fila("SELECT lanzamiento_id FROM lanzamientos")
        self.assertIsNotNone(fila)
        self.assertEqual(fila["lanzamiento_id"], id1)
        cerrada = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(cerrada.estado, "bloqueada")
        self.assertFalse(archivo.exists())

    def test_el_archivo_sin_identidad_no_devuelve_y_se_registra(self):
        # F13: un archivo sin lanzamientoId (hijo anterior, corrupto) no
        # devuelve NADA: el reclamo vivo queda con su reserva, la anomalía
        # queda registrada como incidencia y el archivo no revive la
        # validación en cada vuelta.
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban)
        reclamo = tienda.reclamar_lanzamiento(RAIZ, "a", 1, "tarjeta-x")
        self.assertFalse(hasattr(reclamo, "codigo"))
        archivo = archivo_de_resultado(tienda)
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text(
            json.dumps(
                {
                    "rootId": RAIZ,
                    "unitId": "a",
                    "attempt": 1,
                    "outcome": "workspace-unavailable",
                }
            ),
            encoding="utf-8",
        )
        lanzar, _llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        self.assertIsNotNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertIn(
            "invalid-result", [i["incident"]["kind"] for i in vista["incidencias"]]
        )
        cerrada = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(cerrada.estado, "bloqueada")
        self.assertFalse(archivo.exists())

    def test_sin_driver_devuelve_la_reserva_y_no_toca_la_tarjeta(self):
        # F8: sin el driver de la cadena el hijo no llega ni a tocar la
        # tarjeta, pero la reserva del lanzamiento tampoco se consumió: se
        # devuelve (el relanzamiento tras el TTL reserva la suya limpia).
        tienda, kanban = self.frescar()
        lanzar, _llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        sin_driver = mock.patch.object(ejecutor, "_driver_de_cadena", return_value=None)
        sin_driver.start()
        self.addCleanup(sin_driver.stop)
        resultado = ejecutor.correr_intento(
            str(tienda.ruta), str(self.ruta_kanban), RAIZ, "a", 1
        )
        self.assertEqual(resultado["outcome"], "sin-driver")
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "pendiente")

    def test_desenlace_tras_la_llamada_tras_podar_el_ttl_no_se_relanza(self):
        # F11 (b): una tarjeta YA BLOQUEADA (desenlace posterior a la llamada)
        # no se relanza aunque el reclamo venza y lo pode el TTL: la tarjeta
        # cerrada la maneja decidir (reintento = intento nuevo con tarjeta
        # nueva), nunca un relanzamiento del mismo intento.
        tienda, kanban = self.frescar()
        lanzar, llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        # El hijo real: desenlace "blocked" (POSTERIOR a la llamada: conserva
        # la reserva) + bloquea la tarjeta él mismo.
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        with kanban.write_txn() as escritura:
            escritura.bloquear(tarjeta.id, "unit blocked by codex: no puedo")
        archivo = archivo_de_resultado(tienda)
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text(
            json.dumps(
                {
                    "rootId": RAIZ,
                    "unitId": "a",
                    "attempt": 1,
                    "outcome": "blocked",
                    "lanzamientoId": llamadas[0][4],
                }
            ),
            encoding="utf-8",
        )
        # Con el tope de reintentos agotado, decidir NO crea un intento nuevo:
        # el intento 1 queda vivo (admitted) con su tarjeta bloqueada — el
        # caso del churn real.
        tienda._conexion.execute(
            "INSERT INTO contadores VALUES (?, 'reintentos:a', 1)", (RAIZ,)
        )
        tienda._conexion.commit()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        estado = tienda._una_fila("SELECT state FROM intentos WHERE unit_id = 'a'")
        self.assertEqual(estado["state"], "admitted")
        # El TTL vence y lo poda: sin el gate de tarjeta abierta, aqui
        # renaceria un hijo para una tarjeta ya bloqueada.
        tienda._conexion.execute(
            "UPDATE lanzamientos SET vence_at = '2000-01-01T00:00:00Z'"
        )
        tienda._conexion.commit()
        tercera = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(tercera.lanzamientos, 0)
        self.assertEqual(len(llamadas), 1)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        cerrada = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(cerrada.estado, "bloqueada")

    def test_la_devolucion_tardia_no_borra_el_reclamo_de_otro_lanzamiento(self):
        # F11 (c): el reclamo es de UN lanzamiento. La devolución tardía del
        # hijo del lanzamiento 0 (con el id que recibio) no toca el reclamo
        # del lanzamiento 1, y la del lanzamiento 1 sí. La ruta sin id queda
        # para el ticker, que consume el archivo ANTES de reclamar.
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban)  # el intento admitted que se reclama
        primero = tienda.reclamar_lanzamiento(RAIZ, "a", 1, "tarjeta-x")
        self.assertFalse(hasattr(primero, "codigo"))
        id0 = primero.valor
        # El lanzamiento 0 vence, lo poda el TTL y nace el lanzamiento 1.
        tienda._conexion.execute(
            "UPDATE lanzamientos SET vence_at = '2000-01-01T00:00:00Z'"
            " WHERE lanzamiento_id = ?",
            (id0,),
        )
        tienda._conexion.commit()
        tienda.limpiar_lanzamientos()
        segundo = tienda.reclamar_lanzamiento(RAIZ, "a", 1, "tarjeta-x")
        self.assertFalse(hasattr(segundo, "codigo"))
        id1 = segundo.valor
        self.assertNotEqual(id0, id1)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 2)
        # La devolución TARDÍA del hijo 0: ni borra el reclamo 1 ni toca el
        # contador de él.
        self.assertFalse(tienda.devolver_lanzamiento(RAIZ, "a", 1, id0))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 2)
        fila = tienda._una_fila("SELECT lanzamiento_id FROM lanzamientos")
        self.assertEqual(fila["lanzamiento_id"], id1)
        # La devolución del lanzamiento 1, sí; e idempotente.
        self.assertTrue(tienda.devolver_lanzamiento(RAIZ, "a", 1, id1))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        self.assertFalse(tienda.devolver_lanzamiento(RAIZ, "a", 1, id1))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        # La ruta sin id (el ticker: consume el archivo antes de reclamar)
        # sigue devolviendo el reclamo que haya.
        tercero = tienda.reclamar_lanzamiento(RAIZ, "a", 1, "tarjeta-x")
        self.assertTrue(tienda.devolver_lanzamiento(RAIZ, "a", 1))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        self.assertFalse(hasattr(tercero, "codigo"))

    def test_la_tarjeta_completada_consume_el_archivo_del_hijo(self):
        # F12: el hijo que completa la tarjeta también escribe SU archivo de
        # resultado al salir; la rama completada pasa por _reportar, no por
        # _cerrar_hijo_terminal — sin consumo, <raiz>/raices/ejecutor/
        # acumula un JSON por intento completado.
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        resultado = self.corer_intento(tienda, RouterDoble())
        self.assertEqual(resultado["outcome"], "completed")
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "completada")
        archivo = archivo_de_resultado(tienda)
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text(
            json.dumps(
                {
                    **resultado,
                    "lanzamientoId": "lanzo-de-prueba",
                }
            ),
            encoding="utf-8",
        )
        lanzar, _llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        vista = tienda.leer_vista(RAIZ, hechos_de(tienda))
        self.assertEqual([d["state"] for d in vista["unidades"]], ["reported"])
        self.assertFalse(archivo.exists(), "el archivo del hijo completado queda")

    def test_hijo_con_error_tras_la_llamada_conserva_la_reserva(self):
        # "error" puede llegar después de un run: la reserva NO se devuelve
        # (el suelo cierto del presupuesto), pero la tarjeta sí se cierra con
        # la causa — el reintento a ciegas se acaba.
        tienda, kanban = self.frescar()
        lanzar = self.hijo_que_escribe("error", "RuntimeError: boom")
        self.vuelta(tienda, kanban, lanzador=lanzar)
        self.vuelta(tienda, kanban, lanzador=lanzar)
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(tarjeta.estado, "bloqueada")
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        self.assertIsNotNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        self.assertFalse(archivo_de_resultado(tienda).exists())

    def test_renovar_extiende_sin_tocar_la_reserva_y_un_podado_no_renace(self):
        # F5 (store): el latido del hijo extiende el vencimiento; la reserva
        # se mantiene en UNA y un reclamo ya podado no renace.
        tienda, kanban = self.frescar()
        lanzar, _llamadas = self.grabador()
        self.vuelta(tienda, kanban, lanzador=lanzar)
        antes = tienda._una_fila("SELECT claimed_at, vence_at FROM lanzamientos")
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        renovado = tienda.renovar_lanzamiento(RAIZ, "a", 1, ttl_s=7200)
        self.assertFalse(hasattr(renovado, "codigo"))
        self.assertTrue(renovado.valor)
        despues = tienda._una_fila("SELECT claimed_at, vence_at FROM lanzamientos")
        self.assertEqual(despues["claimed_at"], antes["claimed_at"])
        self.assertGreater(despues["vence_at"], antes["vence_at"])
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        # Devolución idempotente: la primera devuelve, la segunda no toca nada.
        self.assertTrue(tienda.devolver_lanzamiento(RAIZ, "a", 1))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertFalse(tienda.devolver_lanzamiento(RAIZ, "a", 1))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        # Y un reclamo podado no renace.
        self.assertFalse(tienda.renovar_lanzamiento(RAIZ, "a", 1, ttl_s=7200).valor)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))

    def test_el_latido_renueva_el_reclamo_mientras_el_hijo_corre(self):
        # F5 (hijo): una corrida que supera el TTL del reclamo no pierde el
        # reclamo — el latido lo renueva durante el run. Con TTL de 1 s, un
        # router lento espera a ver el vencimiento EXTENDIDO antes de
        # contestar: sin latido, el run termina con el reclamo vencido.
        ruta_repo, sha = self.repositorio()
        tienda, kanban = self.frescar()
        self.encredar(tienda, kanban, sha=sha, workspace=ruta_repo)
        tarjeta = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        with mock.patch.object(store, "TTL_LANZAMIENTO_S", 1):
            reclamo = tienda.reclamar_lanzamiento(RAIZ, "a", 1, tarjeta.id, ttl_s=1)
            self.assertFalse(hasattr(reclamo, "codigo"))
            vence_original = tienda._una_fila("SELECT vence_at FROM lanzamientos")[
                "vence_at"
            ]

            class LentoDoble(RouterDoble):
                def __call__(self):
                    router, rutas = super().__call__()

                    class Lento:
                        @staticmethod
                        def run(peticion, rutas_de):
                            limite = time.monotonic() + 10
                            while time.monotonic() < limite:
                                fila = tienda._una_fila(
                                    "SELECT vence_at FROM lanzamientos"
                                )
                                if fila is not None and fila["vence_at"] > (
                                    vence_original
                                ):
                                    break
                                time.sleep(0.02)
                            return router.run(peticion, rutas_de)

                    return Lento(), rutas

            resultado = self.corer_intento(tienda, LentoDoble())
        self.assertEqual(resultado["outcome"], "completed")
        fila = tienda._una_fila("SELECT vence_at FROM lanzamientos")
        self.assertIsNotNone(fila)
        self.assertGreater(fila["vence_at"], vence_original)


class ElLanzadorDeProduccion(Escenario):
    """F4: el lanzador de producción con un intérprete REAL (sys.executable):
    el bootstrap descomprime el argv, llega a correr_intento y escribe el JSON
    de resultado. Los dobles son de ruta: el kanban es una base temporal que
    lee el MISMO doble hermes_cli (el que hermes_falso instala es el que
    _runtime_de_hermes resuelve) y el escenario sin workspace no llega a
    llamar al router."""

    def test_el_bootstrap_llega_a_correr_intento_y_escribe_el_resultado(self):
        # El lanzador de producción nace apuntando a la disposición real del
        # home (<base>/sistema-agentes.db), así que el escenario se monta en
        # esa disposición, no en la de los escenarios unitarios.
        base = Path(self.directorio.name)
        tienda = store.Store(str(base / "sistema-agentes.db"))
        self.addCleanup(tienda.cerrar)
        tienda.abrir_raiz(meta_de())
        self.ruta_kanban = base / "kanban.db"
        kanban = KanbanHermes(self.ruta_kanban)
        self.addCleanup(kanban.cerrar)
        tarjeta = self.encredar(tienda, kanban)  # slug sin workspace
        # F8: con el reclamo de producción en la mesa (el ticker lo toma
        # ANTES de nacer al hijo), el hijo REAL — este bootstrap con
        # sys.executable — bloquea la tarjeta por el workspace ausente Y
        # DEVUELVE la reserva: su desenlace es previo a la llamada al modelo.
        reclamo = tienda.reclamar_lanzamiento(RAIZ, "a", 1, tarjeta.id)
        self.assertFalse(hasattr(reclamo, "codigo"))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        lanzar = ejecutor.lanzador_de(base, self.ruta_kanban)
        nacido = lanzar(RAIZ, "a", 1, tarjeta.id, reclamo.valor)
        self.assertIsNotNone(nacido)
        archivo = Path(nacido["resultFile"])
        self.assertEqual(
            archivo,
            ejecutor._archivo_de_resultado(base / "raices" / "ejecutor", RAIZ, "a", 1),
        )
        limite = time.monotonic() + 60
        while not archivo.exists() and time.monotonic() < limite:
            time.sleep(0.05)
        self.assertTrue(archivo.exists(), "el hijo no escribió su resultado")
        documento = json.loads(archivo.read_text(encoding="utf-8"))
        self.assertEqual(documento["outcome"], "workspace-unavailable")
        cerrada = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(cerrada.estado, "bloqueada")
        # F8 en el camino REAL: el hijo devolvió su reserva (no solo en el
        # camino de prueba donde el ticker consume el archivo) y el reclamo
        # se fue de la mesa.
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        # Cosechar al hijo detached: sin zombie ni ResourceWarning al salir.
        try:
            os.waitpid(nacido["pid"], 0)
        except ChildProcessError:
            pass

    def test_tres_vueltas_con_el_hijo_real_un_solo_hijo_y_presupuesto_cuadrado(self):
        # F11 (a): el churn que el bloqueo del hijo quería cortar. Con el
        # lanzador de PRODUCCIÓN (sys.executable, sin dobles) y el hijo que
        # bloquea su tarjeta (workspace ausente, desenlace previo a la
        # llamada), tres vueltas del ticker tienen que dejar UN solo hijo
        # lanzado, UN solo reclamo (devuelto por el propio hijo) y el
        # presupuesto cuadrado: la tarjeta cerrada no relanza el intento.
        base = Path(self.directorio.name)
        tienda = store.Store(str(base / "sistema-agentes.db"))
        self.addCleanup(tienda.cerrar)
        tienda.abrir_raiz(meta_de())
        self.ruta_kanban = base / "kanban.db"
        kanban = KanbanHermes(self.ruta_kanban)
        self.addCleanup(kanban.cerrar)
        self.encredar(tienda, kanban)  # slug sin workspace
        # Con el tope de reintentos agotado, decidir no crea un intento nuevo
        # (solo levanta INESPERADO): el intento queda vivo con su tarjeta
        # bloqueada — exactamente el caso del churn que F11 corta.
        tienda._conexion.execute(
            "INSERT INTO contadores VALUES (?, 'reintentos:a', 1)", (RAIZ,)
        )
        tienda._conexion.commit()
        lanzar = ejecutor.lanzador_de(base, self.ruta_kanban)
        archivo = ejecutor._archivo_de_resultado(
            base / "raices" / "ejecutor", RAIZ, "a", 1
        )
        primera = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual(primera.lanzamientos, 1)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 1)
        # El hijo real trabaja desacoplado: esperar a que bloquee la tarjeta
        # y devuelva su reserva antes de la vuelta siguiente.
        limite = time.monotonic() + 60
        while time.monotonic() < limite:
            cerrada = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
            if (
                cerrada is not None
                and cerrada.estado == "bloqueada"
                and tienda.llamadas_modelo(RAIZ) == 0
                and archivo.exists()
            ):
                break
            time.sleep(0.05)
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.cosechar_hijos()
        segunda = self.vuelta(tienda, kanban, lanzador=lanzar)
        tercera = self.vuelta(tienda, kanban, lanzador=lanzar)
        self.assertEqual((segunda.lanzamientos, tercera.lanzamientos), (0, 0))
        self.assertEqual(tienda.llamadas_modelo(RAIZ), 0)
        self.assertIsNone(tienda._una_fila("SELECT * FROM lanzamientos"))
        cerrada = kanban.leer_estado(clave_de_tarjeta(RAIZ, "a", 1))
        self.assertEqual(cerrada.estado, "bloqueada")
        # El archivo del hijo bloqueado lo consumió la conciliación.
        self.assertFalse(archivo.exists())
        self.cosechar_hijos()

    def cosechar_hijos(self):
        """Recolecta los hijos detached que ya salieron (sin zombies)."""
        limite = time.monotonic() + 10
        while time.monotonic() < limite:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                time.sleep(0.05)


class ExtremoAExtremo(Escenario):
    """El camino medido en vivo (raíz e2e-sua07-1018), de punta a punta y sin
    inyectar nada a mano: submit → instruction_put → reconcile (target scout)
    → ticker → ejecutor (router doble) → resultado → despertar pide verify →
    claim → unidad aceptada."""

    def test_del_submit_a_la_aceptacion(self):
        base = Path(self.directorio.name) / "home"
        base.mkdir()
        ruta_repo = base / "repo"
        ruta_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(ruta_repo)], check=True)
        subprocess.run(
            ["git", "-C", str(ruta_repo), "config", "user.email", "p@test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(ruta_repo), "config", "user.name", "p"], check=True
        )
        (ruta_repo / "README.md").write_text("uno", encoding="utf-8")
        subprocess.run(["git", "-C", str(ruta_repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(ruta_repo),
                "remote",
                "add",
                "origin",
                "https://github.com/david/repo-e2e.git",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(ruta_repo), "commit", "-q", "-m", "uno"], check=True
        )
        sha = subprocess.run(
            ["git", "-C", str(ruta_repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        meta = meta_de(repos=("david/repo-e2e",))
        meta["plan"]["units"][0]["scope"] = {
            "repos": ["david/repo-e2e"],
            "mode": "read",
        }
        # La puerta de capacidades abierta (en la Mac no hay gateway vivo) y
        # el paquete poteto del perfil scout (el gate H4b2 del despacho).
        puerta = mock.patch("raices.capacidades.abrir_ruta", return_value=(True, None))
        puerta.start()
        self.addCleanup(puerta.stop)
        # F9: la raíz de trabajo permitida de esta PC de pruebas (el store
        # vive en <base>/sistema-agentes.db, así que la lista va en
        # <base>/raices/workspaces.json y el checkout e2e queda dentro).
        raices_base = base / "raices"
        raices_base.mkdir(parents=True, exist_ok=True)
        (raices_base / "workspaces.json").write_text(
            json.dumps([str(base)]), encoding="utf-8"
        )
        perfil_scout = base / "profiles" / "scout"
        perfil_scout.mkdir(parents=True)
        (perfil_scout / "goncloud-managed.json").write_text(
            json.dumps({"poteto": {"digest": DIGEST}}), encoding="utf-8"
        )

        # 1. submit
        recibo = herramientas.enviar_raiz(
            {"plan": meta}, "claw", home=base, ruta_abierta=lambda: (True, None)
        )
        self.assertNotIn("error", recibo)
        root_id = recibo["rootId"]
        # 2. instruction_put con el brief
        guardada = herramientas.guardar_instruccion(
            {"text": "Levanta el mapa del repo y reporta hallazgos"},
            "claw",
            home=base,
        )
        self.assertNotIn("error", guardada)
        # 3. reconcile con target scout
        recibo = herramientas.reconciliar_raiz(
            {
                "rootId": root_id,
                "asignaciones": [
                    {
                        "unitId": "a",
                        "assignment": {
                            "target": {
                                "kind": "cli",
                                "hostId": "gon-pc",
                                "adapterId": "scout",
                            },
                            "instructionRef": guardada,
                            "resultContract": "review.v1",
                            "inputRevision": {
                                "kind": "code",
                                "repository": "david/repo-e2e",
                                "sha": sha,
                                "workspace": str(ruta_repo),
                            },
                            "continuation": {"kind": "requester"},
                        },
                        "relay": False,
                    }
                ],
            },
            "claw",
            home=base,
        )
        self.assertNotIn("error", recibo)

        # 4. ticker + ejecutor (router doble, el hijo corre EN el proceso)
        tienda = store.Store(base / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        puerto = KanbanHermes(base / "kanban.db")
        self.addCleanup(puerto.cerrar)
        doble = RouterDoble(sobre_json(summary="Mapa levantado"))
        permisos = permisos_de_prueba("scout", "read", "read")

        def lanzar(root, unit, attempt, card, id_lanzamiento=None):
            return {
                "outcome": ejecutor.correr_intento(
                    str(base / "sistema-agentes.db"),
                    str(base / "kanban.db"),
                    root,
                    unit,
                    attempt,
                    fabrica_router=doble,
                    id_lanzamiento=id_lanzamiento,
                )["outcome"]
            }

        informe = ticker.vuelta(
            tienda,
            puerto,
            hechos_por_raiz=tienda.hechos_por_raiz(),
            permisos_h=permisos,
            home=base,
            lanzador=lanzar,
        )
        self.assertEqual(informe.lanzamientos, 1)
        tarjeta = puerto.leer_estado(clave_de_tarjeta(root_id, "a", 1))
        self.assertIsNotNone(tarjeta)
        self.assertEqual(tarjeta.estado, "completada")
        carga = tarjeta.resultado
        self.assertEqual(carga["verdict"], "approved")
        # La petición del hijo llevó el brief, el formato del sobre y el workspace.
        peticion = doble.peticion()
        self.assertEqual(peticion.role, "scout")
        self.assertIn("Levanta el mapa del repo", peticion.prompt)
        self.assertEqual(peticion.workspace, ruta_repo.resolve())
        self.assertEqual(peticion.start_revision, sha)

        # 5. resultado: el ticker lo reporta tal cual
        ticker.vuelta(
            tienda,
            puerto,
            hechos_por_raiz=tienda.hechos_por_raiz(),
            permisos_h=permisos,
            home=base,
            lanzador=lanzar,
        )
        vista = tienda.leer_vista(root_id, tienda.hechos_por_raiz().get(root_id, {}))
        self.assertEqual([d["state"] for d in vista["unidades"]], ["reported"])
        self.assertEqual(
            vista["resultados"][0],
            {
                "taskId": tarjeta.id,
                "contrato": "review.v1",
                "verdict": "approved",
                "kind": "produced",
                "ref": carga["evidenceRef"]["ref"],
            },
        )

        # 6. despertar pide verify
        salida = StringIO()
        with redirect_stdout(salida):
            despertar.correr(home=base)
        self.assertIn("verify unidad=a intento=1 criterios=a1", salida.getvalue())

        # 7. claw verifica y sube el claim
        evidencia = herramientas.guardar_instruccion(
            {"text": "CI verde y revisión del head"}, "claw", home=base
        )
        recibo = herramientas.reconciliar_raiz(
            {
                "rootId": root_id,
                "claims": [
                    {
                        "unitId": "a",
                        "attempt": 1,
                        "criterionId": "a1",
                        "artifactRef": evidencia,
                        "environment": {"host": "gon-pc", "versions": {}},
                        "procedure": "pytest tests/",
                        "outcome": "passed",
                        "at": "2026-10-18T00:00:00Z",
                        "expiresAt": None,
                        "role": "verifier",
                    }
                ],
            },
            "claw",
            home=base,
        )
        self.assertNotIn("error", recibo)

        # 8. la unidad se acepta
        informe = ticker.vuelta(
            tienda,
            puerto,
            hechos_por_raiz=tienda.hechos_por_raiz(),
            permisos_h=permisos,
            home=base,
            lanzador=lanzar,
        )
        self.assertEqual(informe.estado, "done")
        vista = tienda.leer_vista(root_id, {})
        self.assertEqual([d["state"] for d in vista["unidades"]], ["accepted"])
        self.assertEqual(tienda.llamadas_modelo(root_id), 1)


class WorkspaceYForma(Escenario):
    def test_un_slug_no_es_workspace(self):
        self.assertIsNone(ejecutor.workspace_de({"repository": "owner-01/repo-a"}))
        self.assertIsNone(ejecutor.workspace_de({}))
        self.assertIsNone(
            ejecutor.workspace_de({"workspace": "/no/existe/por/ninguna/parte"})
        )

    def test_el_workspace_o_una_ruta_absoluta_si(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(
                ejecutor.workspace_de({"workspace": str(Path(base))}),
                Path(base).resolve(),
            )
            self.assertEqual(
                ejecutor.workspace_de({"repository": str(Path(base))}),
                Path(base).resolve(),
            )

    def test_reviewer_y_perfiles_fuera_de_cadena_usan_la_vara_verifier(self):
        from workflow import INITIAL_STAGES

        self.assertEqual(ejecutor.perfil_de_sobre("scout", INITIAL_STAGES), "scout")
        self.assertEqual(ejecutor.perfil_de_sobre("codex", INITIAL_STAGES), "verifier")
        self.assertEqual(
            ejecutor.perfil_de_sobre("operaciones", INITIAL_STAGES), "verifier"
        )
        self.assertEqual(
            ejecutor.perfil_de_sobre("reviewer", INITIAL_STAGES), "verifier"
        )

    def test_la_instruccion_no_promete_revision_de_reviewer(self):
        texto = ejecutor.instruccion_de_raiz("brief")
        self.assertIn("Use review=null", texto)
        cuerpo = texto.split("Exact keys:")[1]
        self.assertNotIn("reviewer", cuerpo)


if __name__ == "__main__":
    unittest.main()
