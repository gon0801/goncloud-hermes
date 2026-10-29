# Enrutador de suscripciones: aceptacion local 2026-09-14

Fuente autoritativa: `model-routing.source.json` (tabla v9 del propietario,
2026-10-29). Todas las filas llevan primario y 4 respaldos, y en una fila no se
repite suscripcion (Claude, ChatGPT, Kimi, Grok, Z.AI, OpenCode Go). GLM solo
aparece en codigo, operativo y simples. Kimi queda fuera de operativo y
simples. El
mapa `profile_inheritance` reparte los 21 perfiles de Hermes (`default`
incluido) asi: `claw` -> `dirigir-claw` (el mismo carril DIRIGIR que
`dirigir`), `entrega` -> `dirigir`, `ingenieria` y `reviewer` ->
`planear`, `implementer` -> `codigo`, `verifier` -> `verificar`, `scout` ->
`investigar`, `operaciones`, `amazon`, `meli`, `odoo`, `pers` y `arras` ->
`operativo`, `mail`, `limpia`, `gonserver`, `skillverif`, `tinkabot`,
`eggbot` y `default` -> `simples`, y `adversary` -> `adversary`. Copilot y
Copilot ACP siguen excluidos.

## Directores: Sol por suscripcion

`dirigir` y `dirigir-claw` llevan Opus 5.5 (high) → GPT-6.1 Sol (medium) →
Kimi K3.1 Pro (high) → Grok 4.7 (high) → DeepSeek V4.1 Flash (high). Hermes
usa `openai-codex`; Claw usa el runtime `codex` del plugin Codex habilitado
para `openai/gpt-6.1-sol`, permitido en `agents.defaults.modelPolicy.allow`.
El app-server de Claw usa `homeScope: user` y el ejecutable Codex local
verificado (0.163.0), no el 0.158.0 incluido en el plugin, que rechaza Sol 6.1.
Ambos autentican con la suscripcion de ChatGPT, sin API key.

## Suscripciones y cadena nativa

Los perfiles de Hermes van con Anthropic por SU SUSCRIPCION de Claude, no por
una API de pago por token; ninguna posicion resuelve a una ruta API.
`opencode-go` solo sirve DeepSeek Flash; Fable queda fuera hasta confirmar el
plan de Claude. El binding es por familia del modelo en TODAS las posiciones.
Los marcadores `(xhigh)`, `(high)`, `(medium)` y `(low)` compilan a
`agent.reasoning_overrides: {model: effort}`; `(off)` es una posicion sin
parametro de razonamiento; todo marcador desconocido bloquea la compilacion.

El 2026-10-17 alguien reescribio a mano en la PC la ruta de los 20 perfiles vivos
(transcripcion congelada en
`tests/fixtures/rutas-vivas-20261017.json`), asi que el vivo dejo de coincidir
con lo gestionado (`source_revision` 4e4112e) y `configure_team --check`
fallaba con `user-managed model conflict`. La salida acordada es
`configure_team.py --adoptar-ruteo-manual`: SOLO para el bloque de ruteo
(`model`, `fallback_providers` y `agent.reasoning_overrides`) acepta el vivo
como previo, lo registra en `goncloud-managed.json` con fecha y motivo y luego
aplica la tabla; sin la bandera el rechazo sigue igual. Cada perfil de equipo
lleva ahora su cadena nativa compilada de su carril; `specialist_row`
desaparecio.

## Aceptacion local 2026-09-14 (historica)

Ocho agentes por siete posiciones: 56 celdas; `main` correspondia a `claw` y
`entrega` heredaba `main`. Esa estructura y sus probes se conservan como
registro; para decisiones operativas rige la tabla v6 de arriba.

## Toda etapa necesita manos: las familias se atan a un CLI

Cada etapa trabaja sobre un repositorio, asi que su modelo necesita herramientas
de leer archivo, grep y shell apuntadas al workspace. Seis adaptadores lanzan un
CLI que las trae (`opencode`, `grok`, `kimi`, `claude`, `codex`, `muse`). Dos
hablan por HTTP plano contra una API y no tienen bucle de herramientas: `zai` y
`deepseek`. Por ese camino el modelo no puede mirar nada, y lo mas honesto que
puede hacer es declararse bloqueado, que es exactamente lo que ocurrio en un
scout real el 2026-09-26 cuando el failover llego por primera vez a F1.

