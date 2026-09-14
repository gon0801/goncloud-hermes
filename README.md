# Goncloud Hermes

Equipo local de nueve perfiles Hermes: un director, negocio y una cadena de ingenieria con verificacion independiente. Configuracion versionada; credenciales, memoria y tareas permanecen fuera de Git.

```text
Tu objetivo -> claw (director)
                 |-> operaciones <-> especialistas mediante consultas persistentes
                 `-> scout -> ingenieria -> implementer -> verifier
                          -> adversary -> reviewer -> entrega -> claw
```

Una ronda de revision; segunda solamente por hallazgos HIGH en la primera. Si quedan problemas, se bloquea la entrega. Cada traspaso debe identificar el repositorio, revision y evidencia. Los perfiles son identidades de trabajo, no usuarios Windows aislados.

## Uso local

Desde este repositorio, abre el director:

```powershell
.\scripts\hermes.ps1
```

Ejemplo de orden: "En C:\dev\mi-proyecto, implementa X, comprueba Y y entrega una rama local. Consulta entre especialistas; no publiques ni despliegues."

El director utiliza `goncloud_engineering_start` para crear la cadena, `goncloud_consult` para preguntar a otro rol y `goncloud_engineering_status` para consolidar resultados. El revisor utiliza `goncloud_engineering_review`. Cualquier rol puede encargar una tarea especializada a la suscripcion autenticada de Muse mediante `goncloud_muse_delegate`, sin pedir al usuario que retransmita mensajes. El tablero nativo conserva dependencias, comentarios e intentos entre reinicios.

## Instalacion reproducible

Windows: Hermes oficial v0.21.2, commit `3f86ed75dad1933036c52018e991dbd839837126`. Runtime instalado en `%LOCALAPPDATA%\hermes`, upstream en `hermes-agent`, perfiles en `profiles`. No se modifica upstream.

Despues de instalar ese commit con el instalador oficial de Windows:

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts/configure_team.py
```

Reaplicar es idempotente. Se conservan modelo, credenciales y opciones no administradas. Si cambias una opcion o archivo administrado, el script se detiene y pide reconciliar; no lo pisa. Guarda una copia `.goncloud-backup` antes del primer reemplazo. No borra perfiles.

## Modelo y ejecucion continua

La seleccion de proveedor y las pruebas con inferencia real se registran en [estado](docs/status.md). Muse usa el login oficial de su CLI dentro de WSL; sus encargos conservan el sandbox de Muse y un archivo temporal privado, eliminado al terminar, mantiene el prompt fuera de la linea de comandos. No pegues claves en el chat ni en Git. No se copiaron tokens, canales, crons ni conexiones comerciales de OpenClaw.

El despacho automatico necesita el gateway de `claw` ejecutandose. En esta computadora se instalo el inicio automatico de sesion de Windows para el gateway, pero permanece detenido hasta conectar el bot nuevo de Telegram con token y lista de usuarios autorizados.

## Calidad

No hay remoto ni CI configurados inicialmente: la bateria completa se ejecuta localmente una vez al cierre; durante desarrollo se ejecuta solamente el archivo de pruebas cambiado.

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m unittest discover -s tests -v
python -m pre_commit run --all-files
```

Candados: higiene, JSON/YAML, lint y formato Python. Nunca usar `--no-verify`. El plugin usa la base Kanban nativa en directorios temporales para las pruebas; no crea trabajos reales ni llama modelos en los tests.

En esta computadora, pre-commit esta instalado en `C:\Python314\python.exe`; se puede usar ese ejecutable en lugar de `python`. La bateria usa el Python del runtime Hermes, que contiene las dependencias nativas del tablero.

## Alcance de autonomia

Resolver detalles tecnicos y consultar entre roles entra en una orden autorizada. Gastos, mensajes externos, secretos, borrados amplios o despliegues fuera del alcance requieren tu decision. La politica y el plugin no son una sandbox: un proceso con tus permisos Windows puede usar terminal y acceder a tus archivos. No se promete inmunidad a instrucciones maliciosas ni correccion matematica de una revision por IA.

Fuentes comerciales heredadas: packing usa MercadoLibre y Amazon Seller Central/Flex, no Odoo. Toda regla nueva debe conservar origen y fecha; no se importo informacion comercial viva.
