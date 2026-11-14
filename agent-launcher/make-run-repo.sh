#!/usr/bin/env bash
# Build a clean run repo from the HEAD commit of a prep repo.
#
# Usage: make-run-repo.sh SRC DEST [--replace] [--agents-from FILE] [--exclude NAME]... [--rewrite PATH]
#
# The convention: everything the agents should not see lives at the top of the prep repo
# with a name starting "__" (launcher scripts, notes, drafts). The run repo is the rest.
#
# - Source is HEAD of SRC; refuses if tracked files have uncommitted changes.
# - DEST must be missing or empty, unless --replace is given. --replace only wipes a run repo
#   this script built (its own git repo whose HEAD is "Run repo from prep ..."), and even then
#   refuses if DEST/threads.tsv exists (a run has started there).
# - Copies tracked files except top-level names starting with "__" and each --exclude NAME
#   (top level). Symlinks and exec bits are preserved (git archive).
# - --agents-from FILE: DEST/AGENTS.md becomes a regular-file copy of SRC's FILE (for a prep
#   repo that keeps, say, AGENTS_REAL.md and a draft); FILE and SRC's AGENTS.md are not copied.
# - --rewrite PATH: replaces PATH (not followed by a name character) with DEST in text files,
#   for prep files that name the run repo's usual location.
# - git init -b main and one commit "Run repo from prep <short sha>".
# - Prints a summary and any leftovers (exit 2 if any are found): mentions of SRC's path,
#   top-level "__" items, and mentions of SRC's top-level "__" names (files the run repo lacks).
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
# Escape a literal string for grep -E / sed -E. sed also needs the "#" delimiter escaped,
# which grep -E must not see (GNU grep warns about a stray backslash).
ere() { printf '%s' "$1" | sed -e 's/[][\\.*^$+?(){}|]/\\&/g'; }
usage() { echo "usage: $(basename "$0") SRC DEST [--replace] [--agents-from FILE] [--exclude NAME]... [--rewrite PATH]" >&2; exit 1; }

