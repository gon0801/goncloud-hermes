#!/usr/bin/env bash
# A.2: quita la tarea y deja el dashboard como antes: el .cmd vuelve a Inicio y se relanza.
set -uo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$aqui/comun.sh"

schtasks /End /TN "$TAREA" >/dev/null 2>&1 || true
if schtasks /Query /TN "$TAREA" >/dev/null 2>&1; then
  if ! salida="$(schtasks /Delete /TN "$TAREA" /F 2>&1)"; then
    echo "ERROR: no se pudo borrar la tarea $TAREA (¿hace falta administrador?)" >&2
    printf '%s\n' "$salida" >&2
    exit 2
  fi
  echo "tarea $TAREA borrada"
fi

if [ -e "$RESPALDO_DIR/$CMD_VIEJO" ] && [ ! -e "$STARTUP_DIR/$CMD_VIEJO" ]; then
  mv -f "$RESPALDO_DIR/$CMD_VIEJO" "$STARTUP_DIR/$CMD_VIEJO"
  echo "devuelto $CMD_VIEJO a Inicio"
fi

detener_puerto
if [ -e "$STARTUP_DIR/$CMD_VIEJO" ]; then
  "$CMD_BIN" /c "$(ruta_win "$STARTUP_DIR/$CMD_VIEJO")" >/dev/null 2>&1 || true
  for ((i = 1; i <= ESPERA_INTENTOS; i++)); do
    http_vivo && { echo "el dashboard responde en $DASH_URL"; exit 0; }
    sleep "$ESPERA_SEGUNDOS"
  done
  echo "ERROR: el dashboard relanzado no respondió en $DASH_URL" >&2
  exit 4
fi
exit 0
