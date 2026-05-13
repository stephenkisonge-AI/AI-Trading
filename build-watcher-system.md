# Build My Strategy Watcher + Cost Tracking System

You are going to help me build a complete monitoring system around the
crypto strategy in `Crypto Strategy.md`. Multi-step build. Walk me
through it phase by phase, pausing for confirmation between phases.

---

## My environment — read this carefully

- **OS:** Windows 11, PowerShell. Every command in this build must be
  PowerShell-native. NO bash idioms (`ls`, `find`, `grep`, `cd ~`,
  `$VAR` in single-quoted strings, etc.).
- **Working folder:** `C:\Users\StephenAI\AI Trading` (note the SPACE
  in the folder name — paths with spaces need quoting)
- **Git:** GitHub CLI (`gh`) is installed and already authenticated.
  My GitHub username is `stephenkisonge-AI`. (My Windows username
  `StephenAI` is different — don't conflate them.)
- **Repo:** Already exists at
  `https://github.com/stephenkisonge-AI/AI-Trading` (private).
  Do NOT run `gh repo create` — it would fail or create a duplicate.
- **Tier:** GitHub free tier. Private repo Actions cap is 2000
  min/month. We must design for that ceiling.
- **Subscription:** Claude Pro. Token costs for my Claude Code
  investigations are covered by the subscription.
- **Timezone:** Nairobi, Kenya (UTC+3 / EAT). Scans fire once per day
  in the morning local time.

### What's already in `C:\Users\StephenAI\AI Trading`

```
AI Trading/
├── .claude/
│   └── settings.local.json
├── scripts/
│   ├── compute_regime.py       # PRIMARY working classifier — refactor
│   ├── direct-stderr.log       # Log (leave alone)
│   ├── direct-stdout.log       # Log (leave alone)
│   ├── mcp-stderr.log          # Log (leave alone)
│   ├── mcp-stdout.log          # Log (leave alone)
│   └── start-alpaca-mcp.ps1    # FALLBACK MCP launcher — leave alone
├── .env                        # ALPACA_API_KEY + ALPACA_SECRET_KEY present
├── .env.example                # Template (leave alone)
├── .gitignore
├── Alpaca MCP Sever and Trading prompt.md
├── Crypto Strategy.md          # THE STRATEGY SPEC — exact filename
└── preferences.md
```

### Role of existing files — critical to understand

- **`scripts/compute_regime.py`** is the PRIMARY working scanner. We
  will refactor it in Phase 3 so its logic lives in `src/strategy.py`
  and `compute_regime.py` becomes a thin CLI wrapper that imports from
  there. Single source of truth — no duplicated math.
- **`scripts/start-alpaca-mcp.ps1`** is my FALLBACK. It launches the
  Alpaca MCP server locally so I can run scans manually through Claude
  Code if the GitHub Actions watcher fails or I want to investigate
  ad-hoc. **DO NOT TOUCH IT.** It must remain working as my backup.
- **Trade execution path:** When the watcher fires a Telegram alert,
  I open Claude Code and execute trades through the Alpaca MCP with
  manual confirmation. The watcher does NOT place orders. The Python
  scripts do NOT place orders. Only Claude Code + MCP places orders,
  and only after I confirm.

### Files that must NOT be modified, renamed, or deleted

- `.claude/settings.local.json`
- `scripts/start-alpaca-mcp.ps1`
- `scripts/direct-stderr.log`, `scripts/direct-stdout.log`,
  `scripts/mcp-stderr.log`, `scripts/mcp-stdout.log`
- `Crypto Strategy.md`
- `Alpaca MCP Sever and Trading prompt.md`
- `preferences.md`
- `.env.example`

`scripts/compute_regime.py` IS modified in Phase 3 (refactored, not
replaced). `.env`, `.gitignore`, `requirements.txt` are appended to,
never overwritten.

---

## What we're building

Three integrated pieces, all in the `AI-Trading` repo:

### Piece 1 — Watcher script (runs ONCE per day, no human, no tokens)

A Python script running on GitHub Actions at **06:00 Nairobi time
(03:00 UTC)** every day. Pulls Alpaca data via REST, evaluates the
strategy's setup conditions on closed daily/4H/1H candles, sends me a
Telegram alert **only when something qualifies**. Zero tokens. Zero
Claude. Just Python + Alpaca REST API.

**Why once per day:**
- Strategy is swing-style (hold hours to days), so a morning scan
  catches everything that matters
- 1 run/day ≈ 30 min/month of Actions time (well under the 2000 min
  free-tier cap)
- 06:00 EAT gives me coffee-time visibility before the day starts

**Known trade-off — accepted:**
`Crypto Strategy.md` specifies scans on every 4H candle close (00, 04,
08, 12, 16, 20 UTC — 6×/day). The watcher fires 1×/day. **This will
miss most intraday Setup B (breakout retest) windows**, which are
timing-sensitive and often resolve within hours. Setup A (pullback)
tolerates the daily cadence well; Setup B does not. We're accepting
reduced Setup B coverage in exchange for the budget. If Setup B coverage
matters more later, we can add a second cron (e.g., 14:02 UTC) — still
well inside the 2000 min cap.

### Piece 2 — Telegram bot

Delivers alerts to my phone. Push notification → I open Claude Code
→ I run a scan via Alpaca MCP to confirm → I confirm and execute
via MCP.

### Piece 3 — Cost tracking inside Claude Code

Per-investigation token logging + on-demand daily summaries. I'm on
Claude Pro, so dollars don't change with usage — but tracking helps
me understand my consumption against the Pro 5-hour rate limits and
shows the API-equivalent cost for context.

---

## Phase 1 — Telegram bot setup

Walk me through these interactively, one at a time. Pause after each.

### 1.1 Create the bot

Tell me: "Open Telegram. Search for **@BotFather** (blue verified
checkmark). Send `/newbot`. Provide:

