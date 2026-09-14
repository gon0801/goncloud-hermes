# Estado — 2026-09-14

## Estado actual: aceptacion local completa (2026-09-14)

- Codigo de runtime aprobado y desplegado: `0f206ca`. Nueve perfiles verificados;
  fuente literal byte-identica, sin cadenas nativas administradas y con Copilot
  excluido. Backup previo de 18 archivos, con hashes comprobados, en
  `%LOCALAPPDATA%\hermes\recovery-backups\20260914T214024Z-task9b`; nada eliminado.
- Matriz live actual: **5/9 ready**: Go, Claude Max, Z.AI y Grok por suscripcion;
  DeepSeek por API. Cuatro cuarentenas durables: Zen `timeout` (301 s, limite de la
  capa free), Muse `model_unavailable`, Codex `quota`, Kimi `transport`. Las cuatro
  avanzan de slot; ninguna bloquea una etapa. Probes individuales secuenciales, sin
  login interactivo, migracion ni limpieza de lanzadores. Tabla y slots intactos.
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
- Bateria final: **338 tests, 335 aprobados y 3 skips de plataforma/privilegios**,
  282.318 s, exit 0. Pre-commit completo: 8/8; `git diff --check` limpio.
- Plugin Doctor: OK, 7 herramientas y 1 hook. Este runtime ofrece
  `hermes -p claw plugins doctor goncloud-team --ci`; el comando del brief
  `tools plugin doctor` no existe (exit 2). Deep gateway status: exit 0, stopped.

Telegram: stopped pending separate transport cutover

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

### Pendiente de decision del propietario

El slot F4 de la tabla vincula el adaptador Muse al modelo
`muse-spark-1.3-contributor-free`. El CLI de Muse responde textualmente que ese
modelo no existe o no hay acceso: el sufijo `-free` es el nombre de la capa
gratuita de OpenCode Zen, no un identificador de modelo de Muse. Quitarlo seria
reinterpretar un slot, lo que el diseño prohibe, asi que no se toco: Muse queda
`model_unavailable` y el router avanza. Tratar `-free` como el marcador `dup`
requiere una decision explicita.

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
