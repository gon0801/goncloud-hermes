# Salud de los eslabones — 2026-10-12

Fase 0B de `docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md`: un
veredicto por cada eslabon distinto de las cadenas, en hermes (PC) y en claw
(Mac Mini). Evidencia pasiva primero (registros); sonda real solo donde no la
habia. Ninguna sonda paso por el router de suscripciones ni cambio
configuracion.

## Como se midio

- **hermes, trabajadores:** `adapter.invoke()` directo con el mismo pedido que
  `--live-probe-all` (marcador `GONCLOUD_PROBE_OK`, `max_steps=1`), sin
  `SubscriptionRouter` ni `SubscriptionStateStore`. 16 llamadas, una por ruta,
  sin reintentos, 177 s en total. `subscription-router.db` no cambio (mismo
  tamano, mtime y filas antes y despues).
- **hermes, director:** solo su `agent.log` del 05 al 12 de octubre. No se le
  corrio un turno: cargaria su rol y sus herramientas y podria actuar.
- **claw:** registros del gateway (solo existen los del 11 y 12 de octubre,
  unas 37 h) y `openclaw models status`. Sonda:
  `openclaw infer model run --gateway --agent <verifier|reviewer> --model <p/m> --prompt "Reply with exactly: PROBE_OK" --json`
  (sesion nueva por llamada, sin herramientas, sin fallbacks). 5 llamadas, 4
  validas. Ningun perfil entro en cooldown por ellas.

## hermes: rutas de los trabajadores

| Ruta (adaptador, modelo, esfuerzo) | Veredicto | Evidencia | Causa |
|---|---|---|---|
| opencode_go, muse-spark-1.3-contributor | vivo | sonda OK 4.5 s | |
| opencode_go_2, muse-spark-1.3-contributor | vivo (indirecto) | sin sonda propia; misma llave OK con deepseek y el director la uso hoy 12:21 | |
| opencode_go, deepseek-v4.1-flash | vivo | sonda OK 4.6 s | |
| opencode_go_2, deepseek-v4.1-flash | vivo | sonda OK 6.1 s | |
| opencode_zai, glm-5.3 | vivo | sonda OK 6.2 s | |
| opencode_zai, glm-5.3-flash | vivo | sonda OK 8.1 s | |
| kimi, k3 | vivo | sonda OK 13.0 s | |
| kimi, kimi-for-coding (high y sin esfuerzo) | vivo | sonda OK 9.9 s; el adaptador no pasa el esfuerzo, es la misma llamada | |
| deepseek, deepseek-flash | vivo | HTTP 200, 2.4 s | |
| muse, muse-spark-1.3, max | vivo | sonda OK 65.7 s (incluye arranque de WSL); el CLI repite `muse-spark-1.3` | los 4 `model_unavailable` del 09-28 no se reproducen |
| claude, claude-opus-5, xhigh | muerto | sonda 21.6 s, `auth` | "OAuth session expired and could not be refreshed" |
| claude, claude-sonnet-5, xhigh | muerto | sonda 8.2 s, `auth` | igual |
| claude, claude-fable-5-1 | muerto | sonda 8.5 s, `auth` | igual |
| grok, grok-4.6 | muerto | sonda 0.5 s, `auth` | "Not signed in" |
| codex, gpt-5.6-terra, xhigh | muerto | sonda 4.8 s, `quota` | limite de uso hasta 2026-11-07 19:06 |
| codex, gpt-5.6-sol, xhigh | muerto | sonda 9.1 s, `invalid_request` | "model is not supported when using Codex with a ChatGPT account" |
| codex, gpt-6-astra | muerto | sonda 4.0 s, `invalid_request` | igual |

El probe barato del adaptador `claude` (`claude auth status`) sigue diciendo
que hay sesion, asi que el router lo daria por listo; solo la llamada real lo
detecta.

## hermes: director (`claw`)

| Proveedor / modelo | Veredicto | Evidencia |
|---|---|---|
| opencode-go / muse-spark-1.3-contributor | vivo | 9,075 llamadas en 7 dias; ultimo turno OK 10-12 12:21. La cuenta 1 dio 403 siete veces (10-11 y 10-12) y roto a la cuenta 2. El 10-07 y 10-08 las dos cuentas dieron 429 |
| anthropic / claude-opus-5 | vivo | 221 llamadas como fallback, ultima OK 10-08 06:50, 0 fallos |
| meta-ai / muse-spark-1.3 | sin evidencia | ultima llamada 09-28 |
| xai-oauth / grok-4.6 | sin evidencia | ultima llamada 09-28 |
| zai / glm-5.3 | sin evidencia | ultima llamada 09-28 |
| openai-codex / gpt-5.6-sol | sin evidencia | ultima llamada 09-28 |
| kimi-coding / k3 | sin evidencia | ultima llamada 09-28 |