- A **name** (e.g., 'AI Trading Watcher') — any text
- A **username** ending in `bot` (e.g., `stephenai_ai_trading_bot`) —
  must be globally unique

BotFather replies with a token like
`1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`. Copy it. Type 'done'."

**[PAUSE]**

### 1.2 Get my chat ID

Tell me: "In Telegram, find your new bot by its username. Send it any
message (e.g., 'hi'). Then open in your browser:
`https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`

Find `\"chat\":{\"id\":<NUMBER>` — that number is your chat ID.
Type 'done' with the value ready."

**[PAUSE]**

### 1.3 Test message (PowerShell)

```powershell
$token  = "<bot token>"
$chatId = "<chat id>"
Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" `
    -Method Post `
    -Body @{ chat_id = $chatId; text = "Watcher test received" }
```

Confirm I got it on Telegram. **[PAUSE]**

---

## Phase 2 — Repository setup

### Status checklist

- ✅ `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` already in `.env`
- ✅ Git CLI authenticated as `stephenkisonge-AI`
- ✅ `Crypto Strategy.md` at folder root (exact name)
- ✅ GitHub repo exists at `github.com/stephenkisonge-AI/AI-Trading`
  (private) — just need to verify the local folder is connected to it
- ❌ Telegram credentials need adding to `.env` and as secrets
- ❌ `ALPACA_PAPER_TRADE=True` needs adding to `.env` and as a secret

### 2.1 Verify folder + git state

```powershell
cd "C:\Users\StephenAI\AI Trading"
Get-ChildItem -Force
git status
git remote -v
Select-String -Path .gitignore -Pattern "^\.env$"
```

Confirm:
- `Crypto Strategy.md` is present (exact name with space, capital C, S)
- `.env` is in `.gitignore`
- `git status` does NOT show `.env` as tracked

If `.env` is not gitignored, STOP. My Alpaca keys are in there.

### 2.2 Verify repo connection

The repo already exists on GitHub. We just need the local folder
pointing at it. Check `git remote -v` output:

- **Case A:** Already shows `origin` pointing to
  `https://github.com/stephenkisonge-AI/AI-Trading` → done, skip to 2.3.
