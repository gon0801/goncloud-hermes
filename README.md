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

La tabla de siete posiciones por agente se compila por slot, no por prefijo historico: Primary usa OpenCode Go y F1-F6 se vinculan a la suscripcion de la familia del modelo (Muse, Claude, ChatGPT/Codex, Kimi, Z.AI Coding Plan, Grok). Los marcadores de la tabla son ejecutables: en `main`, Claude Opus 5 usa `xhigh` y Muse Spark usa `max`. DeepSeek es la unica ruta facturada por API y queda marcada `billing_mode=api` en toda la evidencia. Los fallos de autenticacion, cuota, modelo o timeout avanzan de posicion; despues de una mutacion del workspace el siguiente intento recibe una continuacion con la revision y los archivos cambiados registrados, nunca una repeticion a ciegas. Las credenciales viven en los almacenes nativos de cada CLI o en el env privado `%LOCALAPPDATA%\hermes\subscription-router.env` (fuera de Git); los prompts viajan por stdin o archivo temporal privado y la salida de cada CLI se redacta y acota antes de convertirse en evidencia.

Las conversaciones de `claw` usan proveedores Hermes nativos compilados directamente de esa misma fila (`opencode-go`, `anthropic`, `xai-oauth`, `zai`, `openai-codex`, `kimi-coding`), con llamadas a herramientas nativas; no hay un proveedor local que las escriba como texto. La posicion de Muse se salta en esas cadenas nativas y en las de claw (`api_only_prefixes` en `claw-routing.json`): su unico camino nativo es la API de Meta, que se paga por uso, y Muse solo debe gastar la suscripcion, que llega por su CLI en el enrutador de trabajadores (decision del propietario, 2026-10-15). Si Primary falla, el siguiente intento es F1 Claude Opus 5 `xhigh`, no Kimi. Detalle en [docs/model-routing.md](docs/model-routing.md).

Aceptacion local 2026-09-14: 8/9 pruebas live aprobadas (Go, Zen, Muse, Claude Max, Kimi, Z.AI y Grok por suscripcion; DeepSeek por API). Solo Codex queda en cuarentena por `quota`, con fecha de reposicion declarada por el proveedor; avanza de slot y no bloquea ninguna etapa. En esta maquina el router necesita la variable de usuario `NODE_EXTRA_CA_CERTS` apuntando a la raiz TLS de Kaspersky (detalle en [docs/status.md](docs/status.md)); un proceso arrancado sin ella vera colgarse a Zen y fallar a Kimi. Scout `t_f2e2bb6d` aprobado sin cambios y la cadena completa `t_be290d83` fue **entregada**: siete etapas en orden, un intento cada una, revisor aprobado y el cambio autorizado acotado a un solo archivo.

La matriz vigente, la evidencia durable y los gates estan en [docs/model-routing.md](docs/model-routing.md) y [docs/status.md](docs/status.md). `configure_subscriptions.py --live-probe-all --only ADAPTADOR --json RUTA` verifica una cuenta y persiste su salud sanitizada. Un fallo deja cuarentena durable durante una hora; despues el router vuelve a intentar esa cuenta por si solo cuando un encargo llega a su posicion, y solo una inferencia real exitosa la levanta (un fallo repetido la vuelve a armar por otra hora). La cuota que se agota y se repone entra sola sin correr nada a mano. Ejecuta los probes uno por uno en el orden de la tabla.

## Instalacion reproducible

Windows: Hermes oficial v0.21.2, commit `3f86ed75dad1933036c52018e991dbd839837126`. Runtime instalado en `%LOCALAPPDATA%\hermes`, upstream en `hermes-agent`, perfiles en `profiles`. No se modifica upstream.

