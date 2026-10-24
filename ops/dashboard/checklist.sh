#!/usr/bin/env bash
# A.2: sale 0 solo si el dashboard quedó como lo deja instala.sh.
set -uo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$aqui/comun.sh"

fallos=0
ok() { echo "OK    $1"; }
mal() { echo "FALLA $1"; fallos=$((fallos + 1)); }

xml="$(schtasks /Query /TN "$TAREA" /XML 2>/dev/null | tr -d '\r\0')"
if [ -z "$xml" ]; then
  mal "la tarea $TAREA no existe"
else
  printf '%s' "$xml" | grep -q '<BootTrigger' && ok "la tarea tiene BootTrigger" || mal "la tarea no tiene BootTrigger"
  printf '%s' "$xml" | grep -q '<LogonType>S4U</LogonType>' && ok "la tarea corre con S4U" || mal "la tarea no usa S4U"
fi

[ ! -e "$STARTUP_DIR/$CMD_VIEJO" ] && ok "el .cmd viejo ya no está en Inicio" || mal "el .cmd viejo sigue en Inicio"

if n="$(pids_escuchando "${DASH_IP}:${DASH_PUERTO}" | awk 'END { print NR }')"; then
  [ "$n" -eq 1 ] && ok "un proceso escucha en ${DASH_IP}:${DASH_PUERTO}" || mal "escuchan $n procesos en ${DASH_IP}:${DASH_PUERTO} (debe ser 1)"
else
  mal "no se pudo consultar el proceso que escucha en ${DASH_IP}:${DASH_PUERTO}"
fi

if t="$(pids_ajenos | awk 'END { print NR }')"; then
  [ "$t" -eq 0 ] && ok "nada escucha en :${DASH_PUERTO} fuera de ${DASH_IP}" || mal "hay $t procesos en :${DASH_PUERTO} en otra dirección (0.0.0.0, [::] u otra)"
else
  mal "no se pudo consultar si algo escucha en :${DASH_PUERTO} fuera de ${DASH_IP}"
fi

http_vivo && ok "$DASH_URL responde" || mal "$DASH_URL no responde 200, 302 o 401 (dio $(codigo_http))"

[ "$fallos" -eq 0 ]
