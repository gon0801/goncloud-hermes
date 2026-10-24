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
    [ -e "$FAKE/falla-create" ] && { echo "ERROR: algo raro." >&2; exit 1; }
    [ "$(head -c 4 "$(xml_de "$@")" | od -An -tx1 | tr -d ' \n')" = fffe3c00 ] || { echo "ERROR: The task XML is malformed." >&2; exit 1; }
    if [ -e "$FAKE/tarea.xml" ]; then case " $* " in *" /F "*) ;; *) echo "ERROR: Cannot create a file when that file already exists." >&2; exit 1 ;; esac; fi
    cp "$(xml_de "$@")" "$FAKE/tarea.xml" ;;
  /Query)
    [ -e "$FAKE/falla-query" ] && { echo "ERROR: Access is denied." >&2; exit 1; }
    [ -e "$FAKE/tarea.xml" ] || { echo "ERROR: The system cannot find the file specified." >&2; exit 1; }
    case " $* " in *" /XML "*) tr -d '\377\376\000' < "$FAKE/tarea.xml" ;; esac ;;
  /Delete)
    echo "borrada" >> "$FAKE/bitacora-delete"
    [ -e "$FAKE/falla-delete" ] && { echo "ERROR: Access is denied." >&2; exit 1; }
    [ -e "$FAKE/delete-sin-borrar" ] || rm -f "$FAKE/tarea.xml" ;;
  /End) ;;
  /Run)
    [ -e "$FAKE/tarea.xml" ] || exit 1
    [ -e "$FAKE/falla-run" ] && { echo "ERROR: no se pudo ejecutar." >&2; exit 1; }
    echo "$IP:$PUERTO 222" > "$FAKE/escucha" ;;
esac
exit 0
FAKE

cat > "$tmp/bin/netstat" <<'FAKE'
#!/usr/bin/env bash
if [ -e "$FAKE/netstat-falla-segunda" ]; then
  llamadas=$(($(cat "$FAKE/netstat-llamadas" 2>/dev/null || printf 0) + 1))
  printf '%s\n' "$llamadas" > "$FAKE/netstat-llamadas"
  [ "$llamadas" -eq 2 ] && exit 1
fi
printf 'Conexiones activas\r\n\r\n  Proto  Dirección local         Dirección remota       Estado           PID\r\n'
[ -e "$FAKE/escucha" ] && while read -r dir pid; do
  printf '  TCP    %-22s 0.0.0.0:0              ESCUCHANDO      %s\r\n' "$dir" "$pid"
done < "$FAKE/escucha"
printf '  TCP    %s:%s   10.0.0.9:50000   ESTABLECIDO     777
' "$IP" "$PUERTO"
printf '  TCP    127.0.0.1:5000         0.0.0.0:0              ESCUCHANDO      999\r\n'
exit 0
FAKE

cat > "$tmp/bin/taskkill" <<'FAKE'
#!/usr/bin/env bash
echo "taskkill $*" >> "$FAKE/bitacora"
[ -e "$FAKE/taskkill-denegado" ] && { echo "ERROR: Access is denied." >&2; exit 1; }
pid="$2"
[ -e "$FAKE/escucha" ] && grep -v " $pid\$" "$FAKE/escucha" > "$FAKE/escucha.n"
mv -f "$FAKE/escucha.n" "$FAKE/escucha" 2>/dev/null
exit 0
FAKE

cat > "$tmp/bin/curl" <<'FAKE'
#!/usr/bin/env bash
echo "curl $*" >> "$FAKE/bitacora"
if [ -s "$FAKE/escucha" ]; then printf 200; else printf 000; fi
exit 0
FAKE

cat > "$tmp/bin/cmd-falso" <<'FAKE'
#!/usr/bin/env bash
echo "cmd $*" >> "$FAKE/bitacora"
if [ -e "$FAKE/cmd-no-arranca" ]; then
  :
