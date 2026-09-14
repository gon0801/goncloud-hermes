# Fuentes y conocimiento del negocio

Procedencia: repos privados goncloud-workspace-main, operaciones e ingenieria, revisados el 2026-09-13. Son referencias historicas; validar el estado real de cada integracion antes de usarla.

- Para digests de packing/ventas, la directiva del usuario del 2026-09-13 establece MercadoLibre, Amazon Seller Central y Seller Flex como fuentes. No usar Odoo para decidir inclusion o estado en estos digests.
- Odoo es otro dominio: productos, inventario y compras tienen sus propios contratos. Consultar operaciones antes de trasladar reglas de un flujo a otro.
- Los reportes y envios requieren destinatarios y formato confirmados por el runbook vigente. No copiar automaticamente destinatarios ni habilitar tareas de OpenClaw.
- Fuente no disponible no equivale a cero ventas: informar datos incompletos y recuperar la fuente.
- Conservar id de pedido, plataforma, zona horaria y fecha de consulta al reconciliar datos. Una diferencia debe quedar explicada antes de cerrar.
- No se han conectado cuentas comerciales ni el gateway anterior. El unico canal activo es el bot nuevo de Telegram del director.

Para nuevos hechos compartidos, usar comentarios y adjuntos de Kanban con: afirmacion, fuente, fecha, especialista responsable y si reemplaza una decision previa. Guardar procedimientos largos como skills; los hechos breves y preferencias estables van en memoria. No guardar secretos ni datos personales innecesarios.
