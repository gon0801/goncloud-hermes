# Estado — 2026-10-12

## El instalador vuelve a desplegar y el despachador ya no toca la cadena

Causa: el 2026-10-03 se editaron a mano en la PC el perfil `claw` (toolset
`kanban`, `kanban.dispatch_in_gateway: true`, `SOUL.md` con la seccion
"Especialistas") y `workflow.py` en las 20 copias (11 roles nuevos). Nada de
eso estaba en el repo, asi que `configure_team.py` abortaba con
`Local configuration conflict` y `Local managed-file conflict`.

Al revisarlo aparecio un defecto anterior: el gateway corre un solo
despachador Kanban para todos los perfiles y lee el `config.yaml` raiz, donde
esta encendido por defecto desde 2026-10-02. El `dispatch_in_gateway: false`
de cada perfil nunca le llego, asi que cualquier tarjeta de etapa en `ready`
podia correr con un agente nativo, fuera del router. No ocurrio porque ningun
workflow corrio desde 2026-09-28.

Arreglo: el repo adopta el estado vivo (decision del propietario, 2026-10-12)
y corrige lo que estaba mal. `configure_team.py` escribe en el `config.yaml`
raiz la lista blanca `kanban.dispatch_profiles` con los once especialistas y
`auto_decompose: false`; el rol del director usa `kanban_create` solo para
ellos; `--check` valida contra los perfiles sin escribir. Pendiente para la
fase 1: los especialistas no tienen `model:` propio.
`docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md`.

# Estado — 2026-09-28

## Director en proveedores nativos

Causa: desde 2026-09-27 13:00 `claw` conversaba por el proveedor
`goncloud-subscriptions/main`, que corria las CLI de cada proveedor con sus
herramientas apagadas (`tools: {"*": false}`) y le pedia al modelo escribir
cada llamada como texto `<tool_call>{json}</tool_call>`. Ese canal se pierde
con quotes anidados: el 2026-09-28, 8 de las 13 llamadas emitidas en la
conversacion real se descartaron por JSON mal escapado, y el director
prometio trabajo ("lo verifico ahorita") durante siete horas sin ejecutar una
sola herramienta.

Arreglo: `configure_team.py` compila el `model` + `fallback_providers` de
`claw` directamente de la fila `main` de `model-routing.source.json`, en
siete proveedores Hermes nativos (ocho posiciones) con llamadas a
herramientas nativas — `opencode-go` (slots 0 y 1, una sola entrada con un
pool de dos credenciales), `anthropic`, `meta-ai`, `xai-oauth`, `zai` sobre
el endpoint del Coding Plan, `openai-codex` y `kimi-coding` sobre su
endpoint OpenAI `/coding/v1` (`chat_completions`: el wire Anthropic Messages
que Hermes elige para toda URL `/coding` le perdia 5 de 12 llamadas a
herramienta a K3, el OpenAI solo 1) — y los marcadores de esfuerzo a
`agent.reasoning_overrides`. Ya no existe una cadena nativa escrita a mano
que pueda contradecir la tabla. `entrega` hereda la fila `main` como etapa
trabajadora y sigue en el router de suscripciones; los demas trabajadores no
cambiaron. Detalle en
[spec](superpowers/specs/2026-09-28-director-native-chain-design.md).

Se elimino de punta a punta el proveedor `goncloud-subscriptions`,
`subscriptions/conversation.py`, `InvocationRequest.tool_bridge` y cada rama
`tool_bridge` de los adaptadores. Un deploy ahora retira ademas las claves de
configuracion y los archivos administrados que deja de servir, acotado al
perfil. Suite completa: 443 pruebas, `OK (skipped=3)` en `379268c`.

Pendiente (Tarea 6): cargar las credenciales nativas que falten, el live
probe por proveedor y el despliegue. Nada de esto esta desplegado todavia.

