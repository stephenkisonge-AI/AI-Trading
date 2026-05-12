# Trading Preferences

## Setup
- **Broker:** Alpaca (paper trading mode)
- **MCP server:** alpaca (registered with Claude Code at user scope)
- **Credentials:** Loaded from `.env` (gitignored) via `scripts/start-alpaca-mcp.ps1`

## Profile
- **Asset focus:** Crypto (BTC/USD, ETH/USD primary; SOL/USD gated)
- **Max trade size:** $500 notional (hard cap)
- **Strategy:** See `Crypto Strategy.md` — synthesized swing-trading rule set with two tracks (active swing + DCA accumulation)

## Operating principles
- Paper trading only until explicit live switch
- Every trade requires explicit 'go' confirmation
- Full proposal (regime check + setup conditions + risk math + thesis + invalidation) before any order
- Stops never widen
- No leverage, no shorts, long-only spot crypto
