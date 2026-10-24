#!/usr/bin/env bash
# A.3: prueba de instala.sh, checklist.sh y desinstala.sh con schtasks, curl, netstat, taskkill y el
# proceso del dashboard falsos (por PATH). Sin tocar la PC. El estado falso vive en $FAKE.
set -uo pipefail
aqui="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dash="$(cd "$aqui/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"

IP=100.92.227.24
PUERTO=9119

cat > "$tmp/bin/schtasks" <<'FAKE'
#!/usr/bin/env bash
echo "schtasks $*" >> "$FAKE/bitacora"
op="${1:-}"
xml_de() { while [ $# -gt 0 ]; do [ "$1" = /XML ] && { echo "$2"; return; }; shift; done; }
case "$op" in
  /Create)
    [ -e "$FAKE/denegado" ] && { echo "ERROR: Access is denied." >&2; exit 1; }
    cp "$(xml_de "$@")" "$FAKE/tarea.xml" ;;
  /Query)
    [ -e "$FAKE/tarea.xml" ] || { echo "ERROR: The system cannot find the file specified." >&2; exit 1; }
    case " $* " in *" /XML "*) tr -d '\377\376\000' < "$FAKE/tarea.xml" ;; esac ;;
  /Delete) rm -f "$FAKE/tarea.xml" ;;
  /End) ;;
  /Run)
    [ -e "$FAKE/tarea.xml" ] || exit 1
    echo "$IP:$PUERTO 222" > "$FAKE/escucha" ;;
esac
exit 0
FAKE

cat > "$tmp/bin/netstat" <<'FAKE'
#!/usr/bin/env bash
printf 'Conexiones activas\r\n\r\n  Proto  Dirección local         Dirección remota       Estado           PID\r\n'
[ -e "$FAKE/escucha" ] && while read -r dir pid; do
  printf '  TCP    %-22s 0.0.0.0:0              ESCUCHANDO      %s\r\n' "$dir" "$pid"
done < "$FAKE/escucha"
printf '  TCP    127.0.0.1:5000         0.0.0.0:0              ESCUCHANDO      999\r\n'
exit 0
FAKE

cat > "$tmp/bin/taskkill" <<'FAKE'
#!/usr/bin/env bash
echo "taskkill $*" >> "$FAKE/bitacora"
pid="$2"
[ -e "$FAKE/escucha" ] && grep -v " $pid\$" "$FAKE/escucha" > "$FAKE/escucha.n"
mv -f "$FAKE/escucha.n" "$FAKE/escucha" 2>/dev/null
exit 0
FAKE

cat > "$tmp/bin/curl" <<'FAKE'
#!/usr/bin/env bash
if [ -s "$FAKE/escucha" ]; then printf 200; else printf 000; fi
exit 0
FAKE

cat > "$tmp/bin/cmd-falso" <<'FAKE'
#!/usr/bin/env bash
echo "cmd $*" >> "$FAKE/bitacora"
echo "$IP:$PUERTO 333" > "$FAKE/escucha"
exit 0
FAKE
chmod +x "$tmp/bin/"*

export PATH="$tmp/bin:$PATH"
export IP PUERTO
export ESPERA_INTENTOS=2 ESPERA_SEGUNDOS=0
export CMD_BIN="$tmp/bin/cmd-falso"

fallos=0
verifica() {
  if "$@"; then echo "ok    $DESC"; else echo "FALLA $DESC"; fallos=$((fallos + 1)); fi
}
d() { DESC="$1"; shift; verifica "$@"; }

nuevo_mundo() {
  export FAKE="$tmp/mundo.$1"
  export STARTUP_DIR="$FAKE/Inicio" RESPALDO_DIR="$FAKE/respaldo"
  mkdir -p "$STARTUP_DIR"
  printf '@echo off\r\nstart "HermesDashboard" hermes.exe\r\n' > "$STARTUP_DIR/hermes-dashboard-claw.cmd"
  echo "$IP:$PUERTO 111" > "$FAKE/escucha"
  : > "$FAKE/bitacora"
}
cuenta() { grep -c -- "$1" "$FAKE/bitacora"; }
n_escuchan() { grep -c . "$FAKE/escucha"; }
xml_txt() { tr -d '\377\376\000\r' < "$FAKE/tarea.xml"; }

nuevo_mundo checklist-vacio
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "el checklist falla antes de instalar" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"

