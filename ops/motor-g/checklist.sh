#!/usr/bin/env bash
# G.5: sale 0 solo si Hermi responde por ssh sin gateway caído, la cadena retirada ya no está
# registrada en el plugin de claw y no queda ninguno de los crons de raíces. Cualquier fallo de
# ssh (o una respuesta que no se pueda leer) sale != 0: sin evidencia no hay "listo".
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/comun.sh"

mal=0
falla() { echo "FALLA: $1" >&2; mal=1; }

estado="$(en_pc "\"$PC_HERMES\" -p claw gateway status" 2>&1 | tr -d '\r')" \
  || { echo "FALLA: Hermi no responde por ssh ($PC_HOST)" >&2; exit 1; }
# "parked" es un estado válido de claw (Hermi lo reporta así con el gateway multiplexado); solo cuenta como caído lo que diga que no corre.
printf '%s\n' "$estado" | grep -qiE "not running|stopped|failed|error" && falla "el gateway no está corriendo"

yaml="$PC_PERFIL_CLAW\\plugins\\goncloud-team\\plugin.yaml"
cadena="$(en_pc "if not exist \"$yaml\" (echo SIN_PLUGIN) else (findstr /c:goncloud_local_run \"$yaml\" >nul && echo REGISTRADA || echo LIBRE)" 2>&1 | tr -d '\r\n ')" \
  || { echo "FALLA: no pude leer el plugin de claw por ssh" >&2; exit 1; }
[ "$cadena" = "LIBRE" ] || falla "la cadena fija sigue registrada o no se pudo comprobar ($cadena)"

crons="$(en_pc "\"$PC_HERMES\" -p default cron list" 2>&1 | tr -d '\r')" \
  || { echo "FALLA: no pude listar los crons por ssh" >&2; exit 1; }
for nombre in "${CRONS_RETIRADOS[@]}"; do
  printf '%s\n' "$crons" | grep -q "Name: *$nombre\$" && falla "existe el cron $nombre"
done

[ "$mal" = 0 ] && echo "ok: Hermi responde, la cadena ya no está registrada y no hay crons de raíces"
exit "$mal"
