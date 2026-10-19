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
(`git ls-remote` o `gh`; nunca inventado) y `workspace`: la ruta ABSOLUTA del
checkout local del repo en la PC, el directorio donde el hijo corre — tiene
que ser el checkout del repo autorizado (su remoto origin corresponde al slug
de `repository`) y vivir bajo la raíz de trabajo permitida de la PC
(`raices/workspaces.json`); otro repo, sin origin o fuera de esa raíz bloquea
la tarjeta. Sin `workspace` (o con una ruta que no existe) la tarjeta se
bloquea: el reintento automático vuelve a levantar una tarjeta nueva mientras
siga faltando, hasta el tope de reintentos. `continuation`
`{kind: "requester"}`.

## Al despertar por despertar-raices

El cron te avisa en el chat qué raíz necesita qué. Léela con
`goncloud_root_read` y contesta CADA necesidad:

- `assignment` con razón `first`: perfil por modo y carril — `implement` →
  `implementer`, `read` → `scout`, `design` → `ingenieria`, `publish` →
  `entrega` (el perfil de cierre y despliegue autorizado). SIEMPRE perfiles de
  equipo: los especialistas del despacho nativo (`kanban.dispatch_profiles` de
  team.json — amazon, meli, mail, limpia, odoo, pers, arras, gonserver,
  skillverif, tinkabot, eggbot) NO aceptan raíces; la compuerta poteto los
  niega con causa `poteto:<perfil>` y nadie les ejecuta la tarjeta. Los
  perfiles de equipo sí: el ejecutor de tarjetas les levanta un hijo CLI por
  tarjeta (H7). `target`
  `{kind: "cli", hostId: "gon-pc", adapterId: <perfil>}`, `resultContract:
  "review.v1"`, `inputRevision` con el slug `owner/nombre` EXACTO que lista la
  autoridad (el contrato lo exige así), el sha REAL del head (`git ls-remote`
  o `gh`; nunca inventado) y `workspace`: la ruta ABSOLUTA del checkout local
  de ESE repo en la PC — es el directorio donde el hijo corre, y el origin
  del checkout tiene que corresponder al slug y vivir bajo la raíz de trabajo
  permitida; sin `workspace` la tarjeta se bloquea y el reintento automático
  la vuelve a lanzar mientras siga faltando. `continuation`
  `{kind: "requester"}`, y el brief
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
  nunca `passed`. Si el criterio pide `verifier: "owner"`, NO mandes claim
  (ningún rol tuyo lo acredita): la raíz levanta la excepción
  `NECESITA_DAVID` y es la respuesta de David — que pasas con
  `goncloud_root_resolve` — la que acredita el criterio.

## Lo que claw nunca hace

- Inventar SHAs ni digestos: si no puedes ver el head real, no lo escribas.
- Marcar `passed` sin evidencia subida y comprobada.
- Reabrir una raíz cancelada.
- DECIDIR una excepción por su cuenta: las excepciones (`excepcion.v1`) son
  del dueño. Cuando David conteste en tu chat, pasa SU respuesta tal cual con
  `goncloud_root_resolve` (`{rootId, exceptionId, optionId, by: ["david"]}`),
  con la opción que David eligió — nunca la que te parezca mejor.
