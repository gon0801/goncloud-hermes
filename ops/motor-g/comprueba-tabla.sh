#!/usr/bin/env bash
# G.3: la tabla de modelos de este repo sale de goncloud-motor-conf, no se edita a mano.
# Sale 0 solo si (1) scripts/genera_modelos.py --comprueba de la conf (origin/main) dice que
# lo generado coincide con su fuente, y (2) los carriles que la conf genera (codigo y planear)
# son idénticos a los de model-routing.source.json de este repo.
set -euo pipefail

aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
conf="${MOTOR_CONF_REPO:-}"
if [ -z "$conf" ]; then
  for c in "$HOME"/.motor-arranque/repos/goncloud-motor-conf-*; do
    [ -d "$c/.git" ] && conf="$c" && break
  done
fi
[ -n "$conf" ] && [ -d "$conf/.git" ] || { echo "no encuentro el repo goncloud-motor-conf (MOTOR_CONF_REPO)" >&2; exit 2; }
ref="${MOTOR_CONF_REF:-origin/main}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$conf" archive --prefix=conf/ "$ref" | tar -x -C "$tmp"
[ -f "$tmp/conf/scripts/genera_modelos.py" ] || { echo "$ref de la conf no trae scripts/genera_modelos.py" >&2; exit 2; }

python3 "$tmp/conf/scripts/genera_modelos.py" --comprueba

python3 - "$aqui/model-routing.source.json" "$tmp/conf/modelos/hermes.json" <<'PY'
import json, sys

mio = json.load(open(sys.argv[1], encoding="utf-8"))["chains"]
gen = json.load(open(sys.argv[2], encoding="utf-8"))["chains"]
mal = [c for c, cadena in gen.items() if mio.get(c) != cadena]
if mal:
    print("model-routing.source.json difiere de lo que genera la conf en:", ", ".join(mal), file=sys.stderr)
    sys.exit(1)
print("ok: los carriles", ", ".join(gen), "coinciden con lo generado por la conf")
PY
