#!/bin/sh
# JobGitOps installer (spec: specs/bootstrap-installer.md §5).
#
# Creates a private job-search repo from the release tarball, wires the
# provider secret and Actions write permissions, then pushes the assembled
# tree. Every mutating step is echoed and aborts on failure; --dry-run prints
# the commands without running them. The pinned release tag is resolved from
# the latest release (override with $JOBGITOPS_TAG for pre-release testing).

set -u

# ---------------------------------------------------------------------------
# Defaults / interface
# ---------------------------------------------------------------------------

POSITIONAL_SEEN="0"
REPO_NAME="job-search"
VISIBILITY="private"
PROVIDER=""
GEMINI_KEY=""
OPENROUTER_KEY=""
TOKEN=""
HAS_TOKEN="0"
YES="0"
DRY_RUN="0"
TMPDIR="${TMPDIR:-}"
WANT_PROJECTS="0"
PRIMARY_KEY=""
SELECT_RESULT=""
CHECKBOX_RESULT=""
TAVILY_KEY=""
BRAVE_KEY=""
JINA_KEY=""


# Capture the terminal state once so an interrupt during any prompt can
# restore it exactly (echo off + raw byte reads for the masked key entry).
STTY_SAVED="$(stty -g 2>/dev/null || true)"

# Never let git fall back to an interactive credential prompt inside a piped
# installer; a missing helper must fail fast with an actionable message.
export GIT_TERMINAL_PROMPT=0

usage() {
    cat >&2 <<'EOF'
Usage: install.sh <repo-name> [options]

  <repo-name>            repository slug (default: job-search)
  --visibility MODE      private | public (default: private)
  --provider PROVIDER    gemini | openrouter (default: auto from whichever
                         key is present; prompts interactively when neither is)
  --gemini-key KEY       Gemini API key (or $GEMINI_API_KEY)
  --openrouter-key KEY   OpenRouter API key (or $OPENROUTER_API_KEY)
  --tavily-key KEY       Tavily API key (or $TAVILY_API_KEY)
  --brave-key KEY        Brave API key (or $BRAVE_API_KEY)
  --jina-key KEY         Jina API key (or $JINA_API_KEY)
  --token TOKEN          PAT fallback when gh is installed but not
                         auth-configured (or $GH_TOKEN)
  --yes                  non-interactive; fail instead of prompting
  --dry-run              print every command, make no changes
  -h, --help             show this help
EOF
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --visibility)
            VISIBILITY="${2:-}"
            shift 2
            ;;
        --provider)
            PROVIDER="${2:-}"
            shift 2
            ;;
        --gemini-key)
            GEMINI_KEY="${2:-}"
            shift 2
            ;;
        --openrouter-key)
            OPENROUTER_KEY="${2:-}"
            shift 2
            ;;
        --tavily-key)
            TAVILY_KEY="${2:-}"
            shift 2
            ;;
        --brave-key)
            BRAVE_KEY="${2:-}"
            shift 2
            ;;
        --jina-key)
            JINA_KEY="${2:-}"
            shift 2
            ;;
        --token)
            TOKEN="${2:-}"
            shift 2
            ;;
        --yes)
            YES="1"
            shift
            ;;
        --dry-run)
            DRY_RUN="1"
            shift
            ;;
        --help | -h)
            usage
            ;;
        -*)
            echo "unknown option: $1" >&2
            usage
            ;;
        *)
            if [ "$POSITIONAL_SEEN" = "0" ]; then
                REPO_NAME="$1"
                POSITIONAL_SEEN="1"
            else
                echo "unexpected argument: $1" >&2
                usage
            fi
            shift
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    printf '%s\n' "$*" >&2
}

# Put the terminal back the way it was captured at startup (echo off + raw
# mode were the only changes). The fallback covers a piped run, where
# `stty -g` failed at the top and STTY_SAVED is empty; it restores echo (so
# typed input is visible again) and canonical mode (so line editing and the
# read()-based prompts behave normally).
restore_tty() {
    # Ensure terminal cursor is always visible
    printf '\033[?25h' >&2
    if [ -n "$STTY_SAVED" ]; then
        stty "$STTY_SAVED" 2>/dev/null || true
    else
        stty echo icanon 2>/dev/null || true
    fi
}

cleanup() {
    restore_tty
    if [ -n "$TMPDIR" ] && [ -d "$TMPDIR" ]; then
        rm -rf "$TMPDIR"
    fi
}

# The EXIT trap makes die() and interrupts restore the terminal and remove the
# temp tree; registered from the top so a Ctrl-C mid-prompt never leaves the
# shell with echo/canonical mode broken.
trap 'cleanup' EXIT
trap 'exit 1' HUP INT TERM

die() {
    printf 'ERROR: %s\n' "$1" >&2
    cleanup
    exit 1
}

# Echo the command, then run it; abort with the command and exit code on
# failure. Under --dry-run the command is echoed and skipped.
run() {
    log "> $*"
    [ "$DRY_RUN" = "1" ] && return 0
    "$@"
    rc=$?
    [ "$rc" -eq 0 ] || die "command failed (exit $rc): $*"
}