En 7 dias el fallback nunca paso de Claude: las cinco posiciones siguientes no
se han ejercitado desde el 09-28.

## claw: eslabones de los 19 agentes

| Eslabon | Agentes que lo usan (posicion) | Veredicto | Evidencia | Causa |
|---|---|---|---|---|
| opencode-go/deepseek-v4.1-flash | 16 (0) | vivo | 156 respuestas 200, ultima 10-12 10:39 | |
| opencode-go-resp/muse-spark-1.3-contributor | 3 (0) | vivo | 2,352 respuestas 200, ultima 10-12 13:23; 8 veces 400 `format` solo en el trabajo de las 03:00 | |
| opencode-go-2/deepseek-v4.1-flash | 16 (1) | vivo | sonda OK | |
| opencode-go-resp-2/muse-spark-1.3-contributor | 3 (1) | sin evidencia | solo aparece en el trabajo de las 03:00, donde tambien falla su gemelo vivo | |
| anthropic/claude-opus-5 | 3 (2-6) | vivo (visto solo en `main`) | 4 exitos por `claude-cli` a las 03:00 del 11 y 12 | |
| anthropic/claude-sonnet-5 | 15 (4-6) | sin evidencia | no alcanzable con la sonda; `models status` da `claude-cli` como `missing` en 18 agentes | |
| anthropic/claude-fable-5-1 | 1 (2) | sin evidencia | la sonda no pasa por `claude-cli` | |
| xai/grok-4.6 | 17 (2-7) | vivo | 1 respuesta 200, 10-12 05:48 | |
| zai/glm-5.3-flash | 15 (2-3) | vivo | 200 a las 03:00 ambos dias | |
| zai/glm-5.3 | 4 (4-7) | vivo | sonda OK | |
| kimi/k3 | 4 (3-7) | vivo | 200 a las 03:00 ambos dias | |
| kimi/kimi-for-coding | 15 (4-7) | vivo | sonda OK | |
| deepseek/deepseek-flash | 2 (5-7) | vivo | sonda OK | |
| meta/muse-spark-1.3 | 19 (2-7) | muerto | 401 el 11 y 12 a las 03:00 | credencial |
| openai/gpt-5.6-sol | 3 (2-7) | muerto | 404 el 11 y 12 | modelo inexistente para la cuenta + cooldown |
| openai/gpt-5.6-terra | 15 (3-7) | muerto | sin llamadas; `missing-auth` en los 15 agentes | credencial + cooldown |
| openai/gpt-6-astra | 1 (4) | muerto | sin llamadas | cooldown de `openai:default` hasta 2026-11-08 03:06 UTC |

## Que hace falta para revivir cada uno

| Que | Donde | Accion | Quien |
|---|---|---|---|
| Claude CLI (3 rutas de trabajadores) | PC | `claude auth login` | propietario, en la PC |
| Grok CLI | PC | `grok login --device-code` | propietario, en la PC |
| OpenAI / Codex (3 modelos, en los dos sistemas) | PC y Mac Mini | La cuenta de ChatGPT responde como plan gratuito: `sol` y `astra` no existen para ella y `terra` esta en limite hasta el 7 de noviembre. Entrar con la cuenta de pago (`codex login`) o decidir que modelo ocupa esas posiciones | propietario |
| `meta/muse-spark-1.3` | Mac Mini | llave nueva o `openclaw models auth login --provider meta`; su perfil ya traia un cooldown `auth` desde el 09-28 | propietario |

Suposiciones sin verificar: que la suscripcion de ChatGPT este vencida o sea la
cuenta equivocada; que la llave de Meta este revocada; que `gpt-6-sol` o
`gpt-6-luna` sean los sucesores utiles.

## Lo que quedo sin probar

- En claw, `anthropic/claude-sonnet-5` (respaldo en 15 agentes) y
  `anthropic/claude-fable-5-1`: solo se prueban con un turno completo de agente
  (`openclaw agent --session-key smoke:<id> --model ...`).
- En claw, Muse por la segunda cuenta de Go.
- En el director de hermes, las cinco posiciones despues de Claude.
- Efectos de las sondas en la PC: los hooks de inicio y cierre de sesion de
  Claude Code corrieron en sus tres lanzamientos; Codex reescribio su
  `models_cache.json`. No hubo autoactualizaciones (versiones iguales antes y
  despues). En claw, una sonda invalida dejo 1 WARN y 2 ERROR en el registro
  del gateway a las 13:32:47.
