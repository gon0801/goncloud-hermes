#!/usr/bin/env bash
# Instala los vigías en la Mac, la Mini y la PC desde un commit, o con --comprueba revisa que estén.
#
#   MOTOR_SHA=<sha> scripts/instala_vigias.sh      # sin MOTOR_SHA usa origin/HEAD
#   scripts/instala_vigias.sh --comprueba          # sale 0 solo si las tres máquinas están bien
#
# Corre en la Mac. Cada máquina recibe la copia fija en ~/goncloud-vigia (la PC, en
# %LOCALAPPDATA%\goncloud-vigia): se arma en .nuevo, se prueba con --seco y recién entonces se
# cambia por mv; la copia anterior queda como .anterior.

set -uo pipefail

AQUI=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=${REPO:-$(cd "$AQUI/.." && pwd)}
MINI=${MINI:-gon@100.73.187.5}
PC=${PC:-gwpc}
PYTHON_MAC=${PYTHON_MAC:-/opt/homebrew/bin/python3}
PYTHON_MINI=${PYTHON_MINI:-/usr/bin/python3}
PYTHON_PC=${PYTHON_PC:-'%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe'}
LOG=${LOG:-$HOME/Library/Logs/goncloud/instala-vigias.log}
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)
ARCHIVOS=(scripts tareas-programadas.json model-routing.source.json claw-routing.json team.json)
W='%LOCALAPPDATA%\goncloud-vigia'
TAREA_PC=GoncloudVigia

# En la copia y en el rollback: el comando corre en la máquina `local` o por ssh, con stdin intacto.
en() {
  if [ "$1" = local ]; then sh -c "$2"; else ssh "${SSH_OPTS[@]}" "$1" "$2"; fi
}

dueno_de() {
  case $1 in
    mac) echo local ;;
    mini) echo "$MINI" ;;
    pc) echo "$PC" ;;
  esac
}

python_de() {
  case $1 in
    mac) echo "$PYTHON_MAC" ;;
    mini) echo "$PYTHON_MINI" ;;
    pc) echo "$PYTHON_PC" ;;
  esac
}

plist_de() {
  local molde=$1 home=$2 py=$3 maquina=${4:-}
  sed -e "s|@HOME@|$home|g" -e "s|@PYTHON@|$py|g" -e "s|@MAQUINA@|$maquina|g" \
    "$AQUI/ai.goncloud.$molde.plist"
}

# bootout y bootstrap: launchd no recarga un plist que ya está cargado.
carga_plist() {
  local host=$1 etiqueta=$2 plist=$3
  en "$host" "launchctl bootout gui/\$(id -u)/$etiqueta 2>/dev/null; launchctl bootstrap gui/\$(id -u) '$plist'"
}

es_0_o_1() { [ "$1" -eq 0 ] || [ "$1" -eq 1 ]; }

# --seco del vigía de tareas (y del de modelos en la Mac) contra la carpeta `raiz`.
seco_posix() {
  local maquina=$1 host=$2 py=$3 raiz=$4 rc
  en "$host" "PYTHONDONTWRITEBYTECODE=1 '$py' '$raiz/scripts/vigila_tareas.py' --maquina $maquina --raiz '$raiz' --seco"
  rc=$?
  echo "$maquina: vigila_tareas --seco salió con $rc"
  es_0_o_1 "$rc" || { echo "FALLA $maquina: vigila_tareas --seco salió con $rc"; return 1; }
  if [ "$maquina" = mac ]; then
    en "$host" "PYTHONDONTWRITEBYTECODE=1 '$py' '$raiz/scripts/vigila_modelos.py' --raiz '$raiz' --seco"
    rc=$?
    echo "$maquina: vigila_modelos --seco salió con $rc"
    es_0_o_1 "$rc" || { echo "FALLA $maquina: vigila_modelos --seco salió con $rc"; return 1; }
  fi
}