validate_slug() {
    case "$1" in
        "" | *[!A-Za-z0-9._-]* | .* | *..* | *. | *- | *.git)
            return 1
            ;;
        -*) return 1 ;;
    esac
    [ "${#1}" -le 100 ] || return 1
    return 0
}

# Prompt-and-read a secret on stderr, echoing '*' per keystroke (plain
# `stty -echo` gives no feedback at all).
#
# Why not `read -s`? The installer is POSIX `sh` (dash/busybox/ash), where
# `read -s` does not exist, and where it does (bash/ksh) it still shows
# nothing — masking without any visual cue is exactly the UX we're fixing. So
# this reads one byte at a time in cbreak mode (stty -icanon min 1 time 0)
# so backspace removes the last '*'/char and Enter/EOF end the entry. The
# saved terminal state is restored before returning; the key lands in $KEY.
read_masked() {
    _secret=""
    stty -echo -icanon min 1 time 0 2>/dev/null
    while :; do
        _byte="$(dd bs=1 count=1 2>/dev/null)"
        case "$_byte" in
            # Empty — EOF, or a bare \n whose trailing newline command
            # substitution strips — plus Enter as \r in raw mode: end entry.
            "" | "$(printf '\r')")
                break
                ;;
            "$(printf '\b')" | "$(printf '\177')")
                if [ -n "$_secret" ]; then
                    _secret="${_secret%?}"
                    printf '\b \b' >&2
                fi
                ;;
            *)
                _secret="${_secret}${_byte}"
                printf '*' >&2
                ;;
        esac
    done
    printf '\n' >&2
    restore_tty
    KEY="$_secret"
    unset _secret _byte
}

# ANSI Escape Sequences
ESC="$(printf '\033')"
C_HIDE="${ESC}[?25l"
C_SHOW="${ESC}[?25h"

# Text Styling
S_BOLD="${ESC}[1m"
S_REVERSE="${ESC}[7m"
S_RESET="${ESC}[0m"
S_GREEN="${ESC}[32m"
S_CYAN="${ESC}[36m"

# Reads a key press, resolving arrow keys (Up/Down), Space, and Enter in terminal raw mode.
# Maps EOF to "eof" so the caller can handle closed stdin/Ctrl+D.
# stty -icanon min 0 time 0 temporarily polls the buffer to parse escape sequences.
read_key() {
    _b1="$(dd bs=1 count=1 2>/dev/null)"
    if [ "$_b1" = "$ESC" ]; then
        stty -icanon min 0 time 0 2>/dev/null || true
        _b2="$(dd bs=1 count=1 2>/dev/null)"
        _b3="$(dd bs=1 count=1 2>/dev/null)"
        stty -icanon min 1 time 0 2>/dev/null || true
        if [ "$_b2" = "[" ]; then
            case "$_b3" in
                A) echo "up"; return 0 ;;
                B) echo "down"; return 0 ;;
            esac
        fi
        echo "esc"
        return 0
    fi

    case "$_b1" in
        "") echo "eof" ;;
        "$(printf '\r')") echo "enter" ;;
        "$(printf '\n')") echo "enter" ;;
        " ") echo "space" ;;
        *) echo "char:$_b1" ;;
    esac
}

