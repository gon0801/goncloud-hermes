"""El estado durable de las raíces: sistema-agentes.db (SUA-07 H1).

Único escritor de la base. Cada mutación es UNA transacción BEGIN IMMEDIATE y los
conflictos son respuestas ordinarias (Ok/Conflicto), no excepciones. Los documentos
que espejan el contrato se validan con la copia fijada antes de entrar, y las
transiciones de intento solo pasan por TRANSICIONES del contrato. Entre el commit y
la tarjeta kanban no hay transacción común: la frontera la cierra la bandeja de
salida (tabla bandeja), que el ticker despacha con la clave de idempotencia.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

compartido = importlib.import_module("raices.compartido")
contrato = compartido.contrato
kanban_port = importlib.import_module("raices.kanban_port")
decidir = importlib.import_module("raices.decidir")
autoridad = importlib.import_module("raices.autoridad")
instrucciones = importlib.import_module("raices.instrucciones")

RENOTICE_MS = decidir.RENOTICE_MS
TEXTO_DE_OPCION = decidir.TEXTO_DE_OPCION
digesto = decidir.digesto

ESQUEMA = """
CREATE TABLE IF NOT EXISTS raices (
  root_id          TEXT PRIMARY KEY,
  requester_json   TEXT NOT NULL,
  authority_json   TEXT NOT NULL,
  budget_json      TEXT NOT NULL,
  quality_policy   TEXT NOT NULL,
  politica_json    TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS controles (
  root_id  TEXT NOT NULL REFERENCES raices(root_id),
  epoch    INTEGER NOT NULL CHECK (epoch >= 1),
  state    TEXT NOT NULL CHECK (state IN ('active','paused','cancelled')),
  reason   TEXT,
  command_id TEXT,
  PRIMARY KEY (root_id, epoch)
);
CREATE TABLE IF NOT EXISTS comandos_de_control (
  root_id        TEXT NOT NULL,
  command_id     TEXT NOT NULL,
  command_digest TEXT NOT NULL,
  receipt_json   TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (root_id, command_id)
);
CREATE TABLE IF NOT EXISTS revisiones (
  root_id     TEXT NOT NULL REFERENCES raices(root_id),
  revision    INTEGER NOT NULL CHECK (revision >= 1),
  plan_digest TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  assumptions_json TEXT NOT NULL,
  PRIMARY KEY (root_id, revision)
);
CREATE TABLE IF NOT EXISTS unidades_de_plan (
  root_id     TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  unit_id     TEXT NOT NULL,
  unit_digest TEXT NOT NULL,
  unit_json   TEXT NOT NULL,
  PRIMARY KEY (root_id, revision, unit_id),
  FOREIGN KEY (root_id, revision) REFERENCES revisiones(root_id, revision)
);
CREATE TABLE IF NOT EXISTS intentos (
  root_id        TEXT NOT NULL,
  unit_id        TEXT NOT NULL,
  attempt        INTEGER NOT NULL CHECK (attempt >= 1),
  plan_revision  INTEGER NOT NULL,
  assignment_json TEXT NOT NULL,
  state          TEXT NOT NULL,
  control_epoch  INTEGER NOT NULL,
  card_id        TEXT,
  outcome_ref    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (root_id, unit_id, attempt)
);
CREATE TABLE IF NOT EXISTS decisiones (
  root_id         TEXT NOT NULL,
  decision_id     TEXT NOT NULL,
  decision_digest TEXT NOT NULL,
  receipt_json    TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (root_id, decision_id)
);
CREATE TABLE IF NOT EXISTS resultados (
  root_id     TEXT NOT NULL,
  unit_id     TEXT NOT NULL,
  attempt     INTEGER NOT NULL,
  result_digest TEXT NOT NULL,
  result_json TEXT NOT NULL,
  consumed_at TEXT,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (root_id, unit_id, attempt)
);
CREATE TABLE IF NOT EXISTS incidencias (
  root_id       TEXT NOT NULL,
  unit_id       TEXT NOT NULL,
  attempt       INTEGER NOT NULL,
  incident_key  TEXT NOT NULL,
  incident_json TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (root_id, unit_id, attempt, incident_key)
);
CREATE TABLE IF NOT EXISTS efectos (
  root_id       TEXT NOT NULL,
  effect_id     TEXT NOT NULL,
  unit_id       TEXT,
  intent_json   TEXT NOT NULL,
  state         TEXT NOT NULL
                CHECK (state IN ('prepared','started','confirmed','uncertain')),
  start_id      TEXT,
  started_epoch INTEGER,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (root_id, effect_id)
);
CREATE TABLE IF NOT EXISTS excepciones (
  exception_id      TEXT PRIMARY KEY,
  root_id           TEXT NOT NULL,
  unit_id           TEXT,
  type              TEXT NOT NULL,
  subject_json      TEXT NOT NULL,
  options_json      TEXT NOT NULL,
  evidence_ref_json TEXT,
  recipient_json    TEXT NOT NULL,
  raised_at         TEXT NOT NULL,
  renotice_after_ms INTEGER NOT NULL CHECK (renotice_after_ms >= 3600000),
  noticed_at        TEXT,
  response_json     TEXT,
  applied_by        TEXT
);
CREATE TABLE IF NOT EXISTS evidencia (
  root_id       TEXT NOT NULL,
  evidence_id   TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (root_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS contadores (
  root_id TEXT NOT NULL,
  counter TEXT NOT NULL,
  value   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (root_id, counter)
);
CREATE TABLE IF NOT EXISTS escritores (
  root_id      TEXT NOT NULL,
  resource_ref TEXT NOT NULL,
  unit_id      TEXT NOT NULL,
  PRIMARY KEY (root_id, resource_ref)
);
CREATE TABLE IF NOT EXISTS bloqueos (
  root_id  TEXT NOT NULL,
  card_id  TEXT NOT NULL,
  motivo   TEXT NOT NULL,
  hecho_at TEXT,
  PRIMARY KEY (root_id, card_id)
);
CREATE TABLE IF NOT EXISTS bandeja (
  root_id         TEXT NOT NULL,
  unit_id         TEXT NOT NULL,
  attempt         INTEGER NOT NULL,
  state           TEXT NOT NULL
                  CHECK (state IN ('pendiente','cumplida','anulada')),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at      TEXT NOT NULL,
  fulfilled_at    TEXT,
  PRIMARY KEY (root_id, unit_id, attempt),
  FOREIGN KEY (root_id, unit_id, attempt)
    REFERENCES intentos(root_id, unit_id, attempt)
);
CREATE TABLE IF NOT EXISTS restricciones (
  root_id    TEXT NOT NULL REFERENCES raices(root_id),
  revision   INTEGER NOT NULL CHECK (revision >= 2),
  created_at TEXT NOT NULL,
  PRIMARY KEY (root_id, revision)
);

CREATE TABLE IF NOT EXISTS flotas (
  root_id    TEXT NOT NULL,
  unit_id    TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  flota_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (root_id, unit_id, attempt),
  FOREIGN KEY (root_id, unit_id, attempt)
    REFERENCES intentos(root_id, unit_id, attempt)
);
CREATE TABLE IF NOT EXISTS asignaciones (
  root_id          TEXT NOT NULL REFERENCES raices(root_id),
  plan_revision    INTEGER NOT NULL CHECK (plan_revision >= 1),
  unit_id          TEXT NOT NULL,
  relay            INTEGER NOT NULL CHECK (relay IN (0, 1)),
  assignment_digest TEXT NOT NULL,
  assignment_json  TEXT NOT NULL,
  consumed_at      TEXT,
  consumed_attempt INTEGER,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (root_id, plan_revision, unit_id, relay)
);
CREATE TABLE IF NOT EXISTS necesidades (
  root_id      TEXT NOT NULL REFERENCES raices(root_id),
  needs_digest TEXT NOT NULL,
  needs_json   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (root_id)
);
"""


class Ok(NamedTuple):
    valor: object
    replayed: bool = False


class Conflicto(NamedTuple):
    codigo: str
    detalle: dict | None = None


class ReciboRaiz(NamedTuple):
    root_id: str
    plan_revision: int
    control_epoch: int


class ReciboControl(NamedTuple):
    control_epoch: int
    state: str
    replayed: bool
    committed_effects: list


class ReciboDecision(NamedTuple):
    root_id: str
    decision_id: str
    decision_digest: str
    plan_revision: int
    control_epoch: int
    intentos_creados: list


class ReciboEfecto(NamedTuple):
    effect_id: str
    state: str
    start_id: str | None
    started_epoch: int | None


class ReciboReconcile(NamedTuple):
    root_id: str
    plan_revision: int
    asignaciones: list
    necesidades: list


class Intencion(NamedTuple):
    root_id: str
    unit_id: str
    attempt: int
    idempotency_key: str
    assignment: dict


class Cupos(NamedTuple):
    global_max: int = 3
    por_perfil: dict | None = None


def ahora_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leer_politicas() -> dict:
    documento = contrato.cargar_json(contrato.POLITICA_DE_CALIDAD)
    return documento.get("versiones", {}) if isinstance(documento, dict) else {}


def _a_json(partes: object) -> str:
    return json.dumps(partes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def serializar_necesidades(necesidades) -> list:
    """El espejo de ControllerNeed de R: las necesidades de decidir en la
    forma que el recibo de reconcile y goncloud_root_read exponen."""
    serializadas = []
    for necesidad in necesidades:
        if isinstance(necesidad, decidir.NecesidadVerificacion):
            serializadas.append(
                {
                    "kind": "verify",
                    "unitId": necesidad.unit_id,
                    "attempt": necesidad.attempt,
                    "criteria": list(necesidad.criterios),
                }
            )
        else:
            serializadas.append(
                {
                    "kind": "assignment",
                    "unitId": necesidad.unit_id,
                    "reason": necesidad.razon,
                    "findingsRef": necesidad.findings_ref,
                    "findingsRefs": list(necesidad.findings_refs),
                }
            )
    return serializadas


def _perfil_de(assignment: dict) -> str:
    target = assignment.get("target") or {}
    return str(target.get("agentId") or target.get("adapterId") or "sin-perfil")


def _repo_de(assignment: dict) -> str | None:
    revision = assignment.get("inputRevision") or {}
    repo = revision.get("repository")
    return f"repo:{repo}" if repo else None


class Store:
    """Una conexión escritora a sistema-agentes.db. Varios Store sobre el mismo
    archivo se serializan con BEGIN IMMEDIATE (dos tickers, I03/I20)."""

    def __init__(
        self,
        ruta,
        cupos: Cupos | None = None,
        reloj=ahora_utc,
        permisos_h: dict | None = None,
    ):
        self.ruta = Path(ruta)
        cupos = cupos or Cupos()
        self.cupos = Cupos(cupos.global_max, dict(cupos.por_perfil or {}))
        self.reloj = reloj
        self.permisos_h_datos = permisos_h
        self.permisos_h = autoridad.PermisosH(permisos_h)
        self._conexion = sqlite3.connect(
            self.ruta,
            isolation_level=None,
            timeout=10,
            check_same_thread=False,
        )
        self._conexion.row_factory = sqlite3.Row
        self._conexion.execute("PRAGMA journal_mode=WAL")
        self._conexion.execute("PRAGMA busy_timeout=10000")
        self._conexion.execute("PRAGMA foreign_keys=ON")
        self._conexion.executescript(ESQUEMA)

    def cerrar(self):
        self._conexion.close()

    def _begin(self):
        self._conexion.execute("BEGIN IMMEDIATE")

    def _commit(self):
        self._conexion.execute("COMMIT")

    def _rollback(self):
        self._conexion.execute("ROLLBACK")

    def _criterio_cubierto(
        self, root_id, claims, unit_id, attempt, criterion_id, revision, ahora
    ) -> bool:
        excluye = None
        autores: set = set()
        politica = json.loads(
            self._una_fila(
                "SELECT politica_json FROM raices WHERE root_id = ?", (root_id,)
            )["politica_json"]
        )
        if politica.get("revisorExcluyeAutores"):
            for fila in self._conexion.execute(
                "SELECT assignment_json FROM intentos"
                " WHERE root_id = ? AND unit_id = ?",
                (root_id, unit_id),
            ):
                documento = json.loads(fila["assignment_json"])
                if isinstance(documento, dict):
                    autores.add(_perfil_de(documento))
            excluye = autores
        vigente = decidir.claim_vigente(
            claims, root_id, revision, unit_id, attempt, criterion_id, ahora, excluye
        )
        if vigente is None or vigente["outcome"] != "passed":
            return False
        if vigente["verifier"]["role"] == "owner":
            ref = (vigente.get("artifactRef") or {}).get("ref") or ""
            if not ref.startswith("exception:"):
                return False
            respuesta = self._una_fila(
                "SELECT response_json FROM excepciones"
                " WHERE exception_id = ? AND response_json IS NOT NULL",
                (ref[len("exception:") :],),
            )
            return respuesta is not None
        return True

    def _una_fila(self, sql, parametros=()):
        return self._conexion.execute(sql, parametros).fetchone()

    def _documento_excepcion(self, root_id: str, borrador: dict, ahora: str) -> dict:
        requester = json.loads(
            self._una_fila(
                "SELECT requester_json FROM raices WHERE root_id = ?", (root_id,)
            )["requester_json"]
        )
        return {
            "schema": "sistema-agentes.excepcion.v1",
            "exceptionId": borrador["exceptionId"],
            "rootId": root_id,
            "unitId": borrador["subject"].get("unitId"),
            "type": borrador["type"],
            "options": [
                {"optionId": opcion, "text": TEXTO_DE_OPCION[opcion]}
                for opcion in borrador["options"]
            ],
            "evidenceRef": borrador.get("evidenceRef"),
            "recipient": {
                "channel": "telegram",
                "principal": requester.get("principal", []),
            },
            "raisedAt": ahora,
            "renoticeAfterMs": RENOTICE_MS,
            "response": None,
        }

    def _insertar_excepcion(
        self, root_id: str, borrador: dict, documento: dict, ahora: str
    ):
        self._conexion.execute(
            "INSERT OR IGNORE INTO excepciones"
            " (exception_id, root_id, unit_id, type, subject_json, options_json,"
            "  evidence_ref_json, recipient_json, raised_at, renotice_after_ms,"
            "  noticed_at, response_json, applied_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
            (
                borrador["exceptionId"],
                root_id,
                documento["unitId"],
                documento["type"],
                _a_json(borrador["subject"]),
                _a_json(documento["options"]),
                _a_json(borrador["evidenceRef"])
                if borrador.get("evidenceRef")
                else None,
                _a_json(documento["recipient"]),
                ahora,
                RENOTICE_MS,
            ),
        )

    def _levantar_excepcion(
        self, root_id: str, tipo: str, sujeto: dict, opciones: list, ahora: str
    ) -> str:
        """La excepción de un gate propio del store (autoridad/permisos de H).
        Va dentro de la transacción del llamante y deduplica por exceptionId."""
        borrador = decidir.borrador_excepcion(root_id, tipo, sujeto, opciones)
        documento = self._documento_excepcion(root_id, borrador, ahora)
        hallazgos = contrato.validar(documento)
        if hallazgos:
            raise ValueError(f"excepción interna inválida: {hallazgos}")
        self._insertar_excepcion(root_id, borrador, documento, ahora)
        return borrador["exceptionId"]

    def _restriccion_vigente(self, root_id: str) -> str:
        """El momento de la última restricción de la autoridad de la raíz: la
        marca más nueva de `restricciones` o, sin ninguna, la apertura (nadie
        reescribe authority_json después de abrir_raiz; una restricción futura
        deja su marca ahí, como managed_root_authority en R)."""
        fila = self._una_fila(
            "SELECT MAX(created_at) AS ultima FROM restricciones WHERE root_id = ?",
            (root_id,),
        )
        if fila is not None and fila["ultima"]:
            return fila["ultima"]
        return self._una_fila(
            "SELECT created_at FROM raices WHERE root_id = ?", (root_id,)
        )["created_at"]

    def _concesiones_de(self, root_id: str) -> frozenset:
        """Los effectId concedidos por el dueño (SUA-02 «Conceder un efecto»,
        como effectGranted en R): una NECESITA_DAVID sobre ese efecto
        respondida `autorizar` DESPUÉS de la última restricción. Concede ese
        effectId y ningún otro; una respuesta anterior a la restricción no
        vale, y una respuesta sin forma no concede (falla cerrado)."""
        try:
            restriccion = decidir.instante(self._restriccion_vigente(root_id))
        except ValueError:
            return frozenset()
        concedidos = set()
        for fila in self._conexion.execute(
            "SELECT subject_json, response_json FROM excepciones"
            " WHERE root_id = ? AND type = 'NECESITA_DAVID'"
            " AND response_json IS NOT NULL",
            (root_id,),
        ):
            try:
                sujeto = json.loads(fila["subject_json"])
                respuesta = json.loads(fila["response_json"])
            except ValueError:
                continue
            if not isinstance(sujeto, dict) or not isinstance(respuesta, dict):
                continue
            if (
                sujeto.get("kind") != "effect"
                or respuesta.get("optionId") != "autorizar"
            ):
                continue
            if not isinstance(sujeto.get("effectId"), str):
                continue
            try:
                respondido = decidir.instante(respuesta["at"])
            except (KeyError, ValueError, TypeError):
                continue
            if respondido > restriccion:
                concedidos.add(sujeto["effectId"])
        return frozenset(concedidos)

    def _razon_de_autoridad(
        self, root_id: str, effect_id: str, efecto: dict, ahora: str
    ) -> str | None:
        """Primera razón por la que el efecto no procede: forma, contrato o H.
        Una concesión del dueño sobre ESTE effectId posterior a la última
        restricción la calla: el efecto inicia igual (F8)."""
        autoridad_meta = json.loads(
            self._una_fila(
                "SELECT authority_json FROM raices WHERE root_id = ?", (root_id,)
            )["authority_json"]
        )
        negar = autoridad.interseccion(autoridad_meta, self.permisos_h, ahora)
        razon = negar(efecto)
        if razon is None:
            return None
        # C1: la concesión calla lo que el dueño puede conceder (autoridad o
        # permisos de H), nunca una razón de forma: un efecto mal formado no
        # procede aunque el dueño lo haya autorizado.
        if razon in autoridad.RAZONES_DE_FORMA:
            return razon
        if effect_id in self._concesiones_de(root_id):
            return None
        return razon

    # F15: un sujeto de INTENTO no ofrece `autorizar`; la concesión del dueño
    # solo existe para efectos (F8), igual que permission-required en R
    # (managed-root.decide.ts:550).
    OPCIONES_DE_INTENTO = ["listo", "omitir", "cancelar"]

    def _revisar_permisos_al_reanudar(self, root_id: str, ahora: str) -> None:
        """Punto 2 de la intersección (I07): al reanudar, lo que va a correr se
        vuelve a contrastar con el contrato y con H; lo denegado levanta su
        excepción y no recupera el permiso de inicio. La concesión de un
        efecto (F8) se respeta: la respuesta `autorizar` del dueño sigue
        valiendo si ninguna restricción posterior la anuló."""
        for fila in self._conexion.execute(
            "SELECT effect_id, intent_json FROM efectos"
            " WHERE root_id = ? AND state = 'prepared'",
            (root_id,),
        ):
            intent = json.loads(fila["intent_json"])
            razon = self._razon_de_autoridad(root_id, fila["effect_id"], intent, ahora)
            if razon is not None:
                self._levantar_excepcion(
                    root_id,
                    "NECESITA_DAVID",
                    {
                        "kind": "effect",
                        "effectId": fila["effect_id"],
                        "cause": autoridad.causa_de(razon),
                    },
                    # r6f: las opciones por razón, la misma tabla de los
                    # cuatro caminos (una razón de forma no se autoriza).
                    autoridad.opciones_por_razon(razon),
                    ahora,
                )
        autoridad_meta = json.loads(
            self._una_fila(
                "SELECT authority_json FROM raices WHERE root_id = ?", (root_id,)
            )["authority_json"]
        )
        for fila in self._conexion.execute(
            "SELECT b.unit_id, b.attempt, i.assignment_json FROM bandeja b"
            " JOIN intentos i ON i.root_id = b.root_id AND i.unit_id = b.unit_id"
            " AND i.attempt = b.attempt"
            " WHERE b.root_id = ? AND b.state = 'pendiente'",
            (root_id,),
        ):
            unidad_plan = self._una_fila(
                "SELECT unit_json FROM unidades_de_plan WHERE root_id = ?"
                " AND revision = (SELECT MAX(revision) FROM revisiones"
                " WHERE root_id = ?) AND unit_id = ?",
                (root_id, root_id, fila["unit_id"]),
            )
            razon = autoridad.negar_arranque(
                autoridad_meta,
                self.permisos_h,
                ahora,
                json.loads(fila["assignment_json"]),
                json.loads(unidad_plan["unit_json"]) if unidad_plan else None,
            )
            if razon is not None:
                self._levantar_excepcion(
                    root_id,
                    "NECESITA_DAVID",
                    {
                        "kind": "attempt",
                        "unitId": fila["unit_id"],
                        "attempt": fila["attempt"],
                        "cause": autoridad.causa_de(razon),
                    },
                    self.OPCIONES_DE_INTENTO,
                    ahora,
                )

    # -- apertura ---------------------------------------------------------

    def abrir_raiz(self, meta: dict, politicas: dict | None = None) -> object:
        politicas = politicas if politicas is not None else leer_politicas()
        version = meta.get("qualityPolicy", {}).get("version")
        if version not in politicas:
            return Conflicto("documento-invalido", {"regla": "quality-policy-unknown"})
        hallazgos = contrato.validar_plan(meta)
        if hallazgos:
            return Conflicto("documento-invalido", {"hallazgos": _a_json(hallazgos)})
        ahora = self.reloj()
        self._begin()
        try:
            existente = self._una_fila(
                "SELECT plan_digest FROM raices r JOIN revisiones v"
                " ON v.root_id = r.root_id"
                " WHERE r.root_id = ? ORDER BY v.revision DESC LIMIT 1",
                (meta["rootId"],),
            )
            if existente is not None:
                self._rollback()
                if existente["plan_digest"] == meta["plan"]["planDigest"]:
                    return Ok(
                        ReciboRaiz(meta["rootId"], meta["plan"]["revision"], 1), True
                    )
                return Conflicto("operation-conflict")
            self._conexion.execute(
                "INSERT INTO raices VALUES (?,?,?,?,?,?,?)",
                (
                    meta["rootId"],
                    _a_json(meta["requester"]),
                    _a_json(meta["authority"]),
                    _a_json(meta["budget"]),
                    version,
                    _a_json(politicas[version]),
                    ahora,
                ),
            )
            self._conexion.execute(
                "INSERT INTO revisiones VALUES (?,?,?,?,?)",
                (
                    meta["rootId"],
                    meta["plan"]["revision"],
                    meta["plan"]["planDigest"],
                    meta["plan"]["requestDigest"],
                    _a_json(meta["plan"]["assumptions"]),
                ),
            )
            for unidad_plan in meta["plan"]["units"]:
                self._conexion.execute(
                    "INSERT INTO unidades_de_plan VALUES (?,?,?,?,?)",
                    (
                        meta["rootId"],
                        meta["plan"]["revision"],
                        unidad_plan["unitId"],
                        digesto(unidad_plan),
                        _a_json(unidad_plan),
                    ),
                )
            self._conexion.execute(
                "INSERT INTO controles VALUES (?,?,?,?,?)",
                (meta["rootId"], 1, "active", None, None),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(ReciboRaiz(meta["rootId"], meta["plan"]["revision"], 1))

    # -- control ----------------------------------------------------------

    def control(self, comando: dict) -> object:
        copia = {k: v for k, v in comando.items() if k != "receipt"}
        hallazgos = contrato.validar(copia)
        if hallazgos:
            return Conflicto("documento-invalido", {"hallazgos": _a_json(hallazgos)})
        root_id = comando["rootId"]
        command_id = comando["commandId"]
        contenido = {
            k: v
            for k, v in comando.items()
            if k not in ("schema", "commandId", "receipt", "issuedAt")
        }
        digest = digesto(contenido)
        ahora = self.reloj()
        self._begin()
        try:
            guardado = self._una_fila(
                "SELECT command_digest, receipt_json FROM comandos_de_control"
                " WHERE root_id = ? AND command_id = ?",
                (root_id, command_id),
            )
            if guardado is not None:
                if guardado["command_digest"] != digest:
                    self._rollback()
                    return Conflicto("operation-conflict")
                recibo = json.loads(guardado["receipt_json"])
                self._rollback()
                return Ok(
                    ReciboControl(
                        recibo["controlEpoch"],
                        recibo["state"],
                        True,
                        recibo["committedEffects"],
                    ),
                    True,
                )
            vigente = self._una_fila(
                "SELECT epoch, state FROM controles WHERE root_id = ?"
                " ORDER BY epoch DESC LIMIT 1",
                (root_id,),
            )
            if vigente is None:
                self._rollback()
                return Conflicto("raiz-desconocida")
            epoch, state = vigente["epoch"], vigente["state"]
            if comando["expectedControlEpoch"] != epoch:
                self._rollback()
                return Conflicto("stale-control-epoch", {"actual": epoch})
            orden = comando["command"]
            admite = {
                "pause": ("active",),
                "resume": ("paused",),
                "cancel": ("active", "paused"),
            }
            if state not in admite[orden]:
                self._rollback()
                return Conflicto("transition-refused", {"state": state})
            efectos_comprometidos = self._efectos_comprometidos(root_id)
            if orden == "cancel":
                self._cancelar_intentos(root_id, command_id, ahora)
            if orden == "resume":
                self._revisar_permisos_al_reanudar(root_id, ahora)
            recibo = self._asentar_control(
                root_id,
                orden,
                epoch,
                command_id,
                comando.get("reason"),
                ahora,
                efectos_comprometidos,
                digest,
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(recibo)

    def _efectos_comprometidos(self, root_id) -> list:
        committed = self._conexion.execute(
            "SELECT effect_id, intent_json, state"
            " FROM efectos WHERE root_id = ? AND state IN ('started','uncertain')"
            " ORDER BY created_at, effect_id",
            (root_id,),
        ).fetchall()
        efectos = []
        for fila in committed:
            intent = json.loads(fila["intent_json"])
            efectos.append(
                {
                    "effectId": fila["effect_id"],
                    "destination": intent.get("destination") or intent["action"],
                    "state": fila["state"],
                    "cancellable": bool(intent.get("cancellable", True)),
                }
            )
        return efectos

    def _cancelar_intentos(self, root_id, command_id, ahora) -> None:
        vivos = self._conexion.execute(
            "SELECT unit_id, attempt, card_id FROM intentos"
            " WHERE root_id = ? AND outcome_ref IS NULL",
            (root_id,),
        ).fetchall()
        for fila in vivos:
            self._conexion.execute(
                "UPDATE intentos SET state = 'cancelled', outcome_ref = ?,"
                " updated_at = ? WHERE root_id = ? AND unit_id = ?"
                " AND attempt = ? AND outcome_ref IS NULL",
                (
                    f"control:{command_id}",
                    ahora,
                    root_id,
                    fila["unit_id"],
                    fila["attempt"],
                ),
            )
            if fila["card_id"] is not None:
                self._conexion.execute(
                    "INSERT OR IGNORE INTO bloqueos VALUES (?,?,?,NULL)",
                    (root_id, fila["card_id"], f"cancel:{command_id}"),
                )
        self._conexion.execute("DELETE FROM escritores WHERE root_id = ?", (root_id,))
        self._conexion.execute(
            "UPDATE bandeja SET state = 'anulada', fulfilled_at = ?"
            " WHERE root_id = ? AND state = 'pendiente'",
            (ahora, root_id),
        )

    def _asentar_control(
        self, root_id, orden, epoch, command_id, reason, ahora, efectos, digest
    ) -> ReciboControl:
        estado = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[orden]
        self._conexion.execute(
            "INSERT INTO controles VALUES (?,?,?,?,?)",
            (root_id, epoch + 1, estado, reason, command_id),
        )
        recibo = ReciboControl(epoch + 1, estado, False, efectos)
        self._conexion.execute(
            "INSERT INTO comandos_de_control VALUES (?,?,?,?,?)",
            (
                root_id,
                command_id,
                digest,
                _a_json(
                    {
                        "controlEpoch": recibo.control_epoch,
                        "state": recibo.state,
                        "replayed": False,
                        "committedEffects": recibo.committed_effects,
                    }
                ),
                ahora,
            ),
        )
        return recibo

    # -- decisión ---------------------------------------------------------

    @staticmethod
    def _forma_de_decision(decision):
        """La forma mínima de las listas que aplicar desempaqueta por llave,
        antes de tocar la base (F20): lo que no es un mapa, o le falta su
        llave, es documento-invalido y no una excepción en la transacción."""
        hallazgos = []
        for seccion, llaves in (
            ("claims", ()),
            ("bindings", ("unitId",)),
            ("outcomes", ("unitId", "attempt", "outcome")),
        ):
            for elemento in decision.get(seccion) or ():
                if (
                    not isinstance(elemento, Mapping)
                    or any(llave not in elemento for llave in llaves)
                    or (
                        seccion == "bindings"
                        and not isinstance(elemento.get("assignment"), Mapping)
                    )
                ):
                    hallazgos.append(
                        {"seccion": seccion, "regla": "elemento-sin-forma"}
                    )
                    break
        for intent in decision.get("efectos") or ():
            if not isinstance(intent, Mapping) or not isinstance(
                intent.get("effectId"), str
            ):
                hallazgos.append({"seccion": "efectos", "regla": "elemento-sin-forma"})
                break
        return hallazgos

    def aplicar(self, root_id: str, decision: dict, decision_id: str) -> object:
        forma = self._forma_de_decision(decision)
        if forma:
            return Conflicto("documento-invalido", {"hallazgos": _a_json(forma)})
        digest = digesto(decision)
        self._begin()
        try:
            guardada = self._una_fila(
                "SELECT decision_digest, receipt_json FROM decisiones"
                " WHERE root_id = ? AND decision_id = ?",
                (root_id, decision_id),
            )
            if guardada is not None:
                if guardada["decision_digest"] != digest:
                    self._rollback()
                    return Conflicto("operation-conflict")
                self._rollback()
                return Ok(ReciboDecision(**json.loads(guardada["receipt_json"])), True)
            vigente = self._una_fila(
                "SELECT epoch, state FROM controles WHERE root_id = ?"
                " ORDER BY epoch DESC LIMIT 1",
                (root_id,),
            )
            if vigente is None:
                self._rollback()
                return Conflicto("raiz-desconocida")
            epoch, state = vigente["epoch"], vigente["state"]
            if decision["expected_control_epoch"] != epoch:
                self._rollback()
                return Conflicto("stale-control-epoch", {"actual": epoch})
            revision_fila = self._una_fila(
                "SELECT MAX(revision) AS revision FROM revisiones WHERE root_id = ?",
                (root_id,),
            )
            revision = revision_fila["revision"]
            if decision["expected_plan_revision"] != revision:
                self._rollback()
                return Conflicto("stale-plan-revision", {"actual": revision})
            hay_trabajo = any(
                decision.get(llave)
                for llave in (
                    "consumidos",
                    "claims",
                    "outcomes",
                    "bindings",
                    "efectos",
                    "contadores",
                    "excepciones",
                    "respuestas_aplicadas",
                    "control",
                )
            )
            if hay_trabajo and state != "active":
                self._rollback()
                return Conflicto("root-not-active", {"state": state})
            ahora = self.reloj()
            unidades = {
                fila["unit_id"]: json.loads(fila["unit_json"])
                for fila in self._conexion.execute(
                    "SELECT unit_id, unit_json FROM unidades_de_plan"
                    " WHERE root_id = ? AND revision = ?",
                    (root_id, revision),
                )
            }
            # Los claims de la decisión se validan antes de la guarda de
            # aceptación: uno sin forma es documento-invalido, no un KeyError
            # dentro de la transacción (F16, I13).
            documentos_de_claims = []
            for claim_nuevo in decision["claims"]:
                documento = {
                    **claim_nuevo,
                    "schema": "sistema-agentes.evidencia.v1",
                }
                if documento.get("rootId") != root_id:
                    self._rollback()
                    return Conflicto(
                        "claim-ajeno", {"evidenceId": documento.get("evidenceId")}
                    )
                hallazgos = contrato.validar(documento)
                if hallazgos:
                    self._rollback()
                    return Conflicto(
                        "documento-invalido", {"hallazgos": _a_json(hallazgos)}
                    )
                documentos_de_claims.append(documento)
            # Guarda de borde: una aceptación exige claims passed por cada criterio
            # vigente, aunque la propuesta no venga de decidir (I13). El orden es el
            # mismo de decidir: evidencia guardada primero y la decisión después,
            # así gana el claim más nuevo (B13).
            claims = [
                json.loads(fila["evidence_json"])
                for fila in self._conexion.execute(
                    "SELECT evidence_json FROM evidencia WHERE root_id = ? ORDER BY rowid",
                    (root_id,),
                )
            ]
            claims.extend(decision["claims"])
            for desenlace in decision["outcomes"]:
                if desenlace["outcome"] != "accepted":
                    continue
                unidad_plan = unidades.get(desenlace["unitId"])
                if unidad_plan is None:
                    self._rollback()
                    return Conflicto(
                        "criterion-removed",
                        {"unitId": desenlace["unitId"]},
                    )
                for criterio in unidad_plan["criteria"]:
                    cubierto = self._criterio_cubierto(
                        root_id,
                        claims,
                        desenlace["unitId"],
                        desenlace["attempt"],
                        criterio["criterionId"],
                        revision,
                        ahora,
                    )
                    if not cubierto:
                        self._rollback()
                        return Conflicto(
                            "criteria-unproven",
                            {
                                "unitId": desenlace["unitId"],
                                "criterionId": criterio["criterionId"],
                            },
                        )
            for desenlace in decision["outcomes"]:
                fila_intento = self._una_fila(
                    "SELECT state FROM intentos"
                    " WHERE root_id = ? AND unit_id = ? AND attempt = ?",
                    (root_id, desenlace["unitId"], desenlace["attempt"]),
                )
                if fila_intento is None or not contrato.transicion_permitida(
                    fila_intento["state"], desenlace["outcome"]
                ):
                    self._rollback()
                    return Conflicto(
                        "outcome-refused",
                        {
                            "unitId": desenlace["unitId"],
                            "state": (fila_intento["state"] if fila_intento else None),
                        },
                    )
                cambio = self._conexion.execute(
                    "UPDATE intentos SET state = ?, outcome_ref = ?, updated_at = ?"
                    " WHERE root_id = ? AND unit_id = ? AND attempt = ?"
                    " AND outcome_ref IS NULL",
                    (
                        desenlace["outcome"],
                        f"dec:{decision_id}",
                        ahora,
                        root_id,
                        desenlace["unitId"],
                        desenlace["attempt"],
                    ),
                )
                if cambio.rowcount != 1:
                    self._rollback()
                    return Conflicto(
                        "outcome-refused",
                        {
                            "unitId": desenlace["unitId"],
                            "attempt": desenlace["attempt"],
                        },
                    )
                self._conexion.execute(
                    "UPDATE bandeja SET state = 'anulada', fulfilled_at = ?"
                    " WHERE root_id = ? AND unit_id = ? AND attempt = ?"
                    " AND state = 'pendiente'",
                    (ahora, root_id, desenlace["unitId"], desenlace["attempt"]),
                )
                if desenlace["outcome"] in ("accepted", "failed", "cancelled"):
                    vivos = self._conexion.execute(
                        "SELECT assignment_json FROM intentos"
                        " WHERE root_id = ? AND unit_id != ? AND outcome_ref IS NULL",
                        (root_id, desenlace["unitId"]),
                    ).fetchall()
                    recursos = {
                        _repo_de(json.loads(fila["assignment_json"])) for fila in vivos
                    }
                    recursos.discard(None)
                    if recursos:
                        marcadores = ",".join("?" for _ in recursos)
                        self._conexion.execute(
                            "DELETE FROM escritores WHERE root_id = ?"
                            f" AND resource_ref NOT IN ({marcadores})",
                            (root_id, *recursos),
                        )
                    else:
                        self._conexion.execute(
                            "DELETE FROM escritores WHERE root_id = ?", (root_id,)
                        )
            vivos_raiz = self._una_fila(
                "SELECT COUNT(*) AS n FROM intentos WHERE root_id = ?"
                " AND outcome_ref IS NULL",
                (root_id,),
            )["n"]
            vivos_globales = self._una_fila(
                "SELECT COUNT(*) AS n FROM intentos WHERE outcome_ref IS NULL"
            )["n"]
            presupuesto_raiz = json.loads(
                self._una_fila(
                    "SELECT budget_json FROM raices WHERE root_id = ?", (root_id,)
                )["budget_json"]
            )["maxConcurrentTasks"]
            perfiles_globales: dict = {}
            for fila in self._conexion.execute(
                "SELECT assignment_json FROM intentos WHERE outcome_ref IS NULL"
            ):
                perfil = _perfil_de(json.loads(fila["assignment_json"]))
                perfiles_globales[perfil] = perfiles_globales.get(perfil, 0) + 1
            escritores_globales: dict = {}
            for fila in self._conexion.execute(
                "SELECT root_id, resource_ref FROM escritores"
            ):
                escritores_globales[fila["resource_ref"]] = fila["root_id"]
            vivos_raiz_despues = vivos_raiz
            vivos_globales_despues = vivos_globales
            perfiles_despues = dict(perfiles_globales)
            for enlace in decision["bindings"]:
                assignment = enlace["assignment"]
                perfil = _perfil_de(assignment)
                vivos_raiz_despues += 1
                vivos_globales_despues += 1
                perfiles_despues[perfil] = perfiles_despues.get(perfil, 0) + 1
                recurso = _repo_de(assignment)
                if vivos_raiz_despues > presupuesto_raiz:
                    self._rollback()
                    return Conflicto(
                        "capacity",
                        {
                            "pool": "root",
                            "needed": vivos_raiz_despues,
                            "free": presupuesto_raiz - vivos_raiz,
                        },
                    )
                if vivos_globales_despues > self.cupos.global_max:
                    self._rollback()
                    return Conflicto(
                        "capacity",
                        {
                            "pool": "gateway",
                            "needed": vivos_globales_despues,
                            "free": self.cupos.global_max - vivos_globales,
                        },
                    )
                tope_perfil = self.cupos.por_perfil.get(perfil)
                if tope_perfil is not None and perfiles_despues[perfil] > tope_perfil:
                    self._rollback()
                    return Conflicto(
                        "capacity",
                        {
                            "pool": f"perfil:{perfil}",
                            "needed": perfiles_despues[perfil],
                            "free": tope_perfil,
                        },
                    )
                vivo = self._una_fila(
                    "SELECT attempt FROM intentos WHERE root_id = ? AND unit_id = ?"
                    " AND outcome_ref IS NULL",
                    (root_id, enlace["unitId"]),
                )
                if vivo is not None:
                    self._rollback()
                    return Conflicto("unit-live", {"unitId": enlace["unitId"]})
                aceptada_previa = self._una_fila(
                    "SELECT attempt FROM intentos WHERE root_id = ? AND unit_id = ?"
                    " AND state = 'accepted'",
                    (root_id, enlace["unitId"]),
                )
                if aceptada_previa is not None:
                    self._rollback()
                    return Conflicto("unit-accepted", {"unitId": enlace["unitId"]})
                dueno = escritores_globales.get(recurso) if recurso else None
                if dueno is not None and dueno != root_id:
                    self._rollback()
                    return Conflicto("writer-held", {"resourceRef": recurso})
            intentos_nuevos = []
            for enlace in decision["bindings"]:
                unit_id = enlace["unitId"]
                assignment = enlace["assignment"]
                previo = self._una_fila(
                    "SELECT MAX(attempt) AS n FROM intentos"
                    " WHERE root_id = ? AND unit_id = ?",
                    (root_id, unit_id),
                )
                attempt = (previo["n"] or 0) + 1
                clave = kanban_port.clave_de_tarjeta(root_id, unit_id, attempt)
                self._conexion.execute(
                    "INSERT INTO intentos VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        root_id,
                        unit_id,
                        attempt,
                        revision,
                        _a_json(assignment),
                        "ready",
                        epoch,
                        None,
                        None,
                        ahora,
                        ahora,
                    ),
                )
                self._conexion.execute(
                    "INSERT INTO bandeja VALUES (?,?,?,?,?,?,?)",
                    (root_id, unit_id, attempt, "pendiente", clave, ahora, None),
                )
                recurso = _repo_de(assignment)
                if recurso is not None:
                    self._conexion.execute(
                        "INSERT OR IGNORE INTO escritores VALUES (?,?,?)",
                        (root_id, recurso, unit_id),
                    )
                self._consumir_asignacion(
                    root_id, revision, unit_id, attempt, assignment, ahora
                )
                intentos_nuevos.append(
                    {"unitId": unit_id, "attempt": attempt, "clave": clave}
                )
            for documento in documentos_de_claims:
                self._conexion.execute(
                    "INSERT OR IGNORE INTO evidencia VALUES (?,?,?,?)",
                    (root_id, documento["evidenceId"], _a_json(documento), ahora),
                )
            for intent in decision["efectos"]:
                self._conexion.execute(
                    "INSERT INTO efectos VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        root_id,
                        intent["effectId"],
                        intent.get("unitId"),
                        _a_json(intent),
                        "prepared",
                        None,
                        None,
                        ahora,
                        ahora,
                    ),
                )
            for contador in decision["contadores"]:
                nombre = contador["counter"]
                esperado = contador["expected"]
                fila = self._una_fila(
                    "SELECT value FROM contadores WHERE root_id = ? AND counter = ?",
                    (root_id, nombre),
                )
                if fila is None:
                    if esperado != 0:
                        self._rollback()
                        return Conflicto(
                            "counter-mismatch", {"counter": nombre, "current": 0}
                        )
                    self._conexion.execute(
                        "INSERT INTO contadores VALUES (?,?,1)", (root_id, nombre)
                    )
                elif fila["value"] != esperado:
                    self._rollback()
                    return Conflicto(
                        "counter-mismatch",
                        {"counter": nombre, "current": fila["value"]},
                    )
                else:
                    self._conexion.execute(
                        "UPDATE contadores SET value = value + 1"
                        " WHERE root_id = ? AND counter = ?",
                        (root_id, nombre),
                    )
            for borrador in decision["excepciones"]:
                documento_excepcion = self._documento_excepcion(
                    root_id, borrador, ahora
                )
                hallazgos = contrato.validar(documento_excepcion)
                if hallazgos:
                    self._rollback()
                    return Conflicto(
                        "documento-invalido", {"hallazgos": _a_json(hallazgos)}
                    )
                self._insertar_excepcion(root_id, borrador, documento_excepcion, ahora)
            for exception_id in decision["respuestas_aplicadas"]:
                self._conexion.execute(
                    "UPDATE excepciones SET applied_by = ? WHERE exception_id = ?"
                    " AND response_json IS NOT NULL AND applied_by IS NULL",
                    (decision_id, exception_id),
                )
            control = decision.get("control")
            if control and control.get("command") == "cancel":
                command_id = f"dec:{decision_id}"
                self._cancelar_intentos(root_id, command_id, ahora)
                self._asentar_control(
                    root_id,
                    "cancel",
                    epoch,
                    command_id,
                    control.get("reason"),
                    ahora,
                    self._efectos_comprometidos(root_id),
                    digesto(control),
                )
            for task_id in decision["consumidos"]:
                intento = self._una_fila(
                    "SELECT unit_id, attempt FROM intentos WHERE root_id = ? AND card_id = ?",
                    (root_id, task_id),
                )
                if intento is None:
                    continue
                self._conexion.execute(
                    "UPDATE resultados SET consumed_at = ? WHERE root_id = ?"
                    " AND unit_id = ? AND attempt = ? AND consumed_at IS NULL",
                    (ahora, root_id, intento["unit_id"], intento["attempt"]),
                )
            recibo = ReciboDecision(
                root_id, decision_id, digest, revision, epoch, intentos_nuevos
            )
            self._conexion.execute(
                "INSERT INTO decisiones VALUES (?,?,?,?,?)",
                (root_id, decision_id, digest, _a_json(recibo._asdict()), ahora),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(recibo)

    def _consumir_asignacion(
        self, root_id, revision, unit_id, attempt, assignment, ahora
    ):
        self._conexion.execute(
            "UPDATE asignaciones SET consumed_at = ?, consumed_attempt = ?"
            " WHERE root_id = ? AND plan_revision = ? AND unit_id = ?"
            " AND assignment_digest = ? AND consumed_at IS NULL",
            (ahora, attempt, root_id, revision, unit_id, digesto(assignment)),
        )

    # -- efectos ----------------------------------------------------------

    @property
    def dir_de_instrucciones(self) -> Path:
        return Path(self.ruta).parent / "raices" / "instrucciones"

    def _hechos_de(self, root_id: str, revision: int) -> dict:
        filas = self._conexion.execute(
            "SELECT unit_id, relay, assignment_json FROM asignaciones"
            " WHERE root_id = ? AND plan_revision = ? AND consumed_at IS NULL"
            " ORDER BY created_at, unit_id, relay",
            (root_id, revision),
        ).fetchall()
        return {
            "principal": [],
            "asignaciones": [
                {
                    "unitId": f["unit_id"],
                    "assignment": json.loads(f["assignment_json"]),
                    "relay": bool(f["relay"]),
                }
                for f in filas
            ],
            "claims": [],
            "efectos": [],
        }

    def hechos_por_raiz(self) -> dict:
        """Las asignaciones no consumidas de la revisión vigente de cada raíz:
        lo que el ticker inyecta como hechos cuando decide. Solo las raíces
        con algo pendiente aparecen; drenadas, fuera."""
        agrupadas: dict = {}
        for fila in self._conexion.execute(
            "SELECT a.root_id, a.unit_id, a.relay, a.assignment_json"
            " FROM asignaciones a JOIN revisiones r ON r.root_id = a.root_id"
            " AND r.revision = a.plan_revision"
            " WHERE a.consumed_at IS NULL"
            " ORDER BY a.created_at, a.root_id, a.unit_id, a.relay"
        ):
            hechos = agrupadas.setdefault(
                fila["root_id"],
                {"principal": [], "asignaciones": [], "claims": [], "efectos": []},
            )
            hechos["asignaciones"].append(
                {
                    "unitId": fila["unit_id"],
                    "assignment": json.loads(fila["assignment_json"]),
                    "relay": bool(fila["relay"]),
                }
            )
        return agrupadas

    def reconciliar_asignaciones(self, root_id: str, asignaciones) -> object:
        """El espejo de managedRootsReconcileInput de R: valida cada assignment
        contra el contrato (envuelto en unidad.v1), exige que su instructionRef
        resuelva en el store de instrucciones y persiste por clave estable
        (root, revisión, unit, relay). La fila ES el recibo (content-addressed
        por assignment_digest): el reintento exacto es replay y otra
        asignación con la fila viva es operation-conflict. En la MISMA
        transacción re-decide la raíz con los hechos vigentes y asienta sus
        necesidades."""
        revision_fila = self._una_fila(
            "SELECT MAX(revision) AS revision FROM revisiones WHERE root_id = ?",
            (root_id,),
        )
        if revision_fila is None or revision_fila["revision"] is None:
            return Conflicto("raiz-desconocida")
        revision = revision_fila["revision"]
        control = self._una_fila(
            "SELECT epoch, state FROM controles WHERE root_id = ? ORDER BY epoch DESC LIMIT 1",
            (root_id,),
        )
        if control["state"] != "active":
            # El ticker nunca tomaría estas filas: sin rechazo quedarían
            # pendientes para siempre.
            return Conflicto("raiz-no-activa", {"state": control["state"]})
        epoch = control["epoch"]
        for item in asignaciones:
            if (
                self._una_fila(
                    "SELECT 1 FROM unidades_de_plan"
                    " WHERE root_id = ? AND revision = ? AND unit_id = ?",
                    (root_id, revision, item.get("unitId")),
                )
                is None
            ):
                return Conflicto("unidad-desconocida", {"unitId": item.get("unitId")})
            assignment = item.get("assignment")
            documento = {
                "schema": contrato.UNIDAD,
                "rootId": root_id,
                "planRevision": revision,
                "unitId": item.get("unitId"),
                "attempt": 1,
                "taskId": None,
                "assignment": assignment,
                "state": "ready",
                "controlEpoch": epoch,
            }
            hallazgos = contrato.validar(documento)
            if not hallazgos and assignment.get("resultContract") not in (
                "review.v1",
                "ready.v1",
            ):
                hallazgos = [
                    {
                        "regla": "schema",
                        "ruta": "assignment.resultContract",
                        "detalle": "fuera del contrato",
                    }
                ]
            if hallazgos:
                return Conflicto(
                    "documento-invalido", {"hallazgos": _a_json(hallazgos)}
                )
            referencia = assignment.get("instructionRef") or {}
            _texto, motivo = instrucciones.resolver(
                self.dir_de_instrucciones,
                referencia.get("ref", ""),
                referencia.get("digest", ""),
            )
            if motivo is not None:
                return Conflicto(
                    "instruccion-invalida",
                    {
                        "unitId": item.get("unitId"),
                        "ref": referencia.get("ref"),
                        "motivo": motivo,
                    },
                )
        ahora = self.reloj()
        self._begin()
        try:
            recibos = []
            for item in asignaciones:
                assignment = item["assignment"]
                unit_id = item["unitId"]
                relay = 1 if item["relay"] else 0
                digest = digesto(assignment)
                fila = self._una_fila(
                    "SELECT assignment_digest, consumed_at FROM asignaciones"
                    " WHERE root_id = ? AND plan_revision = ? AND unit_id = ?"
                    " AND relay = ?",
                    (root_id, revision, unit_id, relay),
                )
                if fila is None:
                    self._conexion.execute(
                        "INSERT INTO asignaciones VALUES (?,?,?,?,?,?,NULL,NULL,?)",
                        (
                            root_id,
                            revision,
                            unit_id,
                            relay,
                            digest,
                            _a_json(assignment),
                            ahora,
                        ),
                    )
                    recibos.append(
                        {"unitId": unit_id, "relay": bool(relay), "replayed": False}
                    )
                elif fila["assignment_digest"] == digest:
                    recibos.append(
                        {"unitId": unit_id, "relay": bool(relay), "replayed": True}
                    )
                elif fila["consumed_at"] is None:
                    self._rollback()
                    return Conflicto(
                        "operation-conflict", {"unitId": unit_id, "relay": bool(relay)}
                    )
                else:
                    self._conexion.execute(
                        "UPDATE asignaciones SET assignment_digest = ?,"
                        " assignment_json = ?, consumed_at = NULL,"
                        " consumed_attempt = NULL, created_at = ?"
                        " WHERE root_id = ? AND plan_revision = ? AND unit_id = ?"
                        " AND relay = ?",
                        (
                            digest,
                            _a_json(assignment),
                            ahora,
                            root_id,
                            revision,
                            unit_id,
                            relay,
                        ),
                    )
                    recibos.append(
                        {"unitId": unit_id, "relay": bool(relay), "replayed": False}
                    )
            vista = self.leer_vista(root_id, self._hechos_de(root_id, revision))
            necesidades = serializar_necesidades(decidir.decidir(vista).necesidades)
            self.asentar_necesidades(root_id, necesidades)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(ReciboReconcile(root_id, revision, recibos, necesidades))

    def asentar_necesidades(self, root_id: str, necesidades) -> None:
        """Sin transacción propia: dentro de la de reconcile va como execute
        directo; sola, autocommit. Acepta las NamedTuple de decidir o la
        forma ya serializada."""
        if necesidades and not isinstance(necesidades[0], dict):
            necesidades = serializar_necesidades(necesidades)
        self._conexion.execute(
            "INSERT OR REPLACE INTO necesidades VALUES (?,?,?,?)",
            (root_id, digesto(necesidades), _a_json(necesidades), self.reloj()),
        )

    def necesidades_de(self, root_id: str) -> dict | None:
        fila = self._una_fila(
            "SELECT needs_digest, needs_json FROM necesidades WHERE root_id = ?",
            (root_id,),
        )
        if fila is None:
            return None
        return {"digest": fila["needs_digest"], "items": json.loads(fila["needs_json"])}

    def texto_de_instruccion(self, ref: str, digest: str) -> object:
        """La única vía del ticker al disco de instrucciones: verifica el
        digest contra el contenido antes de entregar el texto del brief."""
        texto, motivo = instrucciones.resolver(self.dir_de_instrucciones, ref, digest)
        if motivo is not None:
            return Conflicto("instruccion-invalida", {"motivo": motivo})
        return Ok(texto)

    def iniciar_efecto(
        self, root_id: str, effect_id: str, start_id: str, expected_epoch: int
    ) -> object:
        self._begin()
        try:
            fila = self._una_fila(
                "SELECT state, start_id, started_epoch, intent_json FROM efectos"
                " WHERE root_id = ? AND effect_id = ?",
                (root_id, effect_id),
            )
            if fila is None:
                self._rollback()
                return Conflicto("effect-unknown", {"effectId": effect_id})
            if fila["state"] != "prepared":
                if fila["start_id"] == start_id:
                    recibo = Ok(
                        ReciboEfecto(
                            effect_id,
                            fila["state"],
                            fila["start_id"],
                            fila["started_epoch"],
                        ),
                        True,
                    )
                    self._rollback()
                    return recibo
                self._rollback()
                return Conflicto("effect-refused", {"state": fila["state"]})
            vigente = self._una_fila(
                "SELECT epoch, state FROM controles WHERE root_id = ?"
                " ORDER BY epoch DESC LIMIT 1",
                (root_id,),
            )
            if vigente["state"] in ("paused", "cancelled"):
                self._rollback()
                return Conflicto("root-not-active", {"state": vigente["state"]})
            if expected_epoch != vigente["epoch"]:
                self._rollback()
                return Conflicto("stale-control-epoch", {"actual": vigente["epoch"]})
            ahora = self.reloj()
            intent = json.loads(fila["intent_json"])
            razon = self._razon_de_autoridad(root_id, effect_id, intent, ahora)
            if razon is not None:
                self._levantar_excepcion(
                    root_id,
                    "NECESITA_DAVID",
                    {
                        "kind": "effect",
                        "effectId": effect_id,
                        "cause": autoridad.causa_de(razon),
                    },
                    # r6f: la misma tabla por razón que los demás caminos.
                    autoridad.opciones_por_razon(razon),
                    ahora,
                )
                self._commit()
                return Conflicto("authority", {"reason": razon})
            self._conexion.execute(
                "UPDATE efectos SET state = 'started', start_id = ?,"
                " started_epoch = ?, updated_at = ?"
                " WHERE root_id = ? AND effect_id = ? AND state = 'prepared'",
                (start_id, expected_epoch, ahora, root_id, effect_id),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(ReciboEfecto(effect_id, "started", start_id, expected_epoch))

    def liquidar_efecto(
        self, root_id: str, effect_id: str, start_id: str, desenlace: dict
    ) -> object:
        self._begin()
        try:
            fila = self._una_fila(
                "SELECT state, start_id FROM efectos WHERE root_id = ? AND effect_id = ?",
                (root_id, effect_id),
            )
            if fila is None:
                self._rollback()
                return Conflicto("effect-unknown", {"effectId": effect_id})
            if fila["state"] != "started" or fila["start_id"] != start_id:
                self._rollback()
                return Conflicto("effect-refused", {"state": fila["state"]})
            nuevo = "confirmed" if desenlace["kind"] == "confirmed" else "uncertain"
            cambio = self._conexion.execute(
                "UPDATE efectos SET state = ?, updated_at = ?"
                " WHERE root_id = ? AND effect_id = ? AND state = 'started'"
                " AND start_id = ?",
                (nuevo, self.reloj(), root_id, effect_id, start_id),
            )
            if cambio.rowcount != 1:
                self._rollback()
                return Conflicto("effect-refused", {"state": fila["state"]})
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(ReciboEfecto(effect_id, nuevo, start_id, None))

    # -- entradas del mundo ------------------------------------------------

    def reportar_resultado(self, resultado: dict) -> object:
        documento = {
            **resultado,
            "schema": "sistema-agentes.resultado.v1",
        }
        hallazgos = contrato.validar(documento)
        if hallazgos:
            return Conflicto("documento-invalido", {"hallazgos": _a_json(hallazgos)})
        digest = documento["resultDigest"]
        ahora = self.reloj()
        self._begin()
        try:
            previo = self._una_fila(
                "SELECT result_digest FROM resultados"
                " WHERE root_id = ? AND unit_id = ? AND attempt = ?",
                (documento["rootId"], documento["unitId"], documento["attempt"]),
            )
            if previo is not None:
                self._rollback()
                if previo["result_digest"] != digest:
                    return Conflicto("operation-conflict")
                return Ok(True, True)
            fila_intento = self._una_fila(
                "SELECT state FROM intentos"
                " WHERE root_id = ? AND unit_id = ? AND attempt = ?",
                (documento["rootId"], documento["unitId"], documento["attempt"]),
            )
            if fila_intento is None:
                self._rollback()
                return Conflicto("intento-desconocido", {"unitId": documento["unitId"]})
            self._conexion.execute(
                "INSERT INTO resultados VALUES (?,?,?,?,?,?,?)",
                (
                    documento["rootId"],
                    documento["unitId"],
                    documento["attempt"],
                    digest,
                    _a_json(documento),
                    None,
                    ahora,
                ),
            )
            cambio = self._conexion.execute(
                "UPDATE intentos SET state = 'reported', updated_at = ?"
                " WHERE root_id = ? AND unit_id = ? AND attempt = ?"
                " AND state = 'running'",
                (ahora, documento["rootId"], documento["unitId"], documento["attempt"]),
            )
            if cambio.rowcount != 1:
                self._rollback()
                return Conflicto(
                    "transition-refused",
                    {"state": fila_intento["state"], "nuevo": "reported"},
                )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    def registrar_incidencia(self, root_id, unit_id, attempt, clave, incidencia) -> Ok:
        self._begin()
        try:
            self._conexion.execute(
                "INSERT OR IGNORE INTO incidencias VALUES (?,?,?,?,?,?)",
                (root_id, unit_id, attempt, clave, _a_json(incidencia), self.reloj()),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    def responder_excepcion(self, root_id, exception_id, option_id, by) -> object:
        self._begin()
        try:
            fila = self._una_fila(
                "SELECT options_json, response_json FROM excepciones"
                " WHERE exception_id = ? AND root_id = ?",
                (exception_id, root_id),
            )
            if fila is None:
                self._rollback()
                return Conflicto("exception-unknown", {"exceptionId": exception_id})
            if fila["response_json"] is not None:
                self._rollback()
                return Conflicto("exception-answered", {"exceptionId": exception_id})
            ofrecidas = {o["optionId"] for o in json.loads(fila["options_json"])}
            if option_id not in ofrecidas:
                self._rollback()
                return Conflicto("option-not-offered", {"exceptionId": exception_id})
            cambio = self._conexion.execute(
                "UPDATE excepciones SET response_json = ?"
                " WHERE exception_id = ? AND root_id = ? AND response_json IS NULL",
                (
                    _a_json({"optionId": option_id, "by": by, "at": self.reloj()}),
                    exception_id,
                    root_id,
                ),
            )
            if cambio.rowcount != 1:
                self._rollback()
                return Conflicto("exception-answered", {"exceptionId": exception_id})
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    def notificar(self, root_id, exception_ids) -> object:
        self._begin()
        try:
            for exception_id in exception_ids:
                cambio = self._conexion.execute(
                    "UPDATE excepciones SET noticed_at = ?"
                    " WHERE exception_id = ? AND root_id = ?",
                    (self.reloj(), exception_id, root_id),
                )
                if cambio.rowcount != 1:
                    self._rollback()
                    return Conflicto("exception-unknown", {"exceptionId": exception_id})
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    # -- conciliación -------------------------------------------------------

    def transicionar_intento(
        self, root_id, unit_id, attempt, nuevo_estado, ref
    ) -> object:
        self._begin()
        try:
            fila = self._una_fila(
                "SELECT state FROM intentos WHERE root_id = ? AND unit_id = ?"
                " AND attempt = ?",
                (root_id, unit_id, attempt),
            )
            if fila is None:
                self._rollback()
                return Conflicto("intento-desconocido", {"unitId": unit_id})
            if not contrato.transicion_permitida(fila["state"], nuevo_estado):
                self._rollback()
                return Conflicto(
                    "transition-refused",
                    {"state": fila["state"], "nuevo": nuevo_estado},
                )
            cambio = self._conexion.execute(
                "UPDATE intentos SET state = ?, updated_at = ? WHERE root_id = ?"
                " AND unit_id = ? AND attempt = ? AND state = ?",
                (nuevo_estado, self.reloj(), root_id, unit_id, attempt, fila["state"]),
            )
            if cambio.rowcount != 1:
                self._rollback()
                return Conflicto(
                    "transition-refused",
                    {"state": fila["state"], "nuevo": nuevo_estado},
                )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    def reclamar_intencion(self, root_id, unit_id, attempt) -> object:
        """Verifica active dentro de una transacción, antes de tocar el kanban
        (I05). No retiene nada por sí sola: la exclusividad la da buscar_o_crear
        dentro del write_txn del kanban y que admitir_intento exija 'pendiente'."""
        self._begin()
        try:
            fila = self._una_fila(
                "SELECT state FROM bandeja WHERE root_id = ? AND unit_id = ?"
                " AND attempt = ?",
                (root_id, unit_id, attempt),
            )
            if fila is None:
                self._rollback()
                return Conflicto("intento-desconocido", {"unitId": unit_id})
            if fila["state"] != "pendiente":
                self._rollback()
                return Conflicto("intencion-no-pendiente", {"state": fila["state"]})
            vigente = self._una_fila(
                "SELECT epoch, state FROM controles WHERE root_id = ?"
                " ORDER BY epoch DESC LIMIT 1",
                (root_id,),
            )
            if vigente["state"] != "active":
                self._rollback()
                return Conflicto("root-not-active", {"state": vigente["state"]})
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    def admitir_intento(self, root_id, unit_id, attempt, card_id, flota=None) -> object:
        self._begin()
        try:
            fila = self._una_fila(
                "SELECT state FROM bandeja WHERE root_id = ? AND unit_id = ?"
                " AND attempt = ?",
                (root_id, unit_id, attempt),
            )
            if fila is None:
                self._rollback()
                return Conflicto("intento-desconocido", {"unitId": unit_id})
            if fila["state"] != "pendiente":
                if fila["state"] == "anulada" and card_id is not None:
                    self._conexion.execute(
                        "INSERT OR IGNORE INTO bloqueos VALUES (?,?,?,NULL)",
                        (root_id, card_id, "cancel"),
                    )
                self._commit()
                return Ok(True, True)
            vigente = self._una_fila(
                "SELECT epoch, state FROM controles WHERE root_id = ?"
                " ORDER BY epoch DESC LIMIT 1",
                (root_id,),
            )
            if vigente["state"] != "active":
                self._rollback()
                return Conflicto("root-not-active", {"state": vigente["state"]})
            ahora = self.reloj()
            self._conexion.execute(
                "UPDATE bandeja SET state = 'cumplida', fulfilled_at = ?"
                " WHERE root_id = ? AND unit_id = ? AND attempt = ? AND state = 'pendiente'",
                (ahora, root_id, unit_id, attempt),
            )
            self._conexion.execute(
                "UPDATE intentos SET card_id = ?, updated_at = ?"
                " WHERE root_id = ? AND unit_id = ? AND attempt = ? AND state = 'ready'",
                (card_id, ahora, root_id, unit_id, attempt),
            )
            self._conexion.execute(
                "UPDATE intentos SET state = 'admitted', updated_at = ?"
                " WHERE root_id = ? AND unit_id = ? AND attempt = ? AND state = 'ready'",
                (ahora, root_id, unit_id, attempt),
            )
            if flota is not None:
                self._conexion.execute(
                    "INSERT OR IGNORE INTO flotas VALUES (?,?,?,?,?)",
                    (root_id, unit_id, attempt, _a_json(flota), ahora),
                )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    def bloqueos_pendientes(self) -> list:
        return [
            {"rootId": f["root_id"], "cardId": f["card_id"], "motivo": f["motivo"]}
            for f in self._conexion.execute(
                "SELECT root_id, card_id, motivo FROM bloqueos"
                " WHERE hecho_at IS NULL ORDER BY rowid"
            ).fetchall()
        ]

    def bloqueo_hecho(self, root_id, card_id) -> Ok:
        self._begin()
        try:
            self._conexion.execute(
                "UPDATE bloqueos SET hecho_at = ? WHERE root_id = ? AND card_id = ?",
                (self.reloj(), root_id, card_id),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Ok(True)

    # -- lecturas ----------------------------------------------------------

    def raices(self) -> list:
        filas = self._conexion.execute(
            "SELECT r.root_id, c.state, r.created_at FROM raices r"
            " JOIN controles c ON c.root_id = r.root_id"
            " WHERE c.epoch = (SELECT MAX(epoch) FROM controles WHERE root_id = r.root_id)"
            " ORDER BY r.created_at, r.root_id"
        ).fetchall()
        return [
            {"rootId": f["root_id"], "state": f["state"], "createdAt": f["created_at"]}
            for f in filas
        ]

    def snapshot_raiz(self, root_id: str) -> dict | None:
        """El snapshot con la forma de managedTasks.root.read de R (I14). Las
        filas rotas (JSON que no parsea o no es objeto) se saltan: no atribuyen
        ni tumban la proyección (I16)."""
        raiz = self._una_fila("SELECT * FROM raices WHERE root_id = ?", (root_id,))
        if raiz is None:
            return None
        revision = self._una_fila(
            "SELECT MAX(revision) AS revision FROM revisiones WHERE root_id = ?",
            (root_id,),
        )["revision"]
        control = self._una_fila(
            "SELECT epoch, state, reason FROM controles WHERE root_id = ?"
            " ORDER BY epoch DESC LIMIT 1",
            (root_id,),
        )
        unidades_plan = [
            json.loads(f["unit_json"])
            for f in self._conexion.execute(
                "SELECT unit_json FROM unidades_de_plan"
                " WHERE root_id = ? AND revision = ? ORDER BY unit_id",
                (root_id, revision),
            )
        ]
        units = []
        for f in self._conexion.execute(
            "SELECT * FROM intentos WHERE root_id = ? ORDER BY unit_id, attempt",
            (root_id,),
        ):
            try:
                assignment = json.loads(f["assignment_json"])
                if not isinstance(assignment, dict):
                    raise ValueError
            except ValueError:
                continue
            units.append(
                {
                    "schema": "sistema-agentes.unidad.v1",
                    "rootId": root_id,
                    "planRevision": f["plan_revision"],
                    "unitId": f["unit_id"],
                    "attempt": f["attempt"],
                    "taskId": f["card_id"],
                    "assignment": assignment,
                    "state": f["state"],
                    "controlEpoch": f["control_epoch"],
                }
            )
        ligadas = {u["unitId"] for u in units}
        aceptadas = {u["unitId"] for u in units if u["state"] == "accepted"}
        unbound = []
        for unidad_plan in unidades_plan:
            if unidad_plan["unitId"] in ligadas:
                continue
            faltan = [d for d in unidad_plan["dependsOn"] if d not in aceptadas]
            unbound.append(
                {
                    "unitId": unidad_plan["unitId"],
                    "state": "ready" if not faltan else "pending",
                    "missing": faltan,
                }
            )
        evidence = []
        for f in self._conexion.execute(
            "SELECT evidence_json FROM evidencia WHERE root_id = ?"
            " ORDER BY created_at, evidence_id",
            (root_id,),
        ):
            try:
                documento = json.loads(f["evidence_json"])
            except ValueError:
                continue
            if isinstance(documento, dict):
                evidence.append(documento)
        effects = [
            {
                "rootId": root_id,
                "effectId": f["effect_id"],
                "state": f["state"],
                "startId": f["start_id"],
                "startedEpoch": f["started_epoch"],
            }
            for f in self._conexion.execute(
                "SELECT * FROM efectos WHERE root_id = ? ORDER BY created_at, effect_id",
                (root_id,),
            )
        ]
        pending_results = []
        for f in self._conexion.execute(
            "SELECT result_json FROM resultados WHERE root_id = ?"
            " AND consumed_at IS NULL ORDER BY created_at",
            (root_id,),
        ):
            try:
                documento = json.loads(f["result_json"])
            except ValueError:
                continue
            if isinstance(documento, dict):
                reported_by = documento.get("reportedBy")
                pending_results.append(
                    {
                        "taskId": (
                            reported_by.get("taskId")
                            if isinstance(reported_by, Mapping)
                            else None
                        ),
                        "resultDigest": documento.get("resultDigest"),
                        "resultContract": documento.get("resultContract"),
                    }
                )
        counters = {
            f["counter"]: f["value"]
            for f in self._conexion.execute(
                "SELECT counter, value FROM contadores WHERE root_id = ?",
                (root_id,),
            )
        }
        writers = [
            {"resourceRef": f["resource_ref"], "unitId": f["unit_id"]}
            for f in self._conexion.execute(
                "SELECT resource_ref, unit_id FROM escritores WHERE root_id = ?",
                (root_id,),
            )
        ]
        incidents = []
        for f in self._conexion.execute(
            "SELECT i.card_id, n.unit_id, n.attempt, n.incident_key,"
            " n.incident_json FROM incidencias n"
            " JOIN intentos i ON i.root_id = n.root_id AND i.unit_id = n.unit_id"
            " AND i.attempt = n.attempt"
            " WHERE n.root_id = ?"
            " ORDER BY n.created_at, n.unit_id, n.attempt, n.incident_key",
            (root_id,),
        ):
            try:
                incidente = json.loads(f["incident_json"])
            except ValueError:
                continue
            if not isinstance(incidente, dict):
                continue
            incidents.append(
                {
                    "unitId": f["unit_id"],
                    "attempt": f["attempt"],
                    "taskId": f["card_id"] or "",
                    "incidentKey": f["incident_key"],
                    "incident": incidente,
                }
            )
        exceptions = []
        for f in self._conexion.execute(
            "SELECT * FROM excepciones WHERE root_id = ? ORDER BY raised_at, exception_id",
            (root_id,),
        ):
            try:
                documento = {
                    "schema": "sistema-agentes.excepcion.v1",
                    "exceptionId": f["exception_id"],
                    "rootId": root_id,
                    "unitId": f["unit_id"],
                    "type": f["type"],
                    "options": json.loads(f["options_json"]),
                    "evidenceRef": (
                        json.loads(f["evidence_ref_json"])
                        if f["evidence_ref_json"]
                        else None
                    ),
                    "recipient": json.loads(f["recipient_json"]),
                    "raisedAt": f["raised_at"],
                    "renoticeAfterMs": f["renotice_after_ms"],
                    "response": (
                        json.loads(f["response_json"]) if f["response_json"] else None
                    ),
                }
            except ValueError:
                continue
            if isinstance(documento["options"], list):
                exceptions.append(documento)
        return {
            "meta": {
                "schema": "sistema-agentes.meta.v1",
                "rootId": root_id,
                "requester": json.loads(raiz["requester_json"]),
                "authority": json.loads(raiz["authority_json"]),
                "budget": json.loads(raiz["budget_json"]),
                "plan": {
                    "revision": revision,
                    "planDigest": self._una_fila(
                        "SELECT plan_digest FROM revisiones WHERE root_id = ? AND revision = ?",
                        (root_id, revision),
                    )["plan_digest"],
                    "requestDigest": self._una_fila(
                        "SELECT request_digest FROM revisiones WHERE root_id = ? AND revision = ?",
                        (root_id, revision),
                    )["request_digest"],
                    "units": unidades_plan,
                    "assumptions": [],
                },
                "controlEpoch": control["epoch"],
                "control": {
                    "state": control["state"],
                    "epoch": control["epoch"],
                    "reason": control["reason"],
                },
                "qualityPolicy": {"version": raiz["quality_policy"]},
                "createdAt": raiz["created_at"],
            },
            "units": units,
            "unbound": unbound,
            "evidence": evidence,
            "effects": effects,
            "budget": json.loads(raiz["budget_json"]),
            "pendingResults": pending_results,
            "counters": counters,
            "writers": writers,
            "requesterGeneration": 1,
            "incidents": incidents,
            "exceptions": exceptions,
            "authority": {
                "revision": 1,
                "authority": json.loads(raiz["authority_json"]),
            },
        }

    def estado_de_control(self, root_id) -> str | None:
        fila = self._una_fila(
            "SELECT state FROM controles WHERE root_id = ? ORDER BY epoch DESC LIMIT 1",
            (root_id,),
        )
        return fila["state"] if fila else None

    def intenciones_pendientes(self) -> list:
        filas = self._conexion.execute(
            "SELECT b.root_id, b.unit_id, b.attempt, b.idempotency_key,"
            " i.assignment_json FROM bandeja b JOIN intentos i"
            " ON i.root_id = b.root_id AND i.unit_id = b.unit_id"
            " AND i.attempt = b.attempt WHERE b.state = 'pendiente'"
            " ORDER BY b.created_at, b.unit_id, b.attempt"
        ).fetchall()
        return [
            Intencion(
                f["root_id"],
                f["unit_id"],
                f["attempt"],
                f["idempotency_key"],
                json.loads(f["assignment_json"]),
            )
            for f in filas
        ]

    def intentos_con_tarjeta(self) -> list:
        filas = self._conexion.execute(
            "SELECT root_id, unit_id, attempt, card_id, state FROM intentos"
            " WHERE card_id IS NOT NULL AND outcome_ref IS NULL"
            " ORDER BY created_at, unit_id, attempt"
        ).fetchall()
        return [
            {
                "rootId": f["root_id"],
                "unitId": f["unit_id"],
                "attempt": f["attempt"],
                "cardId": f["card_id"],
                "state": f["state"],
            }
            for f in filas
        ]

    def ocupacion(self) -> dict:
        vivos = self._conexion.execute(
            "SELECT root_id, assignment_json FROM intentos WHERE outcome_ref IS NULL"
        ).fetchall()
        por_raiz: dict = {}
        por_perfil: dict = {}
        for fila in vivos:
            por_raiz[fila["root_id"]] = por_raiz.get(fila["root_id"], 0) + 1
            perfil = _perfil_de(json.loads(fila["assignment_json"]))
            por_perfil[perfil] = por_perfil.get(perfil, 0) + 1
        return {"global": len(vivos), "por_raiz": por_raiz, "por_perfil": por_perfil}

    def leer_vista(self, root_id: str, hechos: dict) -> dict | None:
        raiz = self._una_fila("SELECT * FROM raices WHERE root_id = ?", (root_id,))
        if raiz is None:
            return None
        revision = self._una_fila(
            "SELECT MAX(revision) AS revision FROM revisiones WHERE root_id = ?",
            (root_id,),
        )["revision"]
        control = self._una_fila(
            "SELECT epoch, state, reason FROM controles WHERE root_id = ?"
            " ORDER BY epoch DESC LIMIT 1",
            (root_id,),
        )
        unidades_plan = [
            json.loads(f["unit_json"])
            for f in self._conexion.execute(
                "SELECT unit_json FROM unidades_de_plan"
                " WHERE root_id = ? AND revision = ? ORDER BY unit_id",
                (root_id, revision),
            )
        ]
        flotas = {
            (f["unit_id"], f["attempt"]): json.loads(f["flota_json"])
            for f in self._conexion.execute(
                "SELECT unit_id, attempt, flota_json FROM flotas WHERE root_id = ?",
                (root_id,),
            )
        }
        intentos = []
        for f in self._conexion.execute(
            "SELECT * FROM intentos WHERE root_id = ? ORDER BY unit_id, attempt",
            (root_id,),
        ):
            # F13: la fila corrupta (JSON que no parsea o no es objeto) se
            # salta, como hace snapshot_raiz: no tumba la vista ni la vuelta.
            try:
                assignment = json.loads(f["assignment_json"])
                if not isinstance(assignment, dict):
                    raise ValueError
            except ValueError:
                continue
            intentos.append(
                {
                    "schema": "sistema-agentes.unidad.v1",
                    "rootId": root_id,
                    "planRevision": f["plan_revision"],
                    "unitId": f["unit_id"],
                    "attempt": f["attempt"],
                    "taskId": f["card_id"],
                    "assignment": assignment,
                    "state": f["state"],
                    "controlEpoch": f["control_epoch"],
                    "flota": flotas.get((f["unit_id"], f["attempt"])),
                }
            )
        ligadas = {i["unitId"] for i in intentos}
        aceptadas = {i["unitId"] for i in intentos if i["state"] == "accepted"}
        sin_ligar = []
        for unidad_plan in unidades_plan:
            if unidad_plan["unitId"] in ligadas:
                continue
            faltan = [d for d in unidad_plan["dependsOn"] if d not in aceptadas]
            sin_ligar.append(
                {
                    "unitId": unidad_plan["unitId"],
                    "state": "ready" if not faltan else "pending",
                    "missing": faltan,
                }
            )
        evidencia = [
            json.loads(f["evidence_json"])
            for f in self._conexion.execute(
                "SELECT evidence_json FROM evidencia WHERE root_id = ? ORDER BY rowid",
                (root_id,),
            )
        ]
        efectos = [
            {
                "effectId": f["effect_id"],
                "state": f["state"],
                "startId": f["start_id"],
                "startedEpoch": f["started_epoch"],
                "intent": json.loads(f["intent_json"]),
            }
            for f in self._conexion.execute(
                "SELECT * FROM efectos WHERE root_id = ? ORDER BY created_at, effect_id",
                (root_id,),
            )
        ]
        resultados = []
        for f in self._conexion.execute(
            "SELECT result_json FROM resultados WHERE root_id = ?"
            " AND consumed_at IS NULL ORDER BY created_at",
            (root_id,),
        ):
            # F13: fila corrupta se salta, como en snapshot_raiz.
            try:
                documento = json.loads(f["result_json"])
            except ValueError:
                continue
            if not isinstance(documento, dict):
                continue
            payload = documento.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if documento.get("resultContract") == "review.v1":
                veredicto = payload.get("verdict")
                hallazgos_ref = payload.get("findingsRef")
                evidencia_ref = payload.get("evidenceRef")
                referencia = (
                    hallazgos_ref
                    if isinstance(hallazgos_ref, dict)
                    else (evidencia_ref if isinstance(evidencia_ref, dict) else {})
                )
            else:
                veredicto = None
                evidencia_ref = payload.get("evidenceRef")
                referencia = evidencia_ref if isinstance(evidencia_ref, dict) else {}
            reported_by = documento.get("reportedBy")
            resultados.append(
                {
                    "taskId": (
                        reported_by.get("taskId")
                        if isinstance(reported_by, Mapping)
                        else None
                    ),
                    "contrato": documento.get("resultContract"),
                    "verdict": veredicto,
                    "kind": "produced",
                    "ref": referencia.get("ref"),
                }
            )
        incidencias = []
        for f in self._conexion.execute(
            "SELECT * FROM incidencias WHERE root_id = ?"
            " ORDER BY created_at, unit_id, attempt, incident_key",
            (root_id,),
        ):
            # F13: fila corrupta se salta, como en snapshot_raiz.
            try:
                incidente = json.loads(f["incident_json"])
                if not isinstance(incidente, dict):
                    raise ValueError
            except ValueError:
                continue
            incidencias.append(
                {
                    "unitId": f["unit_id"],
                    "attempt": f["attempt"],
                    "incidentKey": f["incident_key"],
                    "incident": incidente,
                }
            )
        excepciones = []
        for f in self._conexion.execute(
            "SELECT * FROM excepciones WHERE root_id = ? ORDER BY raised_at, exception_id",
            (root_id,),
        ):
            excepciones.append(
                {
                    "exceptionId": f["exception_id"],
                    "rootId": root_id,
                    "unitId": f["unit_id"],
                    "type": f["type"],
                    "subject": json.loads(f["subject_json"]),
                    "options": json.loads(f["options_json"]),
                    "evidenceRef": (
                        json.loads(f["evidence_ref_json"])
                        if f["evidence_ref_json"]
                        else None
                    ),
                    "raisedAt": f["raised_at"],
                    "noticedAt": f["noticed_at"],
                    "renoticeAfterMs": f["renotice_after_ms"],
                    "response": (
                        json.loads(f["response_json"]) if f["response_json"] else None
                    ),
                    "appliedBy": f["applied_by"],
                }
            )
        contadores = {
            f["counter"]: f["value"]
            for f in self._conexion.execute(
                "SELECT counter, value FROM contadores WHERE root_id = ?",
                (root_id,),
            )
        }
        escritores = [
            {
                "resourceRef": f["resource_ref"],
                "unitId": f["unit_id"],
                "rootId": f["root_id"],
            }
            for f in self._conexion.execute(
                "SELECT root_id, resource_ref, unit_id FROM escritores"
                " ORDER BY root_id, resource_ref"
            )
        ]
        fallos_con_hallazgo = {
            (f["unit_id"], f["attempt"]): f
            for f in self._conexion.execute(
                "SELECT i.unit_id, i.attempt, r.result_json FROM intentos i"
                " JOIN resultados r ON r.root_id = i.root_id"
                " AND r.unit_id = i.unit_id AND r.attempt = i.attempt"
                " WHERE i.root_id = ? AND i.state = 'failed'",
                (root_id,),
            )
        }
        hallazgos_lista = [
            {
                "unitId": unit_id,
                "attempt": attempt,
                "findingsRef": (
                    (documento.get("payload") or {}).get("findingsRef")
                    or (documento.get("payload") or {}).get("evidenceRef")
                    or {}
                ).get("ref"),
            }
            for (unit_id, attempt), f in sorted(fallos_con_hallazgo.items())
            for documento in [json.loads(f["result_json"])]
        ]
        ocupacion = self.ocupacion()
        presupuesto = json.loads(raiz["budget_json"])
        plazas_por_perfil = {
            perfil: tope - ocupacion["por_perfil"].get(perfil, 0)
            for perfil, tope in self.cupos.por_perfil.items()
        }
        plazas = {
            "raiz": presupuesto["maxConcurrentTasks"]
            - ocupacion["por_raiz"].get(root_id, 0),
            "global": self.cupos.global_max - ocupacion["global"],
            "por_perfil": plazas_por_perfil,
        }
        return {
            "meta": {
                "schema": "sistema-agentes.meta.v1",
                "rootId": root_id,
                "requester": json.loads(raiz["requester_json"]),
                "authority": json.loads(raiz["authority_json"]),
                "budget": presupuesto,
                "plan": {
                    "revision": revision,
                    "planDigest": self._una_fila(
                        "SELECT plan_digest FROM revisiones WHERE root_id = ? AND revision = ?",
                        (root_id, revision),
                    )["plan_digest"],
                    "requestDigest": self._una_fila(
                        "SELECT request_digest FROM revisiones WHERE root_id = ? AND revision = ?",
                        (root_id, revision),
                    )["request_digest"],
                    "units": unidades_plan,
                    "assumptions": json.loads(
                        self._una_fila(
                            "SELECT assumptions_json FROM revisiones"
                            " WHERE root_id = ? AND revision = ?",
                            (root_id, revision),
                        )["assumptions_json"]
                    ),
                },
                "controlEpoch": control["epoch"],
                "control": {
                    "state": control["state"],
                    "epoch": control["epoch"],
                    "reason": control["reason"],
                },
                "qualityPolicy": {"version": raiz["quality_policy"]},
                "createdAt": raiz["created_at"],
            },
            "unidades": intentos,
            "sin_ligar": sin_ligar,
            "evidencia": evidencia,
            "efectos": efectos,
            "resultados": resultados,
            "incidencias": incidencias,
            "excepciones": excepciones,
            "contadores": contadores,
            "escritores": escritores,
            "hallazgos": hallazgos_lista,
            "plazas": plazas,
            "hechos": hechos,
            "politica": json.loads(raiz["politica_json"]),
            "permisos_h": self.permisos_h_datos,
            "concesiones": sorted(self._concesiones_de(root_id)),
            "ahora": self.reloj(),
        }