La carencia era nuestra, no del modelo. La familia `glm-` se ata ahora al
adaptador `opencode_zai`, que es el CLI de OpenCode con el prefijo de proveedor
`zai-coding-plan`, la misma credencial del Coding Plan que `opencode auth list`
ya tenia instalada. Verificado en vivo: `zai-coding-plan/glm-5.3-flash` leyo un
archivo con la herramienta `read`, cerro con `reason: stop` (que es lo que el
parser exige para aceptar el terminal) y reporto `cost: 0`, o sea lo sirve la
suscripcion y no una API medida. La tabla del propietario no cambio: `glm-5.3` y
`glm-5.3-flash` siguen en los slots donde estan, solo que ahora llegan con manos.

`deepseek` se dejo como esta a proposito (2026-09-26, historico): entonces el
adaptador HTTP homonimo era la unica ruta que facturaba como API, declarada
para verifier F6 y scout F4. Con la tabla v6 (2026-10-17) no queda ninguna
ruta facturada por API: DeepSeek Flash corre solo por la suscripcion de
OpenCode Go (`opencode-go/deepseek-v4.1-flash (off)` en operativo y simples)
y el adaptador HTTP `deepseek` queda deliberadamente sin binding; la matriz
de probes lo conserva solo como gemelo HTTP para verificar la llave
guardada (`DEEPSEEK_API_KEY`), con `billing_mode=api` en esa sonda.

## Cadena nativa por perfil (2026-10-17; el director desde 2026-09-28)

Todos los perfiles conversan por proveedores Hermes nativos, con llamadas a
herramientas nativas; `configure_team.py` compila el `model` +
`fallback_providers` + `agent.reasoning_overrides` de cada uno directamente de
su carril de la misma fuente que usan los workflows: `anthropic` (la
suscripcion Claude del propietario), `zai` sobre el endpoint del Coding Plan,
`kimi-coding` sobre su endpoint OpenAI `/coding/v1` (`chat_completions`: el
wire Anthropic Messages que Hermes elige para toda URL `/coding` le perdia 5
de 12 llamadas a herramienta a K3, el OpenAI solo 1), `xai-oauth` y
`opencode-go` para DeepSeek Flash. No hay posiciones `meta-ai` en la v6, asi
que la copia de la llave de `muse login` a `.env` queda inactiva. La cadena
de vision del director (siguiente seccion) sigue siendo una decision aparte
del propietario. Los workflows siguen ejecutandose por el router de
suscripciones de esta pagina.

## Cadena de vision del director (2026-09-28, vigente)

La cadena conversacional de arriba es para codigo y tareas generales. Para
`vision_analyze`, el director obtiene una cadena de vision separada del
propietario: `zai/glm-5.3-flash` (primario), luego `kimi-coding/kimi-for-coding`,
luego `anthropic/claude-opus-5`. El primario rechaza `vision_analyze` con
`400 ModelProtocolUnsupported` y aquella cadena permite el fallback automatico.

## Matriz vigente: 8/8 rutas y 15/15 modelos listos (2026-09-26)

OpenCode Zen no ocupa slot y no se sondea. Probes oficiales uno por uno con
marcador exacto `GONCLOUD_PROBE_OK`. Las ocho rutas activas quedaron listas:
OpenCode Go, Muse, Claude Max, Codex, Kimi, Z.AI Coding Plan por OpenCode,
Grok y DeepSeek API. Una segunda ronda exhaustiva comprobo las 15 combinaciones
unicas adaptador/modelo que cubren las 56 celdas de la tabla; todas respondieron
el marcador exacto. El primer ciclo marco Grok como `auth`, pero `grok models`
confirmo inmediatamente sesion y `grok-4.6`; el probe aislado y la inferencia
exhaustiva posterior pasaron. Un live listo levanta cuarentena; un fallo la
arma una hora y el router avanza de posicion.

## Matriz anterior: 8/9 pruebas live aprobadas (2026-09-14)

