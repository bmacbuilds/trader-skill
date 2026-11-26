#!/bin/bash
# Installs /trader for Claude Code: uv (only if missing), the traderos command in ~/.local/bin, and the skill in
# ~/.claude/skills/trader. No admin password. Safe to run again: a second run updates.
set -e
APP="$HOME/.traderos-app"
mkdir -p "$APP" "$HOME/.local/bin" "$HOME/.claude/skills"
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  UV="$HOME/.local/bin/uv"
fi
rm -rf "$APP/src" && mkdir -p "$APP/src"
curl -LsSf https://github.com/bmacbuilds/trader-skill/archive/refs/tags/v2.0.1.tar.gz | tar -xz -C "$APP/src" --strip-components 1
"$UV" tool install --quiet --force --python 3.12 "$APP/src"
rm -rf "$HOME/.claude/skills/trader"
cp -R "$APP/src/.claude/skills/trader" "$HOME/.claude/skills/trader"
test -f "$HOME/.claude/skills/trader/SKILL.md"
"$HOME/.local/bin/traderos" init >/dev/null
"$HOME/.local/bin/traderos" doctor >/dev/null || { echo "/trader installed, but its self-check failed: run traderos doctor"; exit 1; }
echo "Done: /trader is installed. Type /trader to start."
