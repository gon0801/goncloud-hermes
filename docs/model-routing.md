# Enrutador de suscripciones: aceptacion local 2026-09-14

Fuente autoritativa: `model-routing.source.json`, intacta desde `f86fc8a`.
Ocho agentes por ocho posiciones: 64 celdas; `main` corresponde a `claw` y
`entrega` hereda `main`. Los nueve perfiles tienen la misma fuente literal
instalada y el plugin aprobado en `3797050`; no tienen cadenas nativas
administradas `model`/`fallback_providers`. Copilot y Copilot ACP estan excluidos.

Primary (slot 0) usa OpenCode Go y F1 (slot 1) OpenCode Zen. F2-F7 usan la
suscripcion de la familia correspondiente, sin reinterpretar los prefijos
historicos. Los duplicados mantienen posiciones distintas. Solo el adaptador
DeepSeek factura como API; un modelo DeepSeek servido por Go sigue siendo
suscripcion. No se movio ninguna posicion para compensar cuentas no disponibles.

## Matriz vigente: 4/9 pruebas live aprobadas

Inferencias independientes, estrictamente secuenciales y con marcador exacto
`GONCLOUD_PROBE_OK`. Cada JSON sanitizado se inspecciono antes del siguiente.
Las categorias siguientes son el resultado observado, no un diagnostico de la
cuenta ni una inferencia basada en la existencia de credenciales.

| Adaptador | Resultado live | Facturacion | Cuarentena durable | Tiempo |
|---|---|---|---|---|
| opencode_go | ready | subscription | no | 5.81 s |
| opencode_zen | failed / timeout | subscription | si | 301.57 s, incluida limpieza |
| muse | ready | subscription | no | 34.94 s |
| claude | quarantined / auth | subscription | si | 1.22 s |
| codex | ready | subscription | no | 22.43 s |
| kimi | quarantined / auth | subscription | si | 14.49 s |
| zai | failed / task_failure | subscription | si | 3.49 s |
| grok | quarantined / auth | subscription | si | 2.98 s |
| deepseek | ready | api | no | 1.90 s |

El comando oficial `configure_subscriptions.py --live-probe-all --only ADAPTADOR
--json RUTA` guarda salud normalizada en
`%LOCALAPPDATA%\hermes\subscription-router.db`. Un fallo mantiene cuarentena mas
alla del TTL; un probe superficial o una ejecucion normal no la levantan. Solo
una nueva prueba live exacta puede hacerlo. El bloqueo de probes es global entre
procesos; el bloqueo por adaptador cubre probe, inferencia y publicacion. El
router registra omisiones como `disposition=skipped`, distintas de un fallo
ejecutado, y no invoca adaptadores en cuarentena.

No hubo login interactivo, migracion de lanzadores, limpieza de credenciales ni
lectura de logs crudos. Los JSON por adaptador estan en `.claude/tmp/` con nombres
`go`, `zen`, `muse`, `claude`, `codex`, `kimi`, `zai`, `grok`, `deepseek` y extension
`.json`; no contienen secretos ni identificadores de cuenta.

## Workflow real: NO-GO para aceptacion completa

Scout `t_ef1a2c9b`: completado sin cambios, pausa solicitada tras scout (el
comando `status` lo muestra `pending`). Un intento Go, slot 0,
`deepseek-v4.1-flash`, `billing_mode=subscription`.

Cadena `t_5202e34b`: scout completado; ingenieria bloqueada con un unico intento
Go, slot 0, `muse-spark-1.3-contributor`, suscripcion, `task_failure`,
`disposition=executed`. No se publico un envelope terminal valido para esa
etapa. Ese fallo no autoriza avanzar de slot. Implementer, verifier, adversary,
reviewer y entrega permanecen `todo`; no existe aprobacion del revisor y
`delivery_blocked=true`. La cadena completa y su entrega no estan demostradas.

Los dos repositorios desechables siguen limpios. En `chain`, `acceptance.txt`
conserva los bytes `status: pending` mas LF; el cambio autorizado no ocurrio.
La evidencia durable muestra un propietario por etapa ejecutada y cero intentos
en curso o leases restantes. Hubo una unica invocacion de workflow a la vez.

Evidencia: `.claude/tmp/task9-acceptance-evidence.json`,
`.claude/tmp/task9-scout-status.json` y `.claude/tmp/task9-chain-status.json`.
Kanban nativo: `%LOCALAPPDATA%\hermes\kanban.db`; intentos/salud:
`%LOCALAPPDATA%\hermes\subscription-router.db`. Fixtures preservados en
`%TEMP%\goncloud-router-acceptance-20260914T171500Z\{scout,chain}`.
Gates finales: 290 tests (287 aprobados, 3 skips), 8/8 hooks, Plugin Doctor OK
(7 herramientas, 1 hook). Incidencias y conteos de las repeticiones autorizadas:
[estado](status.md). Aprobar estos gates no demuestra la cadena live pendiente.

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