Inferencias independientes, estrictamente secuenciales y con marcador exacto
`GONCLOUD_PROBE_OK`. Cada JSON sanitizado se inspecciono antes del siguiente.
Las categorias siguientes son el resultado observado, no un diagnostico de la
cuenta ni una inferencia basada en la existencia de credenciales.

| Adaptador | Resultado live | Facturacion | Cuarentena durable | Tiempo |
|---|---|---|---|---|
| opencode_go | ready | subscription | no | 4.4 s |
| opencode_zen | ready | subscription | no | 6.2 s |
| muse | ready | subscription | no | 71.94 s |
| claude | ready | subscription | no | 14.82 s |
| codex | quarantined / quota | subscription | si | 18.12 s |
| kimi | ready | subscription | no | 14.9 s |
| zai | ready | subscription | no | 4.24 s |
| grok | ready | subscription | no | 14.88 s |
| deepseek | ready | api | no | 2.48 s |

Los fallos que avanzan de slot son `auth`, `quota`, `rate_limit`,
`model_unavailable`, `timeout`, `transport`, `invalid_terminal` y
`task_failure`: `invalid_terminal` es el proveedor que respondio fuera del
contrato terminal; `task_failure`, la corrida que ejecuto sin entregar un
veredicto del modelo. `transport`, `invalid_terminal` y `task_failure`
bloquean en cuanto el workspace cambio. `invalid_request` bloquea siempre.