instala_posix() {
  local maquina=$1 host py home dest agentes
  host=$(dueno_de "$maquina") py=$(python_de "$maquina")
  home=$(en "$host" 'printf %s "$HOME"') || { echo "FALLA $maquina: no llegué a la máquina"; return 1; }
  dest=$home/goncloud-vigia agentes=$home/Library/LaunchAgents
  echo "== $maquina: copia fija en $dest desde $SHA"

  git -C "$REPO" archive "$SHA" -- "${ARCHIVOS[@]}" |
    en "$host" "rm -rf '$dest.nuevo' && mkdir -p '$dest.nuevo' '$agentes' '$home/Library/Logs/goncloud' && tar -x -C '$dest.nuevo'" ||
    { echo "FALLA $maquina: no pude copiar los archivos"; return 1; }
  seco_posix "$maquina" "$host" "$py" "$dest.nuevo" || return 1

  plist_de vigila-tareas "$home" "$py" "$maquina" | en "$host" "cat > '$agentes/ai.goncloud.vigila-tareas.plist'" || return 1
  if [ "$maquina" = mac ]; then
    en "$host" "[ -e '$agentes/ai.goncloud.vigila-modelos.plist.anterior' ] || cp '$agentes/ai.goncloud.vigila-modelos.plist' '$agentes/ai.goncloud.vigila-modelos.plist.anterior'"
    plist_de vigila-modelos "$home" "$py" | en "$host" "cat > '$agentes/ai.goncloud.vigila-modelos.plist'" || return 1
    en "$host" "ln -sfn \"\$HOME/.motor/avisos/pendientes\" '$dest.nuevo/avisos'" || return 1
  fi

  en "$host" "rm -rf '$dest.anterior'; { [ ! -d '$dest' ] || mv '$dest' '$dest.anterior'; } && mv '$dest.nuevo' '$dest'" ||
    { echo "FALLA $maquina: no pude cambiar la copia"; return 1; }
  carga_plist "$host" ai.goncloud.vigila-tareas "$agentes/ai.goncloud.vigila-tareas.plist" || { echo "FALLA $maquina: no cargó vigila-tareas"; return 1; }
  if [ "$maquina" = mac ]; then
    carga_plist "$host" ai.goncloud.vigila-modelos "$agentes/ai.goncloud.vigila-modelos.plist" || { echo "FALLA $maquina: no cargó vigila-modelos"; return 1; }
  fi
  echo "OK $maquina: instalada"
}

instala_pc() {
  local py=$PYTHON_PC rc
  echo "== pc: copia fija en $W desde $SHA"
  git -C "$REPO" archive "$SHA" -- "${ARCHIVOS[@]}" |
    ssh "${SSH_OPTS[@]}" "$PC" "(if exist \"$W.nuevo\" rmdir /s /q \"$W.nuevo\") & mkdir \"$W.nuevo\" && tar -xf - -C \"$W.nuevo\"" ||
    { echo "FALLA pc: no pude copiar los archivos"; return 1; }
  ssh "${SSH_OPTS[@]}" "$PC" "\"$py\" \"$W.nuevo\\scripts\\vigila_tareas.py\" --maquina pc --raiz \"$W.nuevo\" --seco"
  rc=$?
  echo "pc: vigila_tareas --seco salió con $rc"
  es_0_o_1 "$rc" || { echo "FALLA pc: vigila_tareas --seco salió con $rc"; return 1; }
  ssh "${SSH_OPTS[@]}" "$PC" "(if exist \"$W.anterior\" rmdir /s /q \"$W.anterior\") & (if exist \"$W\" move \"$W\" \"$W.anterior\" >nul) & move \"$W.nuevo\" \"$W\" >nul" ||
    { echo "FALLA pc: no pude cambiar la copia"; return 1; }
  ssh "${SSH_OPTS[@]}" "$PC" "schtasks /create /f /tn $TAREA_PC /sc minute /mo 30 /tr \"\\\"$py\\\" \\\"$W\\scripts\\vigila_tareas.py\\\" --maquina pc --raiz \\\"$W\\\"\"" ||
    { echo "FALLA pc: no pude crear la tarea $TAREA_PC"; return 1; }
  echo "OK pc: instalada"
}

