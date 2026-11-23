#!/usr/bin/env bash
# Setu installer (POSIX-ish bash; install.ps1 is the Windows equivalent).
#
#   ./install.sh                                  # claude, all packs
#   ./install.sh --target cursor                  # .cursor/rules/
#   ./install.sh --target codex                   # AGENTS.md
#   ./install.sh --profile minimal --domains cyber,finance
#   ./install.sh --target claude --dry-run
#   ./install.sh --uninstall --target claude      # removes exactly what it wrote
#   ./install.sh --list                           # show recorded installs
#
# How uninstall stays exact: every file this script writes is appended to a
# manifest, and scripts/install_state.py records that manifest in
# ~/.setu/install-ledger.json. Uninstall reads the ledger and deletes those paths
# and nothing else. No globbing, no "rm -rf ~/.claude/skills" — a plugin has no
# business guessing which files in your config directory are its own.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="claude"
DOMAINS=""
PROFILE="full"
DRY_RUN=0
UNINSTALL=0
LIST=0
FORCE=0
PY="${PYTHON:-python3}"

ALL_DOMAINS=(cyber webdev admin finance agri)
# The minimal profile is chosen for a small token budget: the two packs with the
# highest day-to-day hit rate that need the fewest agents to be useful.
MINIMAL_DOMAINS=(cyber finance)

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)     TARGET="${2:-}"; shift 2 ;;
    --target=*)   TARGET="${1#*=}"; shift ;;
    --domains)    DOMAINS="${2:-}"; shift 2 ;;
    --domains=*)  DOMAINS="${1#*=}"; shift ;;
    --profile)    PROFILE="${2:-}"; shift 2 ;;
    --profile=*)  PROFILE="${1#*=}"; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    --list)       LIST=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --force)      FORCE=1; shift ;;
    -h|--help)    usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

case "$TARGET" in claude|cursor|codex) ;; *) echo "error: --target must be claude, cursor or codex" >&2; exit 2 ;; esac
case "$PROFILE" in full|minimal) ;; *) echo "error: --profile must be full or minimal" >&2; exit 2 ;; esac

# shellcheck source=/dev/null
command -v "$PY" >/dev/null 2>&1 || { echo "error: $PY not found" >&2; exit 2; }

if [[ "$LIST" == "1" ]]; then
  exec "$PY" "$SCRIPT_DIR/scripts/install_state.py" list
fi

if [[ "$UNINSTALL" == "1" ]]; then
  args=(uninstall --target "$TARGET")
  [[ "$DRY_RUN" == "1" ]] && args+=(--dry-run)
  exec "$PY" "$SCRIPT_DIR/scripts/install_state.py" "${args[@]}"
fi

# ---- resolve domains ------------------------------------------------------ #
if [[ "$PROFILE" == "minimal" && -z "$DOMAINS" ]]; then
  DOMAINS="$(IFS=,; echo "${MINIMAL_DOMAINS[*]}")"
elif [[ -z "$DOMAINS" ]]; then
  DOMAINS="$(IFS=,; echo "${ALL_DOMAINS[*]}")"
fi
IFS=',' read -r -a SELECTED <<< "$DOMAINS"
for d in "${SELECTED[@]}"; do
  found=0
  for a in "${ALL_DOMAINS[@]}"; do [[ "$d" == "$a" ]] && found=1; done
  [[ "$found" == "1" ]] || { echo "error: unknown domain '$d' (have: ${ALL_DOMAINS[*]})" >&2; exit 2; }
done

# ---- resolve destination root -------------------------------------------- #
case "$TARGET" in
  claude) ROOT="${CLAUDE_HOME:-$HOME/.claude}" ;;
  cursor) ROOT="${CURSOR_HOME:-$PWD/.cursor}" ;;
  codex)  ROOT="${CODEX_HOME:-$PWD}" ;;
esac
mkdir -p "$ROOT"
ROOT="$(cd "$ROOT" && pwd)"

MANIFEST="$(mktemp)"     # files we created and own
SHARED="$(mktemp)"       # files we edited in place (AGENTS.md)
chmod 600 "$MANIFEST" "$SHARED"
cleanup() { rm -f "$MANIFEST" "$SHARED"; }
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
run() { if [[ "$DRY_RUN" == "1" ]]; then say "  would: $*"; else "$@"; fi; }

# Copy one file, record its absolute destination.
copy_file() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [[ -e "$dst" && "$FORCE" != "1" ]]; then
    if cmp -s "$src" "$dst"; then
      say "  = $(basename "$dst") (unchanged)"
      echo "$dst" >> "$MANIFEST"
      return
    fi
    say "  ! $(basename "$dst") exists and differs; backing up to $(basename "$dst").setu-bak"
    run cp -p "$dst" "$dst.setu-bak"
    echo "$dst.setu-bak" >> "$MANIFEST"
  fi
  run cp -p "$src" "$dst"
  echo "$dst" >> "$MANIFEST"
  say "  + ${dst#"$ROOT"/}"
}

