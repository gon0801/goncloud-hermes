#!/usr/bin/env bash
# Revisa que los vigías estén en la Mac, la Mini y la PC. Sale 0 solo si las tres están bien.
exec "$(dirname "$0")/instala_vigias.sh" --comprueba