args=(); replace=0; agents_from=""; rewrite=""; excludes=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --replace) replace=1 ;;
    --agents-from) [[ $# -ge 2 ]] || usage; agents_from=$2; shift ;;
    --exclude) [[ $# -ge 2 ]] || usage; excludes+=("$2"); shift ;;
    --rewrite) [[ $# -ge 2 ]] || usage; rewrite=$2; shift ;;
    -h|--help) usage ;;
    -*) die "unknown option: $1" ;;
    *) args+=("$1") ;;
  esac
  shift
done
[[ ${#args[@]} -eq 2 ]] || usage
SRC=$(realpath -- "${args[0]}")
DEST=$(realpath -m -- "${args[1]}")
case "$DEST" in
  /|/home|"$HOME"|/tmp) die "refusing DEST=$DEST" ;;
esac
if [[ "$DEST" == "$SRC" || "$DEST" == "$SRC"/* || "$SRC" == "$DEST"/* ]]; then
  die "DEST $DEST overlaps the prep repo $SRC"
fi
for n in "${excludes[@]}" "$agents_from"; do
  [[ "$n" != */* ]] || die "--exclude and --agents-from take top-level names, got $n"
done

# --- Source checks ---
git -C "$SRC" rev-parse --verify -q HEAD >/dev/null || die "$SRC has no HEAD commit"
if ! git -C "$SRC" diff --quiet HEAD -- || ! git -C "$SRC" diff --cached --quiet --; then
  echo "error: $SRC has uncommitted changes to tracked files; commit or stash them first:" >&2
  git -C "$SRC" status --short --untracked-files=no >&2
  exit 1
fi
SHA=$(git -C "$SRC" rev-parse HEAD)
SHORT=$(git -C "$SRC" rev-parse --short HEAD)
if [[ -n "$agents_from" ]]; then
  git -C "$SRC" cat-file -e "HEAD:$agents_from" 2>/dev/null || die "$agents_from is not in $SRC HEAD"
  excludes+=("AGENTS.md" "$agents_from")
fi

# --- DEST checks ---
if [[ -e "$DEST" && ! -d "$DEST" ]]; then die "DEST $DEST exists and is not a directory"; fi
if [[ -d "$DEST" ]] && [[ -n "$(ls -A -- "$DEST")" ]]; then
  if [[ -e "$DEST/threads.tsv" || -L "$DEST/threads.tsv" ]]; then
    die "$DEST/threads.tsv exists: a run has started there; refusing even with --replace"
  fi
  [[ $replace -eq 1 ]] || die "DEST $DEST is not empty; pass --replace to wipe and rebuild it"
  top=$(git -C "$DEST" rev-parse --show-toplevel 2>/dev/null || true)
  subject=$(git -C "$DEST" log -1 --format=%s 2>/dev/null || true)
  if [[ "$top" != "$DEST" || "$subject" != "Run repo from prep "* ]]; then
    die "DEST $DEST is not a run repo built by this script; --replace refuses to wipe it (remove it yourself)"
  fi
  echo "Replacing contents of $DEST"
  find "$DEST" -mindepth 1 -delete
fi
mkdir -p -- "$DEST"

# --- Copy tracked files at HEAD, minus prep-only items ---
pathspec=(. ':(top,exclude)__*')
for n in "${excludes[@]}"; do pathspec+=(":(top,exclude)$n"); done
git -C "$SRC" archive --format=tar HEAD -- "${pathspec[@]}" | tar -x -C "$DEST" -f -

# Anything prep-only that slipped through is a bug: stop before committing.
for p in "${excludes[@]}" .git; do
  [[ ! -e "$DEST/$p" && ! -L "$DEST/$p" ]] || die "prep-only item copied: $p"
done
if compgen -G "$DEST/__*" >/dev/null; then die "prep-only __ item copied: $(ls -d "$DEST"/__*)"; fi

# --- AGENTS.md: regular file from the chosen source ---
if [[ -n "$agents_from" ]]; then
  git -C "$SRC" show "HEAD:$agents_from" > "$DEST/AGENTS.md"
  [[ -f "$DEST/AGENTS.md" && ! -L "$DEST/AGENTS.md" ]] || die "AGENTS.md is not a regular file"
fi

# --- Path rewrite ---
if [[ -n "$rewrite" && "$DEST" != "$rewrite" ]]; then
  pat=$(ere "$rewrite")
  sed_pat=${pat//#/\\#}
  esc=$(printf '%s' "$DEST" | sed -e 's/[\\&#]/\\&/g')
  n=0
  while IFS= read -r -d '' f; do
    # grep -I skips binaries; match PATH not followed by a name character.
    if grep -IqE "${pat}([^A-Za-z0-9_-]|\$)" -- "$f"; then
      sed -i -E "s#${sed_pat}([^A-Za-z0-9_-]|\$)#${esc}\\1#g" -- "$f"
      n=$((n+1))
    fi
  done < <(find "$DEST" -path "$DEST/.git" -prune -o -type f -print0)
  echo "Rewrote $rewrite -> $DEST in $n text files"
fi

# --- Fresh git history ---
git -C "$DEST" init -q -b main
git -C "$DEST" add -A
git -C "$DEST" commit -q -m "Run repo from prep $SHORT"

# --- Summary ---
count=$(git -C "$DEST" ls-files | wc -l)
echo
echo "Run repo: $DEST"
echo "Source:   $SRC @ $SHA"
echo "Commit:   $(git -C "$DEST" rev-parse HEAD) (branch $(git -C "$DEST" branch --show-current))"
echo "Files:    $count"

src_pat=$(ere "$SRC")
leftover=$( { grep -rInE "${src_pat}([^A-Za-z0-9_-]|\$)" --exclude-dir=.git -- "$DEST" || true
              find "$DEST" -mindepth 1 -maxdepth 1 -name '__*' || true
              # Run files that still name a prep-only "__" item point at something missing.
              git -C "$SRC" ls-tree --name-only "$SHA" | while IFS= read -r name; do
                [[ "$name" == __* ]] || continue
                grep -rInE "(^|[^A-Za-z0-9_])$(ere "$name")([^A-Za-z0-9_-]|\$)" --exclude-dir=.git -- "$DEST" || true
              done; } | cut -c1-240 )
if [[ -n "$leftover" ]]; then
  echo "LEFTOVER prep paths found:"
  echo "$leftover"
  exit 2
fi
echo "Leftover prep paths: none"
