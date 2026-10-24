#!/usr/bin/env bash
set -euo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
raiz="$(cd "$aqui/../.." && pwd)"
repo_script="$raiz/scripts/backup_hermes.py"
destino="C:/Users/ehven/AppData/Local/hermes/profiles/claw/scripts"
instalado="$destino/backup_hermes.py"

if cmp -s -- "$repo_script" "$instalado"; then
  echo "OK    el instalado es igual al del repo"
else
  echo "FALLA el instalado difiere del repo" >&2
  exit 1
fi

if python -B -c "import sys;sys.path.insert(0,sys.argv[1]);import backup_hermes as b;r=b.decide('user',('.config','himalaya','secrets','x.txt'),40);assert r is not None and r.keep" "$destino"; then
  echo "OK    el instalado respalda .config/himalaya/secrets"
else
  echo "FALLA el instalado no respalda .config/himalaya/secrets" >&2
  exit 1
fi
