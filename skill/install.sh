#!/usr/bin/env bash
# Install the skill for Claude Code: copies SKILL.md next to a checkout of the tool so the skill's commands work from anywhere.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
dest="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/server-guardrails"
mkdir -p "$dest"
cp "$here/skill/server-guardrails/SKILL.md" "$dest/SKILL.md"
echo "Installed skill to $dest"
echo "Install the tool too:  pip install -e '$here'   (or run from the checkout: $here/bin/guardrails)"
