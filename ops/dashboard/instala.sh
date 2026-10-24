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

salida="$(schtasks /Create /TN "$TAREA" /XML "$(ruta_win "$xml")" /F 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  if printf '%s' "$salida" | grep -qiE 'denied|denegad|0x80070005|administrad|password|contrase'; then
    echo "ERROR: Windows no dejó crear la tarea $TAREA: hace falta correr instala.sh como administrador (o que Windows acepte S4U sin contraseña). No se pide ni se guarda ninguna contraseña." >&2
  else
    echo "ERROR: schtasks no pudo crear la tarea $TAREA (código $rc)." >&2
  fi
  printf '%s\n' "$salida" >&2
  exit 2
fi
echo "tarea $TAREA creada o reemplazada"

if [ -e "$STARTUP_DIR/$CMD_VIEJO" ]; then
  if mkdir -p "$RESPALDO_DIR" && mv -f "$STARTUP_DIR/$CMD_VIEJO" "$RESPALDO_DIR/$CMD_VIEJO" && [ ! -e "$STARTUP_DIR/$CMD_VIEJO" ]; then
    echo "movido $CMD_VIEJO de Inicio a $RESPALDO_DIR"
  else
    echo "ERROR: no se pudo mover el .cmd viejo de Inicio; no se lanza la tarea para no correr dos dashboards" >&2
    exit 5
  fi
fi

schtasks /End /TN "$TAREA" >/dev/null 2>&1 || true
detener_puerto

if ! salida="$(schtasks /Run /TN "$TAREA" 2>&1)"; then
  echo "ERROR: no se pudo lanzar la tarea $TAREA" >&2
  printf '%s\n' "$salida" >&2
  exit 3
fi

limite=$((SECONDS + ESPERA_TOTAL))
while :; do
  if http_vivo; then
    echo "el dashboard responde en $DASH_URL"
    exit 0
  fi
  [ "$SECONDS" -ge "$limite" ] && break
  sleep "$ESPERA_SEGUNDOS"
done
echo "ERROR: el dashboard no respondió en $DASH_URL tras $ESPERA_TOTAL s" >&2
exit 4
