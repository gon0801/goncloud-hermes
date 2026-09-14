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

Desde este repositorio, usa la entrada local de orquestacion:

```powershell
$env:HERMES_HOME = Join-Path $env:LOCALAPPDATA 'hermes'
$python = Join-Path $env:HERMES_HOME 'hermes-agent\venv\Scripts\python.exe'
& $python scripts/run_local_workflow.py start --repository C:\dev\mi-proyecto --instruction-file C:\ruta\encargo.txt --max-steps 20 --json
```

El archivo contiene un encargo acotado y autorizado. No incluyas secretos en el encargo ni en sus fuentes: el contrato se conserva en el Kanban privado para reanudar. `status --workflow-id ID --json` consulta evidencia y `resume --workflow-id ID --json` continua el mismo workflow. `--stop-after scout` permite una inspeccion inicial.

El driver local crea la cadena durable y ejecuta cada rol mediante el router de suscripciones. La aprobacion del revisor se registra mediante `WorkflowService.review`; la entrega exige esa decision. El tablero nativo conserva dependencias, comentarios e intentos entre reinicios. Hermes y su selector nativo de modelos no eligen un coordinador alternativo para esta entrada.

## Enrutador de suscripciones

La tabla de ocho posiciones por agente se compila por slot, no por prefijo historico: Primary usa OpenCode Go, F1 usa OpenCode Zen y F2-F7 se vinculan a la suscripcion de la familia del modelo (Muse, Claude Max, ChatGPT/Codex, Kimi, Z.AI Coding Plan, Grok). DeepSeek es la unica ruta facturada por API y queda marcada `billing_mode=api` en toda la evidencia. Los fallos de autenticacion, cuota, modelo o timeout avanzan de posicion; despues de una mutacion del workspace el siguiente intento recibe una continuacion con la revision y los archivos cambiados registrados, nunca una repeticion a ciegas. Las credenciales viven en los almacenes nativos de cada CLI o en el env privado `%LOCALAPPDATA%\hermes\subscription-router.env` (fuera de Git); los prompts viajan por stdin o archivo temporal privado y la salida de cada CLI se redacta y acota antes de convertirse en evidencia.

Aceptacion local 2026-09-14: 6/9 pruebas live aprobadas (Go, Muse, Claude Max, Z.AI y Grok por suscripcion; DeepSeek por API). Zen `timeout`, Codex `quota` y Kimi `transport` quedan en cuarentena durable; las tres avanzan de slot, ninguna bloquea una etapa y las tres dependen de la cuenta, no del codigo. Scout `t_f2e2bb6d` aprobado sin cambios y la cadena completa `t_be290d83` fue **entregada**: siete etapas en orden, un intento cada una, revisor aprobado y el cambio autorizado acotado a un solo archivo.

La matriz vigente, la evidencia durable y los gates estan en [docs/model-routing.md](docs/model-routing.md) y [docs/status.md](docs/status.md). `configure_subscriptions.py --live-probe-all --only ADAPTADOR --json RUTA` verifica una cuenta y persiste su salud sanitizada. Un fallo deja cuarentena durable, incluso al vencer el TTL; solo una nueva inferencia con el marcador exacto puede levantarla. Ejecuta los probes uno por uno en el orden de la tabla.

## Instalacion reproducible

Windows: Hermes oficial v0.21.2, commit `3f86ed75dad1933036c52018e991dbd839837126`. Runtime instalado en `%LOCALAPPDATA%\hermes`, upstream en `hermes-agent`, perfiles en `profiles`. No se modifica upstream.

Despues de instalar ese commit con el instalador oficial de Windows:

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts/configure_team.py
```

Reaplicar es idempotente. El configurador retira solamente el `model`/`fallback_providers` que poseia, instala la fuente literal junto al plugin y excluye Copilot. Conserva credenciales y opciones no administradas; ante conflictos se detiene para reconciliarlos. Guarda una copia `.goncloud-backup` antes del primer reemplazo. No borra perfiles.

## Modelo y ejecucion continua

La seleccion de proveedor y las pruebas con inferencia real se registran en [estado](docs/status.md). Muse usa el login oficial de su CLI dentro de WSL; sus encargos conservan el sandbox de Muse y un archivo temporal privado, eliminado al terminar, mantiene el prompt fuera de la linea de comandos. No pegues claves en el chat ni en Git. No se copiaron tokens, canales, crons ni conexiones comerciales de OpenClaw.

Telegram: stopped pending separate transport cutover

El perfil, lista de acceso y entrada de inicio se conservan. Reactivar el gateway requiere primero una cadena local completa aprobada y despues un plan separado que conecte el transporte a esa entrada, con una prueba bidireccional permitida.

## Calidad

El remoto es `github.com/gon0801/goncloud-hermes`; no hay CI configurado todavia, asi que la bateria completa se ejecuta localmente una vez al cierre y durante el desarrollo se ejecuta solamente el archivo de pruebas cambiado.

Gate final 2026-09-14: 340 tests, 337 aprobados y 3 skips; 8/8 hooks; `git diff --check` limpio; Plugin Doctor OK y gateway `stopped`. Los skips son de plataforma/privilegios. Los defectos que impedian cerrar la cadena, con su regresion, estan en [estado](docs/status.md).

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m unittest discover -s tests -v
python -m pre_commit run --all-files
```

Candados: higiene, JSON/YAML, lint y formato Python. Nunca usar `--no-verify`. El plugin usa la base Kanban nativa en directorios temporales para las pruebas; no crea trabajos reales ni llama modelos en los tests.

En esta computadora, pre-commit esta instalado en `C:\Python314\python.exe`; se puede usar ese ejecutable en lugar de `python`. La bateria usa el Python del runtime Hermes, que contiene las dependencias nativas del tablero.

## Alcance de autonomia

Resolver detalles tecnicos y consultar entre roles entra en una orden autorizada. Gastos, mensajes externos, secretos, borrados amplios o despliegues fuera del alcance requieren tu decision. La politica y el plugin no son una sandbox: un proceso con tus permisos Windows puede usar terminal y acceder a tus archivos. No se promete inmunidad a instrucciones maliciosas ni correccion matematica de una revision por IA.

Fuentes comerciales heredadas: packing usa MercadoLibre y Amazon Seller Central/Flex, no Odoo. Toda regla nueva debe conservar origen y fecha; no se importo informacion comercial viva.
