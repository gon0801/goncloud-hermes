# Cron Job: monday-pedidos-semanal

**Job ID:** a9606c0778dd
**Run Time:** 2026-10-21 22:31:40
**Schedule:** 30 22 * * 3

**Prompt Characters:** 14261
## Prompt

[prompt quitado]

## Response

FALLO: paso 3, envío con himalaya. No se pudo enviar el correo. **No salió nada a <correo>**

- **Excel listo:** `Pedidos_Arras_Mx_2026-10-21.xlsx` salió por la API de Monday con 200 filas. Es el mismo número de elementos que Monday reporta para el tablero (grupo "Septiembre 2026"). Fechas de envío del 2026-09-01 al 2026-10-08. Los encabezados están en la fila 5. Ruta: `C:\Users\<usuario>\AppData\Local\hermes\profiles\claw\cache\scratch\monday\`
- **Por qué no se envió:** en esta PC nueva faltaba himalaya. Lo reinstalé (v2.1.0 oficial, en `%LOCALAPPDATA%\Programs\himalaya`) y su configuración sí está. Pero la contraseña de aplicación de Gmail que lee la cuenta no existe en la PC: `C:\Users\<usuario>\.config\himalaya\secrets\ehventasmx.txt`. himalaya responde "Secret command error". No usé la API de Gmail porque la rutina lo prohíbe.
- **Otro detalle:** la cuenta en himalaya ahora se llama `ehventasmx`, no `ehventas` como dice la rutina. Ya lo anoté en la skill. Falta corregir la rutina del cron.

**Necesito de ti:** genera una contraseña de aplicación nueva de Google para <correo> (Cuenta de Google → Seguridad → Contraseñas de aplicaciones). Guárdala en un archivo de texto en esa ruta. Hazlo tú directo, no me la pases por chat. Cuando esté, respóndeme "dale" y hago un solo envío de este Excel.
