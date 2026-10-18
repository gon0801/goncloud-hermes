#!/usr/bin/env python3
"""La puerta de capacidades de la ruta de raíces (SUA-07 H4a).

La decisión es pura sobre hechos armados a mano (sin PC): el orden fijo de
causas, el par vivo contra el certificado y la forma de certificadas.json
sobre el archivo real del repo. Los lectores se prueban con archivos de
verdad en un home temporal: por construcción el par vive en la raíz del
home, nunca en perfiles.
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

import hermes_falso  # noqa: E402

capacidades = importlib.import_module("raices.capacidades")

CERTIFICADAS = REPO / "plugins/goncloud-team/raices/certificadas.json"

SHA = "a" * 40
OTRO_SHA = "b" * 40
VERSION = capacidades.VersionCLI("0.21.5", SHA[:7])
TEXTO_VERSION = f"Hermes Agent v0.21.5+5822.g{SHA[:7]} (2026.9.24) · upstream e4635a16"


def par_vivo(home="/home/hermes"):
    return capacidades.ParGateway(
        kind="hermes-gateway",
        gateway_state="running",
        code_version="0.21.5",
        code_sha=SHA,
        hermes_home=str(home),
        updated_at="2026-10-17T00:00:00Z",
    )


def hechos_de(
    version=VERSION,
    gateway=par_vivo(),
    certificadas=(),
    capacidades_vivas=None,
    home="/home/hermes",
):
    vivas = capacidades_vivas
    if vivas is None:
        vivas = frozenset(capacidades.CAPACIDADES_REQUERIDAS)
    return capacidades.Hechos(version, gateway, tuple(certificadas), vivas, home)


def entrada(version="0.21.5", sha=SHA):
    return {
        "version": version,
        "code_sha": sha,
        "certificadoEn": "2026-10-17T00:00:00Z",
        "evidencia": {"pruebas": "tests.test_sistema_agentes_kanban_real"},
    }


def importador_con(*nombres):
    class Modulo:
        pass

    modulo = Modulo()
    for nombre in nombres:
        setattr(modulo, nombre, lambda: None)
    return lambda _nombre: modulo


class Certificadas(unittest.TestCase):
    def test_el_archivo_del_repo_es_una_lista_json(self):
        entradas = json.loads(CERTIFICADAS.read_text(encoding="utf-8"))
        self.assertIsInstance(entradas, list)

    def test_las_entradas_del_repo_tienen_la_forma_del_contrato(self):
        entradas = json.loads(CERTIFICADAS.read_text(encoding="utf-8"))
        self.assertEqual(capacidades.validar_certificadas(entradas), [])


class ValidarCertificadas(unittest.TestCase):
    def test_acepta_la_lista_vacia_y_una_entrada_valida(self):
        self.assertEqual(capacidades.validar_certificadas([]), [])
        self.assertEqual(capacidades.validar_certificadas([entrada()]), [])

    def test_rechaza_el_sha_corto(self):
        errores = capacidades.validar_certificadas([entrada(sha="a" * 39)])
        self.assertTrue(errores)

    def test_rechaza_el_sha_con_mayusculas(self):
        errores = capacidades.validar_certificadas([entrada(sha="A" * 40)])
        self.assertTrue(errores)

    def test_rechaza_la_version_mal_formada(self):
        errores = capacidades.validar_certificadas([entrada(version="0.21")])
        self.assertTrue(errores)

    def test_rechaza_los_duplicados(self):
        errores = capacidades.validar_certificadas([entrada(), entrada()])
        self.assertTrue(errores)

    def test_rechaza_el_instante_y_la_evidencia_ausentes(self):
        sin_instante = entrada()
        del sin_instante["certificadoEn"]
        sin_evidencia = entrada()
        del sin_evidencia["evidencia"]
        self.assertTrue(capacidades.validar_certificadas([sin_instante]))
        self.assertTrue(capacidades.validar_certificadas([sin_evidencia]))


class LeerVersion(unittest.TestCase):
    def test_parsea_la_salida_del_cli(self):
        hallada = capacidades.leer_version(lambda: TEXTO_VERSION)
        self.assertEqual(hallada, capacidades.VersionCLI("0.21.5", SHA[:7]))

    def test_un_texto_sin_coincidencia_da_none(self):
        self.assertIsNone(capacidades.leer_version(lambda: "otra cosa 1.2.3"))

    def test_un_runner_que_lanza_da_none(self):
        def explota():
            raise OSError("no hay hermes")

        self.assertIsNone(capacidades.leer_version(explota))

    def test_un_runner_que_no_trae_texto_da_none(self):
        self.assertIsNone(capacidades.leer_version(lambda: None))

    def test_una_version_sin_sufijo_g_no_da_par(self):
        self.assertIsNone(
            capacidades.leer_version(lambda: "Hermes Agent v0.21.5 (2026.9.24)")
        )

    def test_la_linea_literal_de_022_parsea_version_y_sha_local(self):
        linea = "Hermes Agent v0.22.0 (2026.9.24) · stable · local e06df9c2eae6"
        self.assertEqual(
            capacidades.leer_version(lambda: linea),
            capacidades.VersionCLI("0.22.0", "e06df9c2eae6"),
        )

    def test_el_sufijo_g_manda_sobre_la_forma_de_release(self):
        linea = (
            f"Hermes Agent v0.21.5+5822.g{SHA[:7]} (2026.9.24) · stable · local {SHA}"
        )
        self.assertEqual(capacidades.leer_version(lambda: linea), VERSION)

    def test_una_version_de_release_sin_local_da_none(self):
        self.assertIsNone(
            capacidades.leer_version(
                lambda: "Hermes Agent v0.22.0 (2026.9.24) · stable"
            )
        )

    def test_upstream_sin_local_ni_sufijo_g_da_none(self):
        self.assertIsNone(
            capacidades.leer_version(
                lambda: "Hermes Agent v0.22.0 (2026.9.24) · upstream e4635a16"
            )
        )

    def test_un_hex_tras_local_de_menos_de_siete_no_cuenta(self):
        self.assertIsNone(
            capacidades.leer_version(
                lambda: "Hermes Agent v0.22.0 (2026.9.24) · stable · local e06df9"
            )
        )


class ElEjecutableDeVersion(unittest.TestCase):
    """Windows: el CLI vive en %LOCALAPPDATA%\\hermes\\bin\\hermes.CMD y
    CreateProcess sin shell no resuelve el nombre pelado (WinError 2). El
    lector único llama a la ruta que devuelve shutil.which (respeta PATHEXT)
    y sin ruta no llama a nada: fail-closed."""

    def test_subprocess_recibe_la_ruta_resuelta_no_el_nombre_pelado(self):
        recibido = {}

        def falso_run(argv, **kwargs):
            recibido["argv"] = list(argv)
            recibido["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, stdout=TEXTO_VERSION, stderr="")

        with mock.patch.object(capacidades.subprocess, "run", falso_run):
            texto = capacidades._correr_hermes_version(
                resolvedor=lambda nombre: "/su/camino/bin/hermes.CMD"
            )
        self.assertEqual(texto, TEXTO_VERSION)
        self.assertEqual(recibido["argv"][0], "/su/camino/bin/hermes.CMD")
        self.assertEqual(recibido["argv"][1], "--version")
        # La lectura es text=True pero con encoding fijo: sin el, Windows
        # decodea con la code page local y el texto de --version se rompe
        # (mojibake) en una PC no UTF-8.
        self.assertEqual(recibido["kwargs"].get("encoding"), "utf-8")
        self.assertEqual(recibido["kwargs"].get("errors"), "replace")

    def test_sin_ruta_resuelta_no_invoca_subprocess_y_da_none(self):
        llamadas = []

        def falso_run(argv, **kwargs):
            llamadas.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=TEXTO_VERSION)

        with mock.patch.object(capacidades.subprocess, "run", falso_run):
            self.assertIsNone(
                capacidades._correr_hermes_version(resolvedor=lambda nombre: None)
            )
        self.assertEqual(llamadas, [])

    @unittest.skipUnless(os.name == "posix", "el doble de CLI es un script sh")
    def test_la_resolucion_por_omision_honra_el_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            falso = Path(tmp) / "hermes"
            falso.write_text(
                "#!/bin/sh\necho 'Hermes Agent v0.21.5+5822.gaabbccd (2026.9.24)'\n"
            )
            falso.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": tmp}):
                texto = capacidades._correr_hermes_version()
        self.assertIsNotNone(texto, "el hermes del PATH temporal debe correr")
        self.assertIn("v0.21.5", texto)


class LeerGateway(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.home = Path(self.directorio.name) / "hermes"

    def escribir(self, documento, *partes):
        partes = partes or ("gateway_state.json",)
        camino = self.home.joinpath(*partes)
        camino.parent.mkdir(parents=True, exist_ok=True)
        camino.write_text(
            documento if isinstance(documento, str) else json.dumps(documento),
            encoding="utf-8",
        )
        return camino

    def documento_de_gateway(self, estado="running"):
        return {
            "kind": "hermes-gateway",
            "gateway_state": estado,
            "code_version": "0.21.5",
            "code_sha": SHA,
            "hermes_home": str(self.home),
            "updated_at": "2026-10-17T00:00:00Z",
        }

    def test_solo_lee_el_gateway_state_de_la_raiz(self):
        self.escribir(
            self.documento_de_gateway("stopped"),
            "perfiles",
            "claw",
            "gateway_state.json",
        )
        self.escribir(self.documento_de_gateway("running"))
        par = capacidades.leer_gateway(self.home)
        self.assertEqual(par.gateway_state, "running")
        self.assertEqual(par.code_sha, SHA)

    def test_la_raiz_detenida_se_ve_detenida(self):
        self.escribir(self.documento_de_gateway("stopped"))
        par = capacidades.leer_gateway(self.home)
        self.assertEqual(par.gateway_state, "stopped")

    def test_el_json_corrupto_da_none(self):
        self.escribir("{ no es json")
        self.assertIsNone(capacidades.leer_gateway(self.home))

    def test_el_json_que_no_es_objeto_da_none(self):
        self.escribir("[1, 2]")
        self.assertIsNone(capacidades.leer_gateway(self.home))

    def test_sin_archivo_da_none(self):
        self.assertIsNone(capacidades.leer_gateway(self.home))


class LeerCertificadas(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.ruta = Path(self.directorio.name) / "certificadas.json"

    def test_la_ruta_inexistente_da_la_tupla_vacia(self):
        self.assertEqual(capacidades.leer_certificadas(self.ruta), ())

    def test_un_archivo_que_no_es_lista_da_la_tupla_vacia(self):
        self.ruta.write_text('{"version": "0.21.5"}', encoding="utf-8")
        self.assertEqual(capacidades.leer_certificadas(self.ruta), ())

    def test_la_lista_entera_como_tupla(self):
        self.ruta.write_text(json.dumps([entrada()]), encoding="utf-8")
        self.assertEqual(capacidades.leer_certificadas(self.ruta), (entrada(),))


class Sondeo(unittest.TestCase):
    def test_con_el_doble_instalado_hay_cinco_capacidades(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        halladas = capacidades.sondear_capacidades()
        self.assertEqual(halladas, frozenset(capacidades.CAPACIDADES_REQUERIDAS))

    def test_el_importe_que_falla_da_el_conjunto_vacio(self):
        def explota(_nombre):
            raise ImportError("no hay hermes_cli")

        self.assertEqual(capacidades.sondear_capacidades(explota), frozenset())

    def test_cada_nombre_se_busca_en_su_modulo(self):
        halladas = capacidades.sondear_capacidades(importador_con("create_task"))
        self.assertEqual(halladas, frozenset({"create_task"}))


class Decidir(unittest.TestCase):
    def test_el_par_vivo_certificado_abre(self):
        hechos = hechos_de(certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (True, None))

    def test_la_version_distinta_cierra_por_version_no_certificada(self):
        hechos = hechos_de(certificadas=[entrada(version="0.22.0")])
        self.assertEqual(capacidades.decidir(hechos), (False, "version-no-certificada"))

    def test_el_sha_distinto_cierra_por_version_no_certificada(self):
        hechos = hechos_de(certificadas=[entrada(sha=OTRO_SHA)])
        self.assertEqual(capacidades.decidir(hechos), (False, "version-no-certificada"))

    def test_el_sufijo_que_no_es_prefijo_del_sha_deja_el_par_incierto(self):
        version = capacidades.VersionCLI("0.21.5", OTRO_SHA[:7])
        hechos = hechos_de(version=version, certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (False, "par-incierto"))

    def test_el_kind_que_no_es_hermes_gateway_deja_el_par_incierto(self):
        gateway = par_vivo()._replace(kind="hermes-perfil")
        hechos = hechos_de(gateway=gateway, certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (False, "par-incierto"))

    def test_el_gateway_detenido_deja_el_par_incierto(self):
        gateway = par_vivo()._replace(gateway_state="stopped")
        hechos = hechos_de(gateway=gateway, certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (False, "par-incierto"))

    def test_el_code_sha_mal_formado_deja_el_par_incierto(self):
        gateway = par_vivo()._replace(code_sha=SHA.upper())
        hechos = hechos_de(gateway=gateway, certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (False, "par-incierto"))

    def test_el_code_version_distinto_deja_el_par_incierto(self):
        gateway = par_vivo()._replace(code_version="0.21.6")
        hechos = hechos_de(gateway=gateway, certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (False, "par-incierto"))

    def test_el_hermes_home_distinto_deja_el_par_incierto(self):
        gateway = par_vivo()._replace(hermes_home="/otro/home")
        hechos = hechos_de(gateway=gateway, certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (False, "par-incierto"))

    def test_el_updated_at_vacio_deja_el_par_incierto(self):
        gateway = par_vivo()._replace(updated_at="")
        hechos = hechos_de(gateway=gateway, certificadas=[entrada()])
        self.assertEqual(capacidades.decidir(hechos), (False, "par-incierto"))

    def test_sin_version_o_sin_gateway_el_par_es_incierto(self):
        self.assertEqual(
            capacidades.decidir(hechos_de(version=None)), (False, "par-incierto")
        )
        self.assertEqual(
            capacidades.decidir(hechos_de(gateway=None, certificadas=[entrada()])),
            (False, "par-incierto"),
        )

    def test_la_capacidad_faltante_cierra_Aunque_el_par_este_certificado(self):
        hechos = hechos_de(certificadas=[entrada()], capacidades_vivas=frozenset())
        self.assertEqual(
            capacidades.decidir(hechos),
            (False, "capacidad-faltante:create_task"),
        )

    def test_la_causa_nombra_la_primera_capacidad_que_falta(self):
        vivas = frozenset(("create_task", "complete_task", "block_task", "write_txn"))
        hechos = hechos_de(capacidades_vivas=vivas)
        self.assertEqual(
            capacidades.decidir(hechos), (False, "capacidad-faltante:link_tasks")
        )

    def test_capacidades_presentas_con_par_sin_certificar_no_abre(self):
        self.assertEqual(
            capacidades.decidir(hechos_de(certificadas=[])),
            (False, "version-no-certificada"),
        )

    def test_el_par_de_release_022_sin_certificar_da_version_no_certificada(self):
        version = capacidades.VersionCLI("0.22.0", "e06df9c2eae6")
        gateway = par_vivo()._replace(
            code_version="0.22.0",
            code_sha="e06df9c2eae6901f4c88f464e0ae6e753c0428b8",
        )
        hechos = hechos_de(version=version, gateway=gateway)
        self.assertEqual(capacidades.decidir(hechos), (False, "version-no-certificada"))

    def test_el_par_de_release_022_certificado_abre(self):
        sha22 = "e06df9c2eae6901f4c88f464e0ae6e753c0428b8"
        version = capacidades.VersionCLI("0.22.0", "e06df9c2eae6")
        gateway = par_vivo()._replace(code_version="0.22.0", code_sha=sha22)
        hechos = hechos_de(
            version=version,
            gateway=gateway,
            certificadas=[entrada(version="0.22.0", sha=sha22)],
        )
        self.assertEqual(capacidades.decidir(hechos), (True, None))

    def test_decidir_nunca_lanza_con_hechos_rotos(self):
        rotos = (
            capacidades.Hechos(42, "x", None, None, None),
            capacidades.Hechos(None, 7, 3, frozenset(), None),
            capacidades.Hechos(VERSION, par_vivo(), [{"version": 1}], 9, "/h"),
        )
        for hechos in rotos:
            with self.subTest(hechos=hechos):
                abierta, causa = capacidades.decidir(hechos)
                self.assertFalse(abierta)
                self.assertIsInstance(causa, str)


class PuertaViva(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.home = Path(self.directorio.name) / "hermes"
        self.home.mkdir()
        self.certificados = Path(self.directorio.name) / "certificadas.json"
        self.certificados.write_text(json.dumps([entrada()]), encoding="utf-8")
        (self.home / "gateway_state.json").write_text(
            json.dumps(
                {
                    "kind": "hermes-gateway",
                    "gateway_state": "running",
                    "code_version": "0.21.5",
                    "code_sha": SHA,
                    "hermes_home": str(self.home),
                    "updated_at": "2026-10-17T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        self.todo = importador_con(*capacidades.CAPACIDADES_REQUERIDAS)

    def abrir(self, runner=lambda: TEXTO_VERSION, certificados=None, home=None):
        return capacidades.abrir_ruta(
            home=home or self.home,
            runner=runner,
            importador=self.todo,
            ruta_certificados=certificados or self.certificados,
        )

    def test_el_par_vivo_certificado_abre_la_ruta(self):
        self.assertEqual(self.abrir(), (True, None))

    def test_el_certificado_ilegible_cuenta_como_vacio(self):
        inexistente = Path(self.directorio.name) / "no-hay" / "certificadas.json"
        self.assertEqual(
            self.abrir(certificados=inexistente),
            (False, "version-no-certificada"),
        )

    def test_el_estado_detenido_de_un_perfil_no_cuenta(self):
        perfiles = self.home / "perfiles" / "claw"
        perfiles.mkdir(parents=True)
        (perfiles / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "stopped"}), encoding="utf-8"
        )
        self.assertEqual(self.abrir(), (True, None))

    def test_la_raiz_detenida_cierra_por_par_incierto(self):
        (self.home / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "stopped"}), encoding="utf-8"
        )
        self.assertEqual(self.abrir(), (False, "par-incierto"))

    def test_hermes_ausente_cierra_por_par_incierto(self):
        def explota():
            raise OSError("no hay hermes")

        self.assertEqual(self.abrir(runner=explota), (False, "par-incierto"))

    def test_una_salida_de_version_sin_coincidencia_cierra_por_par_incierto(self):
        self.assertEqual(
            self.abrir(runner=lambda: "no entiende nada"), (False, "par-incierto")
        )


if __name__ == "__main__":
    unittest.main()
