#!/usr/bin/env bash
# A.4: sale 0 solo si el instalado es igual al del repo y la prueba real pasa.
set -euo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$aqui/comun.sh"

if cmp -s <(sin_cr "$repo_script") <(sin_cr "$instalado"); then
  echo "OK    el instalado es igual al del repo"
else
  echo "FALLA el instalado difiere del repo" >&2
  exit 1
fi

# prueba-real: el script instalado conserva .config/himalaya/secrets
if python -c "import sys;sys.path.insert(0,sys.argv[1]);import backup_hermes as b;r=b.decide('user',('.config','himalaya','secrets','x.txt'),40);assert r is not None and r.keep" "$destino"; then
  echo "OK    el instalado respalda .config/himalaya/secrets"
else
  echo "FALLA el instalado no respalda .config/himalaya/secrets" >&2
  exit 1
fi
