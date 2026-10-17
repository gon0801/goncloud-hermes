"""La interseccion de los permisos de H con la configuracion real (H4b3).

Va DETRAS de hogar.leer_permisos (la unica fuente del documento): hogar lee
<raiz>/raices/permisos.json —el pedido del operador— y valida su forma (F5);
este modulo cruza los toolsets DECLARADOS por perfil con lo que el config real
le da: el hijo por tarjeta corre por la CLI, asi que manda
platform_toolsets.cli de <raiz>/profiles/<perfil>/config.yaml, traducido con la
tabla de abajo.

Ninguna entrada amplía la autoridad: un perfil declarado con write cuyo config
real no le da coding/file/terminal queda sin write (toolset-faltante); un
perfil sin config legible queda sin toolsets (perfil-sin-toolset). El YAML roto
no revienta (YAMLError del PyYAML real no hereda de ValueError).

acciones, comandos y cobertura siguen siendo datos del operador: H no tiene
otra fuente para ellos, pasan tal cual.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:  # el runtime de Hermes trae PyYAML; sin él todo falla cerrado
    yaml = None

# Toolsets de Hermes (platform_toolsets.cli) a capacidades que PermisH
# entiende. "write" si el perfil puede cambiar archivos o ejecutar comandos
# (coding, file, terminal); "read" si puede leer archivos, mirar sesiones o
# la web (write, file, session_search, web). La tabla es explícita para que
# un toolset nuevo de Hermes no otorgue nada por accidente.
CAPACIDADES_POR_TOOLSET = {
    "coding": frozenset({"write"}),
    "file": frozenset({"read", "write"}),
    "terminal": frozenset({"write"}),
    "write": frozenset({"read"}),
    "session_search": frozenset({"read"}),
    "web": frozenset({"read"}),
}


def capacidades_del_config(config_yaml: Path) -> set | None:
    """Las capacidades que el config real del perfil le da (None si el config
    no es legible: el perfil queda sin toolsets). Nunca lanza."""
    if yaml is None:
        return None
    try:
        datos = yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # El error de parseo del PyYAML real no hereda de ValueError.
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(datos, dict):
        return None
    platform = datos.get("platform_toolsets")
    if not isinstance(platform, dict):
        return None
    cli = platform.get("cli")
    if not isinstance(cli, list):
        return None
    capacidades: set = set()
    for toolset in cli:
        if isinstance(toolset, str):
            capacidades |= CAPACIDADES_POR_TOOLSET.get(toolset, frozenset())
    return capacidades


def toolsets_efectivos(raiz, datos: dict) -> dict:
    """El documento ya validado por hogar con los toolsets efectivos: lo
    declarado por perfil intersecado con el config real. Un perfil sin config
    legible sale del mapa (el gate lo niega como perfil-sin-toolset). Nunca
    lanza."""
    declarados = datos.get("toolsets")
    if not isinstance(declarados, dict) or not declarados:
        return datos
    efectivos = {}
    for perfil, lista in declarados.items():
        capacidades = capacidades_del_config(
            Path(raiz) / "profiles" / str(perfil) / "config.yaml"
        )
        if capacidades is None:
            continue  # sin config legible: sin toolsets, perfil-sin-toolset
        efectivos[perfil] = sorted(
            {cap for cap in (lista if isinstance(lista, list) else [])} & capacidades
        )
    return {**datos, "toolsets": efectivos}