- **Case B:** Local git repo, no remote → add the existing repo as
  origin:
  ```powershell
  git remote add origin https://github.com/stephenkisonge-AI/AI-Trading.git
  git branch -M main
  # Try fetching first to see what's on the remote
  git fetch origin
  git status
  ```
  If the remote has commits you don't have locally, pull them. If your
  local has commits the remote doesn't, push them. Tell me what
  `git status` shows BEFORE pushing or pulling — we sort that out
  together to avoid losing work.
- **Case C:** Not a git repo at all → initialize and connect:
  ```powershell
  git init
  # CRITICAL — verify .env in .gitignore BEFORE the next line
  Select-String -Path .gitignore -Pattern "^\.env$"
  # Only proceed if match found
  git add .
  git status              # confirm .env NOT in list
  git commit -m "Initial commit"
  git remote add origin https://github.com/stephenkisonge-AI/AI-Trading.git
  git branch -M main
  git fetch origin
  # If remote has content, may need: git pull origin main --allow-unrelated-histories
  git push -u origin main
  ```

**DO NOT** run `gh repo create AI-Trading` — the repo already exists.
That command would fail with "name already exists" and could be
confusing. Tell me which case applies before any destructive action.

### 2.3 Check for duplicate strategy files

```powershell
Get-ChildItem -Filter *.md
Get-ChildItem -Recurse -Filter "*crypto*strategy*" |
    Where-Object { $_.FullName -notmatch "\\\.git\\" }
```

Exactly ONE file should match: `Crypto Strategy.md`. If you see
variants (`crypto-strategy.md`, `my-crypto-strategy.md`, etc.), show
me with dates/sizes and ASK which is the source of truth. Do not
silently rename or delete.

### 2.4 Append missing values to `.env`

```powershell
$envContent = Get-Content .env -Raw

# Add ALPACA_PAPER_TRADE if missing
if ($envContent -notmatch "ALPACA_PAPER_TRADE=") {
    Add-Content -Path .env -Value "`nALPACA_PAPER_TRADE=True"
    Write-Host "Added ALPACA_PAPER_TRADE=True"
}

# For Telegram values, ASK me first — don't make them up
# Then:
# Add-Content -Path .env -Value "`nTELEGRAM_BOT_TOKEN=<value>"
# Add-Content -Path .env -Value "`nTELEGRAM_CHAT_ID=<value>"
# If a key already exists with a different value, ASK before overwriting.
```

Show me the keys in `.env` (masked):
```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match "^([^#=]+)=") { "$($matches[1])=***" } else { $_ }
}
```

### 2.5 Set GitHub Actions secrets

```powershell
$envVars = @{}
Get-Content .env | ForEach-Object {
    if ($_ -match "^([^#=]+)=(.+)$") {
        $envVars[$matches[1].Trim()] = $matches[2].Trim()
    }
}

foreach ($key in @("ALPACA_API_KEY","ALPACA_SECRET_KEY",
                   "ALPACA_PAPER_TRADE","TELEGRAM_BOT_TOKEN",
                   "TELEGRAM_CHAT_ID")) {
    if ($envVars.ContainsKey($key)) {
        gh secret set $key --body $envVars[$key]
        Write-Host "Set: $key"
    } else {
        Write-Host "MISSING from .env: $key"
    }
}

