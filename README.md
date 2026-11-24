# ccs — Claude Code Sessions

`ccs` lists every [Claude Code](https://docs.anthropic.com/en/docs/claude-code) session you have run, across all your projects, newest first. Search it, preview what you asked, and press Enter to jump back into a session in the right folder.

![ccs: statistics on top, sessions of all projects below with cost, time and context fill, the selected session's context and cache bars and prompts on the right; then text search and renaming](demo/ccs.svg)

*The demo runs on made-up sessions ([demo/make_demo.py](demo/make_demo.py)).*

## Why not `claude --resume`?

The built-in picker only shows sessions of the folder you are in. `ccs` shows all of them in one place, with the project, cost, duration and state of each, and opens a session from any directory.

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

Run the same command again to upgrade.

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

## Usage

```bash
ccs                 # interactive list
ccs -s kafka lag    # only sessions that talk about both words, in text search
ccs --list          # plain table, no fzf
ccs stats           # activity heatmap, last 14 days, models, projects
ccs --version
ccs --help
```

| Key | Action |
|---|---|
| type | filter by title, project or date |
| ↑ ↓ | move; the preview follows |
| Enter | resume the session in its folder (`cd <folder> && claude --resume <id>`) |
| Ctrl-S | search inside conversations; press again to go back to the list |
| Ctrl-R | rename the session; Claude Code's own `/resume` shows the new name too |
| Ctrl-T | statistics full screen (`ccs stats`); arrows scroll, `q` goes back to the list |
| Esc | quit |

A session that is already running is not opened a second time without asking:

- A background session (`claude --bg`) is attached with `claude attach <id>`.
- A session open in another terminal or an IDE (`●` or `○`): `ccs` says where it is open (pid and terminal) and asks
  whether to open a copy (`claude --resume <id> --fork-session`) or to resume it anyway. Enter cancels.

Search examples (fzf fuzzy search, words narrow it down):

| Type | Finds |
|---|---|
| `payments` | sessions in the `payments-api` project |
| `'2026-11-2` | sessions from 20–29 November 2026 (`'` = exact match) |
| `refund retry` | sessions whose title mentions both words |

Ctrl-R writes the name the way `/rename` does and keeps the session's place in Claude Code's own list. A session
that is open in a running Claude is not renamed, because that Claude would write its old name back: use `/rename`
there.

### Search inside conversations

Ctrl-S turns the prompt into `text>`: what you type is looked up in your prompts and in Claude's replies, not in the
title alone. Tool calls, their output and subagents are left out, so a match is something that was said.

- Every word has to occur somewhere in the session, in any case. A word matches inside longer words, so a stem finds
  its forms: `кеш` finds `кешу` and `кешування`.
- `"retry policy"` in quotes is one phrase.
- The preview shows the messages that match, with the words highlighted.
- Ctrl-S again goes back to the list; the query stays and filters the list as usual.

`ccs -s WORDS` opens straight in text search, and `ccs -s WORDS --list` prints the matching sessions.

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

### `ccs stats`

How you work with Claude, from the same cache as the list (instant after the first run):

- **Activity**: a GitHub-style heatmap of Claude's working time per day, weeks as columns, Monday on top; four greens
  by quartile of your active days. Below it: active days, the busiest day and the current streak. It covers the weeks
  Claude Code still keeps (see [Keep more history](#keep-more-history)), at most `--weeks` (26 by default).
- **Last 14 days**: sessions, cost and Claude time per day, with a bar by cost.
- **Models**: cost and share, sessions, output tokens and how much of the input came from the prompt cache.
- **Projects**: the ten most expensive, with sessions, cost and Claude time.

Claude time adds up across sessions that run at the same time, so a busy day can show more than 24 hours.
`ccs stats --json` prints days, models, projects and totals for scripts.

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

The preview also shows two bars: 🧠 the context window (tokens and model) and ⚡ how much of the input Claude read from the prompt cache over the whole session, subagents included. Cached input costs a tenth, so green is good there.

## How it works

- Claude Code stores each session as `~/.claude/projects/<project>/<session-id>.jsonl`, and its subagents under `<session-id>/subagents/`. A running Claude Code registers its session in `~/.claude/sessions/<pid>.json`, which gives `●` and `○`. `ccs` reads these files and nothing else; your sessions never leave your machine.
- Prices come from [LiteLLM's public price table](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json), the same source [ccusage](https://github.com/ryoppippi/ccusage) uses. `ccs` downloads it once a week and keeps only the Claude models. Costs match ccusage within a few cents per session, except for sessions forked from another session.
- Everything it reads is cached in `~/.cache/cc-sessions`: `index-v<N>.json` for the list, `text-v<N>.json` for text search. The first run reads all sessions (a few seconds for a few GB); after that only sessions that changed are read again.

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

The code is in `src/ccs/`: `sessions.py` reads transcripts, `pricing.py` handles prices, `index.py` the cache, `cli.py` the list and fzf, `search.py` text search, `launch.py` how a session is opened.