# Single-select menu
# Usage: prompt_select <prompt_message> <key1:display1> <key2:display2> ...
prompt_select() {
    _prompt="$1"
    _val=""
    _key=""
    _display=""
    _chosen_val=""
    _chosen_key=""
    _chosen_display=""
    shift 1
    
    _num_options=$#

    # Check if stdin/stdout are terminals and stty works before entering TUI mode.
    # We do not call restore_tty here because stty leaves it in the correct state.
    _use_tui=0
    if [ -t 0 ] && [ -t 1 ] && stty -echo -icanon min 1 time 0 2>/dev/null; then
        _use_tui=1
    fi

    if [ "$_use_tui" -eq 1 ]; then
        stty -echo -icanon min 1 time 0 2>/dev/null || true
        _current=1
        printf "\n%s%s (Use arrow keys, press Enter to select):%s\n" "$S_BOLD" "$_prompt" "$S_RESET" >&2
        printf "%s" "$C_HIDE" >&2
        
        while :; do
            _i=1
            for _opt in "$@"; do
                _display="${_opt#*:}"
                
                if [ "$_i" -eq "$_current" ]; then
                    printf "  %s❯ %s%s%s%s\n" "$S_CYAN" "$S_BOLD" "$S_REVERSE" "$_display" "$S_RESET" >&2
                else
                    printf "    %s\n" "$_display" >&2
                fi
                _i=$((_i + 1))
            done
            
            _key_press="$(read_key)"
            
            if [ "$_key_press" = "up" ]; then
                _current=$((_current - 1))
                [ "$_current" -lt 1 ] && _current=$_num_options
            elif [ "$_key_press" = "down" ]; then
                _current=$((_current + 1))
                [ "$_current" -gt "$_num_options" ] && _current=1
            elif [ "$_key_press" = "enter" ]; then
                break
            elif [ "$_key_press" = "eof" ]; then
                restore_tty
                die "stdin closed during prompt."
            fi
            
            printf '%s[%dA' "$ESC" "$_num_options" >&2
        done
        
        # Restore cursor and clear the menu from screen atomically using clear-down
        printf "%s" "$C_SHOW" >&2
        printf '%s[%dA' "$ESC" "$_num_options" >&2
        printf '%s[J' "$ESC" >&2
        
        # Resolve chosen option from arguments list
        _i=1
        for _opt in "$@"; do
            if [ "$_i" -eq "$_current" ]; then
                _chosen_val="$_opt"
                break
            fi
            _i=$((_i + 1))
        done
        _chosen_key="${_chosen_val%%:*}"
        _chosen_display="${_chosen_val#*:}"
        printf "Selected LLM provider: %s%s%s\n\n" "$S_GREEN" "$_chosen_display" "$S_RESET" >&2
        SELECT_RESULT="$_chosen_key"
        restore_tty
    else
        # Fallback to standard line-based read prompt
        _i=1
        for _opt in "$@"; do
            _display="${_opt#*:}"
            printf "  [%d] %s\n" "$_i" "$_display" >&2
            _i=$((_i + 1))
        done
        while :; do
            printf "%s (enter number): " "$_prompt" >&2
            if [ -t 0 ]; then
                IFS= read -r _choice < /dev/tty || exit 1
            else
                IFS= read -r _choice || exit 1
            fi
            if [ -n "$_choice" ] && [ "$_choice" -eq "$_choice" ] 2>/dev/null; then
                if [ "$_choice" -ge 1 ] && [ "$_choice" -le "$_num_options" ]; then
                    _i=1
                    for _opt in "$@"; do
                        if [ "$_i" -eq "$_choice" ]; then
                            _chosen_val="$_opt"
                            break
                        fi
                        _i=$((_i + 1))
                    done
                    SELECT_RESULT="${_chosen_val%%:*}"
                    break
                fi
            fi
            printf "Invalid choice. Please enter a number between 1 and %d.\n" "$_num_options" >&2
        done
    fi
}

# Checkbox (multi-select) menu
# Usage: prompt_checkbox <prompt_message> <key1:display1> <key2:display2> ...
prompt_checkbox() {
    _prompt="$1"
    _val=""
    _chk=""
    _display=""
    _box=""
    _key_press=""
    _answer=""
    _key=""
    shift 1
    
    _num_items=$#
    
    # Comma-separated list to track selected keys (e.g. ",tavily,brave,")
    _selected_keys=","
    
    _use_tui=0
    if [ -t 0 ] && [ -t 1 ] && stty -echo -icanon min 1 time 0 2>/dev/null; then
        _use_tui=1
    fi
    
    if [ "$_use_tui" -eq 1 ]; then
        stty -echo -icanon min 1 time 0 2>/dev/null || true
        _current=1
        printf "\n%s%s (Use arrow keys, Space to toggle, Enter to confirm):%s\n" "$S_BOLD" "$_prompt" "$S_RESET" >&2
        printf "%s" "$C_HIDE" >&2
        
        while :; do
            _i=1
            for _opt in "$@"; do
                _key="${_opt%%:*}"
                _display="${_opt#*:}"
                
                # Check status indicator icon
                _box="◯"
                case "$_selected_keys" in
                    *",$_key,"*) _box="✅" ;;
                esac
                
                if [ "$_i" -eq "$_current" ]; then
                    printf "  %s❯ %s %s%s%s%s\n" "$S_CYAN" "$_box" "$S_BOLD" "$S_REVERSE" "$_display" "$S_RESET" >&2
                else
                    printf "     %s %s\n" "$_box" "$_display" >&2
                fi
                _i=$((_i + 1))
            done
            
            _key_press="$(read_key)"
            
            if [ "$_key_press" = "up" ]; then
                _current=$((_current - 1))
                [ "$_current" -lt 1 ] && _current=$_num_items
            elif [ "$_key_press" = "down" ]; then
                _current=$((_current + 1))
                [ "$_current" -gt "$_num_items" ] && _current=1
            elif [ "$_key_press" = "space" ]; then
                # Find current item key
                _i=1
                for _opt in "$@"; do
                    if [ "$_i" -eq "$_current" ]; then
                        _chosen_val="$_opt"
                        break
                    fi
                    _i=$((_i + 1))
                done
                _key="${_chosen_val%%:*}"
                
                case "$_selected_keys" in
                    *",$_key,"*)
                        # Remove key from selected list
                        _selected_keys="$(echo "$_selected_keys" | sed "s/,$_key,/,/g")"
                        ;;
                    *)
                        # Add key to selected list
                        _selected_keys="${_selected_keys}${_key},"
                        ;;
                esac
            elif [ "$_key_press" = "enter" ]; then
                break
            elif [ "$_key_press" = "eof" ]; then
                restore_tty
                die "stdin closed during prompt."
            fi
            
            printf '%s[%dA' "$ESC" "$_num_items" >&2
        done
        
        # Restore cursor and clear the menu from screen atomically using clear-down
        printf "%s" "$C_SHOW" >&2
        printf '%s[%dA' "$ESC" "$_num_items" >&2
        printf '%s[J' "$ESC" >&2
        restore_tty
    else
        # Fallback to simple question prompts
        for _opt in "$@"; do
            _key="${_opt%%:*}"
            _display="${_opt#*:}"
            while :; do
                printf "Configure %s? [y/N]: " "$_display" >&2
                if [ -t 0 ]; then
                    IFS= read -r _answer < /dev/tty || exit 1
                else
                    IFS= read -r _answer || exit 1
                fi
                case "$_answer" in
                    y | Y | yes | YES)
                        _selected_keys="${_selected_keys}${_key},"
                        break
                        ;;
                    n | N | no | NO | "")
                        break
                        ;;
                    *)
                        printf "Please enter y or n.\n" >&2
                        ;;
                esac
            done
        done
    fi

    # Output results and save to variables
    printf "Configured additional services:\n" >&2
    for _opt in "$@"; do
        _key="${_opt%%:*}"
        _display="${_opt#*:}"
        
        case "$_selected_keys" in
            *",$_key,"*)
                printf "  - %s: %sEnabled%s\n" "$_display" "$S_GREEN" "$S_RESET" >&2
                ;;
            *)
                printf "  - %s: Disabled\n" "$_display" >&2
                ;;
        esac
    done
    printf "\n" >&2
    CHECKBOX_RESULT="$_selected_keys"
}

