# quotaboard

A local page that shows how much of your LLM quotas you have used and what is left: Claude Code, Antigravity, Codex and OpenRouter. It breaks spending down by session, agent and model, flags waste with plain rules, and lets you act from the page. Every action asks for confirmation first.

It runs on your own computer, listens only on `127.0.0.1`, and needs no account or API key of its own. It reads the files and CLIs you already have.

## What you get

The page is laid out like a strip-chart recorder's paper: what happened is drawn in ink, the pace ahead in dashes, and limits in red pen.

- **Recorder**: every quota on one shared time axis (the last 18 hours and the next 6), one lane each, with what is left and when it resets.
  - **Claude**: the current 5-hour window drawn as spend inside each window, against an estimated cap. The dashed pace line marks the time it fills, and the plan tier is shown. Claude's own percentages are only shown in the Claude app; you can type them in and the page shows how old they are.
  - **Antigravity**: every limit `agy -p /usage` reports (Gemini, Claude/GPT, Argon; 5-hour and weekly), for as many accounts as you configure. Readings are stored, so each lane builds a trace over time. When a quota falls steadily, the page projects when it runs out.
  - **Codex**: limits from `codex app-server` (no model call).
  - **OpenRouter**: free requests and credit for the key opencode signed in with.

  Hover anywhere on the recorder to read every lane at that moment.
- **Notes**: plain rules, each with the reason it fired, and an action button where one applies. For example:
  - "this session is 45% of today's spend, start a new one";
  - "this session carries a 600k-token context";
  - "agent X uses an expensive model";
  - "issue ABC-12 has failed 3 runs in a row";
  - "at this pace the window fills at 18:40".
- **Today**: Claude spend against the 7-day average, the open window, this week, and how much went to cache reads.
- **Analysis**, filterable by time (today, this window, 7 days), by kind (Multica, long-running, interactive, scheduled, side session) and by text:
  - **who spent it**, with the top sessions;
  - **when it burns**: an hour-by-day heatmap of the last 7 days;
  - **by day and kind**, and **models by day** (21 days);
  - **what the tokens cost**: input, output, cache write and cache read, as share of tokens and as share of cost. Cache reads are usually almost all of the tokens and much of the cost;
  - **5-hour windows** against the cap;
  - **projects**, and Multica **agents** and **issues**;
  - **sessions**: a sortable table with each session's context size;
  - **Multica runs**: outcome per agent, failure reasons, and the run list with cancel buttons;
  - **agents and switches**: change an agent's model or thinking level, and pause or resume a watchdog that honours a stop file.
- **English and Turkish**: the page follows the browser's language. The EN/TR switch in the corner is remembered.
- **Light and dark**: the page follows the system setting.

Nothing on the page deletes data or spends money.

## Requirements

- Python 3.9+ (standard library only)
- [ccusage](https://github.com/ryoppippi/ccusage) for Claude spending: `npm i -g ccusage`

Optional: `claude`, `agy` (Antigravity CLI), `codex`, `opencode`, `multica`. Each one that is installed adds its section.

## Install

```bash
git clone https://github.com/cmlonder/quotaboard.git ~/Dev/quotaboard
~/Dev/quotaboard/install.sh
```

`install.sh` links `quotaboard` into `~/.local/bin` and lists which optional tools it found.

## Use

```bash
quotaboard
```

This opens `http://127.0.0.1:9998` in your browser. Press Ctrl-C in the terminal to stop it. The first collection takes about half a minute, and after that each source refreshes every 1–5 minutes.

- `quotaboard --no-open` starts it without opening a browser.
- `quotaboard --port 9000` uses another port.
- `python3 quotaboard.py --once` collects once and prints the JSON the page reads, which is handy for checks.

## Configure

The config is optional. Without it, quotaboard detects what is installed and uses defaults. To change something:

```bash
mkdir -p ~/.config/quotaboard
cp config.example.json ~/.config/quotaboard/config.json
```

| Key | What it does |
|---|---|
| `port` | Port to listen on (default 9998). |
| `window_cap_usd` | Your Claude 5-hour cap in ccusage dollars. Without it, the cap is the median of recent windows that ended early after heavy use, i.e. windows that most likely hit the limit. |
| `antigravity` | One entry per account: `name`, `command` (an `agy` wrapper signed in to that account), `note`, and `runtimes` (Multica runtime names that use it). Default: plain `agy`. |
| `codex`, `openrouter` | Set to `false` to skip them. |
| `multica.command` | How to call the CLI, e.g. `["multica", "--profile", "…", "--workspace-id", "…"]`. Default: `["multica"]` when it is installed. |
| `multica.days` | How many days of runs to load (default 7). |
| `multica.agent_files` + `multica.push_command` | Set these if you keep agents as files with a front matter (`name`, `model`, `thinking_level`, `runtime_id`, `id`) and push them to Multica with your own script. A model change then rewrites the file, runs the push command and commits the file (`commit: false` to skip the commit). Without them, the page uses `multica agent update`. |
| `stop_files` | Switches: `name`, `path` of the stop file, optional `log` to tail, and `description`. |
| `rules.expensive_models` | Model name parts to flag (default `["fable"]`). |
| `rules.opus_allowed_agents` | If set, any other Claude agent on Opus is flagged. |
| `rules.session_share`, `rules.context_warning`, `rules.long_run_minutes` | Advice thresholds (defaults 0.3, 400000, 60). |

The config lives in `~/.config/quotaboard/` (or wherever `QUOTABOARD_CONFIG` points), outside the repo. So do the Claude percentages you type in (`claude-percentages.json`) and the quota readings (`history.jsonl`, kept for 14 days).

## How the numbers are made

- **Claude cost per session**: ccusage reports a session's lifetime total, not what it spent today. So quotaboard reads `~/.claude/projects/**/*.jsonl` itself, incrementally, and sums tokens per session, hour and model.
  - Each token type gets Anthropic's price ratio: input 1, output 5, cache write 1.25, cache read 0.1.
  - The weighted total is multiplied by a per-model unit price fitted from ccusage's own daily cost. The totals therefore agree with ccusage.
- **Claude window**: comes from `ccusage blocks`. The percentage is this window's spend divided by the cap above. It is an estimate; the Claude app shows the real figure.
- **Daily and weekly charts**: show Claude models only. ccusage also prices some Antigravity models, but those do not count against the Claude plan.
- **Multica sessions**: a run's Claude transcript sits in a folder like `…multica-workspaces…-abc-12-<last 12 hex of the run id>-worktree`. That ties the session to its agent and issue.

## Security

- The server binds to `127.0.0.1` and answers only requests whose `Host` is `127.0.0.1` or `localhost`. This blocks DNS rebinding.
- Actions need a token that is generated at start and embedded in the page. Other sites cannot trigger them.
- The OpenRouter key is read from opencode's auth file and sent only to `openrouter.ai`. It never reaches the page.

## Files

```
quotaboard.py         server, collectors, advice rules, actions
quotaboard.html       the page (no build step, no external assets; EN and TR strings inside)
PRODUCT.md            what the product is for, used by design tooling
bin/quotaboard        launcher
install.sh            puts the launcher on PATH
config.example.json   every config key with an example
```
