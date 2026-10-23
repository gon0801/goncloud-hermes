# vigias-01 D.2: parches de claw en la Mini (2026-10-22, hora de la Mini)

Los parches se corrieron desde una copia de `scripts/` en `/tmp/vg` de la Mini, con `/usr/bin/python3` y `--openclaw /Users/gon/.openclaw/bin/openclaw`.

## claw_parche.py dreaming

```
dreaming: aplicado; respaldo en /Users/gon/.openclaw/respaldos-parche/dreaming-20261022-211249.pre.json
```

## claw_parche.py sesiones

```
sesiones: la config ya esta asi; no escribi nada
```

## claw_parche.py defaults-v6

No dijo que no hay nada que cambiar, como esperaba el brief. Esperó una ventana libre del gateway (más de 7 minutos, sondea cada 5) para escribir esto sobre la config viva (`merge_patch` calculado en solo lectura):

```
{'agents': {'defaults': {'model': {'fallbacks': ['opencode-go/deepseek-v4.1-flash', 'zai/glm-5.3-flash', 'xai/grok-4.7']}}}}
```

La config viva tiene `opencode-go-2/deepseek-v4.1-flash` en ese lugar (la segunda cuenta de OpenCode Go, la que usan los demás agentes). La tabla (`desired_agent`) solo agrega el sufijo `-2` cuando la fila repite el modelo de la posición 0, y la fila `default` no lo hace. Aplicarlo cambiaba el respaldo de claw a otra cuenta, así que se cortó el proceso (SIGTERM, rc=143) antes de escribir nada. La config de `agents.defaults.model` queda como estaba:

```
{'primary': 'anthropic/claude-haiku-5-5', 'fallbacks': ['opencode-go-2/deepseek-v4.1-flash', 'zai/glm-5.3-flash', 'xai/grok-4.7']}
```

Pendiente de decisión de una persona: si la fila `default` debe llevar `-2` (corregir la tabla o `claw-routing.json`) o si la cuenta 1 es la correcta (cambiar la config viva).

## Revisión semanal de skills de main

`openclaw cron edit 8c26f371-f132-41af-a772-b3f13641c3ad --cron "30 5 * * 2" --tz America/Los_Angeles --exact` no se dejó editar:

```
Error: system-owned monitor jobs cannot be edited by cron clients
```

Por eso se agregó a `tareas-programadas.json` como `excepciones_ventana` con su motivo. `openclaw cron list --all --json`, solo esa tarea y sin el prompt:

```json
{
  "id": "8c26f371-f132-41af-a772-b3f13641c3ad",
  "declarationKey": "skill-collection-review:main",
  "displayName": "Skill collection review (main)",
  "agentId": "main",
  "name": "skill-collection-review-main",
  "enabled": true,
  "createdAtMs": 1790142912668,
  "schedule": {
    "kind": "every",
    "everyMs": 604800000,
    "anchorMs": 471276323
  },
  "sessionTarget": "isolated",
  "wakeMode": "next-heartbeat",
  "delivery": {
    "mode": "none"
  },
  "scheduledToolPolicy": {
    "version": 1,
    "mode": "trusted"
  },
  "state": {
    "nextRunAtMs": 1793098476547,
    "lastRunAtMs": 1792493676547,
    "lastRunStatus": "error",
    "lastStatus": "error",
    "lastDurationMs": 335158,
    "lastDeliveryStatus": "not-requested",
    "lastFailureNotificationDeliveryStatus": "not-requested",
    "consecutiveErrors": 1,
    "consecutiveSkipped": 0,
    "lastError": "Gateway restarting.",
    "lastDiagnostics": {
      "summary": "Gateway restarting.",
      "entries": [
        {
          "ts": 1792494011700,
          "source": "cron-setup",
          "severity": "error",
          "message": "Gateway restarting."
        }
      ]
    },
    "lastDiagnosticSummary": "Gateway restarting."
  },
  "updatedAtMs": 1792494011705,
  "configRevision": "sha256:a8xE1cXTQ8PPIc-RE32wqh9CFhzK7URPTkVqrR-oW0I",
  "nextRunAtMs": 1793098476547,
  "lastRunAtMs": 1792493676547,
  "lastRunStatus": "error",
  "lastRunError": "Gateway restarting.",
  "lastDeliveryStatus": "not-requested",
  "lastFailureNotificationDeliveryStatus": "not-requested",
  "effectiveAgentId": "main",
  "status": "error"
}
```
