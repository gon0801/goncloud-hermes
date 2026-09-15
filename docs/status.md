# Estado — 2026-09-14

## Estado actual: aceptacion local completa (2026-09-14)

- Codigo de runtime aprobado y desplegado: `17ec1e7`. Nueve perfiles verificados;
  fuente literal byte-identica, sin cadenas nativas administradas y con Copilot
  excluido. Backup previo de 18 archivos, con hashes comprobados, en
  `%LOCALAPPDATA%\hermes\recovery-backups\20260914T214024Z-task9b`; nada eliminado.
- Matriz live actual: **8/9 ready**: Go, Zen, Muse, Claude Max, Kimi, Z.AI y
  Grok por suscripcion; DeepSeek por API. Una cuarentena durable: Codex `quota`,
  que avanza de slot y no bloquea ninguna etapa. Probes individuales secuenciales,
  sin migracion ni limpieza de lanzadores. Tabla y slots intactos.
- **Codex** es la unica cuenta pendiente y no depende del codigo: `codex login
  status` responde `Logged in using ChatGPT`, el modelo `gpt-6-astra` esta
  vinculado y `codex exec` devuelve textualmente `You've hit your usage limit ...
  try again at Sep 19th, 2026 10:36 AM`. Configuracion completa y comprobada.
  Entra sola: la cuarentena vence a la hora y el router la reintenta al llegar
  un encargo a su posicion; la primera inferencia real exitosa la levanta, sin
  correr la matriz a mano (ver "Cuarentena con vencimiento").
- **Dependencia del entorno en esta maquina**: Kaspersky intercepta TLS y
  presenta su propio certificado raiz (`Kaspersky Anti-Virus Personal Root
  Certificate`) en hosts como `auth.kimi.com`. Windows lo acepta; los CLIs
  hechos en Node no, porque traen su propia lista de raices. El remedio fue
  exportar esa raiz a `%USERPROFILE%\.certs\kaspersky-root.pem` y fijar la
  variable de usuario `NODE_EXTRA_CA_CERTS` a esa ruta. No relaja la
  verificacion: solo agrega la misma raiz que el sistema ya aprueba. **Todo
  proceso que ejecute el router (gateway incluido) debe heredar esa variable**;
  un proceso arrancado antes de fijarla no la tiene.
- Scout `t_f2e2bb6d`: completado por Go slot 0, `deepseek-v4.1-flash`, suscripcion;
  repositorio limpio y pausa tras scout, sin entrega.
- Cadena completa `t_be290d83`: **entregada**. Las siete etapas en orden —scout,
  ingenieria, implementer, verifier, adversary, reviewer, entrega— con un unico
  intento ejecutado cada una, todas en Go slot 0 y facturacion `subscription`
  (`deepseek-v4.1-flash` en scout/adversary/reviewer, `muse-spark-1.3-contributor`
  en el resto). Revisor aprobo con `{"decision":"approve","severity":null}`;
  `delivery_blocked=false` y cero rondas correctivas.
- Cambio autorizado realizado y acotado: `acceptance.txt` del fixture desechable
  quedo en `status: accepted` y `git status --porcelain` solo lista ese archivo.
  El fixture de scout sigue limpio.
- Evidencia de propiedad: un propietario por etapa ejecutada, cero intentos
  activos y cero leases restantes; un workflow ejecutado a la vez.
- Evidencia sanitizada: `.claude/tmp/task9b-acceptance-evidence.json`,
  `.claude/tmp/task9b-scout-status.json`, `.claude/tmp/task9b-chain-status.json`
  y los nueve JSON de probes `task9b-*.json`. Estado durable en
  `%LOCALAPPDATA%\hermes\kanban.db` y `%LOCALAPPDATA%\hermes\subscription-router.db`;
  fixtures preservados en
  `%TEMP%\goncloud-router-acceptance-20260914T214024Z\{scout,chain3}`.
