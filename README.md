# Agent Race: three AI traders, $100 each, first to $5,000 wins

Three AI agents each get **$100** of paper money in a shared Alpaca paper account and race to
**$5,000**. Each sticks to its specialty but has full control of its own balance: which setups to
take, how many dollars, which instrument, where the stop goes, and when to get out.

| Agent | Specialty | Instruments |
|---|---|---|
| **Trend agent** | Breakouts to new highs in uptrends; breakdowns via puts | Fractional shares, long calls, long puts |
| **Mean-reversion agent** | RSI(2) washouts in stocks above their 200-day | Fractional shares, long calls |
| **Vol-selling agent** | Credit spreads on SPY/QQQ/IWM when implied vol is rich | $1-wide credit spreads |
| **SPY benchmark** | Not racing. $100 in SPY, held | Fractional shares |

## Rules of the race (the only things agents can't decide)

- **Cash only, no margin.** An agent can only spend its own available balance.
- **Simple options only.** Long calls/puts and defined-risk spreads. Nothing naked.
- **No overtrading.** At most 2 new trades per day and 4 open positions per agent; no re-buying a
  symbol the day it was sold.
- **Stops can be tightened, never loosened.** Default exits (trailing stops, option -50% stop,
  +100% target, time exits, spread profit/loss limits) always run; agents may exit earlier.
- **Failing:** below **$10** = eliminated (positions closed). Unable to afford its smallest trade
  with nothing open = failed. Failed agents are frozen, not deleted.
- **Finish:** the first agent to **$5,000** wins. All positions are then closed and every agent,
  including failed ones, gets a written post-mortem.
- Options results include a **slippage haircut** of half the bid-ask spread, so paper fills
  aren't flattering.

## How a run works (every 30 min in market hours)

```
default exits run for every position
-> each active agent sees: its balance, positions, its scanner's setups, its notes, the standings
-> the agent decides: close / tighten stops / open (dollars + instrument) / write a note
-> the rulebook checks every order (cash, instrument, caps, cooldown)
-> orders go to Alpaca paper; every agent's ledger is updated
-> fail and finish checks; dashboard data saved and committed
Fridays after the close: each agent reviews its own week and may retune up to 2 of its own
strategy settings (within hard bounds, max 25% of the range per week)
```

## Is the Anthropic API required?

**For the race, yes.** The API is what lets each agent actually make its own decisions.
Without `ANTHROPIC_API_KEY`, the agents fall back to simple default rules (take the top setup with
40% of cash), which is useful for testing but isn't the experiment you want.

Expected cost at current API pricing is roughly **$1–2 per month**:
- Per-run decisions use **Claude Haiku 5.5**, about $0.0007 per decision. Up to ~950 decisions a month,
  and agents with nothing to decide are skipped.
- Weekly reviews and the final post-mortem use **Claude Sonnet 5.5**, which is a few cents each.

Set a monthly spend limit in the Claude Console to be safe. You can change models with the
`BRAIN_MODEL` / `REVIEW_MODEL` repository variables.

## Setup (all from your iPhone)

1. **Alpaca paper keys.** Sign up at alpaca.markets, open the paper account, and generate API
   keys. The paper account's own balance doesn't matter; each agent keeps its own $100 ledger.
2. **Anthropic API key.** Create one at console.anthropic.com, add billing, and set a spend limit.
3. **Create a public GitHub repo** and upload every file from the zip, keeping the folders
   (including `.github/workflows/agent.yml`).
4. **Secrets.** *Settings → Secrets and variables → Actions*: add `ALPACA_API_KEY`,
   `ALPACA_API_SECRET`, `ANTHROPIC_API_KEY`.
5. **Optional screener.** *Variables* tab: `SCREENER_URL` = `https://<your-screener>/api/candidates`,
   returning `{"candidates":[{"ticker":"XYZ"}]}`.
6. **Pages.** *Settings → Pages → Deploy from a branch → main / (root)*, then add
   `https://<you>.github.io/<repo>/` to your home screen.
7. **Start.** *Actions → Trading agent race → Run workflow*. From then on it runs itself.

Keys live only in GitHub Secrets. The public repo shows paper trades and agent notes, never keys.

## Controls

- **Pause:** *Actions → Trading agent race → ⋯ → Disable workflow*.
- **New race:** delete the files in `data/` (keep `.gitkeep`) and reset the Alpaca paper account.
- **Change the stakes:** `RACE` and `RULES` in `config.py`.

## Things to know

- **The vol-selling agent starts at a disadvantage.** A $1-wide spread can lose ~$80–95 per
  contract, so one bad trade can sink a $100 account. That's the real math of small-account
  premium selling, and part of what the race will show.
- **Watch the risk-adjusted score as well as the standings.** Getting from $100 to $5,000 (50×) usually
  rewards the boldest bets. The "Risk-adj" number (return ÷ max drawdown) helps separate skill from luck.
- **Paper fills are approximate.** The free data feeds are delayed or indicative, and GitHub Actions schedules can drift a few minutes.
- All three agents share one Alpaca account, so two agents can't hold shares of the same stock at
  the same time.