elif [ -e "$FAKE/cmd-comodin" ]; then
  echo "0.0.0.0:$PUERTO 333" >> "$FAKE/escucha"
else
  echo "$IP:$PUERTO 333" >> "$FAKE/escucha"
fi
exit 0
FAKE

cat > "$tmp/bin/mv" <<'FAKE'
#!/usr/bin/env bash
if [ -e "$FAKE/falla-movimiento-cmd" ] && [ "${2:-}" = "$STARTUP_DIR/hermes-dashboard-claw.cmd" ]; then
  echo "mv simulado: no se pudo mover el .cmd" >&2
  exit 1
fi
exec /usr/bin/mv "$@"
FAKE

cat > "$tmp/bin/date" <<'FAKE'
#!/usr/bin/env bash
if [ "${1:-}" = +%Y%m%d%H%M%S ] && [ -n "${FECHA_RESPALDO:-}" ]; then
  printf '%s\n' "$FECHA_RESPALDO"
else
  exec /usr/bin/date "$@"
fi
FAKE
chmod +x "$tmp/bin/"*

export PATH="$tmp/bin:$PATH"
[ "$(command -v schtasks)" = "$tmp/bin/schtasks" ] || { echo "ABORTA: schtasks no es el falso"; exit 2; }
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
d "la tarea corre el comando tal cual" bash -c 'argumentos=$(tr -d "\377\376\000\r" < "$1" | sed -n "s|.*<Arguments>\(.*\)</Arguments>.*|\1|p"); [ "$argumentos" = "$2" ]' _ "$FAKE/tarea.xml" "--profile claw dashboard --host 100.92.227.24 --port 9119 --no-open --skip-build"
d "la tarea usa hermes.exe del venv" bash -c 'tr -d "\377\376\000\r" < "$0" | grep -qF "hermes-agent\\venv\\Scripts\\hermes.exe"' "$FAKE/tarea.xml"
d "el .cmd viejo salió de Inicio" test ! -e "$STARTUP_DIR/hermes-dashboard-claw.cmd"
d "el .cmd viejo quedó en el respaldo intacto" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "detuvo el proceso viejo del puerto" test "$(cuenta 'taskkill /PID 111 /T /F')" -eq 1
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
echo "$IP:$PUERTO 222" > "$FAKE/escucha"; echo "[::]:$PUERTO 225" >> "$FAKE/escucha"
d "el checklist falla si algo escucha en [::]" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
echo "$IP:$PUERTO 222" > "$FAKE/escucha"; echo "127.0.0.1:$PUERTO 226" >> "$FAKE/escucha"
d "el checklist falla si algo escucha en otra IP del puerto" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
echo "$IP:$PUERTO 222" > "$FAKE/escucha"
cp "$FAKE/cmd-original" "$STARTUP_DIR/hermes-dashboard-claw.cmd"
d "el checklist falla si el .cmd viejo vuelve a Inicio" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
rm -f "$STARTUP_DIR/hermes-dashboard-claw.cmd"
: > "$FAKE/escucha"
d "el checklist falla si el dashboard no responde" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
echo "$IP:$PUERTO 222" > "$FAKE/escucha"

: > "$FAKE/bitacora"
bash "$dash/desinstala.sh" < /dev/null > "$FAKE/salida3" 2>&1; rc3=$?
d "desinstala sale 0" test "$rc3" -eq 0
d "desinstala borra la tarea" test ! -e "$FAKE/tarea.xml"
d "desinstala devuelve el .cmd a Inicio igual que estaba" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "desinstala vacía el respaldo" test ! -e "$RESPALDO_DIR/hermes-dashboard-claw.cmd"
d "desinstala relanza el dashboard con el .cmd" test "$(cuenta '^cmd /c')" -eq 1
d "desinstala detiene el proceso que ocupa el puerto" test "$(cuenta 'taskkill /PID 222 /T /F')" -eq 1
d "desinstala deja un solo proceso" test "$(n_escuchan)" -eq 1
d "desinstalar otra vez sale 0" bash -c 'bash "$0" </dev/null >/dev/null 2>&1' "$dash/desinstala.sh"