- Bateria final: **362 tests, 359 aprobados y 3 skips de plataforma/privilegios**,
  288.213 s, exit 0. Pre-commit completo: 8/8; `git diff --check` limpio.
- Plugin Doctor: OK, 7 herramientas y 1 hook. Este runtime ofrece
  `hermes -p claw plugins doctor goncloud-team --ci`; el comando del brief
  `tools plugin doctor` no existe (exit 2). Deep gateway status: exit 0, stopped.

Telegram: GO — gateway running (restarted 2026-09-15 with the final plugin); bidirectional acceptance passed with `t_4fe237e6`

### Cutover de Telegram (plan `2026-09-14-telegram-transport-cutover.md`)

Commit `776e871`. Tareas 1 y 2 del plan cerradas y la tarea 3 en curso:

- **Transporte, no enrutador.** El director recibe el mensaje y llama
  `goncloud_local_run`, que registra la cadena durable con los mismos limites
  del CLI local (`LocalOrchestrator.create`, sin ejecutar etapas) y la deja
  corriendo en un proceso desprendido (`launch_detached`: solo identificadores
  en argv, salida a ninguna parte, resultado acotado en
  `%HERMES_HOME%\goncloud-local\<workflow_id>.json`). Contesta al momento con el
  `workflow_id`; `goncloud_local_status` reporta el avance. Solo `claw` puede
  llamarla. `roles/claw.md` le indica al director ese camino.
- **Despachador nativo apagado.** `kanban.dispatch_in_gateway: false` en los
  nueve perfiles (gestionado desde `team.json`). El despachador del gateway
  toma cualquier tarjeta `ready` con asignado y la corre con un agente nativo:
  con la cadena en el tablero, eso era el segundo enrutador. Regresion en
  `test_configuration`.
- **Modelo del director.** La configuracion global de Hermes manda todo a
  OpenRouter (`model.base_url`) sin llave: el director no podia ni contestar
  (`HTTP 401: Missing Authentication header` con cualquier modelo). Primero se
  le dio un solo modelo nativo (`zai/glm-5.3`, la unica credencial nativa valida
  entonces). Despues el propietario registro su llave de OpenCode Go en Hermes
  (`hermes auth add opencode-go`) y `claw` lleva ahora la **cadena de
  conversacion** de `team.json`: `director_model` = Primary de `main`
  (`opencode-go/muse-spark-1.3-contributor`) y `director_fallbacks` =
  `opencode-zen/muse-spark-1.3-contributor-free` -> `kimi-coding/k3` ->
  `zai/glm-5.3`. Son exactamente las posiciones de `main` que Hermes puede
  alcanzar por API nativa **con la misma suscripcion**, en el orden de la
  tabla; una regresion prohibe cualquier otra (Claude Max, ChatGPT, Grok y Muse
  por API nativa serian otra cuenta: la cadena de clase equivocada que la
  recuperacion quito). El bloque `model` es de propiedad completa y nunca se
  mezcla con el bloque por defecto; un cambio manual es un conflicto que el
  configurador se niega a pisar. El propietario registro despues las llaves
  nativas de Zen y Kimi (`hermes auth add opencode-zen` / `kimi-coding`); las
  llaves viejas del `.env` del perfil `claw` (`OPENCODE_ZEN_API_KEY`,
  `KIMI_API_KEY`, ambas 401) las tapaban y se retiraron con
  `hermes -p claw config unset`. `hermes -p claw auth list` muestra las cuatro
  posiciones de la cadena de conversacion con credencial manual activa y sin
  `auth failed`, y cada una respondio el marcador con la configuracion del
  perfil (cambio temporal de `model` y regreso a la cadena gestionada): Go
  6.7 s, Zen 17.6 s, Kimi 20.2 s, Z.AI 13.2 s. Los trabajadores no llevan
  modelo ni cadena.
