#!/usr/bin/env bash
# A.4: devuelve el backup_hermes.py que había antes de desplegar.sh.
set -euo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$aqui/comun.sh"

[ -f "$copia" ] || { echo "no existe $copia; nada que restaurar" >&2; exit 1; }
cp -p "$copia" "$instalado.nuevo"
mv -f "$instalado.nuevo" "$instalado"
echo "restaurado $instalado desde $copia"
