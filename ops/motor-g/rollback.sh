#!/usr/bin/env bash
# G.5: vuelve a Hermi al SHA que guardó desplegar.sh, re-despliega los perfiles y reinicia el gateway.
# No recrea los crons borrados: con ese SHA se vuelven a instalar con su instalador de entonces.
# configure_team corre con --allow-regression: el SHA de vuelta no contiene la revisión ya
# desplegada y refuse_regression lo bloquearía (así murió el rollback del intento 5).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/comun.sh"

previo="$(en_pc "type \"$PC_SHA_ANTERIOR\"" | tr -d '\r\n ')"
case "$previo" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) echo "no hay SHA anterior guardado en la PC" >&2; exit 1 ;;
esac
en_pc "git -C \"$PC_REPO\" checkout -B $PC_RAMA $previo"
aplica_y_reinicia " --allow-regression"
echo "rollback: la PC quedó en $previo (los crons de raíces no se recrean aquí)"
