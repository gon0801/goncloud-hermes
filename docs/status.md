# Estado — 2026-09-14

Base local implementada y operativa; enrutador de suscripciones por slot integrado, verificado con inferencia real por adaptador y cadena Telegram en curso.

## Enrutador de suscripciones (2026-09-14)

- Nueve adaptadores (OpenCode Go/Zen, Muse, Claude Max, ChatGPT/Codex, Kimi, Z.AI Coding Plan, Grok, DeepSeek API) con prompts por stdin/archivo temporal privado, `shell=False`, salida redactada y acotada, y sandbox conservado por CLI.
- Matriz live (inferencia real minima por adaptador, sin identificadores de cuenta): PASS `opencode_go` (READY), `muse` (READY), `claude` (LISTO), `codex` (READY), `zai` (READY), `grok` (READY), `deepseek` (READY, unica ruta `api`). FAIL `kimi`: `provider.connection_error` del endpoint tras 3 reintentos (el coordinador nativo tambien ve 401 en kimi-coding); el router le pasa de largo. CUARENA `opencode_zen`: credencial presente pero el CLI cuelga en silencio >2.5h; el coordinador nativo reporta `FreeUsageLimitError` 429 en opencode-zen — pendiente de saldo/limite de la capa free, no de autenticacion.
- Fallback comprobado en vivo: Go(auth) -> Zen(quota) -> F2 Claude Max con inferencia real ("READY"); continuacion tras mutacion con revision y diff registrados ("LISTO"). 65 pruebas unitarias del enrutador; idempotencia de etapa y continuacion nunca repeticion a ciegas.
- Credenciales GLM/DeepSeek migradas de lanzadores en texto plano (dos fases: probe real antes de reescribir; backups `.goncloud-backup` fuera de Git) al env privado `%LOCALAPPDATA%\hermes\subscription-router.env`. Ningun token OAuth copiado entre aplicaciones; `auth.json` de OpenCode jamas leido por el plugin.
- Bateria final: **96/96 pruebas, exit 0** con el venv de Hermes. Pre-commit completo: 8/8 candados. `git diff --check` limpio.
- Plugin desplegado a los nueve perfiles (idempotente): 7 herramientas y 1 hook; Plugin Doctor OK. Gateway reiniciado y saludable (estado `running`).

## Instalado y comprobado

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
- Inicio automatico del gateway instalado en la carpeta Startup de Windows; proceso activo y estado profundo `running`.
- Bot nuevo de Telegram conectado en modo polling y restringido a la lista privada del propietario. Mensaje entrante, inferencia y respuesta saliente comprobados.

## Operacion real

- El usuario ya proporciono su tabla actual de ocho posiciones por agente: `model-routing.source.json`. Esta aplicada; las vinculaciones y limites comprobados estan en `docs/model-routing.md`.
- WSL 2.7.14, Ubuntu 26.04 y Muse Code CLI 1.2.1 instalados. Login e inferencia real comprobados sin copiar secretos al repositorio ni convertir la suscripcion en API.
- Canal del director: bot nuevo de Telegram creado con BotFather; token privado y lista de usuarios autorizados guardados fuera del repositorio.
- El gateway recibio `Prueba Hermes`, resolvio el proveedor de respaldo y envio respuesta. La conversacion bidireccional fue confirmada por el usuario.
- Conectar fuentes comerciales bajo una autorizacion futura sigue fuera de este alcance; no se copiaron tareas recurrentes ni operaciones de produccion.
- Conectar fuentes comerciales bajo autorizacion explicita. No hay tareas recurrentes ni operaciones de produccion activas.

## Limites y reproducibilidad

- Se fijo el codigo upstream, pero el instalador oficial recurrio a resolucion de dependencias al fallar su lock; no es un entorno hermetico.
- El instalador produjo un cambio en `package-lock.json` del runtime. No se modifico su codigo fuente; no se oculta ese cambio generado.
- El instalador aviso de fallo en dependencias Node/TUI. Existen dependencias Node, pero la TUI grafica no esta compilada ni verificada. El lanzador usa la consola Python `--cli`. Computer-use se omitio expresamente.
- Contratos, grafo y hook controlan el flujo normal; no son aislamiento del sistema operativo. Las herramientas nativas/terminal y edicion directa pueden eludir politica de aplicacion.
- La exclusion de trabajos simultaneos sobre un repositorio opera en el tablero actual. No apuntes tableros distintos al mismo directorio de trabajo. Archivar manualmente un director activo tambien elude esa exclusion.
- La calidad de evidencia, las reglas comerciales y la revision exacta producida se exigen en los handoffs, pero no quedan demostradas solo por aprobar pruebas del coordinador.
- Repositorio nuevo, sin remoto/CI/PR. La bateria completa de este proyecto se ejecuta localmente una vez al final; no equivale a ejecutar toda la bateria upstream de Hermes.
- Durante pruebas focalizadas del plugin hubo una incidencia transitoria de limpieza del directorio temporal de Windows; no se reprodujo en las repeticiones del worker ni en la bateria final. No se oculto ni deshabilito ninguna prueba.
