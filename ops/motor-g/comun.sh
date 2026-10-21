# Compartido por desplegar.sh, checklist.sh y rollback.sh (bloque G): cómo llegar a Hermi (la PC).
# Todo corre por ssh; los comandos remotos son de cmd.exe (la PC es Windows).
SSH_BIN="${SSH_BIN:-ssh}"
PC_HOST="${PC_HOST:-gwpc}"
PC_REPO="${PC_REPO:-C:\\dev\\goncloud-hermes}"
PC_RAMA="${PC_RAMA:-feat/hermes-team}"
PC_HERMES="${PC_HERMES:-C:\\Users\\ehven\\AppData\\Local\\hermes\\bin\\hermes.exe}"
PC_PYTHON="${PC_PYTHON:-C:\\Users\\ehven\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe}"
PC_PERFIL_CLAW="${PC_PERFIL_CLAW:-C:\\Users\\ehven\\AppData\\Local\\hermes\\profiles\\claw}"
PC_SHA_ANTERIOR="${PC_SHA_ANTERIOR:-C:\\Users\\ehven\\AppData\\Local\\hermes\\motor-g-sha-anterior.txt}"
# Crons de la cadena retirada: despertar-raices (pedido) y su ticker, que sin raices/ fallaría cada minuto.
CRONS_RETIRADOS=(despertar-raices ticker-raices)
# Tras un restart el host del gateway tarda unos segundos en servir de nuevo. El intento 5
# devolvió 0 con el gateway a medio levantar y el checklist dio rojo justo después: ahora
# se espera hasta este límite antes de darlo por caído (la prueba falsa los baja a 0s).
GATEWAY_ESPERA_INTENTOS="${GATEWAY_ESPERA_INTENTOS:-20}"
GATEWAY_ESPERA_SEGUNDOS="${GATEWAY_ESPERA_SEGUNDOS:-3}"

en_pc() { "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=15 "$PC_HOST" "$1"; }

# ¿El gateway de claw está servido o corriendo? Misma evidencia que exige checklist.sh:
# "parked" (gateway stop estaciona claw y lo saca del host) o una salida sin señal de vida es caído.
gateway_arriba() {
  local estado
  estado="$(en_pc "\"$PC_HERMES\" -p claw gateway status" 2>&1 | tr -d '\r')" || return 1
  printf '%s\n' "$estado" | grep -qi "parked" && return 1
  printf '%s\n' "$estado" | grep -qiE "is running|claw.*served|served.*claw"
}

# Espera a que el gateway responda de verdad tras reiniciarlo (un restart puede volver
# antes de que el host esté sirviendo).
espera_gateway() {
  local intento
  for ((intento = 1; intento <= GATEWAY_ESPERA_INTENTOS; intento++)); do
    gateway_arriba && return 0
    sleep "$GATEWAY_ESPERA_SEGUNDOS"
  done
  return 1
}

# Ids de los crons del perfil default cuyo nombre es $1 (una línea por id).
ids_de_cron() {
  local lista
  lista="$(en_pc "\"$PC_HERMES\" -p default cron list")" || return 1
  printf '%s\n' "$lista" | tr -d '\r' | awk -v n="$1" '
    $2 ~ /^\[/ && $1 ~ /^[0-9a-f]+$/ { id = $1 }
    $1 == "Name:" && $2 == n { print id }'
}

# Despliega el checkout actual de la PC: perfiles de Hermes + reinicio del gateway esperado
# hasta que responde. $1 se le pasa a configure_team.py (" --allow-regression" cuando rollback
# vuelve a un SHA viejo: refuse_regression no deja sobrescribir perfiles desplegados desde
# un commit que ese SHA no contiene, y volver atrás es justo ese caso — el intento 5 murió ahí).
aplica_y_reinicia() {
  en_pc "\"$PC_PYTHON\" \"$PC_REPO\\scripts\\configure_team.py\"${1:-}"
  # `gateway stop` ESTACIONA claw (gateway.parked) y schtasks no lo regresa: se reinicia en caliente con
  # `restart` (unserve/serve; si ya estaba parked lo desestaciona). schtasks solo levanta el host si no corre.
  if en_pc "\"$PC_HERMES\" -p claw gateway restart" && espera_gateway; then return 0; fi
  en_pc "schtasks /run /tn Hermes_Gateway" || return 1
  en_pc "\"$PC_HERMES\" -p claw gateway restart" || return 1
  espera_gateway
}
