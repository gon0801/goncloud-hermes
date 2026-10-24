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
STARTUP_DIR="${STARTUP_DIR:-$APPDATA/Microsoft/Windows/Start Menu/Programs/Startup}"
RESPALDO_DIR="${RESPALDO_DIR:-$HOME/.motor/respaldo-dashboard}"
CMD_BIN="${CMD_BIN:-cmd.exe}"
ESPERA_INTENTOS="${ESPERA_INTENTOS:-60}"
ESPERA_SEGUNDOS="${ESPERA_SEGUNDOS:-1}"

# PIDs que escuchan en $1 (ip:puerto), sin depender del idioma de Windows: un socket que escucha
# tiene destino 0.0.0.0:0, así que no se lee la columna del estado.
pids_escuchando() {
  netstat -ano | tr -d '\r' | awk -v l="$1" '$1 == "TCP" && $2 == l && $3 ~ /:0$/ { print $NF }' | sort -u
}

codigo_http() { curl -s -o /dev/null -m 5 -w '%{http_code}' "$DASH_URL" 2>/dev/null || true; }

http_vivo() {
  case "$(codigo_http)" in 200|302|401) return 0 ;; *) return 1 ;; esac
}

ruta_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

detener_puerto() {
  local pid
  for pid in $(pids_escuchando "${DASH_IP}:${DASH_PUERTO}"); do
    echo "deteniendo el proceso $pid que ocupa ${DASH_IP}:${DASH_PUERTO}"
    taskkill /PID "$pid" /F >/dev/null 2>&1 || true
  done
}