nuevo_mundo denegado
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
touch "$FAKE/denegado"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida4" 2>&1; rc4=$?
d "acceso denegado sale distinto de 0" test "$rc4" -ne 0
d "acceso denegado dice que hace falta administrador" grep -qi 'administrador' "$FAKE/salida4"
d "acceso denegado no pide contraseña" bash -c '! grep -qiE "^(contrase|password)" "$0"' "$FAKE/salida4"
d "instala no pasa contraseña ni usuario a schtasks /Create" bash -c '! grep -E "^schtasks /Create" "$0" | grep -qE "/(RP|RU)( |$)"' "$FAKE/bitacora"
d "acceso denegado no mueve el .cmd viejo" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "acceso denegado no detiene el dashboard que corre" test "$(cuenta 'taskkill')" -eq 0

nuevo_mundo respaldo-imposible
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
: > "$FAKE/archivo"
RESPALDO_DIR="$FAKE/archivo/r" bash "$dash/instala.sh" < /dev/null > "$FAKE/salida7" 2>&1; rc7=$?
d "respaldo imposible sale distinto de 0" test "$rc7" -ne 0
d "respaldo imposible deja el .cmd en Inicio" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "respaldo imposible no lanza la tarea" test "$(cuenta 'schtasks /Run')" -eq 0
d "respaldo imposible lo dice claro" grep -q 'no se pudo mover' "$FAKE/salida7"

d "la espera por defecto es de al menos 60 s" bash -c 'unset ESPERA_INTENTOS ESPERA_SEGUNDOS; . "$0"; [ "$ESPERA_TOTAL" -ge 60 ]' "$dash/comun.sh"

nuevo_mundo xml-escapado
HERMES_EXE='C:\a&b\hermes.exe' bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
d "el XML escapa & en la ruta" bash -c 'tr -d "\377\376\000\r" < "$0" | grep -qF "a&amp;b"' "$FAKE/tarea.xml"

d "los scripts no leen stdin" bash -c '! grep -nE "(^|[;&| ])(read|select)( |$)|/dev/stdin|<&0" "$0"/instala.sh "$0"/desinstala.sh "$0"/checklist.sh "$0"/comun.sh | grep -v "while \[ \$#" ' "$dash"
nuevo_mundo sin-stdin
bash "$dash/instala.sh" <&- > "$FAKE/salida5" 2>&1; rc5=$?
d "instala con stdin cerrado sale 0" test "$rc5" -eq 0

nuevo_mundo no-responde
: > "$FAKE/sin-curl"
cat > "$tmp/bin/curl" <<'FAKE'
#!/usr/bin/env bash
echo "curl $*" >> "$FAKE/bitacora"
if [ -e "$FAKE/sin-curl" ]; then printf 000; elif [ -s "$FAKE/escucha" ]; then printf "${CODIGO:-200}"; else printf 000; fi
exit 0
FAKE
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida6" 2>&1; rc6=$?
d "instala sale 4 si el dashboard no responde" test "$rc6" -eq 4
rm -f "$FAKE/sin-curl"
d "checklist acepta 302" env CODIGO=302 bash "$dash/checklist.sh"
d "checklist acepta 401" env CODIGO=401 bash "$dash/checklist.sh"
d "checklist rechaza 404" bash -c '! CODIGO=404 bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
d "checklist rechaza 500" bash -c '! CODIGO=500 bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
d "instala detiene la tarea antes de relanzar" test "$(cuenta 'schtasks /End')" -ge 1
for quita in BootTrigger S4U; do
  nuevo_mundo "sin-$quita"
  bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
  tr -d '\377\376\000' < "$FAKE/tarea.xml" | sed "s/$quita/Otro/g" > "$FAKE/tarea.n"; mv "$FAKE/tarea.n" "$FAKE/tarea.xml"
  d "checklist falla si la tarea no tiene $quita" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"