Vision: el primario (`opencode-go/muse-spark-1.3-contributor`) rechaza
`vision_analyze` con `400 ModelProtocolUnsupported`; el director lee
`auxiliary.vision` como una cadena ordenada (decision del propietario,
2026-09-28): `zai/glm-5.3-flash` primero, luego `kimi-coding/kimi-for-coding`,
luego `anthropic/claude-opus-5`.

# Estado — 2026-09-27

## Enrutamiento conversacional exacto, desplegado (superado 2026-09-28)

Superado por «Director en proveedores nativos» arriba: el proveedor
`goncloud-subscriptions/main` que describe esta seccion ya no existe en el
repo. Se conserva como registro historico de esa aceptacion; para el estado
vigente del director, esta seccion no aplica.

Se elimino de `team.json` la excepcion `director_model`/`director_fallbacks`
que reducia `main` a OpenCode Go, Kimi y Z.AI. `claw` selecciona ahora un unico
proveedor local, `goncloud-subscriptions/main`, que compila las siete posiciones
de la fila `main`. La regresion fuerza Primary a `model_unavailable` y comprueba
la secuencia exacta `opencode_go -> claude`, terminal `claude-opus-5`, esfuerzo
`xhigh`, facturacion `subscription` y cero intentos de Kimi.

Verificacion previa al despliegue: **414 tests aprobados, 3 skips de plataforma
y 8/8 hooks de pre-commit**. Las pruebas temporales despliegan los nueve
perfiles, comparan byte por byte la tabla y el proveedor, y prueban que `claw`
y `scout` cargan solamente su propia copia.

Despliegue 2026-09-27: los nueve perfiles recibieron la misma tabla
(`SHA-256 f7ca3b8f715c3137104bdb98f97f5c7b4264f8650c0fd0f67dfff6cc3dad11d6`)
y una copia byte-identica del proveedor. Las modificaciones locales de
`SOUL.md` se preservaron. `claw/config.yaml` quedo en
`goncloud-subscriptions/main`, sin `fallback_providers`, y el gateway se detuvo
con drenado limpio y arranco de nuevo correctamente.

Prueba live controlada: se inyecto solamente `model_unavailable` en el limite
del adaptador Primary y se dejo F1 real. La secuencia observada fue slot 0
`opencode_go` -> slot 1 `claude`; termino en `claude-opus-5`, esfuerzo `xhigh`,
facturacion `subscription` y marcador exacto `GONCLOUD_FALLBACK_OK`. Kimi no
se intento.

## Tabla del propietario y verificacion live completa (2026-09-26)

La fuente `model-routing.source.json` coincide con la tabla del propietario:
ocho agentes, siete posiciones y 56 celdas. Las nueve copias desplegadas
(`claw`, los siete trabajadores y `entrega`) tienen el mismo SHA-256 que la
fuente; `entrega` hereda `main`.

El comando live predeterminado conservaba dos adaptadores historicos que ya no
ocupan slots (`opencode_zen` y `zai` HTTP) y no incluia `opencode_zai`, que es
la ruta real de GLM con herramientas. Se separo la matriz activa de los
adaptadores admitidos expresamente por `--only`: el default prueba ahora las
ocho rutas en uso, mientras los probes de migracion historicos siguen
disponibles de forma explicita.

Resultado live secuencial: **8/8 adaptadores activos listos y 15/15
combinaciones unicas adaptador/modelo listas**. Esto cubre las 56 celdas sin
repetir modelos identicos. El primer ciclo marco Grok como `auth`; `grok
models` devolvio inmediatamente exit 0, sesion y `grok-4.6`, y tanto el probe
aislado como la inferencia exhaustiva posterior pasaron. No se imprimieron
credenciales ni identidad de cuenta.

# Estado anterior — 2026-09-20

## Matriz live 2026-09-20 (sin OpenCode Zen)