Despues de instalar ese commit con el instalador oficial de Windows:

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts/configure_team.py
```

Antes de cada despliegue corre el mismo comando con `--check`: valida los nueve perfiles contra este checkout y sale sin escribir nada; si reporta un conflicto, no hay despliegue hasta reconciliarlo.

Reaplicar es idempotente. El configurador retira solamente el `model`/`fallback_providers` que poseia, instala la fuente literal y el proveedor local en los nueve perfiles, y excluye Copilot. Conserva credenciales y opciones no administradas; ante conflictos se detiene para reconciliarlos. Guarda una copia `.goncloud-backup` antes del primer reemplazo. No borra perfiles.

Rollback: vuelve a una revision anterior del repo y reaplica `configure_team.py --allow-regression`; despues reinicia el gateway y abre una sesion nueva con `/new`. El proveedor que quede copiado pero ya no seleccionado es inerte. Para una recuperacion manual, conserva los archivos `.goncloud-backup` antes de tocar la configuracion.

## Respaldo y reinstalacion

`scripts/backup_hermes.py` respalda todo lo necesario para reinstalar Hermes en otra PC. Corre cada dia a las 03:00 como trabajo `no_agent` del director y deja `hermes-backup-YYYYMMDD.tar.gz` en dos lugares: `%LOCALAPPDATA%\hermes-respaldo\` (los 2 mas nuevos) y gonserver `/mnt/data/backups/hermes/` (los 7 mas nuevos, receptor `recibe-respaldo hermes`). Si todo sale bien no imprime nada y el cron no manda mensaje. Si algo falla imprime una sola linea `ERROR respaldo Hermes: ...`, sale con 1 y el cron la entrega por Telegram. Si la corrida no llega a imprimir nada (la PC apagada o el cron muerto), lo detecta gonserver: `vigila-respaldos` avisa si el ultimo tiene mas de 36 h o pesa menos de la mitad que el anterior.

Que entra. Todo el home `%LOCALAPPDATA%\hermes` (los perfiles, `config.yaml`, `.env`, `auth.json`, `subscription-router.env`, `kanban.db`, memorias, skills, crons, `backup-key`) menos lo que el instalador o el uso recrean: `hermes-agent`, `tools`, `installs`, `node`, `bin`, caches, `lsp`, logs, copias viejas (`backups`, `recovery-backups`, `state-snapshots`), los candados conocidos (`auth.lock` y los de la base del enrutador) y los pids. Cada `profiles\<nombre>\` es un home completo y omite lo mismo. De `%USERPROFILE%` entra una lista cerrada: de cada CLI del enrutador (`.claude`, `.codex`, `.grok`, `.kimi`, `.kimi-code`, `.config\opencode`, `.local\share\opencode`) solo sus archivos sueltos de hasta 1 MB (login y ajustes, como `auth.json`, `config.toml`, `.credentials.json`, `settings.json`, `AGENTS.md`) y las carpetas que escribe el dueno (`skills`, `hooks`, `agents`, `rules`, `prompts`, `commands`, `memories`, `credentials` y parecidas); ademas `.claude.json`, `.claude\plugins\known_marketplaces.json`, `AppData\Roaming\himalaya`, `.gitconfig` y `.ssh`. Quedan fuera los paquetes, los plugins (se reinstalan desde la configuracion), caches, sesiones e historiales, temporales, logs, `*.log` y `*.bak*`. Un tope protege el tamano: un archivo del perfil de usuario de mas de 20 MB queda fuera, y si el perfil de usuario suma mas de 200 MB la corrida sube el archivo igual y termina en ERROR nombrando el archivo, para que una cache nueva no infle el respaldo en silencio; la solucion es agregar una regla. La tabla `RULES` del script es la lista completa con la razon de cada omision, y el `manifest.json` del archivo repite que se omitio y por que. Cada SQLite (reconocido por su encabezado, no por la extension) se copia con la API de backup en linea, asi que entra lo que todavia esta en el WAL, y la copia debe pasar `integrity_check` o el respaldo aborta sin subir nada. El login de Muse vive dentro de WSL y no entra.

El archivo lleva credenciales en claro: solo vive en la PC y en gonserver, nunca en Git ni en otro servidor.

La subida usa la llave existente `%LOCALAPPDATA%\hermes\backup-key` (`id_ed25519` y `known_hosts`) contra `clawbackup@10.13.13.1`, cuyo comando forzado recibe el archivo y devuelve su sha256; el script lo compara con el local y reintenta hasta 3 veces. Una subida fallida deja la copia local y la nombra en la linea de error. Hermes corta un script `no_agent` a los 3600 s, asi que el respaldo revisa el reloj antes de cada archivo que junta, escribe o sube y se detiene solo a los 3300 s (`--budget`) para que la linea de error llegue. Al empezar borra lo que dejo una corrida cortada a la mitad (`hermes-backup-*.tar.gz.tmp` y carpetas `.trabajo-*`). Si el archivo crece hasta no caber, sube `cron.script_timeout_seconds` y `--budget` juntos.

`configure_team.py` instala el script en `profiles\claw\scripts`. El cron diario reemplaza al semanal `respaldo-semanal-claw` (`backup-claw.sh`, que solo copiaba el perfil claw). Antes de crearlo, el receptor de gonserver debe aceptar la familia `hermes`: el viejo solo acepta nombres `claw-backup-*` y la subida fallaria con "nombre invalido".

```powershell
hermes -p claw cron list                  # anota el id de respaldo-semanal-claw
hermes -p claw cron remove <id>
hermes -p claw cron create "0 3 * * *" --name respaldo-diario-hermes --script backup_hermes.py --no-agent --deliver telegram
hermes -p claw cron run <id-del-nuevo>    # primera corrida ahora; no debe llegar ningun ERROR
```

Cuando la primera corrida termine sin ERROR y el archivo aparezca en gonserver, puedes borrar `profiles\claw\scripts\backup-claw.sh` y `%LOCALAPPDATA%\hermes\scripts\backup-claw.sh`.

### Simulacro de restauracion

`scripts/restore_hermes_check.py` extrae un archivo en una carpeta temporal nueva y comprueba que coincide con su manifiesto (tamano y sha256 de cada archivo), corre `integrity_check` en cada SQLite, lee cada `config.yaml` y cada `.env` sin repetir sus valores, e imprime `OK restauracion de prueba ...: N perfiles, N crons, N bases, N archivos` o `ERROR ...` con salida 1. Nunca toca el home vivo y borra el temporal al terminar. Usa una ruta corta en `--workdir` para no chocar con el limite de 260 caracteres de Windows.

```powershell
& $python scripts/restore_hermes_check.py C:\r\hermes-backup-20261015.tar.gz --workdir C:\r
```

### Reinstalar en una PC nueva

`$python` es `"$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe"`.

1. Instala Git for Windows, WSL y la VPN a gonserver (WireGuard `10.13.13.1` o Tailscale `100.127.167.103`). Si la PC tiene Kaspersky, exporta su raiz y fija `NODE_EXTRA_CA_CERTS` como dice [estado](docs/status.md) antes de arrancar cualquier proceso de Hermes.
2. Instala Hermes en el commit fijado (seccion "Instalacion reproducible") y clona este repo en `C:\dev\goncloud-hermes`. Deten el gateway: `hermes -p claw gateway stop`.
3. Trae el archivo mas nuevo de gonserver con tu cuenta de administrador (la cuenta `clawbackup` solo puede recibir): `scp <admin>@10.13.13.1:/mnt/data/backups/hermes/hermes-backup-YYYYMMDD.tar.gz C:\r\`. Si la PC vieja sigue viva, sirve tambien su copia de `%LOCALAPPDATA%\hermes-respaldo\`.
4. Corre el simulacro sobre ese archivo. Si no imprime `OK`, prueba con el del dia anterior.
5. Con el gateway detenido, pon los archivos en su lugar:

   ```powershell
   mkdir C:\r\x; tar -xzf C:\r\hermes-backup-YYYYMMDD.tar.gz -C C:\r\x
   robocopy C:\r\x\home "$env:LOCALAPPDATA\hermes" /E
   robocopy C:\r\x\user "$env:USERPROFILE" /E
   ```

   `manifest.json` se queda fuera. Si el usuario de Windows ya no es `ehven`, busca rutas viejas: `Get-ChildItem "$env:LOCALAPPDATA\hermes" -Recurse -Include *.yaml,*.json | Select-String 'Users\\ehven'`.
6. Corre `& $python scripts/configure_team.py --check` y despues sin `--check`. Reescribe los scripts del director y `autoupdate.json` con las rutas de esta PC.
7. Rehaz los logins que fallen en el paso 9: los archivos de login copiados suelen bastar, pero un proveedor puede invalidar la sesion en una maquina nueva.
8. Vuelve a crear Muse en WSL: instala su CLI en Ubuntu e inicia sesion con el login oficial (ver [estado](docs/status.md)); su login no viaja en el respaldo.
9. Comprueba:
   - `hermes -p claw gateway start`; `hermes -p claw gateway status --deep --full` dice que esta corriendo.
   - Una sonda por adaptador de la fila `main`; cada una debe imprimir `"success": true, "marker_exact": true`:

     ```powershell
     $chain = (Get-Content model-routing.source.json | ConvertFrom-Json).chains.main
     for ($i = 0; $i -lt $chain.Count; $i++) { & $python scripts/probe_route.py --position $i $chain[$i] }
     ```

   - Un turno real del director: `hermes -p claw chat -q "Responde solo: PONG" --oneshot -Q` responde `PONG`.
   - `hermes -p claw cron list` muestra `respaldo-diario-hermes` y `vigia-modelos`.
   - En Telegram, `/new` en la sesion del director.

## Modelo y ejecucion continua

La seleccion de proveedor y las pruebas con inferencia real se registran en [estado](docs/status.md). Muse usa el login oficial de su CLI dentro de WSL; sus encargos conservan el sandbox de Muse y un archivo temporal privado, eliminado al terminar, mantiene el prompt fuera de la linea de comandos. No pegues claves en el chat ni en Git. No se copiaron tokens, canales, crons ni conexiones comerciales de OpenClaw.

Telegram: GO — gateway corriendo y aceptacion bidireccional aprobada con el workflow `t_4fe237e6` (ver [estado](docs/status.md)). Tras cada redespliegue del plugin, la sesion de Telegram debe renovarse con `/new`: una sesion restaurada conserva la lista de herramientas con la que nacio.

Telegram es solo transporte para el trabajo tecnico: el director recibe el mensaje y llama `goncloud_local_run`, que crea la cadena durable y la deja corriendo en segundo plano por el router de suscripciones; `goncloud_local_status` reporta el avance. El director conserva su terminal en Telegram por decision del propietario (Gmail, Calendario y Drive corren por la skill `google-workspace`, autorizada en el perfil `claw`; las demas cuentas de correo, por `himalaya` con su configuracion fuera de Git, ver [estado](docs/status.md)); ante un bloqueo su rol le exige reportar y parar. El despachador nativo del gateway es uno solo para todos los perfiles y lee el `config.yaml` raiz, donde esta encendido; `configure_team.py` le escribe ahi la lista blanca `kanban.dispatch_profiles` (los once especialistas de `team.json`) para que despache sus tarjetas y nunca una etapa de la cadena, que debe correr por el router. `claw` conversa por proveedores Hermes nativos compilados de la tabla; los trabajadores no llevan una cadena nativa competidora y siguen en el router de suscripciones.

## Especialistas

Los once perfiles especialistas (`kanban.dispatch_profiles` en `team.json`) son del propietario: `configure_team.py` nunca los crea ni toca su `SOUL.md`, sus toolsets ni su workspace. Solo les administra dos cosas: la cadena nativa de modelos de la fila que nombra `specialist_row` (hoy `operaciones`, la misma que corren en claw), escrita como `model` + `fallback_providers` + `agent.reasoning_overrides`, y su copia del plugin con la tabla. Asi una tarjeta despachada a un especialista corre en una cadena conocida. Un perfil de la lista que no existe se omite con un aviso.

## Claw sigue la misma tabla

Las cadenas de claw (OpenClaw, Mac Mini) son las mismas filas de `model-routing.source.json`. `claw-routing.json` dice que fila corre cada agente de claw, y `scripts/claw_models.py` deriva lo que claw deberia tener y lo compara con una foto de lo vivo; nunca escribe en claw.

```bash
python3 scripts/claw_models.py desired
python3 scripts/claw_models.py check --live ../goncloud-openclaw/docs/patches/modelos-vivos-2026-10-12.json5
```

`check` sale con 1 y lista cada diferencia (agente, posicion o thinking). La foto se toma en el host del gateway con un parse de solo lectura de `openclaw.json`; su formato esta en el encabezado del archivo.

## Cambiar un modelo

Un modelo entra a la tabla despues de probarse con una llamada real, nunca antes. Una version nueva de un modelo en uso la cambia el vigia solo (ver abajo); a mano queda pasar a otro modelo.

1. `python3 scripts/promote_model.py <viejo> <nuevo>` muestra las celdas que cambiarian y las sondas que hay que correr; no escribe nada.
2. En la PC, con el Python del runtime Hermes, cada sonda debe imprimir `"success": true, "marker_exact": true`: `python scripts/probe_route.py --position 3 "meta/muse-wonder-1.0 (max)"`. La sonda llama al adaptador directo, fuera del router, asi que un fallo no pone a nadie en cuarentena. Si la celda esta en la fila `main`, el director la usa por su proveedor nativo y esa ruta se prueba aparte.
3. `python3 scripts/promote_model.py <viejo> <nuevo> --apply` escribe la tabla. Con `--hold-claw` claw se queda en el modelo viejo (queda anotado en `id_overrides` de `claw-routing.json`); `--release-claw <nuevo>` lo suelta cuando claw ya pueda servirlo.
4. Haz commit de la tabla y despliega con `configure_team.py --check` y luego sin `--check`. Las pruebas que nombran modelos leen `tests/fixtures/model-routing.source.json`, no la tabla viva.
5. Para claw: `claw_models.py check` contra una foto fresca dice que falta; el parche se aplica en el host del gateway en un hueco sin corridas.

## Vigia de modelos

`scripts/model_watch.py` corre una vez al dia como trabajo programado sin agente del director (`cron`, `no_agent`). Lee los catalogos publicos (models.dev y la lista de OpenCode Go), avisa de lo que cambio desde la corrida anterior (un modelo nuevo de una familia que la tabla usa, un modelo en uso que desaparecio de su ruta, el CLI de Muse atrasado, una fuente que lleva tres corridas sin poder leerse) y cambia solo a la version nueva de un modelo en uso.

Una version nueva es otro id de la misma linea con numero mayor: `grok-4.7` -> `grok-4.8`, `gpt-5.6-sol` -> `gpt-6.1-sol`, `claude-opus-5-5` -> `claude-opus-6`, `k3` -> `k4`. La familia Kimi es `k` seguida de un numero (`k3`, `k4`, `k3-256k`) y `kimi-`; el router y el vigia comparten esas familias. No cuentan las fotos con fecha (`claude-opus-5-5-20260922`), los ids sin version (`kimi-for-coding`), los marcados `preview`, `exp`, `alpha`, `beta`, `free` o `latest`, ni los `deprecated`. Una etiqueta en las posiciones 0 y 1 sigue la lista de OpenCode Go; en 2 o mas, el catalogo de su proveedor. Una etiqueta en las dos a la vez no se cambia sola y se avisa.

Por cada cambio, en el checkout de la PC (`hermes_autoupdate.py`):

1. Corre todas las sondas reales de la etiqueta nueva (`probe_route.py`). Si una falla, no escribe nada y avisa por que.
2. Corre el mismo turno `PONG` del paso 3 sobre la cadena de hoy. Si ya falla (por ejemplo, OpenCode Go caido), el cambio queda pendiente sin escribir nada: la comprobacion de despues no podria distinguir el modelo nuevo.
3. Escribe la tabla, hace commit `auto(modelos): viejo -> nuevo` (solo ese archivo; si la tabla tiene cambios sin commit no toca nada), despliega con `configure_team.py` y comprueba `configure_team.py --check` y un turno real del director que debe responder `PONG`.
4. Si la comprobacion falla, `git revert` y vuelve a desplegar. Si pasa, `git pull --no-rebase` y `git push`; un push fallido se avisa y el cambio se queda.

El mensaje dice que se cambio, que se probo y fallo (con la falla de la sonda), y nada mas. Un aviso de falla se repite solo cuando su texto cambia, asi que un modelo que espera cuota avisa una vez. Un cambio revertido no se reintenta solo: su aviso termina en "No lo vuelvo a intentar hasta que lo revises." y queda en el estado bajo la clave `revertido:viejo -> nuevo`. Para que lo vuelva a intentar, borra esa clave (en `model_watch.state.json`, dentro de `autoupdate`). Si sale otra version mas nueva, ese par es otro y si se intenta. Sin novedades no imprime nada y el trabajo queda en silencio. `--dry-run` no guarda nada ni cambia ningun modelo.

`configure_team.py` instala en `profiles\claw\scripts` el vigia, `model_lines.py`, `hermes_autoupdate.py`, `promote_model.py` y `autoupdate.json` (ruta del checkout, el Python con que se desplego, `hermes.cmd` y `git`). Sin `autoupdate.json` el vigia solo avisa. Su estado vive junto al script, en `model_watch.state.json`.

Hermes corta un script `no_agent` a los 3600 s de reloj (`HERMES_CRON_SCRIPT_TIMEOUT` o `cron.script_timeout_seconds`; `HERMES_CRON_TIMEOUT` es la inactividad de los trabajos con agente y no aplica). Un cambio empieza solo si su peor caso cabe; si no, queda pendiente para el dia siguiente. El vigia no reinicia el gateway; como en cualquier despliegue, la sesion de Telegram del director toma su cadena nueva con `/new`.

```powershell
hermes -p claw cron create "35 8 * * *" --name vigia-modelos --script model_watch.py --no-agent --deliver telegram
```

### Claw

Claw no lee este repo, asi que se actualiza solo en la Mac Mini con `scripts/claw_autoupdate.py` y `scripts/model_lines.py`, sobre su config viva. Por cada ref de una cadena con version nueva agrega la ref nueva sin quitar nada (fila del proveedor propio, `modelPolicy.allow`, `agents.defaults.models`), la prueba con una llamada real (`openclaw infer model run`; anthropic con un turno real por `claude-cli`) con el thinking que usan las cadenas, renombra cada uso y comprueba un turno real con la ref nueva y que la vieja ya no aparezca en la config. Si la sonda o la comprobacion fallan, deshace solo lo suyo sobre una lectura nueva de la config (un parche que vencio el tiempo pudo haber entrado): la ref nueva vuelve a ser la vieja donde el renombre la puso y se quita lo que agrego, y lo que otro escritor cambio mientras tanto se queda. Los ajustes por modelo de la ref nueva (`agents.defaults.models` y `agents.entries.*.models`) se quitan aunque otro escritor los haya editado: despues de restaurar nada nombra la ref nueva. La fila del proveedor para la ref nueva es del runner y se cambia por la vieja aunque otro escritor la haya editado; el aviso lo dice ("la fila <id> tenia cambios ajenos y se quito"). Si otro escritor apunto a la ref nueva otra cosa (una cadena, la allowlist), restaura la config entera de antes. Si en un mismo arreglo no puede separar su cambio del ajeno (por ejemplo, la allowlist ya tenia las dos refs), restaura la config entera de antes y el aviso lo dice. La config viva se lee de su archivo (`--config`, por defecto `~/.openclaw/openclaw.json`) y no con `gateway call config.get`, que sale con 1 mientras el runner tiene la ventana abierta; un archivo a medio escribir se vuelve a leer, y su contenido nunca va a un aviso porque guarda secretos. Cada parche lleva `--replace-path` para todo arreglo que pierde un elemento, y un parche vacio no se manda: la CLI rechaza los dos casos.

`config patch` mata las corridas en vuelo, asi que cada escritura (agregar, renombrar, restaurar) corre dentro de una ventana exclusiva que el gateway concede solo cuando no hay corridas: `openclaw gateway suspend --wait 0 --json`. Si esta ocupado contesta `busy` (o `draining`, o `conflict` si otro tiene la ventana) sin retener nada, y el runner vuelve a preguntar cada 5 minutos (`WINDOW_POLL`) hasta 40 minutos (`EXCLUSIVE_WAIT`); sin ventana el cambio queda pendiente y no se escribe nada. Los cambios que faltan en esa corrida quedan pendientes al instante, sin pedir ventana ("el gateway siguio ocupado; queda para la siguiente corrida"): cada uno esperaria otros 40 minutos y tres cambios llevarian la corrida de las 04:50 a las 07:00. Una sonda o una comprobacion fallida no detiene los cambios siguientes. El runner ignora el `retryAfterMs` que sugiere el gateway (20 s): cada pregunta, aunque conteste `busy`, cierra la admision del gateway entre 13 y 20 s y en ese lapso rechaza trabajo nuevo; preguntar cada 20 s lo dejaria rechazando trabajo casi todo el tiempo. Por eso corre de noche y pregunta poco. Cada pregunta lleva `--timeout 30000` (con los 10 s de la CLI el gateway no alcanzaba a contestar) y un `--request-id` fijo para toda la ventana (`claw-autoupdate-<uuid>`). Una pregunta sin respuesta (vencio el tiempo o salio sin JSON) pudo haber recibido la ventana igual, asi que se repite una vez al instante con el mismo id: el gateway devuelve la ventana que ya dio a ese id en vez de dar otra. Ninguna respuesta es fatal: lo que no es `ready` es "todavia no". Si una ventana se concedio pero su id nunca llego, el gateway la suelta solo a los 2 minutos. Mientras la ventana esta abierta el gateway rechaza trabajo nuevo, incluidos los mensajes del propietario, por eso dura solo los segundos del parche y se libera siempre con `openclaw gateway resume <id>`, tambien si algo falla; si el runner muere, la ventana vence sola. Nunca se usa `--wait` mayor que 0, que rechazaria trabajo nuevo durante toda la espera. Las sondas y el turno de comprobacion corren fuera de la ventana, porque necesitan que el gateway acepte trabajo. Una restauracion espera ventana hasta 10 minutos (`RESTORE_WAIT`) y despues restaura igual, y el aviso lo dice. Si queda pendiente despues de agregar la ref nueva, la siguiente corrida sigue desde ahi. `shutdownBudget.activeWork` no sirve como senal de reposo: no cuenta las corridas embebidas y se queda con conteos viejos.

Corre a las 04:50 desde launchd y no desde un cron de OpenClaw: un trabajo de cron cuenta como trabajo del gateway, y entonces la ventana nunca estaria lista. A esa hora el propietario duerme y es lo mas probable que no haya corridas; de dia una corrida de agente dura 20 minutos o mas y puede no haber ningun hueco. El runner manda su propio aviso con `openclaw message send --channel telegram`, solo cuando hay algo nuevo, al chat de `--to` (por defecto el del propietario). Sus avisos usan la misma regla de una sola vez, incluido el par en espera tras una restauracion despues del renombre, y viven en `claw_autoupdate.state.json` (clave `notices`) junto al script. Si Telegram no toma el aviso, el estado no se guarda y la siguiente corrida lo repite (un par revertido se vuelve a intentar una vez en ese caso). Lo que imprime queda en `~/Library/Logs/goncloud/claw-autoupdate.log`.

Instalacion en la Mini (una vez). Copia `scripts/claw_autoupdate.py`, `scripts/model_lines.py` y `scripts/ai.goncloud.claw-autoupdate.plist` a `~/goncloud-autoupdate/` (la Mini no tiene este repo; por ejemplo `scp` desde la Mac) y despues, en la Mini:

```bash
mkdir -p ~/Library/Logs/goncloud ~/Library/LaunchAgents
python3 ~/goncloud-autoupdate/claw_autoupdate.py --openclaw "$(command -v openclaw)" --dry-run
sed -e "s|__PYTHON3__|$(command -v python3)|" \
    -e "s|__SCRIPT__|$HOME/goncloud-autoupdate/claw_autoupdate.py|" \
    -e "s|__OPENCLAW__|$(command -v openclaw)|" \
    -e "s|__PATH__|$(dirname "$(command -v node)"):/usr/bin:/bin:/usr/sbin:/sbin|" \
    -e "s|__HOME__|$HOME|g" \
    ~/goncloud-autoupdate/ai.goncloud.claw-autoupdate.plist \
    > ~/Library/LaunchAgents/ai.goncloud.claw-autoupdate.plist
