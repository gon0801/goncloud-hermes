#!/usr/bin/env bash
# A.4: instala el backup_hermes.py del repo; deja la copia previa para rollback.sh.
set -euo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$aqui/comun.sh"

[ -f "$instalado" ] || { echo "no existe $instalado" >&2; exit 1; }
[ -f "$repo_script" ] || { echo "no existe $repo_script" >&2; exit 1; }

if [ ! -e "$copia" ]; then
  cp -p "$instalado" "$copia"
  echo "copia previa guardada en $copia"
else
  echo "ya existe $copia; se conserva la primera copia"
fi

sin_cr "$repo_script" > "$instalado.nuevo"
mv -f "$instalado.nuevo" "$instalado"
echo "instalado $instalado"

"$aqui/checklist.sh"
