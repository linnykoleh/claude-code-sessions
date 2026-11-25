# ccs — Claude Code Sessions

`ccs` lists every [Claude Code](https://docs.anthropic.com/en/docs/claude-code) session you have run, across all your projects, newest first. Search it, preview what you asked, and press Enter to jump back into a session in the right folder.

![ccs: statistics on top, sessions of all projects below with cost, time and context fill, the selected session's lines added and removed, context and cache bars and prompts on the right; then search and renaming](demo/ccs.svg)

## Install

One command, on macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/linnykoleh/claude-code-sessions/main/install.sh | bash
```

The script installs what is missing and then the `ccs` command itself:

| What | Why | How |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | installs and runs `ccs` in its own environment | `mise use -g uv`, else Homebrew |
| [fzf](https://github.com/junegunn/fzf) | the interactive list | `mise use -g fzf`, else Homebrew |
| `ccs` | this tool (Python 3.9+, no other packages) | `uv tool install` |

Already have uv and fzf? Install directly:

```bash
uv tool install git+https://github.com/linnykoleh/claude-code-sessions
# or
pipx install git+https://github.com/linnykoleh/claude-code-sessions
```

From a clone:

```bash
git clone https://github.com/linnykoleh/claude-code-sessions.git
cd claude-code-sessions
./install.sh          # or: mise run install
```

## Upgrade

Run the install command again, the same way you installed:

```bash
curl -fsSL https://raw.githubusercontent.com/linnykoleh/claude-code-sessions/main/install.sh | bash
# or: uv tool upgrade claude-code-sessions-cli
# or: pipx reinstall claude-code-sessions-cli
# or, in a clone: git pull && ./install.sh
```

The script prints the version change, for example `Upgraded ccs 0.1.0 -> 0.2.0`. `ccs --version` shows the version you have.

## Why not `claude --resume`?

The built-in picker only shows sessions of the folder you are in. `ccs` shows all of them in one place, with the project, cost, duration and state of each, and opens a session from any directory.

## Usage

```bash
ccs                 # interactive list
ccs -s kafka lag    # only sessions that have both words, as if typed in the list
ccs --list          # plain table, no fzf
ccs --version
ccs --help
```

| Key | Action |
|---|---|
| type | search titles, projects, branches, dates and conversations ([Search](#search)) |
| ↑ ↓ | move; the preview follows |
| Enter | resume the session in its folder (`cd <folder> && claude --resume <id>`) |
| Ctrl-R | rename the session; Claude Code's own `/resume` shows the new name too |
| Esc | quit |

A session that is already running is not opened a second time without asking:

- A background session (`claude --bg`) is attached with `claude attach <id>`.
- A session open in another terminal or an IDE (`●` or `○`): `ccs` says where it is open (pid and terminal) and asks
  whether to open a copy (`claude --resume <id> --fork-session`) or to resume it anyway. Enter cancels.

Ctrl-R writes the name the way `/rename` does and keeps the session's place in Claude Code's own list. A session
that is open in a running Claude is not renamed, because that Claude would write its old name back: use `/rename`
there.

### Search

What you type is looked up everywhere at once: in the title, the project, the git branch, the day of the last message,
your prompts and Claude's replies. Tool calls, their output and subagents are left out, so a match in a conversation
is something that was said.

| Type | Finds |
|---|---|
| `payments` | sessions in the `payments-api` project |
| `2026-11-2` | sessions last active 20–29 November 2026 |
| `PAY-512` | sessions on the branch `PAY-512-refund-retries` |
| `payments retry` | sessions of `payments-api` that mention a retry |
| `"retry policy"` | sessions where the phrase was said |

- Every word has to occur somewhere in the session, in any case; the words may come from different places. A word
  matches inside longer words, so a stem finds its forms: `кеш` finds `кешу` and `кешування`.
- `"retry policy"` in quotes is one phrase.
- The preview shows the messages that match, with the words highlighted; when only the title, project, branch or
  date matches, it shows your prompts.

`ccs -s WORDS` opens the list with the words typed in, and `ccs -s WORDS --list` prints the matching sessions.

The first search reads the text of all sessions (a few seconds); after that only changed sessions are read again.

`--list` is handy in scripts:

```bash
ccs --list | grep payments-api                    # all sessions of one project
ccs -s "retry policy" --list                      # sessions that mention the phrase
ccs --list | awk -F'\t' '$1 >= "2026-11-20"'         # sessions since 20 November
```

### Statistics

The table on top sums your sessions for today, the last 7 days, the last 30 days and everything `ccs` can see. A session counts in a period if you worked in it then. COST and CLAUDE TIME count only what was spent in the period, by the day of each message, so a session started yesterday and continued today adds only today's part to Today. CLAUDE TIME is how long Claude worked on its turns, not how long the session was open. Top projects are the three that cost the most overall.

The line under the keys counts sessions by status.

### Columns

| Column | Meaning |
|---|---|
| LAST ACTIVITY | time of the last message, local time |
| PROJECT | the folder the session ran in |
| COST | what the tokens would cost at Anthropic API list prices, subagents included. On a subscription this is not what you pay; it shows how much work went into the session |
| TIME | from the first to the last message |
| CTX | how full the context window was at the last answer: green below 50 %, yellow up to 80 %, red from 80 %, when Claude Code is about to compact. The window comes from LiteLLM's table (1M for current models); a session that already held more than 200K counts as 1M |
| (status) | `●` Claude is working in it right now; `○` it is open in a terminal and waits for you; `!` it was closed before Claude finished its turn: you pressed Esc, declined a tool, or your last prompt got no answer. Blank: closed after Claude answered |
| TITLE | the name you gave it with `/rename`, else Claude's title, else your first prompt. It comes last, so a narrow window cuts only the title |

In its top right corner, the preview shows how many lines Claude added (green `+`) and removed (red `-`) in the session with Edit and Write, subagents included, counted the way `git diff --stat` counts them. Changes made through Bash (`sed`, `git checkout`) and your own edits are not in the transcript, so they do not count.

Under the title, the preview names the session's folder, branch and session ID, then shows two bars: Context, how full the context window is (tokens and model), and Cache, how much of the input Claude read from the prompt cache over the whole session, subagents included. Cached input costs a tenth, so green is good there.

## How it works

- Claude Code stores each session as `~/.claude/projects/<project>/<session-id>.jsonl`, and its subagents under `<session-id>/subagents/`. A running Claude Code registers its session in `~/.claude/sessions/<pid>.json`, which gives `●` and `○`. `ccs` reads these files and nothing else; your sessions never leave your machine.
- Prices come from [LiteLLM's public price table](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json), the same source [ccusage](https://github.com/ryoppippi/ccusage) uses. `ccs` downloads it once a week and keeps only the Claude models. Costs match ccusage within a few cents per session, except for sessions forked from another session.
- Everything it reads is cached in `~/.cache/cc-sessions`: `index-v<N>.json` for the list, `text-v<N>.json` for search. The first run reads all sessions (a few seconds for a few GB); after that only sessions that changed are read again.

Settings, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude` | where Claude Code keeps its data |
| `XDG_CACHE_HOME` | `~/.cache` | where the cache goes |
| `CCS_OFFLINE` | unset | `1` = never download prices; COST shows `—` until a price table is cached |
| `FZF` | `fzf` on PATH | path to a specific fzf binary |

## Keep more history

Claude Code deletes sessions older than 30 days by default. To keep a year of them, add this to `~/.claude/settings.json`:

```json
{ "cleanupPeriodDays": 365 }
```

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/linnykoleh/claude-code-sessions/main/install.sh | bash -s -- --uninstall
# or: uv tool uninstall claude-code-sessions-cli && rm -rf ~/.cache/cc-sessions
```

uv and fzf stay installed.

## Troubleshooting

- **`ccs: command not found` after installing.** uv puts commands in `~/.local/bin`. Run `uv tool update-shell` and open a new terminal.
- **COST shows `—`.** The price table could not be downloaded (no internet, proxy) and none is cached yet, or the model is too new for the table. It fills in on a later run.
- **Old sessions are missing.** Claude Code already deleted them; see [Keep more history](#keep-more-history).
- **"Folder … no longer exists".** The project folder was moved or deleted. The message shows the session ID, so you can still run `claude --resume <id>` from the new location.

## Development

```bash
mise install            # uv and fzf
mise run test           # or: uv run --group dev pytest -q
uv run ccs --list       # run from the checkout
python demo/record.py   # re-record demo/ccs.svg on made-up sessions (needs tmux)
```

The code is in `src/ccs/`: `sessions.py` reads transcripts, `pricing.py` handles prices, `index.py` the cache, `cli.py` the list and fzf, `search.py` search, `launch.py` how a session is opened.