- **Python tambien necesitaba la raiz de Kaspersky.** El SDK de Hermes usa
  `httpx` con el paquete `certifi`, no el almacen de Windows, y Kaspersky
  intercepta `api.anthropic.com` y `api.openai.com`. Se creo
  `%USERPROFILE%\.certs\ca-bundle.pem` (certifi + raiz de Kaspersky) y las
  variables de usuario `SSL_CERT_FILE` y `REQUESTS_CA_BUNDLE`. Junto con
  `NODE_EXTRA_CA_CERTS`, todo proceso del router o del gateway debe heredar las
  tres; el gateway se arranco desde una shell que las tenia.
- Gates de la tarea 3, paso 1: 356 tests (353 aprobados, 3 skips), nueve perfiles
  verificados, Plugin Doctor 9 herramientas y 1 hook. Paso 2: gateway
  `running`, seis sondas PASS.
- **Primer mensaje real del propietario** (`t_9aed7af7`, encargo de solo
  lectura): el director llamo `goncloud_local_run`; scout, ingenieria,
  implementer y verifier completaron en slot 0 (`opencode_go`) y el repo
  desechable quedo intacto. `adversary` termino `blocked` con `task_failure`:
  al reproducir la etapa con el mismo modelo el envelope salio valido, asi que
  fue una respuesta mal formada en esa corrida, bloqueada con evidencia y sin
  falso exito. La leccion real es otra: una inspeccion no debe correr siete
  etapas. `306bb8a` agrega `stop_after` a `goncloud_local_run` (identificador
  en argv, mismos limites del CLI) y `roles/claw.md` indica `stop_after="scout"`
  para preguntas e inspecciones; la cadena completa queda para cambios
  autorizados.
- **Segundo mensaje** (`t_1849d088`, `stop_after` aun ausente en la sesion vieja
  de Telegram; `/new` la renueva): el router rechazo primero la repeticion
  sobre el mismo repositorio (exclusion por repositorio, por diseno) y, sobre
  un repo nuevo, scout termino `local terminal validation failed` en slot 0 y la
  cadena se bloqueo. El director, con terminal, se puso a leer logs y codigo
  del plugin; se detuvo el gateway, se comprobo que no modifico nada
  (plugin identico en los nueve perfiles) y `f0ea38e` cierra las dos cosas:
  - un envelope rechazado es ahora `FailureKind.INVALID_TERMINAL`: avanza de
    slot mientras el workspace no haya cambiado y bloquea con evidencia si
    cambio (misma regla que `transport`); la cuenta sigue `ready`, porque el
    proveedor si respondio. Regresiones para ambos casos.
  - el director perdio el toolset `coding` sobre Telegram y `roles/claw.md`
    le ordena, ante un bloqueo, leer `goncloud_local_status`, reportar y
    parar: no logs, no codigo, no base. **Revertido por decision del
    propietario** al conectar Gmail: la skill `google-workspace` corre por
    terminal y el director necesita acceso completo desde Telegram para esos
    casos; queda `coding` de vuelta y la regla del rol como unica guarda.
- **Tercer mensaje**: el director acorto la ruta del repositorio por su cuenta
  (`C:\dev\...` en lugar de la ruta bajo `%TEMP%`) y recibio `revision is
  required`, porque la revision se derivaba de un directorio inexistente.
  `e03fadd` comprueba el repositorio antes que nada y lo nombra en el error;
  `roles/claw.md` le exige la ruta exacta, una herramienta por llamada, y el
  repo desechable se movio a una ruta corta (`C:\dev\prueba-telegram`). Tras el
  reinicio del gateway, Hermes restauro la sesion anterior con su lista de
  herramientas vieja (terminal incluida): la configuracion nueva aplica a una
  sesion nueva, que se abre con `/new`.
