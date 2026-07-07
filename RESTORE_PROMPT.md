# AI-Trading — Disaster-Recovery Restore Prompt

**How to use this file:** on the new machine, install Claude Code, open a
terminal, and paste everything below the line into a fresh Claude Code
session as your first message. Fill in the one `<PATH-TO-BACKUP-ZIP>`
placeholder first. Keep the backup zip (`AI-Trading-backup-*.zip`) at hand —
it contains the secrets and session history that are NOT on GitHub.

---

Restore my AI-Trading project on this machine. Work through every step,
verify as you go, and tell me exactly what you need from me when a step
needs my interactive input. My backup zip is at: `<PATH-TO-BACKUP-ZIP>`

## What this project is

Two automated trading strands sharing ONE Alpaca **paper** account
($100K-ish equity, never live):

1. **Crypto swing** (`src/watcher.py` + `src/trader.py`) — BTC/ETH/SOL/LINK/AVAX,
   4H/daily regime, runs on GitHub Actions cron every 4h (`watcher.yml`),
   long-only, manual-confirm ethos with auto-execute behind `WATCHER_AUTO_EXECUTE`.
2. **Equity day-trade** (`src/day_watcher.py` + `src/day_trader.py`) — 21 mega-cap
   universe, 5-min ORB + VWAP-reclaim setups (long + short mirrors), auto-executes
   on paper behind `WATCHER_DAY_AUTO_EXECUTE`, runs every 5 min during US market
   hours via `day-watcher.yml`.

Alerts go to Telegram. A healthchecks.io dead-man's switch watches the day
watcher. GitHub repo: `stephenkisonge-AI/AI-Trading` (this is the source of
truth for all code + state calendars).

## Where everything lives

| Data | Location | In backup zip? |
|---|---|---|
| Code, workflows, strategy docs, state calendars | GitHub repo | yes (`repo/`, incl. `.git`) |
| Local secrets (`.env`: Alpaca keys, paper flag, Telegram token+chat) | local only | **yes — zip is the only copy** |
| GitHub Actions secrets (see list below) | GitHub repo settings | no — survive with the repo |
| Claude Code memory + session transcripts | `~/.claude/projects/<slug>/` | yes (`claude-project/`) |
| External cron (cron-job.org) + healthchecks.io | external services | no — config documented below |

## Restore steps (do these in order)

1. **Prerequisites.** Check `git`, `gh`, and Python 3.11+ are installed
   (`winget install Git.Git GitHub.cli Python.Python.3.11` on Windows if not).
   Then have me authenticate GitHub by telling me to type:
   `! gh auth login` (choose GitHub.com → HTTPS → login via browser with
   stephenkisongeai@gmail.com). Verify with `gh auth status`.

2. **Clone the repo** into a folder named `AI Trading` (space included) under
   my home directory:
   `gh repo clone stephenkisonge-AI/AI-Trading "AI Trading"` — then `cd` into it.
   If GitHub is somehow gone too, extract `repo/` from the backup zip instead —
   it contains the full working tree AND `.git` history; re-create a GitHub repo
   and push it.

3. **Restore secrets.** Extract `repo/.env` from the backup zip into the repo
   root. It is gitignored — confirm `git status` does NOT list it. Never commit it.

4. **Python environment.** `python -m venv .venv`, activate, then
   `pip install -r requirements.txt`.

5. **Verify the code works.** Run `python -m pytest tests/ -q` (expect all
   green), then a read-only live check:
   load `.env`, call `src.data.get_client().get_account()` and print equity —
   proves the Alpaca paper keys still work.

6. **Restore Claude Code memory + sessions.** Launch `claude` once inside the
   repo folder and exit — this creates `~/.claude/projects/<new-slug>/` (slug is
   derived from the folder path, so it will differ from the old machine's
   `C--Users-StephenAI-AI-Trading`). Then copy the CONTENTS of the zip's
   `claude-project/` folder (memory/ + *.jsonl transcripts + MEMORY.md index)
   into that new slug directory. Memory loads next session; `claude --resume`
   lists the old transcripts.

7. **Verify GitHub Actions infrastructure.** Run `gh secret list` — these 9
   must exist (values for the first 5 are in the restored `.env`; the last 4
   are flags/URLs listed below):
   - `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_TRADE` (= `True`),
     `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - `WATCHER_AUTO_EXECUTE` (= `true`), `WATCHER_DAY_AUTO_EXECUTE` (= `true`),
     `WATCHER_DAY_ENABLE_SHORTS` (= `true`), `HEALTHCHECK_PING_URL`
     (from my healthchecks.io account — if lost, create a new check there and
     set its ping URL; it no-ops gracefully while unset)
   If any are missing (e.g. repo was re-created): `gh secret set NAME`.
   Then confirm workflows are enabled: `gh workflow list`.

8. **Re-arm the external cron** (only if the repo was re-created or the
   cron-job.org account was lost — otherwise it's still running): GitHub's
   free-tier scheduler drops most `*/5` ticks, so the day watcher's PRIMARY
   trigger is cron-job.org POSTing every 5 minutes, Mon–Fri during US market
   hours (~13:00–21:00 UTC), to:
   `https://api.github.com/repos/stephenkisonge-AI/AI-Trading/dispatches`
   with headers `Authorization: Bearer <GitHub PAT with repo scope>`,
   `Accept: application/vnd.github+json` and body
   `{"event_type": "day-watcher-tick"}`. Create a fresh PAT for it.

9. **End-to-end smoke test.** Trigger the Telegram pipeline:
   `gh workflow run day-watcher.yml -f test_ping=true`, then check
   `gh run list --workflow=day-watcher.yml -L 3` for success and confirm the
   🧪 ping arrived in my Telegram. Also run `gh run list` generally to confirm
   scheduled runs are firing.

10. **Local MCP crypto strand (optional, manual-confirm workflow).** The local
    Alpaca MCP server setup is described in `Alpaca MCP Sever and Trading prompt.md`
    and `scripts/start-alpaca-mcp.ps1`. Important known trap (from memory):
    NEVER register it via `powershell -File <path with spaces>` — it breaks
    silently; use the direct executable or `-Command "& '<path>'"`.

## Project rules to carry forward (also in restored memory)

- Paper trading only; live is always manual. `ALPACA_PAPER_TRADE=True` is
  load-bearing in both strands' kill switches.
- Commit + push after every meaningful unit of work.
- Smoke-test any order-placement change against Alpaca paper before pushing.
- When a Telegram alert seems missing, check `gh run list` + the run's
  `phase=` stdout before assuming infrastructure failure.
- Both strands share one paper account — every order/position/lifecycle read
  must be scoped to that strand's own symbol universe.
