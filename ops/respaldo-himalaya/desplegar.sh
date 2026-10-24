#!/usr/bin/env bash
set -euo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
raiz="$(cd "$aqui/../.." && pwd)"
repo_script="$raiz/scripts/backup_hermes.py"
destino="C:/Users/ehven/AppData/Local/hermes/profiles/claw/scripts"
instalado="$destino/backup_hermes.py"
copia="$destino/backup_hermes.py.antes-himalaya"

trap 'rm -f -- "$copia.nueva" "$instalado.nuevo"' EXIT

[ -f "$repo_script" ] || { echo "no existe $repo_script" >&2; exit 1; }
[ -f "$instalado" ] || { echo "no existe $instalado" >&2; exit 1; }

if [ ! -e "$copia" ]; then
  cp -- "$instalado" "$copia.nueva"
  mv -f -- "$copia.nueva" "$copia"
  echo "copia previa guardada en $copia"
else
  [ -f "$copia" ] || { echo "no es un archivo regular: $copia" >&2; exit 1; }
  echo "ya existe $copia; se conserva la primera copia"
fi

cp -- "$repo_script" "$instalado.nuevo"
mv -f -- "$instalado.nuevo" "$instalado"
echo "instalado $instalado"

bash "$aqui/checklist.sh"
