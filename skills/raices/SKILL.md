# Raíces del sistema de agentes (claw)

Cómo administrar las raíces de trabajo durable (`sistema-agentes.v1`) con las
herramientas `goncloud_root_*`. Todo es solo para ti (claw); ningún otro
perfil las tiene.

## Cuándo abrir una raíz

Cuando David te pida un objetivo durable que merece plan, criterios y
verificación, ábrela con `goncloud_root_submit`. El plan (`meta.v1`) lleva:
unidades (`unitId`, `title`, `dependsOn`, `criteria` con su `criterionId` y
`verifier`, `scope` con repos y modo), `budget`, la `authority` que David
concedió y `qualityPolicy` (la vigente es `quality-kit.regla4`).

## Recién abierta

En el MISMO turno, por cada unidad sin dependencias (`dependsOn` vacío):

1. Escribe su brief con `goncloud_root_instruction_put` (`{text: ...}`) y
   guarda el `{ref, digest}` que devuelve.
2. Entrega la asignación con `goncloud_root_reconcile`:
   `{rootId, asignaciones: [{unitId, assignment, relay: false}]}`.

El `assignment` lleva: `target` `{kind: "cli", hostId: "gon-pc",
adapterId: <perfil>}`, `instructionRef` `{ref, digest}` (el del brief),
`resultContract: "review.v1"`, `inputRevision` con el sha REAL del head
(`git ls-remote` o `gh`; nunca inventado), `continuation`
`{kind: "requester"}`.

## Al despertar por despertar-raices

El cron te avisa en el chat qué raíz necesita qué. Léela con
`goncloud_root_read` y contesta CADA necesidad:

- `assignment` con razón `first`: perfil por modo y carril — `implement` →
  `implementer`, `read` → `scout`, `design` → `ingenieria`. `target`
  `{kind: "cli", hostId: "gon-pc", adapterId: <perfil>}`, `resultContract:
  "review.v1"`, `inputRevision` con el sha REAL del head (`git ls-remote` o
  `gh`; nunca inventado), `continuation` `{kind: "requester"}`, y el brief
  (instruction_put) con las instrucciones.
- `assignment` con razón `correction`: mismo carril, el brief lleva los
  `findingsRefs` de la necesidad.
- `assignment` con razón `relay`: OTRO perfil del mismo carril que no sea el
  autor previo, con `relay: true` en el hecho.
- `verify` (unidad, intento, criterios): comprueba el criterio de verdad
  (CI del head, prueba corrida, revisión del verifier). Sube la evidencia
  con `goncloud_root_instruction_put` y trae el claim por reconcile en
  `claims`: `[{unitId, attempt, criterionId, artifactRef: {ref, digest},
  environment: {host, versions}, procedure, outcome, at, expiresAt,
  role: "verifier"|"ci"}]`. Si NO puedes comprobarlo, `outcome: "unknown"`,
  nunca `passed`.

## Lo que claw nunca hace

- Inventar SHAs ni digestos: si no puedes ver el head real, no lo escribas.
- Marcar `passed` sin evidencia subida y comprobada.
- Reabrir una raíz cancelada.
- Contestar excepciones por David: las excepciones (`excepcion.v1`) son del
  dueño; tú solo las lees con `goncloud_root_read` y esperas su respuesta.
