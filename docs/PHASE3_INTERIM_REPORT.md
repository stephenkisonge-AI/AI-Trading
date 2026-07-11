# Interim report — after Phase 3 (2026-07-11)

## Tests passing

`pytest -q` → **381 passed** (baseline 267; +40 Phase 1, +41 Phase 2,
+33 Phase 3). No baseline test was changed or removed. Auto-execution
remains disabled (`WATCHER_AUTO_EXECUTE=false`, verified by dry run);
the day strand remains active per the revised addendum.

## Exit lifecycle (crypto swing, new)

```
ENTRY (market, deterministic coid swing-{SYM}-{setup}-{ts}-entry-0)
  └─ resolve fill (timeout ≠ zero fill; cancel remainder → confirmed terminal)
       ├─ fill invalid vs approved risk → UNWIND (cancel exits confirmed →
       │    re-read position → market-sell exact remainder → confirm flat
       │    → CRITICAL → entries frozen)
       └─ fill valid → recompute R/TPs from ACTUAL avg fill
            → place ONE stop-limit for exact position qty
            → verify accepted + remaining == position  → state PROTECTED
                 (verify fails → RECOVERY_REQUIRED + freeze + CRITICAL)

MANAGEMENT PASS (every run; runs even while entries are frozen)
  0. resolve dangling intents via deterministic coid (crash recovery)
  1. gap watchdog: fresh bid ≤ stop trigger & stop unfilled →
       cancel (confirmed) → market-sell exact remainder → confirm flat
       → CRITICAL   [application-dependent; NOT a broker stop-market]
  2. regime exit (BEARISH; unknown regime = hold, stop stays)
  3. time stop (10d without TP1)
  3.5 breakeven enforcement post-TP1 (idempotent, recovery corners)
  4. TP1/TP2 on fresh executable bid (age ≤ 120 s; bid = executable
     side for a long's sell; stale/missing quote → no transition):
       persist intent → confirm position + stop → cancel stop
       (poll to CONFIRMED terminal; fill-during-cancel handled)
       → re-read position → market-sell tranche → re-read
       → replacement stop for EXACT remainder (breakeven floor)
       → verify accepted + qty → persist transitions + unprotected window
  5. runner (post-TP2): 4H close < EMA20 exit, else chandelier trail
     raise (never lower), same cancel-confirm-replace sequence
Dust: remainder below tradable increment → close entire position.
Unknown stop/position state anywhere → NO new orders, RECOVERY_REQUIRED,
entries frozen, CRITICAL alert; reconcile.py must pass to resume.
```

## New invariants

1. **Sell-quantity invariant:** sum of *remaining* (unfilled) qty across
   open sell orders per symbol ≤ current broker position qty. Checked
   in code after every transition, asserted after every mutation in the
   test fake broker, flagged by `scripts/reconcile.py`
   (`SELL_QTY_INVARIANT`). The old bundle (175% resting sells) is gone.
2. **Persist-then-submit:** no broker action without a durably persisted
   intent (GitJournal: committed AND pushed to the private state repo).
3. **One logical action = one deterministic client order ID**; retries
   find the existing order instead of resubmitting; replacements get a
   new leg.
4. **Cancel ≠ cancelled until Alpaca reports a terminal state**; fills
   discovered during cancellation are recorded as realized exits.
5. **Alpaca paper state is final truth**; journal reconciles against it,
   exit 0 only on full agreement.

## Known unprotected transition duration

Between confirmed stop cancellation and verified replacement stop
(TP transitions, trail raises) the position has **no broker-held
protection**. The duration is measured with a monotonic clock and
persisted in every `TP*_FILLED` / trail transition and in alerts.
In-test (instant fake broker) it is ~2-4 poll intervals; in production
expect roughly **2-15 s** per transition (2 cancel-confirm polls + 1
market-sell fill poll + 1 submit + 1 verify read at ~0.5-1 s each,
network dependent). During that window only the gap watchdog (next
management pass) backstops it. This is an accepted, measured risk of
the application-managed TP design — mitigated but not eliminated.

## Known unresolved risks

1. **Management cadence (Option B):** watchdog/TP triggers run only at
   workflow cadence. At the current 4 h scan interval both are
   materially weakened. Addendum E's management-only workflow every
   15-30 min via the existing repository_dispatch pattern is designed
   but **not yet implemented** (lands with Phase 4 wiring).
2. **Stop-limit non-fill:** a fast gap through the limit band
   (trigger × (1−0.5%)) can leave the stop unfilled until the next
   management pass. The watchdog closes at market then — realized loss
   can exceed the modeled loss-at-limit. Quantified in Phase 6 replay.
3. **Watcher wiring:** `src/watcher.py` still calls the old
   `trader.place_entry_bundle`/`manage_open_positions` path. With
   auto-execute disabled it is dormant. The switch-over to
   `swing_exits` + journal + fail-closed gates is Phase 4 work, so the
   cutover happens together with the gate hardening.
4. **GitJournal latency:** every append is a commit+push (~1-3 s each,
   several per transition). Acceptable at this trade frequency; noted
   as a scaling limit.
5. **Quote freshness bound (120 s)** is a chosen constant, not yet
   validated against Alpaca crypto quote cadence in production logs.
6. **Per-strand risk ledgers (Addendum C)** not yet implemented —
   account-level gates still read shared portfolio history (fail-open
   in the legacy path). Phase 4 replaces them with journal-derived
   per-strand gates + one account-wide emergency brake.

## Next

Phase 4 (fail-closed gates + watcher cutover + management-only
workflow + per-strand ledgers), then the equity mini-audit (Addendum
B), then Phase 5 telemetry.