`quota` y `rate_limit` son cosas distintas. `quota` es el plan agotado ("The
usage limit has been reached.", el limite semanal de kimi, "insufficient
balance"): avanza al instante, deja la cuenta 15 minutos fuera y la guarda como
no lista. Un freno que nombra el plan o su reinicio tambien es `quota`: "Rate
limit exceeded for your plan. Resets at 3:00 PM.", "429 Too Many Requests: limit
resets in 5h 12m", "billing period". Si un mensaje trae palabras de los dos
tipos, gana `quota`.

`rate_limit` es un freno de segundos ("429 Output token rate limit exceeded.
Please retry after a brief wait." de OpenCode Go, "too many requests",
"overloaded", un 429 sin palabras de plan). En stderr, el canal del proveedor,
cuenta cualquier palabra de freno. En stdout, donde escribe el modelo, solo
cuentan la frase de error del proveedor ("rate limit exceeded", "too many
requests") o un 429 en contexto HTTP: "I will slow down" o "Added retry-after
handling" son prosa y siguen siendo `task_failure`.

Ante un `rate_limit` el router reintenta la misma posicion hasta dos veces, tras
20 s y luego 60 s, o tras la espera que el proveedor pida. La espera se lee con
su unidad (ms, s, min, h; un numero solo son segundos) y una unidad desconocida
no da espera. Renueva el lease de la etapa antes de cada espera. Si el proveedor
pide mas de 120 s, avanza sin esperar. Una etapa duerme como mucho 180 s en
frenos sumando todas sus posiciones; despues, un freno avanza sin esperar y la
cadena termina con el resultado normal de posiciones agotadas. Al avanzar, la
cuenta queda fuera 2 minutos, o la espera que el proveedor pidio hasta 15
minutos. La cuenta queda lista en todo momento y el freno nunca arma ni renueva
una cuarentena. Un reintento despues de que el workspace cambio corre como
continuacion, igual que el avance a otra posicion. Si el proceso muere durante
una espera, al reanudar la etapa sigue en la siguiente posicion y no vuelve a la
que estaba frenada. Se acepta: es la misma reanudacion que deja cualquier fallo
que avanza.

Antes del 2026-10-15 todo freno contaba como cuota: la cuenta principal de
OpenCode Go quedaba 15 minutos fuera por un freno de segundos y el trabajo caia
en suplentes mas debiles hasta agotar la cadena (12 etapas bloqueadas en el
historial del kanban).

Dos reglas hacen que ese reparto funcione y las dos se violaron en `t_f1c90a69`:

- Un CLI que sale con codigo 0 y sin envelope terminal valido es
  `invalid_terminal`, no `task_failure`. El proveedor respondio y la ruta no
  entrego nada usable; no se demostro que la tarea fallara. Un error de
  lanzamiento, que nunca llego al proveedor, si sigue siendo `task_failure`.
- Un arbol de trabajo sucio, por si solo, no es una continuacion. Sin ningun
  intento previo ni commit sobre la base, la etapa arranca en `mode="start"` y
  el modelo NO recibe los archivos del propietario listados como trabajo de un
  worker anterior. Esa historia falsa es la que rompio la cadena real.
- Para decidir si un fallo avanza o bloquea, el router compara el arbol al
  arrancar **ese intento** contra el arbol al terminarlo (`tree_fingerprint`:
  revision HEAD mas el contenido de cada archivo sucio, con topes de lectura
  y sin seguir enlaces). La suciedad heredada que el intento no toco no
  cuenta como mutacion suya: un fallo limpio avanza de posicion aunque el
  arbol ya viniera sucio. Lo que si bloquea es el delta propio del intento
  (editar un archivo ya sucio, crear archivos, commitear durante la corrida)
  y, en modo continuacion, el trabajo propio de intentos anteriores de la
  misma etapa. El veredicto propio queda guardado en la fila del intento y
  es el que lee la recuperacion tras un reinicio. El detalle historico de
  las dos rondas esta en [estado](status.md).
El unico fallo vigente en la matriz, `quota` en Codex, avanza de slot y no bloquea ninguna etapa;
el proveedor declara la fecha de reposicion. Zen y Kimi pasaron a `ready` al
agregar a Node la raiz TLS de Kaspersky que intercepta esas conexiones, y Kimi
ademas recibe ahora el encargo por archivo privado; los dos detalles y la
variable `NODE_EXTRA_CA_CERTS` que el router necesita en esta maquina estan en
[estado](status.md).

El comando oficial `configure_subscriptions.py --live-probe-all --only ADAPTADOR
--json RUTA` guarda salud normalizada en
`%LOCALAPPDATA%\hermes\subscription-router.db`. Un fallo mantiene cuarentena
durante una hora (`_QUARANTINE_RETRY_SECONDS`), con su propio reloj
`quarantined_at` que un probe superficial no reinicia. Vencida esa hora el
router vuelve a intentar la cuenta por si solo cuando un encargo llega a su
posicion; solo una inferencia real exitosa levanta la cuarentena y un fallo
repetido la vuelve a armar con reloj nuevo. La cuota que se repone entra sola.
El bloqueo de probes es global entre procesos; el bloqueo por adaptador cubre
probe, inferencia y publicacion. El router registra omisiones como
`disposition=skipped`, distintas de un fallo ejecutado, y no invoca adaptadores
con cuarentena vigente.

No hubo login interactivo, migracion de lanzadores, limpieza de credenciales ni
lectura de logs crudos. Los JSON por adaptador estan en `.claude/tmp/` con nombres
`task9b-go`, `task9b-zen`, `task9b-muse`, `task9b-claude`, `task9b-codex`,
`task9b-kimi`, `task9b-zai`, `task9b-grok`, `task9b-deepseek` y extension `.json`;
no contienen secretos ni identificadores de cuenta.

## Workflow real: cadena completa entregada

Scout `t_f2e2bb6d`: completado sin cambios, pausa solicitada tras scout. Un
intento Go, slot 0, `deepseek-v4.1-flash`, `billing_mode=subscription`.

Cadena `t_be290d83`: **entregada**. Las siete etapas se ejecutaron en el orden
durable scout, ingenieria, implementer, verifier, adversary, reviewer, entrega,
con un unico intento cada una, todas en Go slot 0 y `billing_mode=subscription`:
`deepseek-v4.1-flash` en scout, adversary y reviewer;
`muse-spark-1.3-contributor` en ingenieria, implementer, verifier y entrega.
Ningun intento fallo, ninguna etapa avanzo de slot y no hubo ronda correctiva.
El revisor aprobo con `{"decision":"approve","severity":null}` a traves de
`WorkflowService.review`, y solo entonces `delivery_blocked` paso a `false`.

El cambio autorizado se realizo y quedo acotado: `acceptance.txt` del fixture
desechable contiene `status: accepted` mas LF y `git status --porcelain` no
lista ningun otro archivo. El fixture de scout sigue limpio. La evidencia
durable muestra un propietario por etapa ejecutada, cero intentos en curso y
cero leases restantes. Hubo una unica invocacion de workflow a la vez.

Evidencia: `.claude/tmp/task9b-acceptance-evidence.json`,
`.claude/tmp/task9b-scout-status.json` y `.claude/tmp/task9b-chain-status.json`.
Kanban nativo: `%LOCALAPPDATA%\hermes\kanban.db`; intentos/salud:
`%LOCALAPPDATA%\hermes\subscription-router.db`. Fixtures preservados en
`%TEMP%\goncloud-router-acceptance-20260914T214024Z\{scout,chain3}`.
Gates finales: 347 tests (344 aprobados, 3 skips), 8/8 hooks, `git diff --check` limpio,
Plugin Doctor OK (7 herramientas, 1 hook) y gateway `stopped`. Los defectos
corregidos para llegar a esta cadena estan en [estado](status.md).

Telegram: stopped pending separate transport cutover

## Registro historico anterior a esta aceptacion

Lo siguiente conserva antecedentes de la instalacion previa. Sus cuentas,
inferencias, cadenas nativas, migraciones y conteos NO describen la aceptacion
vigente; para decisiones operativas rige exclusivamente la matriz anterior.

### Cadenas de modelos de OpenClaw (historico)

Fuente autoritativa: tabla pegada por el usuario el 2026-09-13, conservada literalmente en `model-routing.source.json`. Ocho agentes, ocho posiciones por agente, 64 celdas. `main` corresponde a `claw` en Hermes. No inferir cadenas a partir de los snapshots historicos.

### Estado historico

Tabla registrada y aplicada a los nueve perfiles con `configure_team.py --apply-model-routing`. Cada perfil conserva Primary y F1-F7 en orden; `main` se traduce a `claw` y `entrega` hereda `main`.

### Enrutador por slot (2026-09-13, historico)

La vinculacion la determina la posicion, no el prefijo historico: Primary consume OpenCode Go, F1 OpenCode Zen y F2-F7 la suscripcion de la familia del modelo (`opencode/gpt-6-astra` en F2+ se enruta a Codex/ChatGPT, `opencode/claude-*` a Claude Max, `opencode/muse-*` a Muse CLI, `opencode-go-resp/muse-*` a Muse). DeepSeek es la unica ruta `billing_mode=api`. `entrega` hereda `main`; los slots `dup` se conservan como intentos distintos y solo se limpia el identificador del modelo.

Implementacion: `plugins/goncloud-team/subscriptions` compila la tabla en rutas tipadas, invoca adaptadores por CLI/API, registra intentos en SQLite privado (`subscription_attempts`/`subscription_health`) y avanza solo en fallos de autenticacion, cuota, modelo o timeout. Tras una mutacion del workspace, el siguiente intento recibe `mode=continue` con revision y archivos cambiados. La herramienta `goncloud_subscription_run` liga el perfil por contexto y la evidencia de cada etapa queda en comentarios Kanban nativos (`GONCLOUD_SUBSCRIPTION_V1`).

Credenciales: OAuth/sesiones nativas por CLI (nunca se copian); GLM y DeepSeek migradas de lanzadores en texto plano al env privado `%LOCALAPPDATA%\hermes\subscription-router.env` con `scripts/configure_subscriptions.py --migrate-launchers --verify` (dos fases: probe real antes de reescribir; backups `.goncloud-backup` fuera de Git). Z.AI Coding Plan consume su endpoint documentado compatible con Anthropic.

Comprobado en vivo: fallback forzado Go(auth) -> Zen(quota) -> F2 Claude Max con inferencia real ("READY"), y continuacion tras mutacion con diff registrado ("LISTO"). Verificaciones unitarias: 65 pruebas del enrutador/adaptadores.

### Matriz live anterior al recovery (historico, no vigente)

| Adaptador | Disponible | Facturacion | Evidencia |
|---|---|---|---|
| opencode_go | si | suscripcion | PASS "READY" (muse-spark-1.3-contributor) |
| opencode_zen | no (cuarentena) | suscripcion | CLI cuelga en silencio; coordinador nativo reporta 429 FreeUsageLimitError |
| muse | si | suscripcion | PASS "READY" (WSL) |
| claude | si | suscripcion | PASS "LISTO" (claude-sonnet-5, Max) |
| codex | si | suscripcion | PASS "READY" (gpt-6-astra, ChatGPT) |
| kimi | no | suscripcion | FAIL provider.connection_error (endpoint Kimi; coordinador nativo ve 401) |
| zai | si | suscripcion | PASS "READY" (glm-5.3, Coding Plan) |
| grok | si | suscripcion | PASS "READY" (grok-4.6, grok.com) |
| deepseek | si | api | PASS "READY" (deepseek-flash; unica ruta API) |

Pendientes: (1) OpenCode Zen sin inferencia completada — hipotesis saldo/limite de la capa free, no autenticacion; re-probar tras reposicion de cuota. (2) Kimi con error de conexion/credencial del proveedor; el router avanza de slot sin intervention. Ninguna ruta se marca disponible solo por existencia de credencial.

### Compatibilidad previa en Hermes 3f86ed75 (historico)

- `fallback_providers` admite una lista ordenada de pares proveedor/modelo.
- `hermes_cli.fallback_config.get_fallback_chain` elimina entradas repetidas por proveedor, modelo y URL. La etiqueta documental ` dup` se conserva en la fuente, pero no se envia como parte del identificador del modelo.
- `agent.chat_completion_helpers.try_activate_fallback` avanza un indice sobre la cadena y omite proveedores que no puede configurar. Esto no demuestra, por si solo, recuperacion real ante todos los errores de cuota de cada proveedor.
- Hermes enruta Muse Spark de OpenCode Go mediante `codex_responses`; por compatibilidad, `opencode-go-resp` se traduce al proveedor nativo `opencode-go` y no se inventa otro endpoint.
- Mantener modelo y proveedor separados: los nombres OpenClaw no se copian ciegamente a la clave `provider` de Hermes. OpenCode Zen/Go y Kimi Coding tienen normalizacion propia.

### Decisiones registradas previamente (historico)

- Mantener la tabla original y el orden Primary a F7. No volver a pedir que elija proveedor.
- `entrega` hereda exactamente la cadena de `main` (perfil Hermes `claw`). Registrado y aplicado al runtime.
- Canal del director: bot nuevo de Telegram, no el bot existente.
- El usuario aclaro que las dos primeras posiciones corresponden a OpenCode Go/Zen y las restantes a su suscripcion normal de Muse. Es su distribucion declarada de cuentas, no una comprobacion de compatibilidad o facturacion.
- No volver a preguntar por `dup`: conservar la fuente literal y verificar tecnicamente la vinculacion de cuentas. La aclaracion del usuario no demuestra por si sola que sean rutas distintas ni que Muse cubra modelos de otros proveedores.
- El usuario no conoce el endpoint de `opencode-go-resp`; investigarlo desde configuracion/documentacion, sin pedirle que lo deduzca ni inventar una URL.

### Verificacion previa (historico)

1. OpenCode Go no tiene credencial instalada. OpenCode Zen respondio limite/saldo insuficiente. Kimi funciono inicialmente y despues respondio credencial expirada. Z.AI sigue respondiendo correctamente.
2. En una sesion real el director salto automaticamente por los proveedores no disponibles hasta Z.AI y termino el encargo. Las ocho posiciones quedan configuradas, pero no se promete que toda API permita ocho reintentos seguros dentro del mismo turno, especialmente despues de herramientas con efectos.
3. Muse CLI 1.2.1 esta autenticado en WSL. La herramienta nativa `goncloud_muse_delegate` fue llamada por el director y devolvio `DIRECTOR_TO_MUSE_OK` usando `muse-spark-1.3-contributor`.

La configuracion no convierte la suscripcion personal de Muse en una API: Muse trabaja mediante su CLI oficial como especialista delegado. Los proveedores nativos de Hermes siguen usando sus propias credenciales y cuotas.

Referencia: [Fallback Providers de Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers). La seccion web que habla de un unico salto por turno no basta para interpretar el runtime instalado, que contiene una cadena con indice; verificar el bucle real y sus errores antes de prometer ocho intentos efectivos.
