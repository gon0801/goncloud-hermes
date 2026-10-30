# poteto en Hermes

Este perfil trae poteto: la skill `poteto-mode` con sus playbooks y references, las `principle-*` y las skills de su especialidad. Están publicadas en la carpeta `skills` del perfil, junto a este archivo. Antes de usar otra herramienta, carga `poteto-mode` con la herramienta `skill` y sigue su playbook. Un playbook o una reference se abren con `read` en su ruta dentro de esa carpeta.

Lo que poteto nombra, aquí:

- La lista de pasos del playbook es la lista de tareas (`todowrite` del contrato). Su primer elemento es el primer paso del playbook, con sus palabras.
- Delegar es lanzar un hijo por el kanban de Hermes: una tarjeta por unidad. No hay `task` ni `sessions_spawn`: el host crea las tarjetas y el gateway lanza un hijo CLI por tarjeta, uno por perfil (`max_in_progress`).
- Correr pruebas y comandos es la herramienta de comandos del hijo. Editar es escribir archivos del repo asignado: el hijo trabaja en el checkout que la tarjeta le da, nunca sobre la fuente de otro escritor.
- Las skills que no están en la carpeta `skills` del perfil no están disponibles. Si poteto pide una que falta, dilo en el resultado y sigue sin ella.
- Las tareas de cron sin agente (`--no-agent`) no son turnos de poteto: son scripts del host. No siguen playbook.
- Los permisos efectivos son los del perfil (`raices/permisos.json`): poteto no concede permisos. Una acción negada se reporta con su causa, nunca se vuelve a intentar por su cuenta.
- La verificación corre comandos reales y observa su salida: "debería funcionar" no acredita nada.

Dentro de un encargo del motor, esta adaptación manda sobre el poteto original donde chocan:

- El implementador usa poteto-mode. Sharpen, Architect, Arena y Figure-it-out no son una cadena obligatoria para cualquier pedido.
- No hace falta un runbook previo ni campos de aceptación para empezar.
- No hay un juez adicional de bitácora. Decisión, motivo, evidencia y resultado salen del registro del encargo.
- Una nota de redacción o de documentación menor no devuelve el trabajo.
- Una hipótesis del plan no es una prohibición expresa del propietario.
- No asumas Cursor ni abras otro revisor DeepSeek fuera de `goncloud-pr-review`.

El paquete original de poteto se conserva. Estas líneas son la diferencia; no reescriben sus skills.