gh secret list
```

Confirm all 5 secrets show in the list. **[PAUSE]**

### 2.6 Add scaffolding

Create (ADD only — never overwrite):

- `.github/workflows/` directory
- `src/__init__.py` (empty)
- `tests/__init__.py` (empty)
- `tests/conftest.py` with:
  ```python
  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).parent.parent))
  ```
  (so `from src.strategy import ...` works when running `pytest` from
  the repo root)
- `state/.gitkeep`
- `requirements.txt`:
  ```
  alpaca-py>=0.21.0
  pandas>=2.0.0
  numpy>=1.24.0
  python-dotenv>=1.0.0
  requests>=2.31.0
  pytest>=7.4.0
  ```

Append to `.gitignore` if missing. The current `.gitignore` already has
`__pycache__/` and `*.py[cod]`, so check each line before appending —
do not duplicate:
```
.pytest_cache/
state/*.json
state/*.jsonl
*.log
old_regime_output.txt
new_regime_output.txt
```

Commit and push:
```powershell
git add .github src tests state requirements.txt .gitignore
git commit -m "Add watcher scaffolding (Phase 2)"
git push
```

---

## Phase 3 — Build the watcher (single source of truth)

### 3.1 `src/indicators.py`

Pure math, no I/O. Functions:
- `ema(series, period)`
- `rsi(series, period=14)` — Wilder's
- `atr(df, period=14)`
- `volume_sma(volume_series, period=20)`
- `add_indicators(df)` — adds EMA20/50/200, RSI14, ATR14, vol_sma20

Test in `tests/test_indicators.py` against known reference values.

### 3.2 `src/data.py`

Wraps `alpaca-py`:
- `get_client()` — reads env keys. **Hard-checks `ALPACA_PAPER_TRADE`
  is exactly the string `"True"`.** Raises RuntimeError otherwise.
- `get_bars(symbol, timeframe, limit=250)` — returns pandas DataFrame.
  Supports `'1Day'`, `'4Hour'`, `'1Hour'`.
- `get_account()`, `get_positions()`, `get_open_orders(symbol=None)`

All functions raise clearly on API failures. No silent fallbacks.

### 3.3 `src/strategy.py` — refactor from `compute_regime.py`

This is the most delicate step. We're moving working code into a new
location while preserving exact behavior.

**Step 1: Read both files.**
- Read `scripts/compute_regime.py` fully. Understand its inputs,
  outputs, every branch of its regime logic, and what it imports.
- Read `Crypto Strategy.md`. Verify the regime rules and 8-condition
  checklists for Setup A and Setup B.

**Step 2: Capture baseline output.**

⚠️ `scripts/compute_regime.py` takes a JSON bars-file path as
`sys.argv[1]` — it does NOT fetch live data itself. So we need a saved
bars snapshot before we can run a baseline. Two cases:

- **Case A — a bars JSON already exists somewhere I've used.** Search
  for it:
  ```powershell
  Get-ChildItem -Recurse -Filter "*.json" |
      Where-Object { $_.FullName -notmatch "\\\.git\\" -and $_.FullName -notmatch "\\node_modules\\" } |
      Select-Object FullName, Length, LastWriteTime
  ```
  Show me the matches. I'll point at the right file.

- **Case B — no snapshot exists.** Save one before the refactor so we
  have a stable input. Use the Alpaca MCP (via `scripts/start-alpaca-mcp.ps1`
  in a separate Claude Code session) to fetch 250 daily bars each for
  BTC/USD and ETH/USD and write them to
  `state/bars_snapshot_daily.json` in this shape:
  ```json
  {"bars": {"BTC/USD": [{"t": "...", "o": 0, "h": 0, "l": 0, "c": 0, "v": 0}, ...],
            "ETH/USD": [...]}}
  ```
  Tell me when it's saved.

Then capture the baseline:

```powershell
python scripts/compute_regime.py state/bars_snapshot_daily.json > old_regime_output.txt 2>&1
Get-Content old_regime_output.txt
```

Show me the output. If it errors, STOP and tell me — we can't refactor
broken code. Fix or surface the problem first.

**Step 3: Build `src/strategy.py`.**

Required structure at the top:
```python
from pathlib import Path

STRATEGY_DOC_PATH = Path(__file__).parent.parent / "Crypto Strategy.md"

if not STRATEGY_DOC_PATH.exists():
    available = [p.name for p in STRATEGY_DOC_PATH.parent.glob("*.md")]
    raise FileNotFoundError(
        f"Strategy doc not found at {STRATEGY_DOC_PATH}. "
        f"Available .md files in root: {available}"
    )
```

No fuzzy filename matching. Exact path or it crashes loudly.

Required functions:
- `classify_regime(daily_df)` → one of `"BULLISH"`, `"IMPROVING_NEUTRAL"`,
  `"CHOPPY_NEUTRAL"`, `"BEARISH"`, `"UNCLASSIFIED"`, or
  `"INSUFFICIENT DATA"` — exact strings, exact case, matching what
  `compute_regime.py` currently returns. Logic must come directly from
  `compute_regime.py` (port it line-by-line, not paraphrase). Returning
  different strings will break the regression diff.
- `evaluate_setup_a(daily_df, h4_df, h1_df, symbol)` — returns dict
  with `qualified` bool and per-condition breakdown
- `evaluate_setup_b(daily_df, h4_df, h1_df, symbol)` — same shape

The 8 conditions per setup are in `Crypto Strategy.md`. Translate
LITERALLY. If a condition is ambiguous when read mechanically, STOP
and ask me — do not guess.

**Step 4: Refactor `compute_regime.py` into a thin wrapper.**

Rewrite `scripts/compute_regime.py` so its real logic now lives in
`src/strategy.py`. The file should:
- Keep the same CLI contract: takes a JSON bars-file path as `sys.argv[1]`
  (do NOT add live Alpaca fetching here — that belongs in `src/watcher.py`)
- Read the JSON the same way the original does
- Convert the per-symbol close arrays into the pandas DataFrame shape
  that `classify_regime` expects, then call it
- Print output in the EXACT same format as the original, including key
  ordering, float formatting (`f"  {k}: {v:.4f}"`), and the
  `=== {sym} — {N} daily candles ===` header

Run as `python scripts/compute_regime.py state/bars_snapshot_daily.json`
with identical stdout/stderr to before.

**Step 5: Regression diff.**

```powershell
python scripts/compute_regime.py state/bars_snapshot_daily.json > new_regime_output.txt 2>&1
Compare-Object (Get-Content old_regime_output.txt) (Get-Content new_regime_output.txt)
```

Outputs MUST match. Empty diff = success. If they differ:
1. Show me the diff
2. Investigate which version is wrong
3. Fix the new code (not the old output)
4. Re-run the diff until empty

Only proceed past this step when the diff is clean.

**Step 6: Add tests.**

In `tests/test_strategy.py`, at minimum:
- Test `classify_regime` with synthetic data for each of the 4 regimes
- Test `evaluate_setup_a` with a known-qualifying setup and a known-
  non-qualifying one
- Same for `evaluate_setup_b`

Run `pytest tests/test_strategy.py`. All must pass.

### 3.4 `src/notifier.py`

Single function: `send_alert(message)`. Reads `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` from env. POSTs to Telegram. Plain text. Failures
log to stderr but never crash the caller.

Alert format:

```
🔔 SETUP QUALIFIED — ETH/USD (Setup A — Pullback)

Regime: BULLISH ✓
8/8 conditions passed:
✓ 4H price 3245.20 > 4H EMA200 3120.40
✓ Pullback to 4H EMA20 (distance 0.4%)
✓ Higher low intact at 3210.00
✓ 4H RSI = 41.2 (zone: 35-50)
✓ 1H close > 1H EMA20 (green candle)
✓ 1H volume 1.05x 20-avg
✓ Stop distance 1.2x ATR (within 1.5x cap)
✓ No open ETH position

Suggested levels:
Entry: ~$3245
Stop:  ~$3145 (-3.1%)
TP1:   +1.5R / TP2: +3R

→ Open Claude Code → ask the Alpaca MCP to confirm and execute.

Time: 06:00 EAT (03:00 UTC)
```

### 3.5 `src/watcher.py` — orchestrator

```python
# Top-to-bottom:
# 1. Load .env via python-dotenv
# 2. Hard-check ALPACA_PAPER_TRADE == "True"
# 3. For each symbol in ["BTC/USD", "ETH/USD"]:
#    a. Pull daily/4H/1H bars (250 of each)
#    b. add_indicators on each
#    c. classify_regime(daily)
#    d. evaluate_setup_a and _b
#    e. If either qualifies, build message and send via notifier
# 4. Log a one-line summary to stdout per symbol
# 5. If any symbol errored, send a single "⚠️ Watcher errors at 06:00 EAT"
#    Telegram and continue with others
# 6. Exit 0
```

Target runtime: under 30 seconds. Longer means something's wrong.

---

## Phase 4 — GitHub Actions workflow

### `.github/workflows/watcher.yml`

```yaml
name: Daily Watcher

on:
  schedule:
    # 03:02 UTC = 06:02 EAT (Nairobi).
    # +2 min buffer so the daily candle has settled.
    - cron: '2 3 * * *'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - run: pip install -r requirements.txt

      - run: python -m src.watcher
        env:
          ALPACA_API_KEY: ${{ secrets.ALPACA_API_KEY }}
          ALPACA_SECRET_KEY: ${{ secrets.ALPACA_SECRET_KEY }}
          ALPACA_PAPER_TRADE: ${{ secrets.ALPACA_PAPER_TRADE }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

`permissions: contents: read` is intentional — the watcher only reads
the repo, doesn't push state changes. No risk of corrupted commits.

**Budget math:**
- 1 run/day × ~30 sec, billed as 1 min minimum
- ≈ 30 min/month vs 2000 min/month free tier ceiling
- Plenty of room to add more workflows later

---

## Phase 5 — Test and deploy

### 5.1 Local test

```powershell
# Verify .env loads
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.environ.get('ALPACA_PAPER_TRADE'))"
# Expected: True

# Verify the MCP fallback path is intact
Get-Content scripts/start-alpaca-mcp.ps1 | Select-Object -First 5
# (Just confirming we haven't touched it. Show me first few lines.)

# Run the watcher
python -m src.watcher
```

Verify:
- Alpaca connects without errors
- Data pulls cleanly for BTC and ETH
- One-line scan summary per symbol logs to stdout
- If anything qualifies → Telegram fires
- If nothing qualifies → silent (correct behavior)

### 5.2 Push and trigger manually

```powershell
git add src tests .github
git commit -m "Add watcher (Phase 3-4)"
git push

# Trigger manually via CLI
gh workflow run "Daily Watcher"

# Or via GitHub UI: Actions tab → Daily Watcher → Run workflow
```

Verify in GitHub:
- Workflow shows green check
- Logs show clean scan
- Telegram fired (or didn't) correctly

### 5.3 Wait for tomorrow's scheduled run

Cron fires at 03:02 UTC = 06:02 EAT the next morning. Confirm it ran
on schedule. **[PAUSE for at least 24 hours of observation]**

---

## Phase 6 — Cost tracking inside Claude Code

Separate from the watcher (which is free). Tracks MY Claude Code Pro
subscription usage when I investigate alerts.

### 6.1 Per-investigation tracking

When I start a Claude Code session triggered by a Telegram alert:

1. At session start, append to `state/cost_log.jsonl`:
   ```json
   {"session_id": "2026-05-12-0602", "started_at": "ISO time",
    "trigger": "Telegram alert: <symbol> Setup <A|B> qualified"}
   ```

2. At session end (when I say 'done' or after placing a trade), run
   `/cost` in Claude Code to read session token usage.

3. Append to the same log entry:
   ```json
   {"ended_at": "ISO time", "input_tokens": N, "output_tokens": N,
    "claude_pro_covered": true,
    "api_equivalent_cost_usd": <calculated>,
    "outcome": "trade_placed" | "skipped" | "investigating"}
   ```

4. One-liner summary: "This session: 15.7K tokens. Pro covers it.
   API-equivalent: $0.18."

### 6.2 Daily summary on demand

When I say `daily summary` or `/daily`, read today's entries from
`state/cost_log.jsonl` (UTC day) and produce:

```
=== DAILY USAGE — 2026-05-12 ===

Watcher: 1 run @ 06:02 EAT (no tokens, free Actions)
Investigations: 2
- 06:15 EAT: BTC alert → trade placed (15.7K tokens)
- 14:30 EAT: ETH alert → skipped (8.2K tokens)

Total tokens today: 23.9K (18.5K input, 5.4K output)
Claude Pro subscription: covers this
API-equivalent cost: $0.27 (informational)

Trades placed: 1
Skip rate: 50%
```

### 6.3 Honest disclaimers (include in every summary)

- `/cost` in Claude Code shows **session-cumulative** usage. For clean
  per-investigation tracking, start a fresh Claude Code session for
  each Telegram alert.
- I'm on **Claude Pro** ($20/mo flat). The dollar figure is the
  subscription, not metered. "API-equivalent" is informational only.
- Claude Pro has 5-hour rolling rate limits. If summary detects I'm
  approaching them, flag it explicitly.
- Token counts from `/cost` are estimates from Anthropic's billing
  model; actual usage may differ slightly.

---

## What's NOT in this build (intentionally)

Staying focused and within budget:

- **No executor module.** This build does NOT place orders. Telegram
  alerts prompt me to act manually through Claude Code + Alpaca MCP
  (with `scripts/start-alpaca-mcp.ps1` as the local launcher).
- **No position manager workflow.** Manage positions manually via
  Claude Code + MCP.
- **No Telegram command listener.** Daily summaries happen inside
  Claude Code on demand, not via Telegram messages.
- **No weekly review workflow.** Manual Sunday review inside Claude
  Code.
- **`scripts/start-alpaca-mcp.ps1` is preserved untouched** as the
  fallback path. If GitHub Actions ever fails, I can still scan and
  trade locally via the MCP.

Total Actions budget consumed: ~30 min/month. Room to add more later.

---

## Acknowledgement before we start

Please:

1. Confirm you've read every section, especially:
   - Environment constraints (Windows PowerShell, `AI Trading` folder
     name with space, repo name `AI-Trading` with hyphen)
   - File preservation list (what NOT to touch)
   - Role of `compute_regime.py` (primary, being refactored) vs
     `start-alpaca-mcp.ps1` (fallback, preserved)
2. List the 3 pieces being built.
3. List the 6 phases in order.
4. Confirm:
   - The watcher uses zero tokens, runs on GitHub Actions only
   - Schedule: 03:02 UTC daily = 06:02 EAT
   - Free tier: 30 min/month budget vs 2000 cap
   - All commands are PowerShell-native (no bash)
   - Strategy filename is exactly `Crypto Strategy.md`
   - Phase 3 refactors `compute_regime.py` into a wrapper over
     `src/strategy.py` with a regression diff
   - `scripts/start-alpaca-mcp.ps1` and existing logs stay untouched
5. Read `Crypto Strategy.md` and confirm you understand the 8
   conditions per setup.
6. Then ask: "Ready to start Phase 1 (Telegram bot setup)?"

Do not start any phase until I say go.

---

## Summary of changes from the previous watcher prompt

For my reference, here's what's different in this version:

1. **Folder path corrected:** `C:\Users\StephenAI\AI Trading` (was
   `~/Trading`). Windows username is `StephenAI`.
2. **GitHub username corrected:** `stephenkisonge-AI` (the GitHub
   account is separate from the Windows account).
3. **Repo exists already** at
   `https://github.com/stephenkisonge-AI/AI-Trading` (private). Phase
   2.2 verifies the local folder is connected to it — does NOT try to
   create a new repo.
4. **All commands PowerShell-native:** `Invoke-RestMethod`,
   `Get-ChildItem`, `Select-String`, `Add-Content`, `Compare-Object`.
   No bash idioms.
5. **Schedule changed:** From every 4 hours to once daily at 03:02 UTC
   (06:02 EAT). Drops Actions minutes from ~6000/month to ~30/month —
   fits comfortably in the 2000 min/month free tier.
6. **Existing files documented and protected:** Explicit list of files
   that must NOT be touched (`.claude/`, all `scripts/` logs,
   `start-alpaca-mcp.ps1`, `Alpaca MCP Sever and Trading prompt.md`,
   `preferences.md`, `.env.example`).
7. **`compute_regime.py` refactor path:** Phase 3 now explicitly
   refactors your working classifier into `src/strategy.py` with a
   regression diff to prove behavior didn't change. Single source of
   truth, no duplicated math.
8. **`start-alpaca-mcp.ps1` preserved as fallback:** Explicitly called
   out as the manual MCP launcher — untouched by this build, available
   if GitHub Actions fails.
9. **Execution path clarified:** Telegram alerts → Claude Code +
   Alpaca MCP → manual confirm. No auto-execution. No code paths place
   orders.
10. **Cost tracking reframed for Claude Pro:** Subscription covers
    tokens, so dollar figures are "API-equivalent" informational only.
    Added 5-hour rate-limit awareness in summaries.
11. **Cut scope to fit free tier:** No executor, no manager, no
    command listener, no weekly review workflow — all manual via
    Claude Code + MCP. Total Actions burn ~30 min/month.