- **Cuarto mensaje, aceptacion (`t_4fe237e6`)**: tras `/new`, el director llamo
  solo `goncloud_local_run` y `goncloud_local_status`; un intento del router en
  slot 0 `opencode_go`/`deepseek-v4.1-flash` (suscripcion), completado, sin
  fallo y sin leases; driver `paused` con scout `done` y las demas etapas sin
  tocar; repositorio limpio en `248aad1`; la respuesta en el chat listo los dos
  archivos. Pasos 3, 5 y 6 del plan cerrados: **GO**, el gateway queda
  corriendo. Paso 4 (rechazo de un id no permitido) sigue abierto porque el
  propietario solo dispone de una identidad de Telegram; la lista de acceso es
  la nativa del gateway y no se toco.

### Defectos encontrados al cerrar la aceptacion

La cadena completa fallaba antes por defectos propios, no por las cuentas. Cada
arreglo lleva su regresion observada en rojo primero.

- `8e68f7f` — seis modulos de prueba registraban `TemporaryDirectory.cleanup`
  crudo; la ventana de borrado pendiente de Windows tumbaba la bateria ~1 de
  cada 3 corridas. Ahora usan el helper con reintento y un guard falla si alguien
  reintroduce el patron.
- `5f24754` — `SubscriptionAdapter.__init__` recortaba a 30 s la ventana de probe
  de 120 s que Muse declara para el arranque en frio de WSL, y todo error de
  lanzamiento en un probe se reportaba como `task_failure`, que **bloquea** la
  etapa en vez de avanzar de slot. `SubscriptionProcessError` ahora lleva su
  propio tipo de fallo.
- `575eb9f` — la matriz live volvia a recortar esa ventana declarada.
- `c259d26` — el probe permitia 32 tokens de salida; un modelo con razonamiento
  (GLM-5.3) gasta ese presupuesto pensando y termina en `max_tokens`, asi que una
  cuenta sana se leia como `task_failure`. Z.AI paso de fallo a `ready`.
- `2152603` — el tope de 20 000 caracteres de evidencia cortaba tambien la
  entrada del parser: el marcador de truncado no es JSON, de modo que una etapa
  **completada** se registraba como `task_failure`. La evidencia sigue acotada;
  el parseo ve el flujo redactado completo.