Zen no esta en la tabla y no se sondeo. Probes oficiales uno por uno, marcador
`GONCLOUD_PROBE_OK`. 6/8 cuentas en uso listas. La cuarentena no se borra a
mano: un live listo la levanta; un fallo la arma una hora y el router avanza
de slot.

| Cuenta | Resultado | Motivo | Cuarentena |
|---|---|---|---|
| OpenCode Go | ready | — | no (se levanto) |
| Muse | ready | — | no |
| Z.AI | ready | — | no |
| Grok | ready | — | no |
| DeepSeek API | ready | — | no |
| Claude Max | quota | cupo de la suscripcion | si, 1 h |
| Kimi | quota | cupo semanal 7 dias; sesion OAuth vigente | si, 1 h |
| Codex | ready | aviso de skills ya no se lee como fallo | no |

OpenCode Zen queda fuera de la tabla; su salud vieja (`auth`) no se usa.

# Estado anterior — 2026-09-19

## Tabla de modelos 2026-09-19

Siete posiciones por agente. Fuente: `model-routing.source.json` (tabla del
propietario, 2026-09-19). Primary sigue siendo OpenCode Go; el prefijo
`opencode-go-resp` se ata a esa misma cuenta. F1-F6 se atan por familia,
incluyendo `kimi-for-coding` y `glm-5.3-flash`. `entrega` hereda `main`.
DeepSeek API queda en verifier F6 y scout F4; adversary ya no la tiene. Kimi
sigue en la tabla y en la cadena nativa del director: Go, luego Kimi, luego
Z.AI. OpenCode Zen sigue existiendo como adaptador/probe, pero no ocupa un
slot. La tabla y el catalogo quedaron copiados en los nueve perfiles; el
configurador no reescribio `claw` porque su cadena nativa de conversacion
esta personalizada (conflicto `model` / `fallback_providers`).

## Tabla anterior 2026-09-16

Siete posiciones por agente. La columna vacia que era OpenCode Zen se omitio.
Primary OpenCode Go; F1-F6 por familia. `entrega` hereda `main`. Kimi en la
tabla de agentes y en la cadena nativa del director. OpenCode Zen como
adaptador/probe, sin slot.

## Estado anterior: aceptacion local completa (2026-09-14)

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
- Plugin Doctor: OK, 7 herramientas (entonces; 9 desde el transporte local)
  y 1 hook. Este runtime ofrece
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

### Cadena bloqueada en slot 0 por el arbol sucio (`t_f1c90a69`, 2026-09-26)

La etapa `scout` quedo `blocked` con un solo intento, `opencode_go` slot 0,
`deepseek-v4.1-flash`, `task_failure`, y las cuatro cuentas sanas de su cadena
nunca se probaron. La cuenta no tuvo nada que ver: quedo `ready`, sin cuarentena
y sin cooldown. El disparador fue el arbol de trabajo con diez archivos sin
commitear. Tres defectos encadenados, cada uno con su regresion en rojo primero:

- **El arbol sucio se leia como continuacion.** `_absorb_workspace_state` ponia
  `mode="continue"` en el intento 0 porque `git status` no estaba limpio, y
  entonces `build_assignment_prompt` le decia al modelo que "un worker anterior
  ya muto este workspace antes de fallar" y le listaba los diez archivos del
  propietario como suyos. No hubo intento previo. Ahora solo un commit mas alla
  de la base, evidencia registrada o un intento previo hacen una continuacion.
  Regresion: `test_owner_uncommitted_edits_are_not_a_continuation`.
- **Esa suciedad previa contaba como mutacion del intento.** Con `mutated` en
  verdadero, `transport` e `invalid_terminal` bloquean por diseno, asi que de
  los ocho tipos de fallo solo cuatro podian avanzar antes de que corriera el
  primer modelo. El router ahora compara contra la suciedad presente al arrancar
  la etapa y contra su revision base, no contra el arbol limpio.