plutil -lint ~/Library/LaunchAgents/ai.goncloud.claw-autoupdate.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.goncloud.claw-autoupdate.plist
```

`--dry-run` lista lo que cambiaria y no toca nada. launchd no expande `~` ni `$HOME`, por eso el `sed` escribe rutas completas; `__PATH__` lleva el directorio de `node` porque `openclaw` arranca con `#!/usr/bin/env node`. Para correrlo ya: `launchctl kickstart gui/$(id -u)/ai.goncloud.claw-autoupdate`. Para quitarlo: `launchctl bootout gui/$(id -u)/ai.goncloud.claw-autoupdate`. Si quedo instalado el cron `autoactualiza-modelos` de OpenClaw de la version anterior, borralo (`openclaw cron list` lo muestra).

## Modelos escritos a mano fuera de la tabla

Lanzadores, agentes de cada CLI y scripts fijan modelos que la tabla no gobierna. `pinned-files.json` lista esos archivos (hoy, los de la Mac del propietario) y `scripts/pinned_models.py` los revisa: dice que modelo fija cada uno, en que linea, y si sigue en los catalogos publicos (`ok`, `deprecated`, `not in the catalogs`). Un modelo Kimi se reconoce como en el router: `k` y un numero, con o sin contexto (`k3`, `k4`, `kimi/k4`, `k3-256k`); `k8s`, `4k` o `k10s` no cuentan. Con `--write` guarda la lista en `scripts/pinned-models.json`, que el instalador entrega junto al vigia; asi el vigia tambien avisa cuando uno de esos modelos desaparece. No cambia ningun archivo ajeno.

