#!/usr/bin/env bash
# G.5: prueba de checklist.sh, desplegar.sh y rollback.sh con un `ssh` falso (sin tocar la PC).
# El ssh falso lleva un estado en $FAKE: crons, plugin de claw, gateway, SHA y bitácora de comandos.
set -uo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"

cat > "$tmp/bin/ssh" <<'FAKE'
#!/usr/bin/env bash
# Como la PC real (Windows), la salida de Hermes lleva CRLF.
# Uso real: ssh -o BatchMode=yes -o ConnectTimeout=15 <host> "<comando de cmd.exe>"
cmd="${@: -1}"
id_de() { printf 'ab%s' "$(printf %s "$1" | cksum | cut -d' ' -f1)"; }  # id hexadecimal, como los de Hermes
echo "$cmd" >> "$FAKE/bitacora"
[ -e "$FAKE/ssh_caido" ] && { echo "ssh: connect to host gwpc port 22: Connection timed out" >&2; exit 255; }
case "$cmd" in
  *"gateway stop"*) echo parked > "$FAKE/gateway" ;;  # como Hermes real: stop ESTACIONA claw
  *"gateway restart"*|*"gateway start"*) echo running > "$FAKE/gateway" ;;
  *"gateway status"*)
    [ -e "$FAKE/gateway_caido" ] && { printf 'Gateway is not running\r\n'; exit 0; }  # rc 0: debe fallar por el texto, no por ssh
    [ -e "$FAKE/gateway_vacio" ] && exit 0
    if [ "$(cat "$FAKE/gateway")" = parked ]; then
      printf 'Profile claw: parked (hermes -p claw gateway start)\r\n'
    else
      printf '✓ Gateway is running via the default-profile multiplexer\r\n'
    fi ;;
  *"cron list"*)
    for n in $(cat "$FAKE/crons"); do
      printf '  %s [active]\r\n    Name:      %s\r\n    Schedule:  every 1m\r\n\r\n' "$(id_de "$n")" "$n"
    done ;;
  *"cron remove"*)
    id="${cmd##* }"
    for n in $(cat "$FAKE/crons"); do [ "$(id_de "$n")" = "$id" ] || echo "$n"; done > "$FAKE/crons.nuevo"
    mv "$FAKE/crons.nuevo" "$FAKE/crons" ;;
  *"findstr /c:goncloud_local_run"*) cat "$FAKE/cadena" ;;
  *"configure_team.py"*) echo configurado > "$FAKE/cadena_nueva"; echo LIBRE > "$FAKE/cadena" ;;
  *"rev-parse HEAD"*) cat "$FAKE/sha" ;;
  *"merge --ff-only"*) echo def9876543210 > "$FAKE/sha" ;;
  *"checkout -B"*) echo "${cmd##* }" > "$FAKE/sha" ;;
  *"echo "*"sha-anterior"*) echo "$cmd" | sed 's/^echo \([0-9a-f]*\)>.*/\1/' > "$FAKE/guardado" ;;
  *"type "*"sha-anterior"*) cat "$FAKE/guardado" 2>/dev/null || exit 1 ;;
esac
exit 0
FAKE
chmod +x "$tmp/bin/ssh"

export FAKE="$tmp/fake" SSH_BIN="$tmp/bin/ssh"
fallos=0
caso() { # nombre esperado(0|!0) comando...
  local nombre="$1" espera="$2"; shift 2
  "$@" >"$tmp/salida" 2>&1; local rc=$?
  if { [ "$espera" = 0 ] && [ $rc -eq 0 ]; } || { [ "$espera" != 0 ] && [ $rc -ne 0 ]; }; then
    echo "ok   $nombre"
  else
    echo "MAL  $nombre (rc=$rc, esperaba $espera)"; sed 's/^/     /' "$tmp/salida"; fallos=$((fallos + 1))
  fi
}
estado_limpio() {
  rm -rf "$FAKE"; mkdir -p "$FAKE"
  printf '' > "$FAKE/crons"; echo LIBRE > "$FAKE/cadena"; echo abc1234567890 > "$FAKE/sha"; echo running > "$FAKE/gateway"
}

estado_limpio
caso "todo en orden sale 0" 0 bash "$aqui/checklist.sh"
touch "$FAKE/ssh_caido"
caso "ssh caído sale != 0" '!0' bash "$aqui/checklist.sh"
estado_limpio; touch "$FAKE/gateway_caido"
caso "gateway caído sale != 0" '!0' bash "$aqui/checklist.sh"
estado_limpio; touch "$FAKE/gateway_vacio"
caso "gateway con salida vacía sale != 0" '!0' bash "$aqui/checklist.sh"
estado_limpio; echo parked > "$FAKE/gateway"
caso "gateway parked sale != 0" '!0' bash "$aqui/checklist.sh"
estado_limpio; echo REGISTRADA > "$FAKE/cadena"
caso "cadena aún registrada sale != 0" '!0' bash "$aqui/checklist.sh"
estado_limpio; echo SIN_PLUGIN > "$FAKE/cadena"
caso "plugin ilegible sale != 0" '!0' bash "$aqui/checklist.sh"
estado_limpio; echo despertar-raices > "$FAKE/crons"
caso "cron despertar-raices existente sale != 0" '!0' bash "$aqui/checklist.sh"

estado_limpio; echo REGISTRADA > "$FAKE/cadena"; printf 'despertar-raices\nticker-raices\notro\n' > "$FAKE/crons"
caso "checklist rojo antes de desplegar" '!0' bash "$aqui/checklist.sh"
caso "desplegar.sh sale 0" 0 bash "$aqui/desplegar.sh"
caso "checklist verde tras desplegar" 0 bash "$aqui/checklist.sh"
caso "desplegar borró solo los crons de raíces" 0 test "$(cat "$FAKE/crons")" = otro
caso "desplegar reinició claw con restart" 0 grep -q -- "-p claw gateway restart" "$FAKE/bitacora"
caso "desplegar no estacionó claw" 0 test "$(cat "$FAKE/gateway")" = running
caso "desplegar subió el checkout" 0 test "$(cat "$FAKE/sha")" = def9876543210
caso "rollback.sh sale 0" 0 bash "$aqui/rollback.sh"
caso "rollback volvió al SHA anterior" 0 test "$(cat "$FAKE/sha")" = abc1234567890

estado_limpio; touch "$FAKE/ssh_caido"
caso "desplegar con ssh caído sale != 0" '!0' bash "$aqui/desplegar.sh"
estado_limpio
caso "rollback sin SHA guardado sale != 0" '!0' bash "$aqui/rollback.sh"

[ "$fallos" = 0 ] && echo "todo ok" || echo "$fallos caso(s) mal"
exit "$fallos"