- **Salir con codigo 0 sin envelope valido se clasificaba `task_failure`.** Ese
  tipo nunca avanza de slot, de modo que un envelope mal formado fijaba la etapa
  al slot 0. `types.py` ya define `invalid_terminal` para exactamente ese caso y
  `test_a_rejected_envelope_advances_to_the_next_slot_when_nothing_changed` ya
  fijaba la conducta esperada. Regresion conjunta de los tres:
  `test_bad_envelope_at_slot_zero_advances_although_the_tree_started_dirty`.

Ademas, `_terminal_transform` reemplazaba el texto del adaptador por
`subscription stage failed`, y la fila durable quedaba sin poder distinguir un
envelope rechazado de una cuenta muerta. El texto del adaptador es sintetico,
se arma con la ruta y el tipo de fallo, y no lleva salida del proveedor, asi que
ahora se conserva. Regresion:
`test_a_failed_stage_keeps_the_adapter_failure_kind_as_evidence`.

No se cambio el flag `terminal` de un intento fallido. Marcarlo es deliberado y
`test_terminal_task_failure_survives_crash_without_reinvocation` lo fija: evita
re-invocar al modelo tras un fallo real de tarea, donde el trabajo pudo haberse
hecho. Con los arreglos de arriba la cadena agota sus slots dentro de la misma
corrida, asi que ese flag ya no sella una etapa que nunca hizo failover.

Queda un residual declarado: una etapa ya bloqueada no se puede reintentar
reusando el mismo `request_key`. `LocalOrchestrator.resume` devuelve `blocked`
en cuanto cualquier etapa lo esta, sin llegar al router, y el router reproduce
el intento registrado en vez de crear uno nuevo. Para reintentar hace falta un
`request_key` nuevo.

### Revision cruzada y por que el detector de mutacion quedo conservador

Dos rondas de revision externa (codex `gpt-5.6-sol`) sobre el arreglo del arbol
sucio. La primera hallo 2 defectos, uno alto. La segunda, sobre el arreglo de
esos dos, devolvio REQUEST_CHANGES con 5 mas, dos altos. El peor:

- **ALTA. Quitar el guard de historial reabrio el pisado de trabajo.** La puerta
  de continuacion dejo de contar la existencia de un intento previo. Un intento
  **interrumpido** que escribio archivos sin commitear y murio antes de
  registrarlos volvia entonces como `mode="start"` y `changed_files=()`, de modo
  que el siguiente modelo podia pisar trabajo real. Ese `or attempts` estaba ahi
  justo para eso. El revisor lo confirmo ejecutando ese camino.

Los otros cuatro apuntaban todos al mismo sitio: el digest de contenido que se
habia introducido para distinguir la suciedad del propietario de la del worker
seguia enlaces simbolicos, no veia cambios de permisos ni de submodulos, perdia
la atribucion de un archivo commiteado y vuelto a ensuciar, y releia todos los
archivos sucios sin tope de tamano ni de tiempo.

**Decision del propietario: borrar el digest en lugar de endurecerlo.** La regla
volvio a ser conservadora: cualquier archivo sucio cuenta como mutacion. Tres de
los cinco hallazgos desaparecieron porque el codigo que los causaba dejo de
existir, y el guard de intentos interrumpidos volvio a su lugar.

De todo el episodio sobrevive exactamente lo que rompio la cadena real:

- El intento 0 ya no recibe la historia falsa. Con el arbol sucio y sin ningun
  intento previo ni commit sobre la base, la etapa arranca en `mode="start"` y
  no se le listan al modelo los archivos del propietario como trabajo de otro.
- Salir con codigo 0 sin envelope terminal valido es `invalid_terminal`, no
  `task_failure`.
- El texto de fallo del adaptador se conserva en la fila durable.

**Consecuencia, dicha sin adornos: con el arbol sucio no hay failover
automatico.** La etapa bloquea con evidencia en el slot 0 y las cuentas sanas no
se prueban. Commitea o stashea antes de lanzar una cadena. Es la eleccion
deliberada: bloquear una etapa se recupera, pisar el trabajo de un worker no.