comprueba_posix() {
  local maquina=$1 host py home dest agentes etiquetas e plist rc malo=0
  host=$(dueno_de "$maquina") py=$(python_de "$maquina")
  home=$(en "$host" 'printf %s "$HOME"') || { echo "FALLA $maquina: no llegué a la máquina"; return 1; }
  dest=$home/goncloud-vigia agentes=$home/Library/LaunchAgents
  etiquetas=ai.goncloud.vigila-tareas
  [ "$maquina" = mac ] && etiquetas="$etiquetas ai.goncloud.vigila-modelos"
  en "$host" "[ -f '$dest/scripts/vigila_tareas.py' ]" || { echo "FALLA $maquina: falta la copia fija $dest"; malo=1; }
  for e in $etiquetas; do
    plist=$agentes/$e.plist
    en "$host" "launchctl print gui/\$(id -u)/$e >/dev/null 2>&1" || { echo "FALLA $maquina: $e no está cargado"; malo=1; }
    en "$host" "grep -q -e '$home/dev' -e '$home/.motor' -e '~/dev' -e '~/.motor' '$plist'" &&
      { echo "FALLA $maquina: $e apunta a ~/dev o ~/.motor"; malo=1; }
  done
  en "$host" "'$py' '$dest/scripts/vigila_tareas.py' --maquina $maquina --raiz '$dest' --seco >/dev/null"
  rc=$?
  es_0_o_1 "$rc" || { echo "FALLA $maquina: una corrida --seco salió con $rc"; malo=1; }
  [ "$malo" -eq 0 ] && echo "OK $maquina"
  return "$malo"
}

comprueba_pc() {
  local malo=0 rc xml
  ssh "${SSH_OPTS[@]}" "$PC" "if not exist \"$W\\scripts\\vigila_tareas.py\" exit 1" || { echo "FALLA pc: falta la copia fija $W"; malo=1; }
  xml=$(ssh "${SSH_OPTS[@]}" "$PC" "schtasks /query /tn $TAREA_PC /xml") || { echo "FALLA pc: la tarea $TAREA_PC no existe"; malo=1; }
  if printf %s "$xml" | grep -q -i -e '\\dev\\' -e '\.motor'; then
    echo "FALLA pc: $TAREA_PC apunta a dev o .motor"; malo=1
  fi
  ssh "${SSH_OPTS[@]}" "$PC" "\"$PYTHON_PC\" \"$W\\scripts\\vigila_tareas.py\" --maquina pc --raiz \"$W\" --seco >nul"
  rc=$?
  es_0_o_1 "$rc" || { echo "FALLA pc: una corrida --seco salió con $rc"; malo=1; }
  [ "$malo" -eq 0 ] && echo "OK pc"
  return "$malo"
}

por_maquina() {
  local f=$1 mal=0 m
  for m in mac mini pc; do
    if [ "$m" = pc ]; then "${f}_pc" || mal=1; else "${f}_posix" "$m" || mal=1; fi
  done
  return "$mal"
}

main() {
  mkdir -p "$(dirname "$LOG")"
  exec > >(tee -a "$LOG") 2>&1
  if [ "${1:-}" = --comprueba ]; then
    por_maquina comprueba
    return
  fi
  SHA=$(git -C "$REPO" rev-parse --verify "${MOTOR_SHA:-origin/HEAD}^{commit}") ||
    { echo "no encuentro ${MOTOR_SHA:-origin/HEAD}"; return 1; }
  echo "$(date '+%F %T') instala_vigias $SHA"
  por_maquina instala
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
