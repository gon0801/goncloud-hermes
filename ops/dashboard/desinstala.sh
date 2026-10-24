#!/usr/bin/env bash
# A.2: quita la tarea y deja el dashboard como antes: el .cmd vuelve a Inicio y se relanza.
set -uo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$aqui/comun.sh"

devuelto=0
if [ -e "$RESPALDO_DIR/$CMD_VIEJO" ] && [ ! -e "$STARTUP_DIR/$CMD_VIEJO" ]; then
  if ! { mkdir -p "$STARTUP_DIR" && mv -f "$RESPALDO_DIR/$CMD_VIEJO" "$STARTUP_DIR/$CMD_VIEJO" && [ -e "$STARTUP_DIR/$CMD_VIEJO" ]; }; then
    echo "ERROR: no se pudo devolver $CMD_VIEJO a Inicio; queda en $RESPALDO_DIR y la tarea $TAREA no se toca" >&2
    exit 5
  fi
  devuelto=1
  echo "devuelto $CMD_VIEJO a Inicio"
fi

salida="$(schtasks /Query /TN "$TAREA" 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  if ! printf '%s\n' "$salida" | grep -qiE 'The system cannot find the file specified|El sistema no puede encontrar el archivo especificado|0x80070002'; then
    echo "ERROR: no se pudo consultar la tarea $TAREA; no se toca" >&2
    printf '%s\n' "$salida" >&2
    if [ "$devuelto" -eq 1 ]; then
      if mv -f "$STARTUP_DIR/$CMD_VIEJO" "$RESPALDO_DIR/$CMD_VIEJO"; then
        echo "$CMD_VIEJO vuelve al respaldo para no correr dos dashboards" >&2
      else
        echo "ERROR: no se pudo revertir $CMD_VIEJO al respaldo; queda residual en Inicio junto a la tarea $TAREA" >&2
      fi
    fi
    exit 2
  fi
else
  schtasks /End /TN "$TAREA" >/dev/null 2>&1 || true
  if ! salida="$(schtasks /Delete /TN "$TAREA" /F 2>&1)"; then
    echo "ERROR: no se pudo borrar la tarea $TAREA (¿hace falta administrador?)" >&2
    printf '%s
' "$salida" >&2
    if [ "$devuelto" -eq 1 ]; then
      if mv -f "$STARTUP_DIR/$CMD_VIEJO" "$RESPALDO_DIR/$CMD_VIEJO"; then
        echo "$CMD_VIEJO vuelve al respaldo para no correr dos dashboards" >&2
      else
        echo "ERROR: no se pudo revertir $CMD_VIEJO al respaldo; queda residual en Inicio junto a la tarea $TAREA" >&2
      fi
    fi
    exit 2
  fi
  salida="$(schtasks /Query /TN "$TAREA" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ] || ! printf '%s\n' "$salida" | grep -qiE 'The system cannot find the file specified|El sistema no puede encontrar el archivo especificado|0x80070002'; then
    echo "ERROR: no se pudo confirmar que la tarea $TAREA quedó borrada; no se relanza el .cmd" >&2
    printf '%s\n' "$salida" >&2
    if [ "$devuelto" -eq 1 ]; then
      if mv -f "$STARTUP_DIR/$CMD_VIEJO" "$RESPALDO_DIR/$CMD_VIEJO"; then
        echo "$CMD_VIEJO vuelve al respaldo para no correr dos dashboards" >&2
      else
        echo "ERROR: no se pudo revertir $CMD_VIEJO al respaldo; queda residual en Inicio junto a la tarea $TAREA" >&2
      fi
    fi
    exit 2
  fi
  echo "tarea $TAREA borrada"
fi

if [ -e "$STARTUP_DIR/$CMD_VIEJO" ]; then
  detener_puerto || exit 6
  "$CMD_BIN" /c "$(ruta_win "$STARTUP_DIR/$CMD_VIEJO")" >/dev/null 2>&1 || true
  for ((i = 1; i <= ESPERA_INTENTOS; i++)); do
    dashboard_activo_exacto && { echo "el dashboard responde en $DASH_URL"; exit 0; }
    sleep "$ESPERA_SEGUNDOS"
  done
  echo "ERROR: el dashboard relanzado no respondió en $DASH_URL" >&2
  exit 4
fi
exit 0