nuevo_mundo instala
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida1" 2>&1; rc1=$?
d "instala sale 0" test "$rc1" -eq 0
d "la tarea tiene BootTrigger" bash -c 'xml_txt() { tr -d "\377\376\000\r" < "$1"; }; xml_txt "$0" | grep -q "<BootTrigger>"' "$FAKE/tarea.xml"
d "la tarea usa S4U" bash -c 'tr -d "\377\376\000\r" < "$0" | grep -q "<LogonType>S4U</LogonType>"' "$FAKE/tarea.xml"
d "la tarea reintenta si falla" bash -c 'tr -d "\377\376\000\r" < "$0" | grep -q "<RestartOnFailure>"' "$FAKE/tarea.xml"
d "la tarea corre el comando tal cual" bash -c 'tr -d "\377\376\000\r" < "$0" | grep -qF -- "--profile claw dashboard --host 100.92.227.24 --port 9119 --no-open --skip-build"' "$FAKE/tarea.xml"
d "la tarea usa hermes.exe del venv" bash -c 'tr -d "\377\376\000\r" < "$0" | grep -qF "hermes-agent\\venv\\Scripts\\hermes.exe"' "$FAKE/tarea.xml"
d "el .cmd viejo salió de Inicio" test ! -e "$STARTUP_DIR/hermes-dashboard-claw.cmd"
d "el .cmd viejo quedó en el respaldo intacto" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "detuvo el proceso viejo del puerto" test "$(cuenta 'taskkill /PID 111 /F')" -eq 1
d "lanzó la tarea" test "$(cuenta 'schtasks /Run')" -eq 1
d "queda un solo proceso escuchando" test "$(n_escuchan)" -eq 1
d "el checklist pasa tras instalar" bash -c 'bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
cp "$FAKE/tarea.xml" "$FAKE/tarea1.xml"

bash "$dash/instala.sh" < /dev/null > "$FAKE/salida2" 2>&1; rc2=$?
d "instala dos veces sale 0" test "$rc2" -eq 0
d "la segunda corrida deja la misma tarea" cmp -s "$FAKE/tarea.xml" "$FAKE/tarea1.xml"
d "la segunda corrida deja el .cmd en el respaldo" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "la segunda corrida sigue con un solo proceso" test "$(n_escuchan)" -eq 1
d "el checklist pasa tras instalar dos veces" bash -c 'bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"

echo "$IP:$PUERTO 222" > "$FAKE/escucha"; echo "$IP:$PUERTO 223" >> "$FAKE/escucha"
d "el checklist falla con dos procesos" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
echo "$IP:$PUERTO 222" > "$FAKE/escucha"; echo "0.0.0.0:$PUERTO 224" >> "$FAKE/escucha"
d "el checklist falla si algo escucha en 0.0.0.0" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
echo "$IP:$PUERTO 222" > "$FAKE/escucha"
cp "$FAKE/cmd-original" "$STARTUP_DIR/hermes-dashboard-claw.cmd"
d "el checklist falla si el .cmd viejo vuelve a Inicio" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
rm -f "$STARTUP_DIR/hermes-dashboard-claw.cmd"
: > "$FAKE/escucha"
d "el checklist falla si el dashboard no responde" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
echo "$IP:$PUERTO 222" > "$FAKE/escucha"

bash "$dash/desinstala.sh" < /dev/null > "$FAKE/salida3" 2>&1; rc3=$?
d "desinstala sale 0" test "$rc3" -eq 0
d "desinstala borra la tarea" test ! -e "$FAKE/tarea.xml"
d "desinstala devuelve el .cmd a Inicio igual que estaba" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "desinstala vacía el respaldo" test ! -e "$RESPALDO_DIR/hermes-dashboard-claw.cmd"
d "desinstala relanza el dashboard con el .cmd" test "$(cuenta '^cmd /c')" -eq 1
d "desinstala deja un solo proceso" test "$(n_escuchan)" -eq 1
d "desinstalar otra vez sale 0" bash -c 'bash "$0" </dev/null >/dev/null 2>&1' "$dash/desinstala.sh"

nuevo_mundo denegado
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
touch "$FAKE/denegado"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida4" 2>&1; rc4=$?
d "acceso denegado sale distinto de 0" test "$rc4" -ne 0
d "acceso denegado dice que hace falta administrador" grep -qi 'administrador' "$FAKE/salida4"
d "acceso denegado no pide contraseña" bash -c '! grep -qiE "^(contrase|password)" "$0"' "$FAKE/salida4"
d "acceso denegado no mueve el .cmd viejo" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "acceso denegado no detiene el dashboard que corre" test "$(cuenta 'taskkill')" -eq 0

echo
if [ "$fallos" -eq 0 ]; then echo "todo bien"; else echo "$fallos pruebas fallaron"; exit 1; fi
