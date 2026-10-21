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

en_pc() { "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=15 "$PC_HOST" "$1"; }

# Ids de los crons del perfil default cuyo nombre es $1 (una línea por id).
ids_de_cron() {
  local lista
  lista="$(en_pc "\"$PC_HERMES\" -p default cron list")" || return 1
  printf '%s\n' "$lista" | tr -d '\r' | awk -v n="$1" '
    $2 ~ /^\[/ && $1 ~ /^[0-9a-f]+$/ { id = $1 }
    $1 == "Name:" && $2 == n { print id }'
}

# Despliega el checkout actual de la PC: perfiles de Hermes + reinicio del gateway.
aplica_y_reinicia() {
  en_pc "\"$PC_PYTHON\" \"$PC_REPO\\scripts\\configure_team.py\""
  en_pc "\"$PC_HERMES\" -p claw gateway stop" || true
  en_pc "schtasks /run /tn Hermes_Gateway"
}
