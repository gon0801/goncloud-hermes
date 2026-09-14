# Cadenas de modelos de OpenClaw

Fuente autoritativa: tabla pegada por el usuario el 2026-09-13, conservada literalmente en `model-routing.source.json`. Ocho agentes, ocho posiciones por agente, 64 celdas. `main` corresponde a `claw` en Hermes. No inferir cadenas a partir de los snapshots historicos.

## Estado

Tabla registrada y aplicada a los nueve perfiles con `configure_team.py --apply-model-routing`. Cada perfil conserva Primary y F1-F7 en orden; `main` se traduce a `claw` y `entrega` hereda `main`.

## Enrutador por slot (2026-09-13)

La vinculacion la determina la posicion, no el prefijo historico: Primary consume OpenCode Go, F1 OpenCode Zen y F2-F7 la suscripcion de la familia del modelo (`opencode/gpt-6-astra` en F2+ se enruta a Codex/ChatGPT, `opencode/claude-*` a Claude Max, `opencode/muse-*` a Muse CLI, `opencode-go-resp/muse-*` a Muse). DeepSeek es la unica ruta `billing_mode=api`. `entrega` hereda `main`; los slots `dup` se conservan como intentos distintos y solo se limpia el identificador del modelo.

Implementacion: `plugins/goncloud-team/subscriptions` compila la tabla en rutas tipadas, invoca adaptadores por CLI/API, registra intentos en SQLite privado (`subscription_attempts`/`subscription_health`) y avanza solo en fallos de autenticacion, cuota, modelo o timeout. Tras una mutacion del workspace, el siguiente intento recibe `mode=continue` con revision y archivos cambiados. La herramienta `goncloud_subscription_run` liga el perfil por contexto y la evidencia de cada etapa queda en comentarios Kanban nativos (`GONCLOUD_SUBSCRIPTION_V1`).

Credenciales: OAuth/sesiones nativas por CLI (nunca se copian); GLM y DeepSeek migradas de lanzadores en texto plano al env privado `%LOCALAPPDATA%\hermes\subscription-router.env` con `scripts/configure_subscriptions.py --migrate-launchers --verify` (dos fases: probe real antes de reescribir; backups `.goncloud-backup` fuera de Git). Z.AI Coding Plan consume su endpoint documentado compatible con Anthropic.

Comprobado en vivo: fallback forzado Go(auth) -> Zen(quota) -> F2 Claude Max con inferencia real ("READY"), y continuacion tras mutacion con diff registrado ("LISTO"). Verificaciones unitarias: 65 pruebas del enrutador/adaptadores.

## Matriz live 2026-09-14 (inferencia real minima, sin identificadores de cuenta)

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

## Compatibilidad comprobada en Hermes 3f86ed75

- `fallback_providers` admite una lista ordenada de pares proveedor/modelo.
- `hermes_cli.fallback_config.get_fallback_chain` elimina entradas repetidas por proveedor, modelo y URL. La etiqueta documental ` dup` se conserva en la fuente, pero no se envia como parte del identificador del modelo.
- `agent.chat_completion_helpers.try_activate_fallback` avanza un indice sobre la cadena y omite proveedores que no puede configurar. Esto no demuestra, por si solo, recuperacion real ante todos los errores de cuota de cada proveedor.
- Hermes enruta Muse Spark de OpenCode Go mediante `codex_responses`; por compatibilidad, `opencode-go-resp` se traduce al proveedor nativo `opencode-go` y no se inventa otro endpoint.
- Mantener modelo y proveedor separados: los nombres OpenClaw no se copian ciegamente a la clave `provider` de Hermes. OpenCode Zen/Go y Kimi Coding tienen normalizacion propia.

## Decisiones confirmadas por el usuario

- Mantener la tabla original y el orden Primary a F7. No volver a pedir que elija proveedor.
- `entrega` hereda exactamente la cadena de `main` (perfil Hermes `claw`). Registrado y aplicado al runtime.
- Canal del director: bot nuevo de Telegram, no el bot existente.
- El usuario aclaro que las dos primeras posiciones corresponden a OpenCode Go/Zen y las restantes a su suscripcion normal de Muse. Es su distribucion declarada de cuentas, no una comprobacion de compatibilidad o facturacion.
- No volver a preguntar por `dup`: conservar la fuente literal y verificar tecnicamente la vinculacion de cuentas. La aclaracion del usuario no demuestra por si sola que sean rutas distintas ni que Muse cubra modelos de otros proveedores.
- El usuario no conoce el endpoint de `opencode-go-resp`; investigarlo desde configuracion/documentacion, sin pedirle que lo deduzca ni inventar una URL.

## Verificacion real

1. OpenCode Go no tiene credencial instalada. OpenCode Zen respondio limite/saldo insuficiente. Kimi funciono inicialmente y despues respondio credencial expirada. Z.AI sigue respondiendo correctamente.
2. En una sesion real el director salto automaticamente por los proveedores no disponibles hasta Z.AI y termino el encargo. Las ocho posiciones quedan configuradas, pero no se promete que toda API permita ocho reintentos seguros dentro del mismo turno, especialmente despues de herramientas con efectos.
3. Muse CLI 1.2.1 esta autenticado en WSL. La herramienta nativa `goncloud_muse_delegate` fue llamada por el director y devolvio `DIRECTOR_TO_MUSE_OK` usando `muse-spark-1.3-contributor`.

La configuracion no convierte la suscripcion personal de Muse en una API: Muse trabaja mediante su CLI oficial como especialista delegado. Los proveedores nativos de Hermes siguen usando sus propias credenciales y cuotas.

Referencia: [Fallback Providers de Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers). La seccion web que habla de un unico salto por turno no basta para interpretar el runtime instalado, que contiene una cadena con indice; verificar el bucle real y sus errores antes de prometer ocho intentos efectivos.