- `0f206ca` — el parser del envelope terminal exigia la valla de apertura al
  inicio de linea. El modelo de `ingenieria` emite ` ```json ` pegado a su prosa,
  y eso bloqueo la cadena en las dos corridas anteriores. La valla de cierre y la
  prohibicion de contenido posterior siguen estrictas.

### Cuarentena con vencimiento

El propietario pidio que la cuota agotada se recupere sola: las cuentas la
agotan y la reponen con frecuencia, y una cuarentena permanente obligaba a
correr la matriz live a mano cada vez. La regla ahora es:

- Toda cuarentena tiene su propio reloj, `quarantined_at` (columna nueva,
  migracion aditiva; las filas existentes usan `updated_at` como respaldo).
  Un probe superficial que pasa no lo reinicia; una publicacion live o un
  fallo real mientras la cuenta esta en cuarentena si lo reinician.
- Vigencia: `_QUARANTINE_RETRY_SECONDS` = 3600 s. Vencida, el router deja de
  tratar la cuarentena como veredicto: vuelve a hacer el probe y, si pasa,
  intenta la cuenta de verdad cuando un encargo llega a esa posicion.
- Solo una inferencia real exitosa levanta la cuarentena (`save_health` con
  `lift_quarantine`, que ademas borra el cooldown). Es la misma prueba que
  exige la matriz live; lo que cambia es que ya no hace falta correrla a mano.
  Un fallo repetido re-arma la cuarentena con reloj nuevo y avanza de slot.
- El cooldown de 15 minutos por cuota dentro de una misma corrida se conserva.
- Aplica a todos los tipos de fallo, no solo a `quota`: el costo de reintentar
  una credencial muerta es un intento fallido por hora de uso real, y evita
  que una cuenta arreglada fuera del router siga vetada.

Regresiones: cuarentena reciente sigue saltando la ruta; vencida se reintenta
y el exito real la levanta; vencida y fallida de nuevo se re-arma y la segunda
corrida ya no reintenta; los cuatro tipos restantes vencen igual.

### Gmail y Google Workspace del director

El propietario pidio que Hermes accediera a su Gmail. El director usa la skill
empaquetada `google-workspace` (Gmail, Calendario, Drive, Contactos, Hojas y
Docs por OAuth de Google, `scripts/google_api.py` por terminal). El cliente
OAuth (`google_client_secret.json`) ya existia en el perfil `claw`; faltaba la
autorizacion: `setup.py --auth-url` genero el enlace, el propietario lo aprobo
en su navegador y pego la URL de retorno, y `setup.py --auth-code` guardo el
token en `%LOCALAPPDATA%\hermes\profiles\claw\google_token.json`
(`--check` = `AUTHENTICATED`). Prueba real desde el perfil: `gmail search
"newer_than:7d" --max 3` respondio en 2.2 s y `gmail labels` listo 92
etiquetas con `INBOX`. Nada de correo se copio a este repositorio. La skill es
Python: hereda `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` del gateway por la
interceptacion de Kaspersky descrita arriba.

### Zen y Kimi: era el certificado, no las cuentas

Dos diagnosticos anteriores de este documento fueron incorrectos y se corrigen
aqui. Zen no estaba limitada por su capa gratuita y la credencial de Kimi no
estaba muerta: ambas conexiones morian en la interceptacion TLS de Kaspersky
descrita arriba. Con la raiz agregada a Node, Zen respondio en 6.2 s (antes
colgaba 300 s en silencio) y `kimi login` renovo su sesion sin pedir codigo. El
401 observado contra `api.kimi.com` se produjo probando la llave `api_key` del
`config.toml`, que es un resto de una configuracion anterior; la sesion vigente
de Kimi es OAuth y el CLI la renueva solo.

Kimi tenia ademas un defecto de adaptador: kimi 0.34 solo acepta el prompt como
argumento de `-p` y no lee stdin, asi que `-p -` le entregaba un guion literal.
El encargo viaja ahora en un archivo privado que kimi carga como definicion de
agente (`--agent-file`, con el frontmatter que exige) y `-p` lleva una sola frase
fija sin contenido del encargo; la regla de no poner prompts en argumentos se
mantiene. El parser acepta el flujo `stream-json` de 0.34 (lineas `role`/
`content`, cierre por `session.resume_hint`) ademas del esquema anterior. Se
descarto el modo `kimi acp` (JSON-RPC por stdio, tambien verificado) porque
requeriria una frontera de proceso bidireccional nueva.

### El sufijo `-free` en la posicion de Muse

`d0cca29`. La tabla vincula el adaptador Muse a
`muse-spark-1.3-contributor-free`, y el CLI de Muse responde textualmente que
ese modelo no existe o no hay acceso. El sufijo `-free` nombra la capa gratuita
de **OpenCode Zen**, donde si es un identificador real y aparece en
`opencode models`; la suscripcion de Muse sirve la misma familia sin el sufijo
y respondio el marcador exacto en una prueba directa.

Por eso el sufijo se trata como procedencia de la etiqueta, igual que ` dup`, y
solo en la vinculacion a Muse: la posicion de Zen sigue enviando `-free`.
`model-routing.source.json` no se modifico; lo unico que cambia es el
identificador que recibe el CLI. La regresion cubre las dos posiciones y que la
fuente no se reescribe.

### Incidencia previa del gate de pruebas

La primera bateria completa descubrio 147 entradas: 144 pasaron y tres fueron
errores de carga, en 131.825 s, exit 1. Tres modulos importaban el helper
`tests.test_subscription_router`, ocultado por el paquete `tests` del upstream.
La segunda bateria ya cargo los 289 tests: 281 pasaron, tres se omitieron por
plataforma/privilegios y cinco tuvieron la misma dependencia en imports diferidos,
en 202.251 s. Se corrigieron tambien los cinco imports de fixtures y dos cadenas
de subprocess; la busqueda exhaustiva no encuentra referencias `tests.*`.
La regresion ahora ejecuta los 144 tests de los tres modulos afectados tras
discovery en un proceso nuevo, y pasa. Se autorizo una tercera y ultima bateria
completa por esta misma causa. El arreglo es exclusivamente de tests: no cambia
produccion, no omite pruebas o candados y no repite inferencias reales.

### Transporte preservado

- El gateway de Telegram del perfil `claw` esta detenido de forma reversible
  hasta un cutover separado. Se preservan el perfil, token, lista de acceso,
  script de servicio y entrada de inicio; no hay conexion, polling ni trafico
  bidireccional activo.
- Los perfiles de Hermes dejan de recibir cadenas nativas
  `model`/`fallback_providers`; la tabla inmutable `model-routing.source.json`
  se instala junto al plugin y excluye Copilot del catalogo nativo.

## Registro historico anterior a la recuperacion

Las afirmaciones siguientes son evidencia archivada de una instalacion previa.
Sus matrices, cuentas, cadenas nativas, conteos y conexiones no describen la
aceptacion actual. Rige el estado parcial anterior; Telegram sigue detenido.

### Enrutador de suscripciones anterior al recovery (historico)

- Nueve adaptadores (OpenCode Go/Zen, Muse, Claude Max, ChatGPT/Codex, Kimi, Z.AI Coding Plan, Grok, DeepSeek API) con prompts por stdin/archivo temporal privado, `shell=False`, salida redactada y acotada, y sandbox conservado por CLI.
- Matriz live (inferencia real minima por adaptador, sin identificadores de cuenta): PASS `opencode_go` (READY), `muse` (READY), `claude` (LISTO), `codex` (READY), `zai` (READY), `grok` (READY), `deepseek` (READY, unica ruta `api`). FAIL `kimi`: `provider.connection_error` del endpoint tras 3 reintentos (el coordinador nativo tambien ve 401 en kimi-coding); el router le pasa de largo. CUARENA `opencode_zen`: credencial presente pero el CLI cuelga en silencio >2.5h; el coordinador nativo reporta `FreeUsageLimitError` 429 en opencode-zen — pendiente de saldo/limite de la capa free, no de autenticacion.
- Fallback comprobado en vivo: Go(auth) -> Zen(quota) -> F2 Claude Max con inferencia real ("READY"); continuacion tras mutacion con revision y diff registrados ("LISTO"). 65 pruebas unitarias del enrutador; idempotencia de etapa y continuacion nunca repeticion a ciegas.
- Credenciales GLM/DeepSeek migradas de lanzadores en texto plano (dos fases: probe real antes de reescribir; backups `.goncloud-backup` fuera de Git) al env privado `%LOCALAPPDATA%\hermes\subscription-router.env`. Ningun token OAuth copiado entre aplicaciones; `auth.json` de OpenCode jamas leido por el plugin.
- Bateria final: **96/96 pruebas, exit 0** con el venv de Hermes. Pre-commit completo: 8/8 candados. `git diff --check` limpio.
- Plugin desplegado a los nueve perfiles (idempotente): 7 herramientas y 1 hook; Plugin Doctor OK. El registro historico anotaba un gateway `running`.

### Instalado y comprobado previamente (historico)

- Hermes oficial v0.21.2 (2026.9.11), commit `3f86ed75dad1933036c52018e991dbd839837126`.
- Python administrado 3.11.16, Node administrado 22.23.2; CLI responde.
- Nueve perfiles privados creados y contratos de rol aplicados; herramientas `coding`, `kanban`, `goncloud_team`.
- Configuracion nativa carga: director `claw`, concurrencia global 3, por perfil 1, maximo 2 fallos antes de bloquear.
- Plugin Doctor del perfil claw: descubrimiento, importacion y registro correctos; cinco herramientas y un hook.
- Configurador probado contra perfiles nativos temporales: nueve perfiles, reaplicacion, conservacion de credenciales/modelo y rechazo de conflictos.
- Bateria completa final: **31/31 pruebas, 11.202 segundos, exit 0**. Todos los candados pre-commit pasan: higiene, JSON/YAML, lint y formato Python.
- Una ronda independiente solicito cambios por tres hallazgos HIGH: cierre nativo tras rechazo, solapamiento de encargos en el mismo repositorio y bloqueo del informe FAIL antes del revisor. Corregidos con regresiones; comprobados en la bateria final. No se solicito segunda revision ni se atribuye un nuevo veredicto al revisor.
- Ultima version del plugin y contratos reaplicada a los nueve perfiles; Plugin Doctor pasa con cinco herramientas y un hook.
- Tabla Primary + F1-F7 aplicada a los nueve perfiles. Una sesion real salto de credenciales/cuotas no disponibles hasta Z.AI y completo la solicitud.
- Puente real director -> `goncloud_muse_delegate` -> Muse CLI verificado con `muse-spark-1.3-contributor`; resultado exacto `DIRECTOR_TO_MUSE_OK`.
- Inicio automatico del gateway instalado en la carpeta Startup de Windows; la entrada se conserva, pero el proceso actual esta detenido.
- Se archivo evidencia de una conexion anterior del bot de Telegram en modo polling, con mensaje entrante y respuesta saliente comprobados; esa conexion no esta activa.

### Operacion real historica anterior al freeze

Este bloque conserva evidencia anterior al freeze de Telegram. No implica
trafico actual: el gateway `claw` permanece detenido hasta un cutover separado.

- El usuario ya proporciono su tabla actual de ocho posiciones por agente: `model-routing.source.json`. Esta aplicada; las vinculaciones y limites comprobados estan en `docs/model-routing.md`.
- WSL 2.7.14, Ubuntu 26.04 y Muse Code CLI 1.2.1 instalados. Login e inferencia real comprobados sin copiar secretos al repositorio ni convertir la suscripcion en API.
- Canal del director: bot nuevo de Telegram creado con BotFather; token privado y lista de usuarios autorizados guardados fuera del repositorio.
- El gateway recibio `Prueba Hermes`, resolvio el proveedor de respaldo y envio respuesta. La conversacion bidireccional fue confirmada por el usuario.
- Conectar fuentes comerciales bajo una autorizacion futura sigue fuera de este alcance; no se copiaron tareas recurrentes ni operaciones de produccion.
- Conectar fuentes comerciales bajo autorizacion explicita. No hay tareas recurrentes ni operaciones de produccion activas.

### Limites y reproducibilidad registrados previamente

- Se fijo el codigo upstream, pero el instalador oficial recurrio a resolucion de dependencias al fallar su lock; no es un entorno hermetico.
- El instalador produjo un cambio en `package-lock.json` del runtime. No se modifico su codigo fuente; no se oculta ese cambio generado.
- El instalador aviso de fallo en dependencias Node/TUI. Existen dependencias Node, pero la TUI grafica no esta compilada ni verificada. El lanzador usa la consola Python `--cli`. Computer-use se omitio expresamente.
- Contratos, grafo y hook controlan el flujo normal; no son aislamiento del sistema operativo. Las herramientas nativas/terminal y edicion directa pueden eludir politica de aplicacion.
- La exclusion de trabajos simultaneos sobre un repositorio opera en el tablero actual. No apuntes tableros distintos al mismo directorio de trabajo. Archivar manualmente un director activo tambien elude esa exclusion.
- La calidad de evidencia, las reglas comerciales y la revision exacta producida se exigen en los handoffs, pero no quedan demostradas solo por aprobar pruebas del coordinador.
- Repositorio nuevo, sin remoto/CI/PR. La bateria completa de este proyecto se ejecuta localmente una vez al final; no equivale a ejecutar toda la bateria upstream de Hermes.
- Durante pruebas focalizadas del plugin hubo una incidencia transitoria de limpieza del directorio temporal de Windows; no se reprodujo en las repeticiones del worker ni en la bateria final. No se oculto ni deshabilito ninguna prueba.
