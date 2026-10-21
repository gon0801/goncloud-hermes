#!/usr/bin/env bash
# G.4: deja en el PR #37 (Raíces, ya cerrado) el aviso de que lo reemplaza el motor (motor-01).
# Idempotente: si el comentario ya está, no escribe otro. Sale 0 solo si el comentario queda en el PR.
set -euo pipefail

repo="${PR37_REPO:-gon0801/goncloud-hermes}"
marca="<!-- motor-01:g:reemplaza-raices -->"
cuerpo="$marca
Reemplazado por el motor (plan \`motor-01\`, bloque G): Hermes ya no usa la cadena fija de 7 pasos ni las raíces. Reparte la lectura a sus agentes y entrega todo lo que cambia algo al motor con la skill \`motor\`. Este PR queda cerrado y su código se retiró de \`feat/hermes-team\`."

ya="$(gh pr view 37 --repo "$repo" --json comments --jq "[.comments[].body | select(contains(\"$marca\"))] | length")"
if [ "$ya" = "0" ]; then
  gh pr comment 37 --repo "$repo" --body "$cuerpo" >/dev/null
fi
ya="$(gh pr view 37 --repo "$repo" --json comments --jq "[.comments[].body | select(contains(\"$marca\"))] | length")"
[ "$ya" -ge 1 ] || { echo "el comentario no quedó en el PR #37" >&2; exit 1; }
echo "ok: PR #37 ya dice que lo reemplaza el motor (motor-01)"
