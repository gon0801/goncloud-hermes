"""El documento flota.v1 que congela el despacho de cada intento (I09, H4b).

El ticker lo arma al admitir el intento y el store lo congela en la tabla
flotas: el replay de la admisión no reescribe. HOST es gon-pc y EXECUTOR cli
porque el despacho Mac lanza los hijos por la CLI del gateway. La attestación
de poteto llega en H4b2; el contrato acepta la lista vacía.

Decisión de SIN_GENERACION: goncloud-managed.json de configure_team hoy no
escribe "generation" y el contrato exige un entero >= 1, así que un perfil sin
el campo declara la primera generación.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

try:
    import yaml
except ImportError:  # el runtime de Hermes trae PyYAML; sin él, null con razón
    yaml = None

compartido = importlib.import_module("raices.compartido")
contrato = compartido.contrato

HOST = "gon-pc"
EXECUTOR = "cli"
# Sin "generation" en goncloud-managed.json no hay entero >= 1 que citar: se
# declara la primera generación en vez de invalidar el despacho.
SIN_GENERACION = 1


def modelo_de(perfil: Path) -> dict:
    """model.id del config.yaml del perfil (primer nivel), o {"id": None,
    "reason": causa}. PyYAML lee; una `default` anidada bajo otra clave no
    es el modelo del perfil."""
    if yaml is None:
        return {"id": None, "reason": "yaml-ausente"}
    config = Path(perfil) / "config.yaml"
    try:
        datos = yaml.safe_load(config.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # El error de parseo del PyYAML real no hereda de ValueError.
        return {"id": None, "reason": "config-ilegible"}
    except FileNotFoundError:
        return {"id": None, "reason": "sin-config"}
    except (OSError, UnicodeDecodeError, ValueError):
        return {"id": None, "reason": "config-ilegible"}
    bloque = datos.get("model") if isinstance(datos, dict) else None
    if not isinstance(bloque, dict):
        return {"id": None, "reason": "modelo-incompleto"}
    proveedor = bloque.get("provider")
    defecto = bloque.get("default")
    if (
        isinstance(proveedor, str)
        and proveedor
        and isinstance(defecto, str)
        and defecto
    ):
        return {"id": f"{proveedor}/{defecto}"}
    return {"id": None, "reason": "modelo-incompleto"}


def generacion_de(perfil: Path) -> tuple[int, str | None]:
    """(generation, source_revision) de goncloud-managed.json, con guardas."""
    try:
        datos = json.loads(
            (Path(perfil) / "goncloud-managed.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return SIN_GENERACION, None
    if not isinstance(datos, dict):
        return SIN_GENERACION, None
    crudo = datos.get("generation")
    if isinstance(crudo, bool) or not isinstance(crudo, int) or crudo < 1:
        crudo = SIN_GENERACION
    revision = datos.get("source_revision")
    return crudo, revision if isinstance(revision, str) else None


def construir(root_id, unit_id, attempt, specialist, home, permisos_h=None) -> dict:
    """El documento flota.v1 del intento que se admite. Nunca lanza."""
    if home is not None:
        perfil = Path(home) / "profiles" / specialist
        modelo = modelo_de(perfil)
        generacion, revision = generacion_de(perfil)
    else:
        modelo = {"id": None, "reason": "sin-home"}
        generacion, revision = SIN_GENERACION, None
    toolsets = (permisos_h or {}).get("toolsets") or {}
    lista = toolsets.get(specialist)
    return {
        "schema": contrato.FLOTA,
        "rootId": root_id,
        "unitId": unit_id,
        "attempt": int(attempt),
        "specialist": specialist,
        "host": HOST,
        "executor": EXECUTOR,
        "accountRef": None,
        "model": modelo,
        "generation": generacion,
        "permissions": sorted(lista) if isinstance(lista, list) else [],
        "attestations": [],
        "sourceRevision": revision,
    }