# Verify the chosen provider key against its API before anything is created —
# a length heuristic can't tell a typo'd key from a real one. The key is sent
# as an Authorization/API header, never in the URL or in output. A 2xx means
# the key is valid; 401/403 means it was rejected; anything else (DNS/TLS
# failure, 5xx, unreachable host) means the key simply could not be verified,
# so the message must not claim it was rejected.
verify_key() {
    if [ "$DRY_RUN" = "1" ]; then
        log "> verifying $PROVIDER key against the API (skipped: dry-run)"
        return 0
    fi
    if [ "$PROVIDER" = "gemini" ]; then
        log "> verifying Gemini key against the API (key never echoed)"
        # curl's own error prints to stderr when it cannot connect; the `||`
        # converts that transport failure into a distinct message.
        HTTP_CODE="$(curl -sS --connect-timeout 10 --max-time 30 -o /dev/null -w '%{http_code}' \
            -H "X-Goog-Api-Key: $KEY" \
            "https://generativelanguage.googleapis.com/v1beta/models")" ||
            die "could not reach the Gemini API to verify the key — check your network and re-run."
    else
        log "> verifying OpenRouter key against the API (key never echoed)"
        HTTP_CODE="$(curl -sS --connect-timeout 10 --max-time 30 -o /dev/null -w '%{http_code}' \
            -H "Authorization: Bearer $KEY" \
            "https://openrouter.ai/api/v1/auth/key")" ||
            die "could not reach the OpenRouter API to verify the key — check your network and re-run."
    fi
    case "$HTTP_CODE" in
        2*) return 0 ;;
        401 | 403) die "$PROVIDER key rejected by the API — check it and re-run." ;;
        *)
            die "could not verify the $PROVIDER key (HTTP $HTTP_CODE) — check the key and your network, then re-run."
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Preflight (§5.3.1): tools, auth, repo slug
# ---------------------------------------------------------------------------

command -v gh >/dev/null 2>&1 ||
    die "GitHub CLI not found — install it (https://cli.github.com/) or use --token with a PAT."
command -v git >/dev/null 2>&1 || die "git not found."
command -v curl >/dev/null 2>&1 || die "curl not found."
command -v tar >/dev/null 2>&1 || die "tar not found."

if [ "$VISIBILITY" != "private" ] && [ "$VISIBILITY" != "public" ]; then
    die "--visibility must be 'private' or 'public' (got '$VISIBILITY')."
fi
case "$PROVIDER" in
    "" | gemini | openrouter) ;;
    *) die "--provider must be 'gemini' or 'openrouter' (got '$PROVIDER')." ;;
esac

# Interactive installs default to 'job-search' (prompt default). In a piped
# or --yes run there is no terminal to confirm with, so the name is required —
# an accidentally-created default-named repo is worse than a quick error.
if [ "$POSITIONAL_SEEN" = "0" ]; then
    if [ "$YES" = "1" ] || [ ! -t 0 ]; then
        die "repo name is required in non-interactive mode — pass it as the first argument."
    fi
    printf 'Repo name [%s]: ' "$REPO_NAME" >&2
    IFS= read -r REPO_INPUT
    [ -n "$REPO_INPUT" ] && REPO_NAME="$REPO_INPUT"
fi
validate_slug "$REPO_NAME" ||
    die "invalid repo name '$REPO_NAME' — use 1-100 chars of [A-Za-z0-9._-], no leading/trailing '-' or '.', no '..', no '.git' suffix."

