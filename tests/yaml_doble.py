"""El doble de PyYAML para los tests que montan config.yaml de perfil.

El mismo patrón del YamlDePrueba de test_sistema_agentes_flota (en la Mac de
pruebas no hay PyYAML; en la Gon-pc sí): el subset que estos tests escriben
(bloques anidados de dos niveles y listas con "- item") y YAML roto. Igual
que el real, el error de parseo es YAMLError y no hereda de ValueError.
Cuando el PyYAML real está disponible, los tests parchean con él.
"""

try:
    import yaml as yaml_real
except ImportError:  # en la Mac de pruebas no hay PyYAML; en la Gon-pc sí
    yaml_real = None


class YamlDoble:
    class YAMLError(Exception):
        pass

    @staticmethod
    def safe_load(texto):
        if "[" in texto and "]" not in texto:
            raise YamlDoble.YAMLError("yaml roto")
        lineas = [
            linea
            for linea in texto.splitlines()
            if linea.strip() and not linea.strip().startswith("#")
        ]
        datos: dict = {}
        bloque = subbloque = None
        i = 0
        while i < len(lineas):
            linea = lineas[i]
            sangria = len(linea) - len(linea.lstrip())
            limpia = linea.strip()
            if limpia.startswith("- "):
                i += 1
                continue
            clave, _, valor = limpia.partition(":")
            clave, valor = clave.strip(), valor.strip()
            if sangria == 0:
                if valor == "":
                    datos[clave] = {}
                    bloque, subbloque = clave, None
                else:
                    datos[clave] = valor
                    bloque, subbloque = None, None
            elif sangria <= 2:
                if bloque is not None:
                    if valor == "":
                        siguiente = lineas[i + 1] if i + 1 < len(lineas) else ""
                        if siguiente.strip().startswith("- "):
                            items = []
                            j = i + 1
                            while j < len(lineas) and lineas[j].strip().startswith(
                                "- "
                            ):
                                items.append(lineas[j].strip()[2:].strip())
                                j += 1
                            datos[bloque][clave] = items
                            subbloque = None
                            i = j
                            continue
                        datos[bloque][clave] = {}
                        subbloque = clave
                    else:
                        datos[bloque][clave] = valor
                        subbloque = None
            else:
                if subbloque is not None:
                    datos[bloque][subbloque][clave] = valor
            i += 1
        return datos