Residual declarado, no vuelto a revisar: para los slots 1 en adelante la regla
conservadora puede afirmar una continuacion que no ocurrio, porque una vez que un
intento se ejecuto el router no intenta distinguir quien ensucio que. Es el lado
seguro del error.

Nota de proceso, con dos hipotesis mias caidas antes de dar con la buena. El
`cross-review.ps1` del kit no pudo con el diff completo: **no** era el limite de
linea de comandos de Windows (el comando mide ~1400 caracteres, porque el prompt
referencia un archivo temporal) ni el sandbox de codex (lee ese archivo sin
problema, verificado). Era el tope de tiempo: 300 s por default contra los 60 000
caracteres de diff que el script esta dispuesto a mandar. Medido: 32 caracteres
tardan 35 s, y 48 981 no alcanzaron ni con 1200 s. Arreglado en el kit
(`88997fe`): el tope ahora escala con el tamano del diff y se anuncia.

### El primer failover real descubrio dos adaptadores sin manos (2026-09-26)

Con el arreglo del arbol sucio desplegado, la cadena `t_a1b513bd` hizo por fin lo
que debia: slot 0 (`opencode_go`/`deepseek-v4.1-flash`) contesto fuera del
contrato, se clasifico `invalid_terminal`, el arbol estaba limpio asi que no habia
mutacion, y el router **avanzo al slot 1**. Ahi `zai`/`glm-5.3-flash` respondio con
un envelope valido cuyo contenido era: no tengo acceso a archivos ni shell, no
puedo inspeccionar el repo, y por la regla de no invencion no reporto hallazgos.

El modelo se porto bien; el adaptador no le daba con que. `zai` y `deepseek` son
HTTP plano sin bucle de herramientas, y `zai` ocupaba el primer failover de scout,
verifier y operaciones. Antes del arreglo esto era invisible porque la cadena
nunca avanzaba de slot. La familia `glm-` se ato al CLI de OpenCode; el detalle y
la verificacion en vivo estan en [enrutador](model-routing.md).

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
corrida ya no reintenta; los cuatro tipos restantes vencen igual; y, desde la
revision adversarial de cierre (abajo), vencida y fallida con *cualquiera* de
los ocho tipos de fallo se re-arma con reloj nuevo, un solo intento real por
corrida en serie y sin volver a sondear en cada corrida.

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

### Varias cuentas de correo por himalaya

`google-workspace` atiende una sola cuenta de Google por perfil: la principal
del propietario (confirmada con `users.getProfile` de la API). Para las demas
el propietario pidio correo (solo correo) y se instalo `himalaya` v2.1.0
(`+imap +smtp +rustls-ring`) en `%LOCALAPPDATA%\Programs\himalaya`, en el PATH
de usuario. La configuracion vive en `%APPDATA%\himalaya\config.toml`, fuera de
Git, con contrasenas de aplicacion (16 caracteres; Gmail acepta los espacios
con que las muestra, aqui se guardan sin ellos). Sintaxis verificada para esa
version, distinta de la que trae la skill empaquetada: `backend = "imap"`,
`imap.server = "imaps://imap.gmail.com:993"`, `imap.sasl.plain.username`,
`imap.sasl.plain.password.raw`, y `message.send.backend = "smtp"` con
`smtp.server = "smtps://smtp.gmail.com:465"`; `imap.port` y `backend.type` no
parsean. Los comandos tambien cambiaron (`mailbox list`, `envelope list -s N`,
`message read ID`, `message compose --to --subject --body --send`; no existen
`folder list` ni `template send`), asi que la copia local de la skill en el
perfil `claw` lleva al inicio una seccion "En esta maquina" con las cuentas y
los comandos reales; el archivo versionado de la skill no se toca.