# PAT fallback: --token or $GH_TOKEN drives every gh call; otherwise the
# script relies on gh's own configured auth.
if [ -n "$TOKEN" ] || [ -n "${GH_TOKEN:-}" ]; then
    HAS_TOKEN="1"
    export GH_TOKEN="${TOKEN:-$GH_TOKEN}"
    gh api user --jq .login >/dev/null 2>&1 ||
        die "token rejected by GitHub — check --token / \$GH_TOKEN."
else
    gh auth status >/dev/null 2>&1 ||
        die "not authenticated with gh — run 'gh auth login' (or pass --token)."
fi

OWNER="$(gh api user --jq .login 2>/dev/null)"
[ -n "$OWNER" ] || die "could not determine your GitHub login."

# Ask if they want to integrate with GitHub Projects V2 (which requires project scopes)
if [ "$YES" = "0" ] && [ -t 0 ] && [ "$HAS_TOKEN" = "0" ] && [ "$DRY_RUN" = "0" ]; then
    printf 'Do you want to integrate with GitHub Projects V2? [y/N] ' >&2
    IFS= read -r ANSWER
    case "$ANSWER" in
        y | Y | yes | YES) WANT_PROJECTS="1" ;;
    esac
fi

# ---------------------------------------------------------------------------
# Permission check (§5.3.2) — confirm repo + workflow scopes before creating
# anything, so a user never ends up with an unconfigured repo.
# ---------------------------------------------------------------------------

check_permissions() {
    # ATTEMPTS bounds the interactive refresh retry, so a user who keeps
    # canceling the browser flow cannot loop the offer forever.
    ATTEMPTS="${1:-1}"
    REQUIRED_SCOPES="repo workflow"
    [ "$WANT_PROJECTS" = "1" ] && REQUIRED_SCOPES="repo workflow project write:discussion"
    [ "$ATTEMPTS" -le 3 ] ||
        die "could not add the required scopes after $((ATTEMPTS - 1)) tries — run 'gh auth refresh -s $REQUIRED_SCOPES' manually and re-run."
    # X-OAuth-Scopes is only returned for classic PAT auth; browser OAuth
    # login (gho_) and fine-grained tokens omit it. When it is absent we
    # cannot introspect scopes, so we warn and continue — the abort-on-failure
    # backstop catches any step that truly lacks permission.
    SCOPES="$(gh api user --include 2>/dev/null |
        sed -n 's/^[Xx]-[Oo][Aa]uth-[Ss]copes:[[:space:]]*//p' | tr -d '\r ')"
    if [ -z "$SCOPES" ]; then
        log "note: could not read token scopes (browser login or fine-grained token); continuing — any permission failure will abort."
        return 0
    fi

    MISSING=""
    case ",$SCOPES," in
        *",repo,"*) ;;
        *) MISSING="$MISSING repo" ;;
    esac
    case ",$SCOPES," in
        *",workflow,"*) ;;
        *) MISSING="$MISSING workflow" ;;
    esac
    if [ "$WANT_PROJECTS" = "1" ]; then
        case ",$SCOPES," in
            *",project,"*) ;;
            *) MISSING="$MISSING project" ;;
        esac
        case ",$SCOPES," in
            *",write:discussion,"*) ;;
            *) MISSING="$MISSING write:discussion" ;;
        esac
    fi
    [ -n "$MISSING" ] || return 0
    MISSING="$(printf '%s' "$MISSING" | sed 's/^ //')"

    # A PAT via --token/$GH_TOKEN cannot be re-scoped by gh (the env token
    # keeps overriding stored auth), and --dry-run must not mutate the token,
    # so the refresh offer only makes sense for gh-managed auth on a real
    # terminal during an actual run.
    if [ "$YES" = "1" ] || [ ! -t 0 ] || [ "$HAS_TOKEN" = "1" ] || [ "$DRY_RUN" = "1" ]; then
        die "token is missing the scope(s): $MISSING — add them: gh auth refresh -s $MISSING (or use a PAT covering $MISSING)."
    fi

    printf 'Token is missing scope(s): %s. Run "gh auth refresh -s %s" now? [y/N] ' "$MISSING" "$MISSING" >&2
    IFS= read -r ANSWER
    case "$ANSWER" in
        y | Y | yes | YES)
            # Device flow: prints a code and opens the browser; blocks until
            # the user authorizes or cancels.
            log "> gh auth refresh -s $MISSING"
            gh auth refresh -s "$MISSING"
            rc=$?
            [ "$rc" -eq 0 ] ||
                die "scope refresh did not complete (exit $rc) — run 'gh auth refresh -s $MISSING' manually and re-run."
            check_permissions "$((ATTEMPTS + 1))"
            ;;
        *)
            die "aborted — run 'gh auth refresh -s $MISSING' yourself and re-run the installer."
            ;;
    esac
}
check_permissions

# ---------------------------------------------------------------------------
# Provider + API key (§5.2 / §5.3.6)
# ---------------------------------------------------------------------------

