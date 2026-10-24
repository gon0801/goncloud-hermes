#!/usr/bin/env bash
# A.1: deja el dashboard de claw como tarea programada de arranque (BootTrigger, S4U, reintento).
# Idempotente. Nunca pide ni guarda contraseña: si Windows la exige o pide administrador, sale ≠0.
set -uo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$aqui/comun.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [ -n "${COMPUTERNAME:-}" ]; then usuario="${COMPUTERNAME}\\${DASH_USUARIO}"; else usuario="$DASH_USUARIO"; fi

xml="$tmp/tarea.xml"
{
  printf '\xff\xfe'
  iconv -f UTF-8 -t UTF-16LE <<XML
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Dashboard web de Hermes (perfil claw) en ${DASH_IP}:${DASH_PUERTO}</Description></RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>$(xml_escapa "$usuario")</UserId><LogonType>S4U</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <RestartOnFailure><Interval>PT1M</Interval><Count>5</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>$(xml_escapa "$HERMES_EXE")</Command><Arguments>$(xml_escapa "$DASH_ARGS")</Arguments></Exec>
  </Actions>
</Task>
XML
} > "$xml"

origen="$STARTUP_DIR/$CMD_VIEJO"
respaldo="$RESPALDO_DIR/$CMD_VIEJO"
cmd_movido=0
historico=""

deshacer_respaldo() {
  local fallo=0
  if [ "$cmd_movido" -eq 1 ]; then
    if [ -e "$origen" ]; then
      echo "ERROR: no se pudo restaurar $respaldo porque $origen ya existe; ambos archivos quedan pendientes de revisión" >&2
      fallo=1
    elif [ -e "$respaldo" ] && mv -f "$respaldo" "$origen" && [ -e "$origen" ] && [ ! -e "$respaldo" ]; then
      cmd_movido=0
    else
      echo "ERROR: no se pudo devolver $CMD_VIEJO a Inicio; revisa $origen y $respaldo" >&2
      fallo=1
    fi
  fi
  if [ -n "$historico" ]; then
    if [ "$cmd_movido" -eq 0 ] && [ ! -e "$respaldo" ] && [ -e "$historico" ]; then
      if mv -f "$historico" "$respaldo" && [ -e "$respaldo" ] && [ ! -e "$historico" ]; then
        historico=""
      else
        echo "ERROR: no se pudo restaurar el respaldo anterior desde $historico a $respaldo" >&2
        fallo=1
      fi
    else
      echo "ERROR: quedan archivos sin restaurar; conserva $historico y revisa $respaldo" >&2
      fallo=1
    fi
  fi
  return "$fallo"
}

if [ -e "$origen" ]; then
  if ! { mkdir -p "$RESPALDO_DIR" && [ -w "$RESPALDO_DIR" ]; }; then
    echo "ERROR: no se pudo mover el .cmd viejo: el respaldo $RESPALDO_DIR no se puede escribir; no se crea la tarea para no correr dos dashboards" >&2
    exit 5
  fi
  if [ -e "$respaldo" ]; then
    historico="$(mktemp "$respaldo.$(date +%Y%m%d%H%M%S).XXXXXX")" || {
      echo "ERROR: no se pudo reservar un respaldo histórico en $RESPALDO_DIR; no se crea la tarea" >&2
      exit 5
    }
    if ! mv -f "$respaldo" "$historico" || [ -e "$respaldo" ] || [ ! -e "$historico" ]; then
      if [ -e "$respaldo" ]; then
        [ ! -s "$historico" ] && rm -f "$historico"
        historico=""
      fi
      deshacer_respaldo || true
      echo "ERROR: no se pudo rotar el respaldo anterior; no se movió el .cmd ni se creó la tarea" >&2
      exit 5
    fi
  fi
  if mv -f "$origen" "$respaldo" && [ ! -e "$origen" ] && [ -e "$respaldo" ]; then
    cmd_movido=1
    echo "movido $CMD_VIEJO de Inicio a $RESPALDO_DIR"
  else
    [ ! -e "$origen" ] && [ -e "$respaldo" ] && cmd_movido=1
    deshacer_respaldo || true
    echo "ERROR: no se pudo mover el .cmd viejo de Inicio; no se ejecuta schtasks /Create, /Delete ni /Run" >&2
    exit 5
  fi
fi

salida="$(schtasks /Create /TN "$TAREA" /XML "$(ruta_win "$xml")" /F </dev/null 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  if printf '%s' "$salida" | grep -qiE 'denied|denegad|0x80070005|administrad|password|contrase'; then
    echo "ERROR: Windows no dejó crear la tarea $TAREA: hace falta correr instala.sh como administrador (o que Windows acepte S4U sin contraseña). No se pide ni se guarda ninguna contraseña." >&2
  else
    echo "ERROR: schtasks no pudo crear la tarea $TAREA (código $rc)." >&2
  fi
  printf '%s\n' "$salida" >&2
  deshacer_respaldo || echo "ERROR: la creación falló y el respaldo no quedó completamente restaurado" >&2
  exit 2
fi
echo "tarea $TAREA creada o reemplazada"

schtasks /End /TN "$TAREA" >/dev/null 2>&1 || true
detener_puerto || exit 6

if ! salida="$(schtasks /Run /TN "$TAREA" 2>&1)"; then
  echo "ERROR: no se pudo lanzar la tarea $TAREA" >&2
  printf '%s\n' "$salida" >&2
  exit 3
fi

limite=$((SECONDS + ESPERA_TOTAL))
while :; do
  if dashboard_activo_exacto; then
    echo "el dashboard responde en $DASH_URL con un único listener en ${DASH_IP}:${DASH_PUERTO}"
    exit 0
  fi
  [ "$SECONDS" -ge "$limite" ] && break
  sleep "$ESPERA_SEGUNDOS"
done
echo "ERROR: el dashboard no quedó con un único listener exacto, sin listeners ajenos y HTTP válido en $DASH_URL tras $ESPERA_TOTAL s" >&2
exit 4