Aceptacion 2026-09-14: la primera cuenta secundaria de Gmail (`undying`, default
de himalaya) lista
carpetas y envio un correo de prueba a si misma (`Message successfully sent`);
la cuenta de iCloud tambien autentico (`imaps://imap.mail.me.com:993`, SMTP
`smtp://smtp.mail.me.com:587` con `smtp.starttls = true`) pero el propietario
la dejo fuera y su bloque quedo apartado en `icloud.apartada.toml` junto a la
configuracion. La segunda cuenta de Gmail (`david`) fallo primero con
`NO Invalid credentials`: la contrasena de aplicacion se habia generado desde
otra sesion y esa cuenta no tenia verificacion en dos pasos (Google no ofrece
contrasenas de aplicacion sin ella). Con la verificacion activada y una
contrasena generada en su propia sesion, autentico, listo carpetas (nombres en
espanol: `[Gmail]/Enviados`) y envio su correo de prueba. El
gateway se reinicio desde una shell con el PATH de usuario fresco y las tres
variables de certificados, porque el proceso anterior nacio antes de instalar
himalaya y no lo veia.

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

### Revision adversarial de cierre

Antes de cerrar, el propietario pidio una pasada adversarial sobre toda la rama
(63 commits desde el plan de recuperacion, mas el cutover de Telegram y los
seguimientos). El agente `adversary` corrio con la bateria completa (362/359/3,
verde) y sin tocar codigo, secretos ni proveedores. Veredicto inicial: BLOQUEA,
por 1 HIGH, 4 MEDIUM y 4 LOW. Todos quedaron corregidos en el commit de cierre,
cada uno con su regresion observada en rojo primero:

- **HIGH. La cuarentena vencida no se re-armaba para tres tipos de fallo.**
  `_record_health` mantenia la cuenta `ready` tras `invalid_terminal`,
  `task_failure` o `invalid_request` (el proveedor respondio), y el `CASE` de
  `save_health` solo reiniciaba `quarantined_at` cuando `ready` era falso. La
  fila quedaba en "vencida" para siempre: probe e invocacion real en cada
  corrida, sin techo, mientras el estado seguia diciendo "en cuarentena". El
  documento afirmaba lo contrario. Ahora `save_health` recibe `attempt_failed`
  desde cualquier intento real fallido y re-arma el reloj (y registra ese fallo
  como causa) aunque el router deje la cuenta `ready` para rutas no
  cuarentenadas. Regresion: `test_an_expired_quarantine_rearms_on_every_failure_kind`
  (los siete tipos restantes; `quota` ya tenia la suya).
- **MEDIUM. `goncloud_local_run` creaba la cadena antes de validar
  `stop_after`.** Un valor fuera del enum dejaba siete tarjetas huerfanas y la
  respuesta sin `workflow_id`. `check_stop_after` corre antes de `create`.
- **MEDIUM. La redaccion no tenia piso de longitud ni punto fijo.** Un valor de
  1-5 caracteres bajo una clave `*_TOKEN`/`*_API_KEY` reescribia las claves del
  JSON terminal y rompia el envelope; y `[redacted]` alimentaba la siguiente
  pasada (10k chars -> 370k en cuatro). Ahora se exigen 6 caracteres, nada que
  sea parte del marcador, y `redact_text` es idempotente.
- **MEDIUM. El unico hook fallaba abierto.** Sin `task_id` resoluble devolvia
  `None` (permitir) y ninguna prueba lo ejercitaba. Para el perfil `reviewer`
  ahora bloquea si no puede resolver el id (`args`, `HERMES_KANBAN_TASK` o el
  `task_id` del host, en ese orden); otros roles siguen fuera de la politica.
  Dos pruebas nuevas cubren el hook registrado.
