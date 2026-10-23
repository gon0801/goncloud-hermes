# Datos reales del vigía de tareas

Capturados el 2026-10-22, solo lectura. Los jueces de `scripts/vigila_tareas.py` se prueban con estos archivos.

| Archivo | Máquina | De dónde sale |
| --- | --- | --- |
| `mac-launchd.json` | Mac | un registro por LaunchAgent de `~/Library/LaunchAgents` con label `ai.goncloud.`, `cc.goncloud.`, `com.goncloud.`, `claw-` o `ai.openclaw.`, más `launchctl print gui/<uid>/<label>` |
| `mini-launchd.json` | Mini | lo mismo, leído por ssh en la Mini |
| `mini-cron.json` | Mini | `openclaw gateway call cron.list` con `includeDisabled`, todas las páginas (`hasMore` y `nextOffset`); mismo contenido que `openclaw cron list --all --json`, que no acepta `--offset` |
| `mini-gateway-memoria.txt` | Mini | las líneas `memory pressure` de `~/Library/Logs/openclaw/gateway.log` del 21 y 22 de oct, cortadas después de `rss=<valor> <unidad>` |
| `mini-chrome-perfil.json` | Mini | `du -sm ~/.openclaw/browser/claw/user-data` |
| `pc-hermes-claw-jobs.json` | PC | `cron/jobs.json` de cada perfil de `C:\Users\ehven\AppData\Local\hermes\profiles\`; solo `claw` tiene trabajos |
| `pc-claw-salidas/<trabajo>.md` | PC | la última salida de `cron/output/<id>/` de cada trabajo; `monday-pedidos-semanal.md` termina en `FALLO:` aunque su `last_status` es `ok` |

Campos de los `*-launchd.json`: `label`, `periodico`, `keepalive`, `cargado`, `state`, `runs`, `last_exit_code` (`null` si dice "never exited") y `rutas_que_faltan` (rutas absolutas de `ProgramArguments` que no existen; los valores con `:` son listas de PATH, no rutas).

## Qué se quitó

- `payload.message`, `payload.argv`, `payload.toolsAllow` y cualquier `prompt`, `env`, `token`, `to`, `accountId`, `sessionKey` y `configRevision` de los trabajos de la Mini.
- De `mini-gateway-memoria.txt`: todo lo que sigue al valor de `rss` en cada línea.
- `deliveryPreviews` de la Mini (traían el chat de Telegram).
- De los trabajos de la PC: `prompt`, `skills`, `origin`, `base_url` y `last_dispatch`.
- De las salidas de la PC: el prompt completo (queda `[prompt quitado]`; la respuesta se conserva), los correos (`<correo>`), el id del chat (`<chat>`) y el nombre de usuario de Windows en las rutas.
