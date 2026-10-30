---
name: motor
description: Cómo repartir el trabajo que pide David. Una tarea acotada la hacen tus agentes. El motor continúa el trabajo durable sin la conversación, coordina máquinas o sostiene entregas de varias etapas. La vía que David nombra se respeta. Misma skill para claw (OpenClaw, Mini) y Hermi (Hermes, PC).
---

# Repartir el pedido

David manda texto o archivos. Eso es el pedido. No lo conviertas en bloques ejecutables, campos de aceptación ni un plan que haya que aprobar antes de empezar.

## Qué vía usar

- **Directa.** Si el pedido cabe en tus agentes y David no pidió continuidad, hazlo con los agentes del director. Una tarea acotada directa está permitida aunque cambie archivos.
- **Durable.** Si el trabajo debe continuar sin esta conversación, coordinar varias máquinas o entregarse en varias etapas, encárgalo al motor.
- **Expresa.** Si David nombró la vía, usa esa. No la sustituyas por tu criterio.

**Antes de implementar, ¿David pidió implementar?** Si te dio un pedido de cambio o un plan, encárgalo directo. Si solo pidió investigar y de ahí salió que hay algo que cambiar, NO lo encargues: cuéntale en dos o tres frases qué encontraste y qué cambiarías, y pregúntale "¿quieres que lo implemente?". Solo con su sí lo encargas.

Un pedido mixto se parte: lo de leer lo haces ya con tus agentes; lo de cambiar sigue la vía que corresponda de las de arriba.

## Un plan externo es contenido del pedido

Si David trae un plan hecho fuera, ese plan es el texto del pedido. Entrégalo tal cual al motor como texto o archivo. No lo reescribas en bloques ejecutables ni le exijas validación previa ni una palabra de aceptación para empezar.

```sh
python3 -B -m motor director encargar \
  --via durable \
  --director hermes \
  --canal <canal-de-origen> \
  --clave "<id-del-mensaje>" \
  --sesion "<id-de-la-conversacion>" \
  --base <base-del-motor> \
  --archivo <plan-o-pedido.txt>
```

Sin archivo, el mismo encargo lleva `--texto "<pedido>"`. Pasa también lo que David sí dijo: `--restriccion` por cada prohibición expresa, `--repo` y `--base-rama` si hay repositorio, y `--responsable` / `--revisor` si eligió modelos. No inventes esos campos. `hermes`, `claw` y `openbot` son los directores válidos. La clave identifica el pedido: retransmitir con la misma clave no duplica el encargo.

Si el pedido es código con GitHub y David no pidió dejarlo local, añade `--entrega pr` o omite `--entrega`. Con `--repo` y sin nivel explícito el motor no cierra en local. Un pedido expresamente local lleva `--entrega terminado`. Integrar o desplegar solo si lo autorizó (`integrado` o `desplegado`).

## Motor nativo de la PC

En la PC de Hermes el código está en `C:/Users/ehven/motor-sucesor` y la base es `C:/Users/ehven/.motor-encargos`. Ejecuta los comandos desde ese código con el Python instalado de Hermes. En Git Bash:

```sh
cd C:/Users/ehven/motor-sucesor
"C:/Users/ehven/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -B -m motor director encargar \
  --via durable \
  --director hermes \
  --canal hermes \
  --clave "<id-del-mensaje>" \
  --sesion "<id-de-la-conversacion>" \
  --base C:/Users/ehven/.motor-encargos \
  --archivo "<archivo-del-pedido>"
```

Usa el mismo directorio, Python y base para `ver`, `parar`, `continuar`, `cancelar`, `responder` y `avisar`. El canal `hermes` devuelve el resultado al transporte registrado para el director.

`motor entrega`, `motor estado` y `motor doctor --como-servicio` pertenecen al circuito anterior que usa `~/.motor`. No los uses para entregar ni comprobar encargos de `~/.motor-encargos`. Su diagnóstico no es un requisito para recibir un pedido en el motor nuevo. Un plan externo entra como contenido del pedido, sin un formato de bloques que deba pasar antes de comenzar.

Una conversación abierta conserva las instrucciones con las que empezó. Después de actualizar esta skill, usa `/motor` seguido del pedido para cargar su contenido actual en esa conversación. Las conversaciones nuevas reciben el `SOUL.md` actualizado. Actualizar estos archivos no reinicia el chat ni borra su historial.

## Consultar, parar, continuar, cancelar y promover

Usa los comandos reales del motor. Consultar no lleva clave. Parar, continuar y cancelar retransmiten la misma clave de la orden.