done
nuevo_mundo sin-tarea
rm -f "$STARTUP_DIR/hermes-dashboard-claw.cmd"
d "checklist falla si la tarea no existe" bash -c '! bash "$0" >/dev/null 2>&1' "$dash/checklist.sh"

nuevo_mundo extras
d "la tarea declara UserId" bash -c 'bash "$0" </dev/null >/dev/null 2>&1; tr -d "\377\376\000\r" < "$FAKE/tarea.xml" | grep -q "<UserId>.*ehven</UserId>"' "$dash/instala.sh"
d "curl va con --noproxy" grep -q 'curl .*--noproxy' "$FAKE/bitacora"
d "el XML llega en UTF-16LE con BOM (el schtasks falso lo exige)" test -e "$FAKE/tarea.xml"

nuevo_mundo respaldo-sin-tarea
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
: > "$FAKE/archivo"
RESPALDO_DIR="$FAKE/archivo/r" bash "$dash/instala.sh" < /dev/null > /dev/null 2>&1
d "respaldo imposible no deja la tarea registrada" test ! -e "$FAKE/tarea.xml"
d "respaldo imposible deja el .cmd en Inicio (sin tarea)" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"

nuevo_mundo taskkill-denegado
touch "$FAKE/taskkill-denegado"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "taskkill denegado: instala sale distinto de 0" test "$rc" -ne 0
d "taskkill denegado: dice que no pudo liberar el puerto" grep -q 'no se pudo liberar' "$FAKE/salida"
d "taskkill denegado: no lanza la tarea" test "$(cuenta 'schtasks /Run')" -eq 0

nuevo_mundo falla-run
touch "$FAKE/falla-run"
bash "$dash/instala.sh" < /dev/null > /dev/null 2>&1; rc=$?
d "/Run fallido: instala sale 3" test "$rc" -eq 3

nuevo_mundo falla-create
touch "$FAKE/falla-create"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "/Create fallido por otra causa: instala sale 2" test "$rc" -eq 2
d "/Create fallido: no lanza la tarea" test "$(cuenta 'schtasks /Run')" -eq 0

nuevo_mundo espera-real
: > "$FAKE/sin-curl"
inicio=$SECONDS
ESPERA_INTENTOS=2 ESPERA_SEGUNDOS=1 bash "$dash/instala.sh" < /dev/null > /dev/null 2>&1; rc=$?
d "la espera respeta el plazo (sale 4 tras >= 2 s)" test "$rc" -eq 4 -a $((SECONDS - inicio)) -ge 2
rm -f "$FAKE/sin-curl"

nuevo_mundo desinstala-falla-delete
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
touch "$FAKE/falla-delete"
bash "$dash/desinstala.sh" < /dev/null >/dev/null 2>&1; rc=$?
d "desinstala sale distinto de 0 si /Delete falla" test "$rc" -ne 0

nuevo_mundo desinstala-no-arranca
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
touch "$FAKE/cmd-no-arranca"
bash "$dash/desinstala.sh" < /dev/null >/dev/null 2>&1; rc=$?
d "desinstala sale 4 si el relanzamiento no responde" test "$rc" -eq 4

nuevo_mundo desinstala-sin-cmd
rm -f "$STARTUP_DIR/hermes-dashboard-claw.cmd"
: > "$FAKE/bitacora"
bash "$dash/desinstala.sh" < /dev/null >/dev/null 2>&1
d "desinstala sin .cmd no mata el dashboard que corre" test "$(cuenta taskkill)" -eq 0

