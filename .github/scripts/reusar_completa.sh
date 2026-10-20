#!/usr/bin/env bash
# Reuso de la bateria `espejo-de-g` por arbol de git -- una sola vez por
# bloque. Patron portado de goncloud-Orbit (PR 477), mismo modelo que
# quality.yml de goncloud-openclaw (orden del dueno 2026-10-18).
#
# En push a la rama por defecto (feat/hermes-team): si el ARBOL del commit
# empujado (`git rev-parse HEAD^{tree}`) es identico al arbol de la cabeza
# del PR que lo produjo y esa cabeza ya tiene el check run `espejo-de-g` en
# success, el contenido ya fue validado: mismo arbol = mismo contenido, asi
# que se reusa el veredicto y el job sale success sin repetir las pruebas.
# La cabeza del PR se consulta con la API (commits/{sha}/pulls -> head.sha),
# por lo que sirve tanto para merge commit como para squash.
#
# FAIL-CLOSED hacia CORRER, nunca hacia saltar: si el arbol difiere (la base
# avanzo y no se mezclo), no hay PR asociado (push directo), o cualquier
# consulta falla (gh ausente, red, rate limit), el script cae al mismo
# fallback y el job corre la bateria completa. Este script JAMAS termina en
# error: su unica salida es reusar=true (salta la bateria) o reusar=false
# (la corre completa).
#
# Fijado por tests/test_sistema_agentes_ci_una_vez.py.

set -u

reusar=false
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  trap 'printf "reusar=%s\n" "$reusar" >> "$GITHUB_OUTPUT"' EXIT
fi

terminar() {
  # $1: razon por la que NO se reusa. Sale 0 para que el job siga y corra.
  echo "reuso: $1 -- se corre la bateria completa" >&2
  exit 0
}

command -v gh >/dev/null 2>&1 || terminar "gh no esta disponible"
command -v jq >/dev/null 2>&1 || terminar "jq no esta disponible"

repo="${GH_REPO:-${GITHUB_REPOSITORY:-}}"
[ -n "$repo" ] || terminar "sin repositorio (GH_REPO/GITHUB_REPOSITORY)"

sha="$(git rev-parse HEAD)" || terminar "no hay HEAD"
arbol="$(git rev-parse 'HEAD^{tree}')" || terminar "no se pudo leer el arbol de HEAD"
echo "reuso: arbol empujado $arbol (commit $sha)"

# Cabeza del PR asociado al commit empujado (merge commit o squash).
if ! cuerpo="$(gh api "repos/$repo/commits/$sha/pulls" 2>/dev/null)"; then
  terminar "la consulta del PR fallo"
fi
cabeza="$(printf '%s' "$cuerpo" | jq -r '([.[] | select(.head.sha != null)][0].head.sha) // empty' 2>/dev/null)" \
  || terminar "la respuesta del PR no se pudo leer"
[ -n "$cabeza" ] || terminar "sin PR asociado al commit empujado"

# Arbol de la cabeza: por API porque el checkout de push puede no traer el
# objeto de la cabeza del PR (fetch-depth por defecto).
arbol_cabeza="$(gh api "repos/$repo/commits/$cabeza" 2>/dev/null | jq -r '.commit.tree.sha // empty' 2>/dev/null)" \
  || terminar "la consulta del arbol de la cabeza fallo"
[ -n "$arbol_cabeza" ] || terminar "la respuesta del arbol de la cabeza vino vacia"
if [ "$arbol" != "$arbol_cabeza" ]; then
  terminar "el arbol difiere de la cabeza del PR ($cabeza, arbol $arbol_cabeza)"
fi

if ! cuerpo="$(gh api "repos/$repo/commits/$cabeza/check-runs" 2>/dev/null)"; then
  terminar "la consulta de check runs fallo"
fi
veredicto="$(printf '%s' "$cuerpo" | jq -r '([.check_runs[]? | select(.name == "espejo-de-g")][0].conclusion) // empty' 2>/dev/null)" \
  || terminar "la respuesta de check runs no se pudo leer"
[ "$veredicto" = "success" ] || terminar "la cabeza $cabeza no tiene espejo-de-g en success (veredicto: ${veredicto:-ninguno})"
run_url="$(printf '%s' "$cuerpo" | jq -r '([.check_runs[]? | select(.name == "espejo-de-g")][0].html_url) // empty' 2>/dev/null || true)"

reusar=true
echo "reuso: ARBOL IDENTICO ya validado -- se reusa el veredicto; cabeza del PR: $cabeza"
echo "reuso: check run reusado: ${run_url:-<sin url>}"