say "Setu installer"
say "  target   : $TARGET  ($ROOT)"
say "  profile  : $PROFILE  (domains: $DOMAINS)"
[[ "$DRY_RUN" == "1" ]] && say "  DRY RUN — nothing will be written"
say ""

# Always installed: the memory layer, the model wrapper, and the CLI that wires
# them together.
SHARED_SCRIPTS=(memory.py model_wrapper.py common.py install_state.py setu.py)
# Only installed when a pack that needs them is selected — a --domains agri
# install has no use for a CVSS scorer.
declare -A PACK_SHARED=(
  [pattern_engine]="cyber finance"     # shared detection rules + scorer
  [severity_scorer]="cyber"
  [dedup]="cyber"
)
resolve_pack_shared() {
  local domain="$1"
  for script in "${!PACK_SHARED[@]}"; do
    for needed_by in ${PACK_SHARED[$script]}; do
      if [[ "$needed_by" == "$domain" ]]; then
        echo "$script.py"
      fi
    done
  done | sort -u
}

declare -A DOMAIN_SCRIPTS=(
  [cyber]="cyber_recon.py cyber_scan.py cyber_chain.py report_formatter.py"
  [webdev]="design_check.py webdev_fix.py"
  [admin]="admin_docs.py"
  [finance]="finance.py"
  [agri]="agri.py"
)
declare -A DOMAIN_RULES=(
  [cyber]="patterns.json dependencies.json"
  [webdev]=""
  [admin]="legal_corpus.json"
  [finance]="patterns.json"
  [agri]=""
)