if [ -z "$PROVIDER" ]; then
    if [ -n "$GEMINI_KEY" ] || [ -n "${GEMINI_API_KEY:-}" ]; then
        PROVIDER="gemini"
    elif [ -n "$OPENROUTER_KEY" ] || [ -n "${OPENROUTER_API_KEY:-}" ]; then
        PROVIDER="openrouter"
    elif [ "$DRY_RUN" = "1" ]; then
        # --dry-run must never prompt; nothing is sent, so the provider is
        # irrelevant to the trace — pick the primary.
        PROVIDER="gemini"
    elif [ "$YES" = "1" ] || [ ! -t 0 ]; then
        die "no provider key supplied and --yes is set / stdin is not a terminal — pass --provider gemini|openrouter with its --...-key or env var."
    else
        prompt_select "Which LLM provider do you want to use?" "gemini:♊ Gemini" "openrouter:🔌 OpenRouter"
        PROVIDER="$SELECT_RESULT"
    fi
fi

if [ "$PROVIDER" = "gemini" ]; then
    SECRET_NAME="GEMINI_API_KEY"
    KEY="${GEMINI_KEY:-${GEMINI_API_KEY:-}}"
else
    SECRET_NAME="OPENROUTER_API_KEY"
    KEY="${OPENROUTER_KEY:-${OPENROUTER_API_KEY:-}}"
fi

if [ -z "$KEY" ] && [ "$DRY_RUN" = "0" ]; then
    if [ "$YES" = "1" ]; then
        die "no $SECRET_NAME supplied and --yes is set — pass the corresponding --...-key or set its env var."
    elif [ ! -t 0 ]; then
        die "no $SECRET_NAME supplied and stdin is not a terminal (curl|sh pipe) — re-run with --provider $PROVIDER --...-key or the env var set."
    else
        printf 'Enter %s: ' "$SECRET_NAME" >&2
        read_masked
        [ -n "$KEY" ] || die "no key entered."
    fi
fi

# Verify the key against the provider before creating anything (see verify_key);
# a length check alone can't catch a typo'd or expired key.
verify_key

# Copy verified key to primary key to avoid global variable mutation from optional keys prompts
PRIMARY_KEY="$KEY"

TAVILY_KEY="${TAVILY_KEY:-${TAVILY_API_KEY:-}}"
BRAVE_KEY="${BRAVE_KEY:-${BRAVE_API_KEY:-}}"
JINA_KEY="${JINA_KEY:-${JINA_API_KEY:-}}"

# Interactive setup for optional additional keys if not already provided.
# We suppress already-configured keys from the checklist.
if [ "$YES" = "0" ] && [ -t 0 ] && [ "$DRY_RUN" = "0" ]; then
    show_tavily=0; [ -z "$TAVILY_KEY" ] && show_tavily=1
    show_brave=0;  [ -z "$BRAVE_KEY" ] && show_brave=1
    show_jina=0;   [ -z "$JINA_KEY" ] && show_jina=1

    if [ "$show_tavily" -eq 1 ] || [ "$show_brave" -eq 1 ] || [ "$show_jina" -eq 1 ]; then
        # Dynamically build positional arguments for prompt_checkbox to avoid combinatorial branching
        set --
        [ "$show_tavily" -eq 1 ] && set -- "$@" "tavily:🔍 Tavily API Key"
        [ "$show_brave" -eq 1 ] && set -- "$@" "brave:🦁 Brave API Key"
        [ "$show_jina" -eq 1 ] && set -- "$@" "jina:🌐 Jina API Key"
        
        prompt_checkbox "Select optional additional services to configure" "$@"

        case "$CHECKBOX_RESULT" in
            *",tavily,"*)
                printf 'Enter TAVILY_API_KEY: ' >&2
                read_masked
                TAVILY_KEY="$KEY"
                ;;
        esac
        case "$CHECKBOX_RESULT" in
            *",brave,"*)
                printf 'Enter BRAVE_API_KEY: ' >&2
                read_masked
                BRAVE_KEY="$KEY"
                ;;
        esac
        case "$CHECKBOX_RESULT" in
            *",jina,"*)
                printf 'Enter JINA_API_KEY: ' >&2
                read_masked
                JINA_KEY="$KEY"
                ;;
        esac
    fi
fi

# ---------------------------------------------------------------------------
# Fetch shell plane (§5.3.3) — resolve tag, download + extract tarball
# ---------------------------------------------------------------------------

TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/jobgitops-install.XXXXXX")" ||
    die "could not create a temp working directory."

if [ -n "${JOBGITOPS_TAG:-}" ]; then
    TAG="${JOBGITOPS_TAG}"
else
    # gh api exits 1 (and prints an error body to stdout) when no release
    # exists yet — the command substitution would otherwise swallow that and
    # leave a garbage $TAG, so treat a failed lookup as "no release".
    TAG="$(gh api repos/menil/jobgitops/releases/latest --jq .tag_name 2>/dev/null)" ||
        die "could not resolve the latest JobGitOps release (none published yet). Set JOBGITOPS_TAG=<tag-or-branch> to test a specific ref."
    [ -n "$TAG" ] ||
        die "could not resolve the latest JobGitOps release (none published yet). Set JOBGITOPS_TAG=<tag-or-branch> to test a specific ref."
