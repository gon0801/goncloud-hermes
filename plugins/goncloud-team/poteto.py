"""La copia fijada de poteto (SUA-07 H4b2): verificación, publicación y sonda.

El espejo shared/sistema-agentes/poteto repite lo que G fijó en su manifiesto
poteto.v1.json; hermes.json registra qué digest, generación, adaptación y
bloque desplegó Hermes. comprobar_copia es la aduana: todo desvío es
CopiaRota y quien despliega propaga el error (falla cerrada, sin despliegue
parcial).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COPIA = REPO / "shared/sistema-agentes/poteto"
MANIFIESTO = "poteto.v1.json"
REGISTRO = "hermes.json"
ADAPTACION = "adaptacion-hermes.md"
BLOQUE = "bloque-soul-hermes.md"

#: Las skills de especialidad por perfil según perfiles.v1.json de G. El
#: espejo de G en este repo no trae ese archivo (solo docs y fixtures), así
#: que el mapeo vive fijado aquí; poteto-mode y las principle-* viajan
#: siempre y las excluidas de hermes.json nunca se publican.
ESPECIALIDADES = {
    "ingenieria": ("architect", "how", "tdd"),
    "reviewer": ("blast-radius", "how"),
    "adversary": ("blast-radius", "how"),
    "verifier": ("how",),
    "entrega": (),
    "operaciones": (),
    "scout": (),
    "claw": (),
    # F10: G no dice nada de implementer (ni poteto.v1.json ni hermes.json
    # lo citan); va () explicito como entrega/operaciones para que quede
    # dicho que la decision existe.
    "implementer": (),
}


class CopiaRota(ValueError):
    """La copia no cuadra con lo que hermes.json registra."""


def _leer_json(copia: Path, nombre: str) -> dict:
    ruta = Path(copia) / nombre
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CopiaRota(f"{nombre}: {exc}") from exc
    if not isinstance(datos, dict):
        raise CopiaRota(f"{nombre}: no es un objeto")
    return datos


def _leer_registro(copia: Path) -> dict:
    registro = _leer_json(copia, REGISTRO)
    for campo in ("manifest", "adaptacion", "bloque", "excluidos"):
        if campo not in registro:
            raise CopiaRota(f"{REGISTRO}: falta {campo}")
    return registro


def leer_manifiesto(copia=COPIA) -> dict:
    """poteto.v1.json de la copia, con guardas; malformado es CopiaRota."""
    manifiesto = _leer_json(copia, MANIFIESTO)
    archivos = manifiesto.get("files")
    if not isinstance(archivos, list):
        raise CopiaRota(f"{MANIFIESTO}: files no es una lista")
    for fila in archivos:
        if (
            not isinstance(fila, dict)
            or not isinstance(fila.get("path"), str)
            or not isinstance(fila.get("sha256"), str)
        ):
            raise CopiaRota(f"{MANIFIESTO}: fila de files malformada")
    if not isinstance(manifiesto.get("digest"), str):
        raise CopiaRota(f"{MANIFIESTO}: digest no es texto")
    if isinstance(manifiesto.get("generation"), bool) or not isinstance(
        manifiesto.get("generation"), int
    ):
        raise CopiaRota(f"{MANIFIESTO}: generation no es entero")
    return manifiesto


def digest_de_manifiesto(manifiesto=None) -> str:
    """El digest canónico del manifiesto, la misma forma que G (contexto.py,
    digest_de): json sin el campo digest, sort_keys y compacto."""
    if manifiesto is None:
        manifiesto = leer_manifiesto()
    canonico = json.dumps(
        {k: v for k, v in manifiesto.items() if k != "digest"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def comprobar_copia(copia=COPIA) -> dict:
    """La aduana de la copia: cada archivo del manifiesto con su sha256 (salvo
    los excluidos), el digest canónico == el del manifiesto == el registrado,
    la generación registrada == la del manifiesto, y la adaptación y el bloque
    con el sha que hermes.json firma. Devuelve el registro."""
    copia = Path(copia)
    registro = _leer_registro(copia)
    manifiesto = leer_manifiesto(copia)
    excluidos = registro["excluidos"]
    if not isinstance(excluidos, dict):
        raise CopiaRota(f"{REGISTRO}: excluidos no es un objeto")
    for fila in manifiesto["files"]:
        ruta = fila["path"]
        if ruta in excluidos:
            continue
        try:
            contenido = (copia / ruta).read_bytes()
        except OSError as exc:
            raise CopiaRota(f"{ruta}: {exc}") from exc
        if hashlib.sha256(contenido).hexdigest() != fila["sha256"]:
            raise CopiaRota(f"{ruta}: sha256 no cuadra con el manifiesto")
    calculado = digest_de_manifiesto(manifiesto)
    if manifiesto["digest"] != calculado:
        raise CopiaRota(f"{MANIFIESTO}: digest {manifiesto['digest']} != {calculado}")
    registrado = registro["manifest"]
    if not isinstance(registrado, dict) or registrado.get("digest") != calculado:
        raise CopiaRota(f"{REGISTRO}: manifest.digest != {calculado}")
    if registro.get("generation") != manifiesto["generation"]:
        raise CopiaRota(
            f"{REGISTRO}: generation {registro.get('generation')} != "
            f"{manifiesto['generation']}"
        )
    for nombre, campo in ((ADAPTACION, "adaptacion"), (BLOQUE, "bloque")):
        try:
            contenido = (copia / nombre).read_bytes()
        except OSError as exc:
            raise CopiaRota(f"{nombre}: {exc}") from exc
        if "sha256:" + hashlib.sha256(contenido).hexdigest() != registro.get(campo):
            raise CopiaRota(f"{nombre}: sha256 != {REGISTRO}[{campo}]")
    return registro


def bloque(copia=COPIA) -> str:
    """El texto de bloque-soul-hermes.md, que configura el SOUL del perfil."""
    return (Path(copia) / BLOQUE).read_text(encoding="utf-8")


def skills_a_publicar(perfil, copia=COPIA) -> list[str]:
    """Las rutas del manifiesto que publica un perfil de H: todas las de
    poteto-mode y las principle-*, más las de su especialidad; nunca las
    excluidas ni las que el manifiesto no traiga."""
    manifiesto = leer_manifiesto(copia)
    excluidos = _leer_registro(copia)["excluidos"]
    propias = ESPECIALIDADES.get(perfil, ())
    publicadas = []
    for fila in manifiesto["files"]:
        ruta = fila["path"]
        partes = ruta.split("/")
        if len(partes) < 2 or partes[0] != "skills":
            continue
        skill = partes[1]
        if ruta in excluidos:
            continue
        if skill == "poteto-mode" or skill.startswith("principle-") or skill in propias:
            publicadas.append(ruta)
    return publicadas


def siguiente_generation(estado, archivos) -> int:
    """La generación del próximo despliegue: sin estado previo (o sin
    generation) es 1; algún contenido nuevo o distinto en las claves de
    `archivos` sube en uno; sin cambios se conserva la previa (o 1)."""
    previa = None
    if isinstance(estado, dict):
        crudo = estado.get("generation")
        if isinstance(crudo, bool) or not isinstance(crudo, int):
            crudo = None
        previa = crudo
    if previa is None:
        return 1
    previos = estado.get("files") if isinstance(estado.get("files"), dict) else {}
    if any(previos.get(clave) != contenido for clave, contenido in archivos.items()):
        return previa + 1
    return previa


def paquete_del_perfil(home, specialist) -> dict | None:
    """El bloque "poteto" de <home>/profiles/<specialist>/goncloud-managed.json
    si existe y es dict; si no, None. Es la puerta del despacho: sin paquete
    la ruta no se admite."""
    try:
        datos = json.loads(
            (Path(home) / "profiles" / specialist / "goncloud-managed.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    paquete = datos.get("poteto") if isinstance(datos, dict) else None
    return paquete if isinstance(paquete, dict) else None


def sonar(home, perfil) -> list[str]:
    """Las fallas de un perfil desplegado en <home>. Sin copia desplegada el
    perfil no tiene poteto: es la primera falla, no un pase."""
    home = Path(home)
    copia = home / "shared/sistema-agentes/poteto"
    if not copia.is_dir():
        return ["copia de poteto ausente en el perfil"]
    try:
        registro = comprobar_copia(copia)
    except CopiaRota as rota:
        return [f"copia rota: {rota}"]
    manifiesto = leer_manifiesto(copia)
    digest = digest_de_manifiesto(manifiesto)
    fallas = []
    soul = home / "SOUL.md"
    texto = soul.read_text(encoding="utf-8") if soul.exists() else ""
    marca_de_bloque = (
        "<!-- sua-contexto inicio",
        "<!-- sua-contexto fin -->",
        f"generacion={registro['generation']}",
        registro["manifest"]["digest"],
    )
    if not all(marca in texto for marca in marca_de_bloque):
        fallas.append(
            "SOUL.md sin el bloque de poteto "
            f"(generacion={registro['generation']} digest={digest})"
        )
    sha_por_ruta = {fila["path"]: fila["sha256"] for fila in manifiesto["files"]}
    descuadradas = []
    for ruta in skills_a_publicar(perfil, copia):
        # La ruta del manifiesto ya trae skills/; el perfil la publica tal
        # cual bajo su raíz (skills/<skill>/SKILL.md).
        try:
            contenido = (home / ruta).read_bytes()
        except OSError:
            descuadradas.append(ruta)
            continue
        if hashlib.sha256(contenido).hexdigest() != sha_por_ruta.get(ruta):
            descuadradas.append(ruta)
    if descuadradas:
        muestra = ", ".join(descuadradas[:5])
        resto = f" (y {len(descuadradas) - 5} mas)" if len(descuadradas) > 5 else ""
        fallas.append(f"skills que no cuadran con el manifiesto: {muestra}{resto}")
    # La adaptación de Hermes publicada por perfil (configure_team la escribe
    # en skills/poteto-hermes/SKILL.md): su sha va en el registro. Ausente o
    # editada localmente es una falla: el hijo correría sin su adaptación o
    # con una versión que la copia fijada no respalda.
    adaptacion = home / "skills/poteto-hermes/SKILL.md"
    try:
        sha_adaptacion = hashlib.sha256(adaptacion.read_bytes()).hexdigest()
    except OSError:
        fallas.append(
            "skills/poteto-hermes/SKILL.md ausente "
            f"(adaptacion={registro['adaptacion']})"
        )
    else:
        if f"sha256:{sha_adaptacion}" != registro["adaptacion"]:
            fallas.append(
                "skills/poteto-hermes/SKILL.md no cuadra con la adaptacion "
                f"del registro ({registro['adaptacion']})"
            )
    try:
        gestionado = json.loads(
            (home / "goncloud-managed.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        gestionado = None
    paquete = gestionado.get("poteto") if isinstance(gestionado, dict) else None
    if not (isinstance(paquete, dict) and paquete.get("digest") == digest):
        fallas.append(f"goncloud-managed.json sin poteto.digest == {digest}")
    return fallas