write_codex_agents_md() {
  local agents_md="$ROOT/AGENTS.md"
  local body
  body="$(cat <<EOF
<!-- BEGIN SETU (managed block — do not edit inside) -->
# Setu — multi-domain agent framework

This block is managed by the Setu installer. Delete the block (or run
\`install.sh --uninstall --target codex\`) to remove it; anything outside the
markers is left untouched.

Installed packs: **$DOMAINS** (profile: $PROFILE)

## Routing

Read the request, then follow the matching pack's instructions:

| Request is about… | Pack | Read |
|---|---|---|
| security testing, vulnerabilities, recon, bug bounty | cyber | \`skills/setu-cyber/SKILL.md\` |
| building a site/component, or fixing broken code | webdev | \`skills/setu-webdev/SKILL.md\` |
| RTI, grievance, consumer complaint, legal citations | admin | \`skills/setu-admin/SKILL.md\` |
| expenses, scams, EMI/loans | finance | \`skills/setu-finance/SKILL.md\` |
| crop disease, pests, treatment, farm advisory | agri | \`skills/setu-agri/SKILL.md\` |

Top-level router: \`skills/setu-orchestrator/SKILL.md\`

## Non-negotiable gates

- **cyber** — no active testing without confirmed authorization; passive recon only otherwise.
- **webdev** — no fix is accepted without a green test run; the design critic rejects
  purple gradients, default fonts with no pairing, the hero+3-cards+footer template,
  emoji as icons, and placeholder copy.
- **admin** — never mark a document final; a named human must confirm.
- **finance** — never produce a number from the model; EMI maths is Python only.
- **agri** — no treatment advice below 0.6 classification confidence.

## Helper scripts

\`\`\`bash
python3 scripts/setu.py doctor      # environment check
python3 scripts/setu.py selftest    # verify every gate still holds
python3 scripts/setu.py context --pack cyber --request "..."
\`\`\`
<!-- END SETU -->
EOF
)"

  if [[ ! -f "$agents_md" ]]; then
    say "  creating AGENTS.md"
    if [[ "$DRY_RUN" == "1" ]]; then
      say "  would create $agents_md"
    else
      printf '%s\n' "$body" > "$agents_md"
    fi
    echo "$agents_md" >> "$MANIFEST"        # we created it: we own it
    return
  fi

  if grep -qF '<!-- BEGIN SETU (managed block' "$agents_md"; then
    say "  updating the existing Setu block in AGENTS.md (content outside it preserved)"
    if [[ "$DRY_RUN" == "1" ]]; then
      say "  would replace the managed block"
    else
      cp -p "$agents_md" "$agents_md.setu-bak"
      echo "$agents_md" >> "$SHARED"
      # Replace only the text between the markers, using Python so multi-line
      # replacement does not depend on sed's dialect.
      SETU_BODY="$body" "$PY" - "$agents_md" <<'PYEOF'
import os, pathlib, sys
p = pathlib.Path(sys.argv[1])
begin = "<!-- BEGIN SETU (managed block"
end = "<!-- END SETU -->"
text = p.read_text(encoding="utf-8")
start = text.index(begin)
stop = text.index(end, start) + len(end)
p.write_text(text[:start] + os.environ["SETU_BODY"] + text[stop:], encoding="utf-8")
print("      managed block replaced")
PYEOF
    fi
  else
    say "  appending a Setu block to the existing AGENTS.md (your content is untouched)"
    if [[ "$DRY_RUN" == "1" ]]; then
      say "  would append the managed block"
    else
      cp -p "$agents_md" "$agents_md.setu-bak"
      echo "$agents_md" >> "$SHARED"
      printf '\n\n%s\n' "$body" >> "$agents_md"
    fi
  fi
}

# ---- install ------------------------------------------------------------- #
say "Shared:"
case "$TARGET" in
  claude)
    for f in "${SHARED_SCRIPTS[@]}"; do copy_file "$SCRIPT_DIR/scripts/$f" "$ROOT/setu/scripts/$f"; done
    copy_file "$SCRIPT_DIR/skills/setu-orchestrator/SKILL.md" "$ROOT/skills/setu-orchestrator/SKILL.md"
    ;;
  cursor)
    for f in "${SHARED_SCRIPTS[@]}"; do copy_file "$SCRIPT_DIR/scripts/$f" "$ROOT/setu/scripts/$f"; done
    ;;
  codex)
    for f in "${SHARED_SCRIPTS[@]}"; do copy_file "$SCRIPT_DIR/scripts/$f" "$ROOT/setu/scripts/$f"; done
    ;;
esac
say ""

for d in "${SELECTED[@]}"; do
  say "Domain: $d"
  for f in $(resolve_pack_shared "$d"); do
    case "$TARGET" in
      claude|codex) copy_file "$SCRIPT_DIR/scripts/$f" "$ROOT/setu/scripts/$f" ;;
      cursor)       copy_file "$SCRIPT_DIR/scripts/$f" "$ROOT/setu/scripts/$f" ;;
    esac
  done
  for f in ${DOMAIN_SCRIPTS[$d]:-}; do
    case "$TARGET" in
      claude) copy_file "$SCRIPT_DIR/scripts/$f" "$ROOT/setu/scripts/$f" ;;
      *)      copy_file "$SCRIPT_DIR/scripts/$f" "$ROOT/setu/scripts/$f" ;;
    esac
  done
  for r in ${DOMAIN_RULES[$d]:-}; do
    case "$TARGET" in
      claude) copy_file "$SCRIPT_DIR/scripts/rules/$r" "$ROOT/setu/scripts/rules/$r" ;;
      *)      copy_file "$SCRIPT_DIR/scripts/rules/$r" "$ROOT/setu/rules/$r" ;;
    esac
  done
  # agents
  while IFS= read -r af; do
    [[ -z "$af" ]] && continue
    case "$TARGET" in
      claude) copy_file "$af" "$ROOT/agents/$(basename "$af")" ;;
      cursor) copy_file "$af" "$ROOT/rules/$(basename "$af" .md).mdc" ;;
      codex)  copy_file "$af" "$ROOT/setu/agents/$(basename "$af")" ;;
    esac
  done < <(find "$SCRIPT_DIR/agents" -maxdepth 1 -name "${d}-*.md" | sort)
  # skill
  case "$TARGET" in
    claude) copy_file "$SCRIPT_DIR/skills/setu-$d/SKILL.md" "$ROOT/skills/setu-$d/SKILL.md" ;;
    cursor) copy_file "$SCRIPT_DIR/skills/setu-$d/SKILL.md" "$ROOT/rules/setu-$d.mdc" ;;
    codex)  copy_file "$SCRIPT_DIR/skills/setu-$d/SKILL.md" "$ROOT/setu/skills/setu-$d/SKILL.md" ;;
  esac
  say ""
done

if [[ "$TARGET" == "codex" ]]; then
  say "Codex:"
  write_codex_agents_md
  say ""
fi

# ---- record -------------------------------------------------------------- #
if [[ "$DRY_RUN" != "1" ]]; then
  SHARED_ARGS=()
  if [[ -s "$SHARED" ]]; then
    while IFS= read -r sf; do
      [[ -n "$sf" ]] && SHARED_ARGS+=(--shared "$sf")
    done < "$SHARED"
  fi
  "$PY" "$SCRIPT_DIR/scripts/install_state.py" record \
      --target "$TARGET" --root "$ROOT" --domains "$DOMAINS" \
      --manifest "$MANIFEST" "${SHARED_ARGS[@]}" >/dev/null
  UNIQUE_COUNT="$(sort -u "$MANIFEST" | wc -l | tr -d ' ')"
  say "Installed $UNIQUE_COUNT unique file(s)."
  say "Ledger: ~/.setu/install-ledger.json  (uninstall reads this)"
else
  say "Dry run complete — nothing written."
fi
say ""
say "Next:"
case "$TARGET" in
  claude) say "  Restart Claude Code, then try: \"setu: audit the security of <in-scope target>\"" ;;
  cursor) say "  Cursor picks up .cursor/rules automatically. Ask: \"using the setu rules, ...\"" ;;
  codex)  say "  Codex reads AGENTS.md. Ask: \"using setu, draft an RTI application for ...\"" ;;
esac
say "  Health check: python3 $ROOT/setu/scripts/setu.py doctor   (claude target)"
say "  Remove:      ./install.sh --uninstall --target $TARGET"