fi

TARBALL="$TMPDIR/jobgitops-$TAG.tgz"
download_tarball() {
    log "> curl -fsSL https://codeload.github.com/menil/jobgitops/tar.gz/refs/tags/$TAG -o $TARBALL"
    [ "$DRY_RUN" = "1" ] && return 0
    # Anonymous codeload first — the public-release path. Its 404 noise is
    # suppressed (2>/dev/null) so the fallbacks below own the messaging.
    if curl -fsSL "https://codeload.github.com/menil/jobgitops/tar.gz/refs/tags/$TAG" -o "$TARBALL" 2>/dev/null; then
        return 0
    fi
    # No such tag (pre-release, before the first release exists) — retry the
    # ref as a branch so JOBGITOPS_TAG=main works on a public repo. Inert once
    # releases exist: a release tag is always a real git tag.
    log "> curl -fsSL https://codeload.github.com/menil/jobgitops/tar.gz/refs/heads/$TAG -o $TARBALL"
    if curl -fsSL "https://codeload.github.com/menil/jobgitops/tar.gz/refs/heads/$TAG" -o "$TARBALL" 2>/dev/null; then
        return 0
    fi
    # Private-source fallback (owner dogfooding before the repo goes public):
    # the authenticated API tarball endpoint accepts tags and branches and
    # follows a signed redirect using the already-verified gh auth / $GH_TOKEN.
    log "> gh api repos/menil/jobgitops/tarball/$TAG > $TARBALL"
    gh api "repos/menil/jobgitops/tarball/$TAG" > "$TARBALL" ||
        die "failed to download the JobGitOps tarball for '$TAG' — use a valid tag or branch, and ensure your gh auth has read access to the source repo."
}
download_tarball
run mkdir -p "$TMPDIR/tree"
run tar -xzf "$TARBALL" -C "$TMPDIR/tree"

# codeload tarballs extract to <repo>-<ref>; the API tarball endpoint extracts
# to <repo>-<sha>. Resolve whichever single top-level dir landed so the public
# and private-source download paths both work.
SRC=""
if [ "$DRY_RUN" = "1" ]; then
    # The tarball was never downloaded or extracted under --dry-run, so the
    # top-level dir cannot exist yet; log the command the real run would
    # execute and fall through with the codeload layout for the trace.
    log "> SRC=\"\$(find \$TMPDIR/tree -mindepth 1 -maxdepth 1 -type d | head -n 1)\""
    SRC="$TMPDIR/tree/jobgitops-$TAG"
else
    SRC="$(find "$TMPDIR/tree" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    [ -n "$SRC" ] || die "downloaded tarball contained no top-level directory."
fi
APP="$TMPDIR/app"

# ---------------------------------------------------------------------------
# Assemble (§5.3.4) — template/ content verbatim + root-pinned shell plane
# ---------------------------------------------------------------------------