```bash
python3 scripts/pinned_models.py            # reporte
python3 scripts/pinned_models.py --write    # reporte y lista para el vigia; despues, desplegar
```

## Calidad

El remoto es `github.com/gon0801/goncloud-hermes`; no hay CI configurado todavia, asi que la bateria completa se ejecuta localmente una vez al cierre y durante el desarrollo se ejecuta solamente el archivo de pruebas cambiado.

Gate y despliegue 2026-09-27: 414 tests aprobados y 3 skips de plataforma; 8/8 hooks; `git diff --check` limpio. La regresion y una inferencia controlada prueban que `main` avanza OpenCode Go -> Claude Opus 5 `xhigh` sin tocar Kimi. Los nueve perfiles y el reinicio del gateway estan documentados en [estado](docs/status.md).

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m unittest discover -s tests -v
python -m pre_commit run --all-files
```

Candados: higiene, JSON/YAML, lint y formato Python. Nunca usar `--no-verify`. El plugin usa la base Kanban nativa en directorios temporales para las pruebas; no crea trabajos reales ni llama modelos en los tests.

En esta computadora, pre-commit esta instalado en `C:\Python314\python.exe`; se puede usar ese ejecutable en lugar de `python`. La bateria usa el Python del runtime Hermes, que contiene las dependencias nativas del tablero.

## Alcance de autonomia

Resolver detalles tecnicos y consultar entre roles entra en una orden autorizada. Gastos, mensajes externos, secretos, borrados amplios o despliegues fuera del alcance requieren tu decision. La politica y el plugin no son una sandbox: un proceso con tus permisos Windows puede usar terminal y acceder a tus archivos. No se promete inmunidad a instrucciones maliciosas ni correccion matematica de una revision por IA.

Fuentes comerciales heredadas: packing usa MercadoLibre y Amazon Seller Central/Flex, no Odoo. Toda regla nueva debe conservar origen y fecha; no se importo informacion comercial viva.
