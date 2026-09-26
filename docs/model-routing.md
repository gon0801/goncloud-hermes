# Enrutador de suscripciones: aceptacion local 2026-09-14

Fuente autoritativa: `model-routing.source.json` (tabla del propietario,
2026-09-19). Ocho agentes por siete posiciones: 56 celdas; `main` corresponde
a `claw` y `entrega` hereda `main`. La columna vacia que era OpenCode Zen se
omitio; no hay octavo slot. Los nueve perfiles instalan esa fuente literal y
no tienen cadenas nativas administradas `model`/`fallback_providers`. Copilot
y Copilot ACP estan excluidos.

Primary (slot 0) usa OpenCode Go (el prefijo `opencode-go-resp` se ata igual).
F1-F6 usan la suscripcion de la familia correspondiente, sin reinterpretar
los prefijos; `kimi-for-coding` entra por familia Kimi y `glm-5.3-flash` por
Z.AI. Las etiquetas repetidas son intentos distintos. Los marcadores `(max)`,
`(xhigh)` y `(high)` se conservan en la etiqueta y no forman parte del
identificador que se envia al proveedor. Solo el adaptador DeepSeek factura
como API; un modelo DeepSeek servido por Go sigue siendo suscripcion. Verifier
tiene esa ruta API en F6 y scout en F4; adversary ya no.

## Matriz vigente: 6/8 cuentas en uso (2026-09-20)

OpenCode Zen no ocupa slot y no se sondea. Probes oficiales uno por uno con
marcador exacto `GONCLOUD_PROBE_OK`. Listas: Go, Muse, Z.AI, Grok, DeepSeek y
Codex.
Claude Max respondio `quota`; Kimi `quota` (cupo semanal, sesion vigente).
Codex paso a `ready` tras no tratar el aviso de skills como fallo. Un live listo levanta cuarentena; un fallo la arma una hora
y el router avanza de posicion.

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

Los fallos que avanzan de slot son `auth`, `quota`, `model_unavailable`,
`timeout`, `transport` e `invalid_terminal` (el proveedor respondio pero fuera
del contrato terminal); `transport` e `invalid_terminal` bloquean en cuanto el
workspace cambio. `task_failure` e `invalid_request` bloquean siempre.

Dos reglas hacen que ese reparto funcione y las dos se violaron en `t_f1c90a69`:

- Un CLI que sale con codigo 0 y sin envelope terminal valido es
  `invalid_terminal`, no `task_failure`. El proveedor respondio y la ruta no
  entrego nada usable; no se demostro que la tarea fallara. Un error de
  lanzamiento, que nunca llego al proveedor, si sigue siendo `task_failure`.
- Un arbol de trabajo sucio, por si solo, no es una continuacion. Sin ningun
  intento previo ni commit sobre la base, la etapa arranca en `mode="start"` y
  el modelo NO recibe los archivos del propietario listados como trabajo de un
  worker anterior. Esa historia falsa es la que rompio la cadena real.
- Pero para decidir si un fallo avanza o bloquea, "el workspace cambio" sigue
  siendo conservador: **cualquier** archivo sucio cuenta. Distinguir quien
  ensucio que requiere una linea base de contenido, y dos rondas de revision
  externa encontraron formas de que esa distincion leyera una mutacion real como
  "sin cambios". Bloquear una etapa se recupera; pisar el trabajo de un worker
  no. Consecuencia practica: **con el arbol sucio no hay failover automatico**,
  asi que commitea o stashea antes de lanzar una cadena. El detalle de las dos
  rondas esta en [estado](status.md).
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