```sh
python3 -B -m motor director ver <encargo> --base <base-del-motor>
python3 -B -m motor director parar <encargo> --base <base-del-motor> --clave <clave-de-la-orden>
python3 -B -m motor director continuar <encargo> --base <base-del-motor> --clave <clave-de-la-orden>
python3 -B -m motor director cancelar <encargo> --base <base-del-motor> --clave <clave-de-la-orden>
```

Al pasar de directa a durable, cierra la sesión anterior y conserva el mismo encargo. Un solo encargo, un solo escritor.

```sh
python3 -B -m motor director cerrar-sesion <encargo> --base <base-del-motor>
python3 -B -m motor director promover <encargo> --base <base-del-motor> --clave <clave-de-promocion>
```

OpenBot no ofrece parada hasta que su comando la demuestre. Un timeout no cancela el trabajo.

## Resultado y aviso

El resultado vuelve al canal de origen aunque este turno cierre. Si el aviso falla, se recupera el aviso. No se repite el encargo.

```sh
python3 -B -m motor director avisar --base <base-del-motor>
```

Cuando David pregunta "¿cómo va?", consulta con `ver` y contesta en una o dos frases de usuario. `ver` trae `seguimiento` (fase actual, resultado, necesidad pendiente, última decisión, revisión y PR) y `bitacora` (decisión, motivo, evidencia y resultado). Esa bitácora sale del registro. No hay otro juez.

`avisar` manda solo un cambio real. Un aviso con `novedad` falsa no se vuelve a contar. `entregado` verdadero exige recibo del transporte. Un archivo local no es un mensaje entregado. Si no hay novedad, no llames a un modelo para decir que sigue igual. Una espera trae su causa en `seguimiento.espera`; el trabajo queda en el mismo encargo.

## Responder en el mismo encargo

Una pregunta del puente llega en un aviso `tipo` `pregunta`, con `solicitud`. La respuesta usa el mismo encargo y el canal de origen. No abras otro encargo.

```sh
python3 -B -m motor director responder <encargo> \
  --base <base-del-motor> \
  --solicitud <id-de-la-pregunta> \
  --respuesta "<texto de David>" \
  --clave "<id-del-mensaje>" \
  --canal <canal-de-origen> \
  --sesion "<id-de-la-conversacion>"
```

La misma clave no repite el efecto. Una respuesta tardía no autoriza otra pregunta. Si el canal no coincide, el motor la rechaza.

OpenBot no tiene recepción bidireccional comprobada (`recepcion` falsa en `director capacidades-openbot`). En ese director la vía para responder al mismo registro es este comando CLI. No presentes un archivo de `canal/` como si David ya lo hubiera recibido.

## Poteto

El implementador usa poteto-mode. Sharpen, Architect, Arena y Figure-it-out se usan cuando el trabajo los necesita; no son una cadena obligatoria ni hace falta un runbook previo. Una hipótesis del plan no es una prohibición. Solo una prohibición expresa, o una decisión que David se reservó, necesita su respuesta. Una nota de redacción no devuelve el encargo.

Reutiliza el paquete poteto completo de Hermes. Vive en `shared/sistema-agentes/poteto/` de este repo: la skill `poteto-mode` completa en `skills/poteto-mode/SKILL.md`, sus playbooks y references, las skills `principle-*` y de especialidad, `adaptacion-hermes.md` y el manifiesto `hermes.json`. No lo sustituyas por el paquete abreviado del repo del motor (`paquetes/poteto/SKILL.md`): ese resumen no acredita la carga ni el uso del paquete completo. No asumas Cursor. No abras otro revisor DeepSeek: la revisión real es `goncloud-pr-review`.

El director carga la copia instalada en `skills/motor/SKILL.md` de su perfil de Hermes.

## Modelos

La política de modelos y esfuerzos sale de la fuente común (`model-routing.source.json` de este repo). No la edites a mano ni la reescribas en esta skill. Si David elige modelos para un encargo, conserva esa elección por rol y comunícala al motor. Si no están disponibles, informa el estado sin sustituirlos por tu cuenta. Un respaldo por cuota no cambia la cadena deseada.

Cuando David pida Muse Wonder para implementar y Kimi 3.1 para revisar, añade a `director encargar`:

```sh
--responsable meta/muse-wonder-1.0 --revisor kimi-code/k3.1-pro
```

Estas opciones pertenecen a ese encargo. Si David no eligió modelos, omítelas para usar la cadena compartida.
