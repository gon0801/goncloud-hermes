#!/usr/bin/env bash
# G.5: pone a Hermi (la PC) en el SHA de $PC_RAMA, retira los crons de raíces y reinicia el gateway.
# Guarda el SHA previo en la PC para rollback.sh. Todo es por ssh; sale != 0 al primer fallo.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/comun.sh"

previo="$(en_pc "git -C \"$PC_REPO\" rev-parse HEAD" | tr -d '\r\n ')"
[ -n "$previo" ] || { echo "no pude leer el SHA actual de la PC" >&2; exit 1; }
en_pc "echo $previo> \"$PC_SHA_ANTERIOR\"" >/dev/null
echo "SHA anterior en la PC: $previo"

en_pc "git -C \"$PC_REPO\" fetch origin"
en_pc "git -C \"$PC_REPO\" checkout $PC_RAMA"
en_pc "git -C \"$PC_REPO\" merge --ff-only origin/$PC_RAMA"

for nombre in "${CRONS_RETIRADOS[@]}"; do
  ids="$(ids_de_cron "$nombre")"
  for id in $ids; do
    en_pc "\"$PC_HERMES\" -p default cron remove $id"
    echo "cron $nombre ($id) borrado"
  done
done

aplica_y_reinicia
echo "desplegado: $(en_pc "git -C \"$PC_REPO\" rev-parse HEAD" | tr -d '\r\n ')"