nuevo_mundo desinstala-inicio-no-escribible
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
: > "$FAKE/archivo"
STARTUP_DIR="$FAKE/archivo/Inicio" bash "$dash/desinstala.sh" < /dev/null >/dev/null 2>&1; rc=$?
d "Inicio no escribible: desinstala sale distinto de 0" test "$rc" -ne 0
d "Inicio no escribible: el .cmd sigue en el respaldo" test -e "$RESPALDO_DIR/hermes-dashboard-claw.cmd"
d "Inicio no escribible: la tarea no se borra" test -e "$FAKE/tarea.xml"

nuevo_mundo respaldo-distinto
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
printf 'otro cmd
' > "$STARTUP_DIR/hermes-dashboard-claw.cmd"
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-nuevo"
printf 'respaldo previo
' > "$RESPALDO_DIR/hermes-dashboard-claw.cmd"
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
d "un respaldo distinto no se pierde" bash -c 'grep -l "respaldo previo" "$0"/hermes-dashboard-claw.cmd.* >/dev/null' "$RESPALDO_DIR"
d "el .cmd nuevo queda en el respaldo" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-nuevo"

nuevo_mundo instala-comodin-ipv4
echo "0.0.0.0:$PUERTO 4242" > "$FAKE/escucha"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "instala sale 0 tras liberar un listener solo en 0.0.0.0" test "$rc" -eq 0
d "instala mata el PID que escucha solo en 0.0.0.0" test "$(cuenta 'taskkill /PID 4242 /T /F')" -eq 1
d "instala deja un solo listener tras liberar 0.0.0.0" test "$(n_escuchan)" -eq 1

nuevo_mundo instala-comodin-ipv4-persistente
echo "0.0.0.0:$PUERTO 4242" > "$FAKE/escucha"
touch "$FAKE/taskkill-denegado"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "un listener 0.0.0.0 persistente impide aceptar el HTTP como éxito" test "$rc" -ne 0
d "instala intenta matar el PID que escucha solo en 0.0.0.0" test "$(cuenta 'taskkill /PID 4242 /T /F')" -eq 1

nuevo_mundo instala-comodin-ipv6
echo "[::]:$PUERTO 4343" > "$FAKE/escucha"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "instala sale 0 tras liberar un listener solo en [::]" test "$rc" -eq 0
d "instala mata el PID que escucha solo en [::]" test "$(cuenta 'taskkill /PID 4343 /T /F')" -eq 1
d "instala deja un solo listener tras liberar [::]" test "$(n_escuchan)" -eq 1

nuevo_mundo instala-ip-especifica-ajena
echo "10.0.0.2:$PUERTO 4444" > "$FAKE/escucha"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "una IP específica ajena impide informar éxito" test "$rc" -ne 0
d "instala no mata un listener de otra IP específica" test "$(cuenta 'taskkill /PID 4444 /T /F')" -eq 0
d "instala no lanza la tarea si queda un listener ajeno" test "$(cuenta 'schtasks /Run')" -eq 0

nuevo_mundo checklist-netstat-falla
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
: > "$FAKE/netstat-llamadas"
touch "$FAKE/netstat-falla-segunda"
bash "$dash/checklist.sh" > "$FAKE/salida" 2>&1; rc=$?
d "checklist falla si no puede consultar listeners ajenos" test "$rc" -ne 0
d "checklist informa el fallo al consultar listeners ajenos" grep -q 'no se pudo consultar si algo escucha' "$FAKE/salida"

