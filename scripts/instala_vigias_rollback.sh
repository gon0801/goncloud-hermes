#!/usr/bin/env bash
# Deshace instala_vigias.sh: pone la copia .anterior donde exista y quita los vigías nuevos.
# En la Mac también devuelve el plist viejo de vigila-modelos.

# shellcheck source=instala_vigias.sh
. "$(dirname "${BASH_SOURCE[0]}")/instala_vigias.sh"

vuelve_posix() {
  local maquina=$1 host home dest agentes tareas
  host=$(dueno_de "$maquina")
  home=$(en "$host" 'printf %s "$HOME"') || { echo "FALLA $maquina: no llegué a la máquina"; return 1; }
  dest=$home/goncloud-vigia agentes=$home/Library/LaunchAgents
  tareas=$agentes/ai.goncloud.vigila-tareas.plist
  en "$host" "launchctl bootout gui/\$(id -u)/ai.goncloud.vigila-tareas 2>/dev/null; rm -f '$tareas'"
  if [ "$maquina" = mac ]; then
    en "$host" "if [ -e '$agentes/ai.goncloud.vigila-modelos.plist.anterior' ]; then launchctl bootout gui/\$(id -u)/ai.goncloud.vigila-modelos 2>/dev/null; mv '$agentes/ai.goncloud.vigila-modelos.plist.anterior' '$agentes/ai.goncloud.vigila-modelos.plist' && launchctl bootstrap gui/\$(id -u) '$agentes/ai.goncloud.vigila-modelos.plist'; fi" ||
      { echo "FALLA $maquina: no pude devolver el plist de vigila-modelos"; return 1; }
  fi
  en "$host" "rm -rf '$dest.nuevo'; if [ -d '$dest.anterior' ]; then rm -rf '$dest' && mv '$dest.anterior' '$dest'; else rm -rf '$dest'; fi" ||
    { echo "FALLA $maquina: no pude devolver la copia"; return 1; }
  echo "OK $maquina: vuelta atrás"
}

vuelve_pc() {
  ssh "${SSH_OPTS[@]}" "$PC" "(schtasks /delete /f /tn $TAREA_PC >nul 2>&1) & (if exist \"$W.nuevo\" rmdir /s /q \"$W.nuevo\") & (if exist \"$W.anterior\" (rmdir /s /q \"$W\" & move \"$W.anterior\" \"$W\" >nul) else (if exist \"$W\" rmdir /s /q \"$W\"))" ||
    { echo "FALLA pc: no pude devolver la copia"; return 1; }
  echo "OK pc: vuelta atrás"
}

main_rollback() {
  local mal=0 m
  mkdir -p "$(dirname "$LOG")"
  exec > >(tee -a "$LOG") 2>&1
  for m in mac mini pc; do
    if [ "$m" = pc ]; then vuelve_pc || mal=1; else vuelve_posix "$m" || mal=1; fi
  done
  return "$mal"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main_rollback "$@"; fi
