# Rutas comunes de desplegar.sh, checklist.sh y rollback.sh. Se carga con `.`.
raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
repo_script="$raiz/scripts/backup_hermes.py"
destino="${HIMALAYA_DESTINO:-C:/Users/ehven/AppData/Local/hermes/profiles/claw/scripts}"
instalado="$destino/backup_hermes.py"
copia="$destino/backup_hermes.py.antes-himalaya"

# El repo se lee con CRLF en esta PC (autocrlf) y el instalado está en LF:
# se compara y se copia siempre sin CR, que es lo que guarda git.
sin_cr() { tr -d '\r' < "$1"; }
