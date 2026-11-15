# AI-Trading — Restore Prompt (this machine, workspace `C:\Users\StephenAI\AI Trading`)

**How to use this file:** open a Claude Code session in the trusted workspace
folder `C:\Users\StephenAI\AI Trading` and paste everything below the line as
your first message. The backup zip is **`AI Trading.zip` at
`C:\Users\StephenAI\AI Trading.zip`**. Its contents were extracted and
arranged into place on 2026-07-09: the workspace root IS the git repo root,
and the Claude memory/transcripts were installed into
`~\.claude\projects\C--Users-StephenAI-AI-Trading\`. Steps 2 and 6 below are
therefore verify-or-do: verify first, and only re-extract from the zip if
something is missing.

**⚠️ Backup-zip handling rule:** `AI Trading.zip` contains LIVE API secrets
(`.env`: Alpaca keys, Telegram bot token). It must always exist in at least
one place OFF the machine running the project (private cloud drive, USB) —
a backup that only lives on the machine it protects is not a backup. The copy
at `C:\Users\StephenAI\AI Trading.zip` is ON this machine, so once the restore
succeeds, confirm an off-machine copy still exists (or make one). Never
commit the zip (or `.env`) to the git repo, never share it, and after any
meaningful change to secrets/memory, regenerate it and re-copy it off-machine.

---

Restore my AI-Trading project into this workspace:
`C:\Users\StephenAI\AI Trading` (already the trusted Claude Code folder, and
already the git repo root — the backup zip at
`C:\Users\StephenAI\AI Trading.zip` was extracted and arranged on 2026-07-09).
Work through every step, verify as you go, and tell me exactly what you need
from me when a step needs my interactive input.

## What this project is

Two automated trading strands sharing ONE Alpaca **paper** account
($100K-ish equity, never live):

1. **Crypto swing** (`src/watcher.py` entry scan, `src/swing_manager.py`
   position management, `src/swing_exits.py` + `src/execution.py` orders) —
   BTC/ETH/SOL/LINK/AVAX/DOGE/UNI/XTZ (`src/universe.py`), 4H/daily regime,
   long-only, Setup B (breakout-retest) only — Setup A is retired from live
   scanning. `watcher.yml` scans every 4h at :17 UTC; `swing-manager.yml`
   manages open positions every ~20 min. Auto-executes on paper behind
   `WATCHER_AUTO_EXECUTE` (enabled 2026-11-15 after a live paper smoke test;
   it was `false` from 2026-07-10 until then). Trade journal + scan telemetry
   live in the PRIVATE repo `stephenkisonge-AI/AI-Trading-State`.
2. **Equity day-trade** (`src/day_watcher.py` + `src/day_trader.py`) — 21 mega-cap
   universe, 5-min ORB + VWAP-reclaim setups (long + short mirrors), auto-executes
   on paper behind `WATCHER_DAY_AUTO_EXECUTE`, runs every 5 min during US market
   hours via `day-watcher.yml`.

Alerts go to Telegram. A healthchecks.io dead-man's switch watches the day
watcher and the swing manager, and `doctor.yml` (every 6h) checks that the
external-cron triggers are alive and classifies failed runs. GitHub repo:
`stephenkisonge-AI/AI-Trading` — PUBLIC (free Actions minutes), so nothing
secret may ever be committed to it; it is the source of truth for all code +
state calendars.

## Where everything lives

| Data | Location | Restored on this machine? |
|---|---|---|
| Code, workflows, strategy docs, state calendars | GitHub repo | yes — workspace root is the repo checkout (incl. `.git`), may be stale vs GitHub |
| Crypto trade journal + scan telemetry (`swing/`) | private GitHub repo `AI-Trading-State` | no — lives on GitHub; clone it OUTSIDE this working tree when you need to read it |
| Local secrets (`.env`: Alpaca keys, paper flag, Telegram token+chat) | local only | **yes — `.env` at workspace root; the zip is the only other copy** |
| GitHub Actions secrets (see list below) | GitHub repo settings | no — survive with the repo |
| Claude Code memory + session transcripts | `~\.claude\projects\C--Users-StephenAI-AI-Trading\` | yes — installed 2026-07-09 |
| External cron (cron-job.org) + healthchecks.io | external services | no — config documented below |

## Restore steps (do these in order)

1. **Prerequisites.** Check `git`, `gh`, and Python 3.11+ are installed
   (`winget install Git.Git GitHub.cli Python.Python.3.11` if not).
   Then have me authenticate GitHub by telling me to type:
   `! gh auth login` (choose GitHub.com → HTTPS → login via browser with
   my `stephenkisonge-AI` GitHub account). Verify with `gh auth status`.
   Then keep my personal email out of this public repo's commits:
   `git config --local user.email
   "278677510+stephenkisonge-AI@users.noreply.github.com"`.

2. **Verify the workspace root is the repo root** (already arranged
   2026-07-09). The layout must be: `C:\Users\StephenAI\AI Trading` IS the
   git repo root — the Claude Code project slug
   `C--Users-StephenAI-AI-Trading` is derived from this exact path, so the
   repo must never be nested in a subfolder. Verify with `git status` that
   the workspace root is a valid checkout on `master`. If it is NOT (fresh
   disaster), extract `repo\` from the backup zip and move its CONTENTS
   (including the hidden `.git`, plus `.env`, `.github`, `.claude`,
   `.gitignore`) up into `C:\Users\StephenAI\AI Trading\`.
   Then sync with GitHub, which is the source of truth (the zip snapshot may
   be stale): `git fetch origin` and fast-forward `git pull` if behind. Only
   if the GitHub repo is gone: the local `.git` contains full history —
   re-create the repo on GitHub and push.

3. **Verify secrets.** `.env` must sit at the workspace root (already there;
   if missing, re-extract `repo/.env` from
   `C:\Users\StephenAI\AI Trading.zip`). It is gitignored — confirm
   `git status` does NOT list it, and confirm `.gitignore` still contains
   `.env` before your first commit on this machine. If it ever shows as
   tracked/staged, STOP and fix `.gitignore` first.

4. **Python environment.** `python -m venv .venv`, activate, then
   `pip install -r requirements.txt`.

5. **Verify the code works.** Run `python -m pytest tests/ -q` (expect all
   green), then a read-only live check:
   load `.env`, call `src.data.get_client().get_account()` and print equity —
   proves the Alpaca paper keys still work.

6. **Verify Claude Code memory + sessions** (already installed 2026-07-09).
   The project slug directory
   `C:\Users\StephenAI\.claude\projects\C--Users-StephenAI-AI-Trading\`
   must contain `memory\MEMORY.md` (plus the other memory files) and the
   restored `*.jsonl` transcripts — `claude --resume` should list the old
   sessions. If missing (fresh disaster), extract `claude-project\` from the
   backup zip and copy its CONTENTS into that slug directory — do not
   overwrite this session's own newer files if names collide — then delete
   the extracted folder: it must never sit inside the git working tree,
   because the transcripts must never be committed.

7. **Verify GitHub Actions infrastructure.** Run `gh secret list` — these 11
   must exist (values for the first 5 are in the restored `.env`; the rest
   are flags/URLs/tokens listed below):
   - `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_TRADE` (= `True`),
     `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - `WATCHER_AUTO_EXECUTE` (= `true`), `WATCHER_DAY_AUTO_EXECUTE` (= `true`),
     `WATCHER_DAY_ENABLE_SHORTS` (= `true`), `HEALTHCHECK_PING_URL`
     (from my healthchecks.io account — if lost, create a new check there and
     set its ping URL; it no-ops gracefully while unset)
   - `STATE_REPO_TOKEN` — fine-grained PAT scoped to `AI-Trading-State`
     (contents: read/write). Without it the crypto strand has no journal and
     fails closed to alerts-only. `FINNHUB_API_KEY` — free-tier key used by
     the weekly earnings-calendar refresh.
   If any are missing (e.g. repo was re-created): `gh secret set NAME`.
   Secret VALUES cannot be read back, so the list above is the intended
   state, not proof. Prove the real state from a run log: `gh run view
   <id> --log` on a Watcher run prints `[watcher] auto-execute enabled:`
   (GitHub masks the word `True` as `***`; `False` prints in clear) and a
   Day Watcher run prints the equivalent line.
   Then confirm all 5 workflows are active: `gh workflow list` (Watcher,
   Swing Manager, Day Watcher, Doctor, Refresh day-trade calendars).

