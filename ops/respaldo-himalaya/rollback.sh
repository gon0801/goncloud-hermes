#!/usr/bin/env bash
set -euo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
destino="C:/Users/ehven/AppData/Local/hermes/profiles/claw/scripts"
instalado="$destino/backup_hermes.py"
copia="$destino/backup_hermes.py.antes-himalaya"

trap 'rm -f -- "$instalado.nuevo"' EXIT

[ -f "$copia" ] || { echo "no existe $copia; nada que restaurar" >&2; exit 1; }
cp -- "$copia" "$instalado.nuevo"
mv -f -- "$instalado.nuevo" "$instalado"
echo "restaurado $instalado desde $copia"