run mkdir -p "$APP/config" "$APP/resumes" "$APP/.github/workflows"
run cp "$SRC/template/config/settings.yaml" "$APP/config/settings.yaml"
run cp "$SRC/template/resumes/resume.yaml" "$APP/resumes/resume.yaml"
run cp "$SRC/template/resumes/template.html" "$APP/resumes/template.html"
run cp "$SRC/template/resumes/style.css" "$APP/resumes/style.css"
run cp "$SRC/template/README.md" "$APP/README.md"
run cp "$SRC/template/.gitignore" "$APP/.gitignore"
# Root-pinned, copied verbatim (spec §3): single source of truth, no drift.
run cp "$SRC/.github/labels.yml" "$APP/.github/labels.yml"
for WF_PATH in "$SRC"/.github/workflows/*.yml; do
    WF="$(basename "$WF_PATH")"
    case "$WF" in
        build-runner.yml|ci.yml|pr-review.yml|release-on-merge.yml)
            ;;
        *)
            run cp "$WF_PATH" "$APP/.github/workflows/$WF"
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Create repo (§5.3.5)
# ---------------------------------------------------------------------------

create_repo() {
    log "> gh repo create $REPO_NAME --$VISIBILITY"
    [ "$DRY_RUN" = "1" ] && return 0
    gh repo create "$REPO_NAME" "--$VISIBILITY"
    rc=$?
    [ "$rc" -eq 0 ] ||
        die "repo create failed (exit $rc). If '$REPO_NAME' already exists, use a different name."
}
create_repo

# ---------------------------------------------------------------------------
# Set provider secret (§5.3.6) — value flows via stdin, never echoed or stored
# ---------------------------------------------------------------------------

set_secret() {
    log "> printf '%s' '<redacted>' | gh secret set $1 --repo $OWNER/$REPO_NAME"
    [ "$DRY_RUN" = "1" ] && return 0
    printf '%s' "$2" | gh secret set "$1" --repo "$OWNER/$REPO_NAME"
    rc=$?
    [ "$rc" -eq 0 ] || die "failed to set $1 (exit $rc)."
}
set_secret "$SECRET_NAME" "$PRIMARY_KEY"
[ -n "$TAVILY_KEY" ] && set_secret "TAVILY_API_KEY" "$TAVILY_KEY"
[ -n "$BRAVE_KEY" ] && set_secret "BRAVE_API_KEY" "$BRAVE_KEY"
[ -n "$JINA_KEY" ] && set_secret "JINA_API_KEY" "$JINA_KEY"

# Check if the active token already has project scopes (populated in check_permissions) so we can set the secret silently
# (Using $SCOPES from check_permissions, no need to query again)

HAS_PROJECT_SCOPE="0"
if [ -n "$SCOPES" ]; then
    case ",$SCOPES," in
        *",project,"*) HAS_PROJECT_SCOPE="1" ;;
    esac
fi

if [ "$HAS_PROJECT_SCOPE" = "1" ]; then
    # Already authorized (either pre-existing or refreshed in check_permissions) — set the secret
    if [ "$DRY_RUN" = "0" ]; then
        PROJECT_TOKEN="$(gh auth token)"
        if [ -n "$PROJECT_TOKEN" ]; then
            set_secret "PROJECT_V2_TOKEN" "$PROJECT_TOKEN"
        fi
    else
        log "> set_secret PROJECT_V2_TOKEN <redacted>"
    fi
fi

# ---------------------------------------------------------------------------
# Enable Actions + write permissions (§5.3.7) — the one thing an in-repo
# workflow can never do for itself.
# ---------------------------------------------------------------------------

run gh api --method PUT "repos/$OWNER/$REPO_NAME/actions/permissions" \
    -F enabled=true -f allowed_actions=all -f default_workflow_permissions=write

# ---------------------------------------------------------------------------
# Push (§5.3.8)
# ---------------------------------------------------------------------------

# Local-only identity so the first commit works even on a machine with no
# global git identity; scoped to this repo, never global.
run git -C "$APP" init -b main
run git -C "$APP" config user.name "$OWNER"
run git -C "$APP" config user.email "$OWNER@users.noreply.github.com"
run git -C "$APP" add -A
run git -C "$APP" commit -m "chore: bootstrap from JobGitOps template $TAG"
run git -C "$APP" remote add origin "https://github.com/$OWNER/$REPO_NAME.git"

# git cannot authenticate the HTTPS push via gh's credential helper when the
# gh account uses the SSH git protocol (or when a PAT is in play with no gh
# auth); feed the token through an askpass shim that reads $JGO_GIT_TOKEN from
# the environment so the secret never touches disk. Reading the token here
# (from gh auth token or the explicit --token/$GH_TOKEN) means no global git
# config is touched and the push works under either git protocol.
#
# Note: the shim is wired via the GIT_ASKPASS env var, not the
# `credential.askpass` config key — git silently ignores the latter
# (verified empirically) and dies with "could not read Username".
if [ "$DRY_RUN" = "0" ]; then
    ASKPASS="$TMPDIR/askpass.sh"
    # Quoted heredoc: content is written verbatim (no expansion here) so the
    # askpass shim reads $JGO_GIT_USERNAME / $JGO_GIT_TOKEN at runtime.
    cat >"$ASKPASS" <<'EOF'
#!/bin/sh
case "$1" in
  *[Uu]sername*) printf "%s\n" "${JGO_GIT_USERNAME:-oauth2}" ;;
  *) printf "%s\n" "$JGO_GIT_TOKEN" ;;
esac
EOF
    chmod +x "$ASKPASS"
    export GIT_ASKPASS="$ASKPASS"
    if [ "$HAS_TOKEN" = "1" ]; then
        export JGO_GIT_TOKEN="${TOKEN:-$GH_TOKEN}"
    else
        JGO_GIT_TOKEN="$(gh auth token)" || die "failed to read the gh auth token — is gh authenticated?"
        export JGO_GIT_TOKEN
    fi
fi

push_repo() {
    log "> git -C $APP push -u origin main"
    [ "$DRY_RUN" = "1" ] && return 0
    git -C "$APP" -c credential.helper= push -u origin main
    rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ "$HAS_TOKEN" = "0" ]; then
            die "git push failed (exit $rc) — run 'gh auth setup-git' and re-push, or re-run with --token."
        fi
        die "git push failed (exit $rc)."
    fi
}
push_repo

# ---------------------------------------------------------------------------
# Summary (§5.3.9)
# ---------------------------------------------------------------------------

cat >&2 <<EOF

Done. Your JobGitOps repo is live: https://github.com/$OWNER/$REPO_NAME

Next steps:
  1. Edit resumes/resume.yaml — replace the placeholder with your real resume.
  2. Commit and push to main — one bootstrap scrape fires automatically.
  3. After that run succeeds, remove the setup badge from the README.

The daily cron takes over from there; tune search in config/settings.yaml.
EOF
