#!/bin/bash
# Installs /trader for Claude Code: uv (only if missing), the traderos command in uv's tool folder (normally
# ~/.local/bin), and the skill in ~/.claude/skills/trader. No admin password. Safe to run again: a second run updates.
set -e
# The release this installer downloads. tests/release/test_install_sh.py keeps it equal to traderos.__version__.
TAG=v2.0.1
APP="$HOME/.traderos-app"
SKILL="$HOME/.claude/skills/trader"
mkdir -p "$APP" "$HOME/.local/bin" "$HOME/.claude/skills"
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  UV="$HOME/.local/bin/uv"
fi
rm -rf "$APP/src" && mkdir -p "$APP/src"
curl -LsSf "https://github.com/bmacbuilds/trader-skill/archive/refs/tags/$TAG.tar.gz" | tar -xz -C "$APP/src" --strip-components 1
"$UV" tool install --quiet --force --python 3.12 "$APP/src"
# uv puts the command where its own settings say (UV_TOOL_BIN_DIR, XDG_BIN_HOME): ask it, never assume ~/.local/bin.
BIN="$("$UV" tool dir --bin)"
# A skill already called trader that is not TraderOS is someone else's: keep it, outside the skills folder so Claude
# does not load two skills named trader, and say where it went. An earlier TraderOS install is replaced.
if [ -e "$SKILL" ] && ! grep -qs "TraderOS" "$SKILL/SKILL.md"; then
  KEPT="$APP/kept-skill-$(date +%Y%m%d-%H%M%S)"
  mv "$SKILL" "$KEPT"
  echo "A skill called trader was already in ~/.claude/skills and is not TraderOS. It is kept, unchanged, at $KEPT"
fi
rm -rf "$SKILL"
cp -R "$APP/src/.claude/skills/trader" "$SKILL"
test -f "$SKILL/SKILL.md"
"$BIN/traderos" init >/dev/null
"$BIN/traderos" doctor >/dev/null || { echo "/trader installed, but its self-check failed: run traderos doctor"; exit 1; }
echo "Done: /trader is installed. Type /trader to start."