nuevo_mundo respaldo-fechado-ocupado
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
mkdir -p "$RESPALDO_DIR"
printf 'anterior\n' > "$RESPALDO_DIR/hermes-dashboard-claw.cmd"
printf 'historico\n' > "$RESPALDO_DIR/hermes-dashboard-claw.cmd.20261024123456"
cp "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/respaldo-anterior"
cp "$RESPALDO_DIR/hermes-dashboard-claw.cmd.20261024123456" "$FAKE/historico-original"
FECHA_RESPALDO=20261024123456 bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "un respaldo fechado ocupado no impide instalar" test "$rc" -eq 0
d "la rotación conserva el respaldo histórico ocupado" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd.20261024123456" "$FAKE/historico-original"
d "la rotación elige un nombre libre para el respaldo anterior" bash -c 'for archivo in "$1"/hermes-dashboard-claw.cmd.*; do if [ "$archivo" != "$1/hermes-dashboard-claw.cmd.$2" ] && cmp -s "$archivo" "$3"; then exit 0; fi; done; exit 1' _ "$RESPALDO_DIR" 20261024123456 "$FAKE/respaldo-anterior"
d "el .cmd nuevo queda en el respaldo principal tras la colisión" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"

nuevo_mundo instala-movimiento-y-delete-fallan
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
touch "$FAKE/falla-movimiento-cmd" "$FAKE/falla-delete"
bash "$dash/instala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "movimiento y /Delete fallidos hacen fallar instala" test "$rc" -ne 0
d "movimiento y /Delete fallidos no dejan tarea residual" test ! -e "$FAKE/tarea.xml"
d "movimiento y /Delete fallidos conservan el .cmd" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "movimiento y /Delete fallidos no anuncian un borrado inexistente" bash -c '! grep -q "se borra la tarea" "$1"' _ "$FAKE/salida"
d "movimiento y /Delete fallidos no lanzan la tarea" test "$(cuenta 'schtasks /Run')" -eq 0

nuevo_mundo desinstala-query-indeterminado
bash "$dash/instala.sh" < /dev/null > /dev/null 2>&1
cp "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
touch "$FAKE/falla-query"
: > "$FAKE/bitacora"
bash "$dash/desinstala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "un /Query indeterminado hace fallar desinstala" test "$rc" -ne 0
d "un /Query indeterminado no borra la tarea" test -e "$FAKE/tarea.xml"
d "un /Query indeterminado devuelve el .cmd al respaldo" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "un /Query indeterminado no deja el .cmd en Inicio" test ! -e "$STARTUP_DIR/hermes-dashboard-claw.cmd"
d "un /Query indeterminado no relanza el dashboard" test "$(cuenta '^cmd /c')" -eq 0

nuevo_mundo desinstala-query-ausente
mkdir -p "$RESPALDO_DIR"
cp "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
mv "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$RESPALDO_DIR/hermes-dashboard-claw.cmd"
: > "$FAKE/bitacora"
bash "$dash/desinstala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "la ausencia real en /Query permite terminar desinstala" test "$rc" -eq 0
d "la ausencia real en /Query devuelve el .cmd a Inicio" cmp -s "$STARTUP_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "la ausencia real en /Query vacía el respaldo" test ! -e "$RESPALDO_DIR/hermes-dashboard-claw.cmd"
d "la ausencia real en /Query relanza el dashboard" test "$(cuenta '^cmd /c')" -eq 1

nuevo_mundo desinstala-delete-sin-borrar
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
cp "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
touch "$FAKE/delete-sin-borrar"
: > "$FAKE/bitacora"
bash "$dash/desinstala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "un /Delete sin borrar hace fallar desinstala" test "$rc" -ne 0
d "un /Delete sin borrar devuelve el .cmd al respaldo" cmp -s "$RESPALDO_DIR/hermes-dashboard-claw.cmd" "$FAKE/cmd-original"
d "un /Delete sin borrar no relanza el dashboard" test "$(cuenta '^cmd /c')" -eq 0

nuevo_mundo desinstala-http-comodin
bash "$dash/instala.sh" < /dev/null >/dev/null 2>&1
touch "$FAKE/cmd-comodin"
bash "$dash/desinstala.sh" < /dev/null > "$FAKE/salida" 2>&1; rc=$?
d "desinstala no acepta HTTP de un listener comodín" test "$rc" -ne 0

echo
if [ "$fallos" -eq 0 ]; then echo "todo bien"; else echo "$fallos pruebas fallaron"; exit 1; fi
