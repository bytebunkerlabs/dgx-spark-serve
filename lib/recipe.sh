# shellcheck shell=bash disable=SC2034  # RECIPE_* are for the scripts that source this
# lib/recipe.sh: finding a recipe, and its variant for a platform.
#
# Recipes v2 are a folder per model, one file per platform:
#   recipes/<name>/model.env     what every platform shares: MODEL, ROLES, DIALECT_*
#   recipes/<name>/dgx.env       each sources model.env (. "$RECIPE_DIR/model.env")
#   recipes/<name>/linux.env     and sets ENGINE, the IMAGE or ARTIFACT, SERVE_ARGS
#   recipes/<name>/windows.env
#   recipes/<name>/mac.env
# A flat recipes/<name>.env from before 1.0 is a vLLM container recipe: it
# serves on a DGX Spark, and on NVIDIA Linux (a rented box, TOPOLOGY=solo).
#
# Recipes are found in $DGX_SERVE_CONFIG/recipes first (your own, outside the
# checkout, kept across updates), then in the checkout's recipes/. Bash 3.2.

PLATFORMS="dgx linux windows mac"
FLAT_PLATFORMS="dgx linux"

# Every recipe name, once (a name of yours hides the checkout's).
recipe_names() {
  local d f n seen=" "
  for d in "$DGX_SERVE_CONFIG/recipes" "${RACK_ROOT:-.}/recipes"; do
    [ -d "$d" ] || continue
    for f in "$d"/*/model.env "$d"/*.env; do
      [ -f "$f" ] || continue
      case "$f" in */model.env) n=$(basename "$(dirname "$f")") ;; *) n=$(basename "$f" .env) ;; esac
      case "$n" in TEMPLATE*) continue ;; esac
      case "$seen" in *" $n "*) continue ;; esac
      seen="$seen$n "
      printf '%s\n' "$n"
    done
  done | sort
}

# Where a recipe lives: its folder (v2) or its file (flat). Empty if none.
recipe_location() {
  local d
  case "$1" in TEMPLATE*|'') return 0 ;; esac
  for d in "$DGX_SERVE_CONFIG/recipes" "${RACK_ROOT:-.}/recipes"; do
    [ -f "$d/$1/model.env" ] && { printf '%s' "$d/$1"; return; }
    [ -f "$d/$1.env" ] && { printf '%s' "$d/$1.env"; return; }
  done
  return 0
}

# The platforms a recipe has a variant for, in PLATFORMS order.
recipe_platforms() {
  local loc p out=""
  loc=$(recipe_location "$1")
  case "$loc" in
    '') return 0 ;;
    *.env) printf '%s' "$FLAT_PLATFORMS"; return ;;
  esac
  for p in $PLATFORMS; do [ -f "$loc/$p.env" ] && out="${out:+$out }$p"; done
  printf '%s' "$out"
}

# A name, or a unique prefix of one: `rack up inkling` finds inkling-small-nvfp4.
recipe_name_of() {
  local r=$1 n matches="" count=0
  case "$r" in TEMPLATE*) die "$r is the scaffold, not a recipe: rack new <name> <org/model>" ;; esac
  [ -n "$(recipe_location "$r")" ] && { printf '%s' "$r"; return; }
  for n in $(recipe_names); do
    case "$n" in "$r"*) matches="${matches:+$matches }$n"; count=$((count + 1)) ;; esac
  done
  [ "$count" -gt 0 ] || die "no such recipe: $r  (rack recipes)"
  [ "$count" -eq 1 ] || die "ambiguous: $r matches $matches"
  printf '%s' "$matches"
}

# recipe_resolve <name|prefix|path> <platform>: find the file to source for
# that platform, or die with the reason there is none and how to add it. Sets
# RECIPE_NAME, RECIPE_FILE and RECIPE_DIR (what a variant's
# `. "$RECIPE_DIR/model.env"` reads). Call it directly, not in $( ).
recipe_resolve() {
  local r=$1 plat=$2 name loc have
  RECIPE_NAME="" RECIPE_DIR="" RECIPE_FILE=""
  if [ -f "$r" ] && [ "${r%.env}" != "$r" ]; then        # a path, as before 1.0
    RECIPE_FILE=$r RECIPE_DIR=$(dirname "$r")
    case "$(basename "$r")" in
      dgx.env|linux.env|windows.env|mac.env)
        RECIPE_NAME=$(basename "$RECIPE_DIR")
        [ "$(basename "$r" .env)" = "$plat" ] || die "$r is the $(basename "$r" .env) variant, not $plat" ;;
      model.env) die "$r holds what the variants share; serve a variant: $RECIPE_DIR/$plat.env" ;;
      *) RECIPE_NAME=$(basename "$r" .env)
         case " $FLAT_PLATFORMS " in *" $plat "*) ;; *) die "$(flat_refusal "$RECIPE_NAME" "$plat")" ;; esac ;;
    esac
    return 0
  fi
  name=$(recipe_name_of "$r")
  loc=$(recipe_location "$name")
  RECIPE_NAME=$name
  case "$loc" in
    *.env)
      case " $FLAT_PLATFORMS " in *" $plat "*) ;; *) die "$(flat_refusal "$name" "$plat")" ;; esac
      RECIPE_DIR=$(dirname "$loc") RECIPE_FILE=$loc ;;
    *)
      RECIPE_DIR=$loc RECIPE_FILE=$loc/$plat.env
      if [ ! -f "$RECIPE_FILE" ]; then
        have=$(recipe_platforms "$name")
        die "$name has no $plat variant (it has: $(printf '%s' "${have:-none}" | sed 's/ /, /g')). Add one: rack new $name --$plat"
      fi ;;
  esac
  return 0
}

flat_refusal() {
  printf '%s is a vLLM container recipe from before 1.0 (one file): it serves on a DGX Spark or NVIDIA Linux, not %s. Give it a %s variant: rack new %s --%s' \
    "$1" "$(platform_title "$2")" "$2" "$1" "$2"
}

# One line per recipe for py/recipes.py: name, location, and whose it is.
recipe_index() {
  local n loc kind
  for n in "$@"; do
    loc=$(recipe_location "$n")
    case "$loc" in "$DGX_SERVE_CONFIG"/*) kind=mine ;; *) kind=repo ;; esac
    printf '%s\t%s\t%s\n' "$n" "$loc" "$kind"
  done
}