8. **Re-arm the external cron** (only if the repo was re-created or the
   cron-job.org account was lost — otherwise it's still running): GitHub's
   free-tier scheduler delays and drops ticks, so every trading workflow's
   PRIMARY trigger is a cron-job.org job POSTing to
   `https://api.github.com/repos/stephenkisonge-AI/AI-Trading/dispatches`
   with headers `Authorization: Bearer <GitHub PAT with repo scope>`,
   `Accept: application/vnd.github+json`. Three jobs, one per body (set each
   job's time zone to UTC):

   | Body | Schedule | Workflow |
   |---|---|---|
   | `{"event_type": "day-watcher-tick"}` | every 5 min, Mon–Fri, ~13:00–21:00 UTC | `day-watcher.yml` |
   | `{"event_type": "swing-manage-tick"}` | every 20 min, every day | `swing-manager.yml` |
   | `{"event_type": "crypto-watcher-tick"}` | minute 17 of hours 0, 4, 8, 12, 16, 20, every day | `watcher.yml` |

   All three share one no-expiration classic PAT (note
   `cron-job-org-dispatch`, repo scope); each job stores its own copy, so a
   rotated token must be updated in all three. A successful test run returns
   HTTP 204. Each workflow keeps its native schedule as a backup, so a dead
   job does not turn runs red — it only degrades cadence. The Doctor alerts
   on a silent dispatch trigger, and crypto scans done by the backup are
   labelled `(FALLBACK)` in Telegram.

9. **End-to-end smoke test.** Trigger the Telegram pipeline:
   `gh workflow run day-watcher.yml -f test_ping=true`, then check
   `gh run list --workflow=day-watcher.yml -L 3` for success and confirm the
   🧪 ping arrived in my Telegram. Also run `gh run list` generally to confirm
   scheduled runs are firing.

10. **Local MCP server (optional — for inspecting the account from a Claude
    session; the trading strands do not depend on it).** The local Alpaca MCP
    server setup is described in `Alpaca MCP Sever and Trading prompt.md`
    and `scripts/start-alpaca-mcp.ps1`. Important known trap (from memory):
    NEVER register it via `powershell -File <path with spaces>` — it breaks
    silently; use the direct executable or `-Command "& '<path>'"`. If it
    times out at session start, `/mcp` reconnects it.

## Project rules to carry forward (also in restored memory)

- **NEVER commit live API secrets to GitHub.** No Alpaca keys, Telegram
  tokens, healthcheck URLs, PATs, `.env` files, or the backup zip may ever
  appear in a commit, PR, workflow file, log statement, or committed doc —
  secrets go ONLY in the local `.env` (gitignored) and GitHub Actions
  **secrets** (`gh secret set`). Before every commit, scan the staged diff
  for anything that looks like a key or token. If a secret ever reaches a
  commit — even one that was never pushed — treat it as burned: rotate the
  Alpaca keys and Telegram token immediately, then rewrite/force-remove the
  commit.
- Paper trading only; live is always manual. `ALPACA_PAPER_TRADE=True` is
  load-bearing in both strands' kill switches.
- Commit + push after every meaningful unit of work.
- Smoke-test any order-placement change against Alpaca paper before pushing.
- When a Telegram alert seems missing, check `gh run list` + the run's
  `phase=` stdout before assuming infrastructure failure.
- Both strands share one paper account — every order/position/lifecycle read
  must be scoped to that strand's own symbol universe.
- Alpaca takes its crypto BUY fee out of the purchased quantity (position =
  fill − 0.25%). The journal records that as `entry_fee_qty`; never assume
  position quantity equals filled quantity.
