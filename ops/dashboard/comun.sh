# Compartido por instala.sh, checklist.sh y desinstala.sh: el dashboard de claw como tarea de arranque.
# Corre en Git Bash en la PC (Windows). Todo lo externo se puede sustituir por variable para probarlo sin tocar la PC.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

TAREA="${TAREA:-HermesDashboardClaw}"
DASH_IP="${DASH_IP:-100.92.227.24}"
DASH_PUERTO="${DASH_PUERTO:-9119}"
DASH_URL="http://${DASH_IP}:${DASH_PUERTO}/"
DASH_USUARIO="${DASH_USUARIO:-ehven}"
HERMES_EXE="${HERMES_EXE:-C:\\Users\\ehven\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\hermes.exe}"
DASH_ARGS="--profile claw dashboard --host ${DASH_IP} --port ${DASH_PUERTO} --no-open --skip-build"
CMD_VIEJO="${CMD_VIEJO:-hermes-dashboard-claw.cmd}"
STARTUP_DIR="${STARTUP_DIR:-${APPDATA:-$HOME/AppData/Roaming}/Microsoft/Windows/Start Menu/Programs/Startup}"
RESPALDO_DIR="${RESPALDO_DIR:-$HOME/.motor/respaldo-dashboard}"
CMD_BIN="${CMD_BIN:-cmd.exe}"
ESPERA_INTENTOS="${ESPERA_INTENTOS:-60}"
ESPERA_SEGUNDOS="${ESPERA_SEGUNDOS:-1}"
ESPERA_TOTAL=$((ESPERA_INTENTOS * ESPERA_SEGUNDOS))

listeners_capturar() {
  local salida
  salida="$(netstat -ano)" || return 1
  printf '%s\n' "$salida" | tr -d '\r' | awk -v exacta="$DASH_IP" -v puerto="$DASH_PUERTO" '
    function ip_normal(s) {
      if (s ~ /^\[.*\]$/) s = substr(s, 2, length(s) - 2)
      return tolower(s)
    }
    BEGIN { exacta = ip_normal(exacta) }
    $1 == "TCP" && $3 ~ /:0$/ {
      direccion = $2
      puerto_local = direccion
      sub(/^.*:/, "", puerto_local)
      if (puerto_local != puerto) next
      ip = direccion
      sub(/:[^:]*$/, "", ip)
      ip = ip_normal(ip)
      clase = ip == exacta ? "exacto" : (ip == "0.0.0.0" || ip == "::" ? "colision" : "ajeno")
      print clase "|" ip "|" puerto_local "|" $NF
    }
  '
}

pids_escuchando() {
  listeners_capturar | awk -F'|' -v destino="$1" '$2 ":" $3 == destino { print $4 }' | sort -u
}

codigo_http() { curl -s --noproxy '*' -o /dev/null --connect-timeout 1 -m 2 -w '%{http_code}' "$DASH_URL" 2>/dev/null || true; }

http_vivo() {
  case "$(codigo_http)" in 200|302|401) return 0 ;; *) return 1 ;; esac
}

ruta_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

pids_ajenos() {
  listeners_capturar | awk -F'|' '$1 != "exacto" { print $4 }' | sort -u
}

xml_escapa() { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }

detener_puerto() {
  local captura pid i
  captura="$(listeners_capturar)" || return 1
  for pid in $(printf '%s\n' "$captura" | awk -F'|' 'NF == 4 && $1 != "ajeno" { print $4 }' | sort -u); do
    echo "deteniendo el proceso $pid que ocupa el puerto $DASH_PUERTO"
    taskkill /PID "$pid" /T /F >/dev/null 2>&1 || true
  done
  for ((i = 0; i < 5; i++)); do
    captura="$(listeners_capturar)" || return 1
    [ -z "$captura" ] && return 0
    sleep "$ESPERA_SEGUNDOS"
  done
  echo "ERROR: no se pudo liberar el puerto $DASH_PUERTO; listeners restantes:" >&2
  printf '%s\n' "$captura" | awk -F'|' '{ print "  " $2 ":" $3 " (PID " $4 ")" }' >&2
  return 1
}

dashboard_activo_exacto() {
  local captura conteos exactos ajenos
  captura="$(listeners_capturar)" || return 1
  conteos="$(printf '%s\n' "$captura" | awk -F'|' '$1 == "exacto" { e++ } $1 != "exacto" { a++ } END { printf "%d %d", e, a }')"
  exactos="${conteos%% *}"
  ajenos="${conteos##* }"
  [ "$exactos" -eq 1 ] && [ "$ajenos" -eq 0 ] && http_vivo
}