- **MEDIUM. Patrones de redaccion incompletos y divergentes.** Slack `xox*-`,
  Google `AIza`, GitLab `glpat-`, AWS `AKIA`, `Authorization: Basic`, tokens
  en query string; y la copia del orquestador (la que llega a Telegram) era mas
  debil que la del router (dejaba pasar `ghp_` y `xai-`). Hay una sola fuente
  (`redaction.SENSITIVE_TEXT`) y una prueba exige que ambas fronteras la
  compartan. Residual declarado: no se redactan runs hexadecimales (32/64)
  porque la evidencia lleva revisiones de git; una clave hex desnuda que un
  worker copie de un archivo del repo no se atrapa por forma.
- **LOW.** El archivo temporal del prompt (`grok`, `kimi`) se crea con
  `open_private_file` (DACL solo-propietario / 0600) en vez de `mkstemp`;
  `_recover_result` convierte una autorizacion revocada en un `invalid_request`
  sanitizado en vez de una excepcion que bloquea sin evidencia; `create` deriva
  la revision de la ruta expandida (una ruta `~/...` daba "revision is
  required"); y `workflow_id` se valida por forma antes de entrar en argv o en
  la ruta del resultado.

Se corrigieron ademas dos cifras desfasadas (README: 347 tests; arriba: 7
herramientas). Lo que el adversary ataco y resistio: idempotencia y concurrencia
del router (dos corridas o dos hilos, una invocacion), la frontera de procesos
en Windows, prompts fuera de argv, `private_files.py`, `configure_team.py`, el
parser del envelope y cero secretos en el historial de Git.

**Segunda ronda** (permitida solo porque la primera hallo un HIGH; no hay
tercera): acotada al diff de las correcciones, veredicto NO BLOQUEA. Verifico
que ninguna correccion regresa nada (una cuenta sana que falla una vez sigue
`ready` y sin cuarentena; el hook no bloquea ningun flujo legitimo del host,
que siempre manda `task_id` y cuya herramienta nativa ya fallaba sin el; los
ids reales `t_`+8 hex pasan la validacion) y revirtio cada fix en memoria para
confirmar que su regresion se pone en rojo. Encontro 2 MEDIUM y 6 LOW. Se
corrigieron en el commit siguiente, con su prueba en rojo primero: el piso de
seis caracteres y la guarda del marcador faltaban en `_sanitize` (la frontera
que llega al director y a Telegram; una instruccion de dos letras rompia un
envelope valido), `AKIA` y `Basic` ahora distinguen mayusculas (la prosa
`basic authentication...` ya no se redacta), y un re-armado sin tipo de fallo
conserva la causa registrada en vez de dejarla en `NULL`. Residuales
declarados, sin tercera ronda:

- "Un solo intento real por hora" vale en serie: el re-armado ocurre despues
  de la invocacion, fuera del bloqueo de despacho, asi que N cadenas que
  lleguen a la vez a la misma cuenta con cuarentena vencida pueden gastar
  hasta N intentos reales antes de que la primera la re-arme. El tablero
  corre una cadena por repositorio, de modo que N es pequeno.
- Falsos positivos por forma, inofensivos porque el marcador conserva el
  tamano: `?key=`/`?apikey=` en URLs de documentacion, `glpat-style`,
  `xoxa-` (que si es un prefijo real de Slack) y `Basic` seguido de una palabra
  de 16+ caracteres.
- (Resuelto despues de la ronda: la redaccion por regex era cuadratica en el
  peor caso por el patron de correos sin tope; 1 MB en una sola linea tardaba
  ~20 min con el patron viejo y el nuevo por igual, mas que el lease de etapa
  de 600 s, y perder el lease saltaba el registro de salud que re-arma la
  cuarentena. Las clases abiertas quedaron acotadas a los maximos del RFC y
  una regresion en subproceso exige que 200 KB adversarios terminen en menos
  de 10 s.)
- `open_private_file` en un `%TEMP%` sobre ruta UNC no esta verificado; el
  transporte por archivo (`grok`, `kimi`) fallaria ahi de forma explicita.
- El hook bloquea al reviewer sin id resoluble; el host lo manda siempre, de
  modo que el caso solo aparece con un host distinto.

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
