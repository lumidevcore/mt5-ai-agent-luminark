# MT5 AI Agent V3.10.8 — UPDATE ONLY

ZPI rate-limit protection:
- HTTP 429 now starts an account-wide 10-minute cooldown (or Retry-After when supplied).
- No repeated ZPI network calls during cooldown.
- Fresh cache is preferred; older successful cache may be used as STALE_CACHE.
- Neutral fail-safe is used only when no cached value exists.
- API calls automatically resume after cooldown.
- No .env, database, venv, or local configuration is included.


## V3.10.9 — Realistic Adaptive Targets + Full Council Review

### Adaptive TP
- The configured 2% target is now a **preferred target**, not a universal hard price-move floor.
- If ATR projection and directional evidence support 2% or more, the engine can still use/extend to that target.
- If the market projection is much smaller (for example EURUSD M1), the target uses an instrument-aware ATR floor instead.
- FOREX, crypto, metals/indices/energies, and other instruments use different realistic minimum price-move bands.
- Minimum reward:risk remains enforced by RiskGuard at **1:2 or better**.
- Logs now expose preferred target, whether it is supported, and the effective floor.

### AI Council
- CHIEF is called after CRITIC on every fully escalated Council review.
- `CRITIC=REJECT` remains a hard safety veto.
- CHIEF reviews/explains a rejected setup but cannot force an entry through a Critic rejection.
- If Critic approves, CHIEF remains the final AI decision maker.


## V3.10.10 — AI Council Confidence Calibration

- Council prompts now explicitly define confidence as confidence in the role's own decision/verdict.
- A valid HOLD or REJECT should no longer use `0.00`; mixed evidence should normally produce calibrated values such as `0.35-0.60`.
- Each Council role gets one targeted calibration retry if it still returns confidence <= 0.01.
- Output that remains <= 0.01 is marked `ABSTAIN`, not treated as a normal opinion.
- A `CRITIC=REJECT` is a hard veto only when it is a valid calibrated reject with confidence >= 0.50.
- `CRITIC=REJECT 0.00` / failed Critic output is treated as abstention; CHIEF performs an independent final review.
- CHIEF abstention/failure remains fail-safe HOLD.
- Council logs now label stages as `VALID`, `CALIBRATED`, `ABSTAIN`, `VALID_REJECT`, or `LOW_CONF_REJECT`.
- All V3.10.9 realistic adaptive-target and RR protections remain unchanged.


## V3.10.11 — Symbol Context Isolation + Chief Diagnostics

- Every engine start now creates a fresh symbol-context generation.
- Market/session/Fibonacci/ZPI context is tagged to the selected symbol and generation.
- A hard Context Integrity Guard blocks the analysis cycle before AI Council/order planning if the symbol changes mid-cycle.
- A price-scale sanity guard catches gross cross-symbol contamination (for example EURUSD-scale context appearing in BTC analysis), invalidates the affected symbol cache, and rebuilds the next cycle.
- ZPI symbol invalidation now covers additional symbol-dependent cache namespaces.
- Council abstain/failure logs expose model name, elapsed time, and reason so Chief `HOLD 0.00` can be diagnosed instead of hidden.
- SAFE STOP logging now explicitly states that new entries are disabled while existing broker SL/TP remain active.
- V3.10.10 confidence calibration and V3.10.9 adaptive-target/RR protections remain in place.


## V3.10.12 — Council Output Reliability + Fast Chief Fallback

- Council response schemas are shorter so local models do not waste tokens repeating data already computed by the engine.
- SCOUT and CHIEF now return only action, confidence, and a short reason.
- Technical/Critic keep only fields needed by downstream Council logic.
- A conservative partial-JSON repair path can close truncated strings/braces instead of immediately discarding otherwise useful action/confidence output.
- Role-specific time budgets reduce trading-loop stalls: Scout 35s, Technical/Critic 60s, Chief 75s.
- Chief 9B failure/timeout/abstention automatically falls back to the Claude-distill 4B model with a shorter budget.
- Logs mark `[JSON_REPAIRED]` and `[4B_FALLBACK]` when those recovery paths are used.
- Compact Chief output no longer erases Technical/primary trend and structure context.
- V3.10.11 Symbol Context Integrity Guard, V3.10.10 confidence calibration, adaptive target, RR and RiskGuard protections remain unchanged.


## V3.10.13 — Adaptive Council Execution
- Technical disagreement/failed confirmation stops before Critic/Chief.
- Valid Critic REJECT is now a terminal hard veto: HOLD immediately, Chief skipped.
- Chief 9B runs only for setups that survive Critic or when Critic abstains/fails.
- 9B -> 4B fallback remains for Chief failures when Chief is actually required.
- V3.10.12 JSON repair and all trading/risk/context safeguards remain unchanged.


## V3.10.14 — Council Performance Monitor + Smart Model Routing

- Every Council call now records persistent telemetry in `data/ai_council_metrics.json`.
- Metrics include calls, average latency, recent timeout/failure/abstain rate, JSON-repair rate and last-call timing.
- AI Model Lab shows a compact rolling performance monitor for Scout, Technical, Critic and Chief without adding a scrollbar.
- Smart Chief routing watches the recent 9B Chief history.
- If the 9B Chief has repeated timeouts/failures/abstentions or excessive latency, Chief is temporarily routed to the 4B model for 15 minutes.
- After cooldown, the 9B model is tried again automatically instead of being permanently disabled.
- Logs mark `[SMART_ROUTE_4B]` when adaptive routing is active.
- If Smart Routing already selected the 4B Chief and that call fails, the engine does not waste time retrying the same 4B model again.
- V3.10.13 Adaptive Council gates remain authoritative: direction conflict or valid Critic REJECT still stops before Chief.


## V3.10.15 — Trade Quality / Expectancy Engine

- Closed bot trades are now evaluated as historical expectancy, not just displayed as learning statistics.
- Expectancy matches completed trades using side, symbol, timeframe, regime, structure and effective trading mode.
- The engine calculates sample count, win rate, average win/loss, normalized expectancy score and a setup grade.
- Tiny samples are strongly shrunk toward neutral to reduce overfitting.
- Expectancy **cannot create BUY/SELL or override AI Council/RiskGuard**. It only modifies deterministic risk sizing.
- Risk modification is intentionally bounded: poor historical expectancy can reduce risk to about 75%; strong expectancy can increase it by at most 10%, still clamped by the existing risk policy.
- No martingale logic is introduced.
- Logs show `EXPECTANCY [...] grade / n / WR / score / E / risk multiplier`.
- Decision snapshots now retain expectancy score/sample/grade so future learning can audit which historical profile influenced an entry.
- V3.10.14 Council telemetry/Smart Routing and all previous safety gates remain intact.


## V3.10.16 — Drawdown & Exposure Controller

- Adds a deterministic account-stress controller on top of the V3.10.15 expectancy engine.
- Risk automatically decreases as session/equity drawdown increases.
- Consecutive losses reduce risk before the existing hard streak cooldown is reached.
- High portfolio position count, low margin level, or low free-margin ratio can further reduce new-entry risk.
- The controller is strictly anti-martingale: its multiplier is capped at `1.00x` and can never increase risk because of a loss.
- Stress states are `NORMAL`, `CAUTIOUS`, `DEFENSIVE`, and `CRITICAL`.
- Existing MarginGuard remains the hard execution blocker; this controller only reduces the risk proposal before MarginGuard.
- Logs expose `ACCOUNT STRESS`, session drawdown, loss streak, position count, margin level and the applied multiplier.
- Stress metadata is saved into decision snapshots for later audit/learning.
- V3.10.15 Expectancy, V3.10.14 Smart Routing and all previous Council/RiskGuard protections remain intact.


## V3.10.17 — Portfolio Correlation Guard

- Adds a deterministic correlation/exposure guard before MarginGuard execution.
- FOREX positions are decomposed into signed currency factors (for example BUY EURUSD = long EUR / short USD).
- New FX entries are blocked when they would reinforce three or more existing FX positions in the same currency direction.
- Two reinforcing FX positions generate a caution but do not automatically block; existing MarginGuard/RiskGuard still decide.
- Crypto, metals, indices, energy and stocks use broad factor buckets to detect repeated same-direction portfolio bets.
- Three or more same-direction positions in the same broad factor bucket are blocked; two generate a caution.
- This guard does not pretend to estimate live statistical correlation. It is a deterministic concentration guard designed to catch obvious shared-factor exposure.
- Multi-entry remains enabled when exposure is diversified and margin/risk constraints are satisfied.
- Logs now show `CorrelationGuard BLOCKED` or `CorrelationGuard CAUTION` when concentration is relevant.
- V3.10.16 Drawdown/Exposure Controller, V3.10.15 Expectancy Engine, Smart Routing and all previous Council safeguards remain intact.


## V3.10.18 — Paper / Shadow Decision Engine
- PAPER mode records complete hypothetical trades after normal Council/RiskGuard/Correlation/Margin planning.
- Shadow positions store entry, SL, TP, RR, timeframe, mode, score, regime and structure.
- Each engine cycle checks open shadows against broker ticks and closes them hypothetically at TP/SL.
- Logs show `SHADOW OPEN`, `SHADOW CLOSED`, and `SHADOW STATS`.
- Shadow results live in a separate `shadow_trades` table and never enter the real `trades` expectancy/loss-streak dataset.
- No MT5 order is sent in PAPER mode.
- V3.10.17 Correlation Guard and all previous protections remain intact.


## V3.10.19 — Live Readiness Guard

- Adds a hard execution gate specifically for MT5 `REAL` accounts.
- Demo and contest accounts remain normal forward-testing environments and do not require the REAL-account gate.
- Before a REAL order can reach broker preflight, the engine checks terminal connection/trading permission, account algorithmic-trading permission, equity/free margin, margin level, valid ENTRY/SL/TP, minimum RR, account stress, loss streak, current symbol-context ownership and final setup score.
- Default live risk readiness cap is `0.50%` per proposed trade unless a future config explicitly changes it.
- A REAL account requires at least 5 completed demo/real-learning + shadow outcomes combined before automatic live entries are considered ready.
- A loss streak of 3+ or CRITICAL account stress blocks new REAL entries.
- The guard does not bypass existing RiskGuard, CorrelationGuard, MarginGuard or broker `order_check`; it is an additional gate before those final execution steps.
- Logs show `LIVE READINESS PASSED` or the exact `LIVE READINESS BLOCKED` reasons.
- Entry snapshots record account type and whether live readiness passed.
- All V3.10.18 Shadow Mode and previous safeguards remain intact.


## V3.10.20 — Market Regime Memory + Strategy Adaptation

- Adds exact-regime historical memory using completed real/demo bot trades.
- Regime memory is evaluated separately from generic expectancy, so the engine can distinguish the same symbol/timeframe under `TRENDING`, `RANGING`, `TRANSITION`, `HIGH_VOL`, and `LOW_VOL` conditions.
- Current regime selects a deterministic strategy posture:
  - `TREND_FOLLOWING` for trending markets.
  - `RANGE_DEFENSIVE` for ranging markets.
  - `TRANSITION_DEFENSIVE` while the market is between regimes.
  - `BALANCED` as fail-safe fallback.
- Regime posture can adjust quality, risk and adaptive-target ambition, but it can never flip BUY/SELL.
- Trending regimes may allow wider targets when evidence supports them; high-vol trending conditions still reduce risk.
- Ranging and transition regimes require stronger setup quality and reduce risk/target ambition.
- Exact-regime historical performance is sample-shrunk and has bounded influence to reduce overfitting.
- Adds `RegimeGuard`: a setup that is technically directional but too weak for the current defensive regime is blocked before order execution.
- Logs show `REGIME ADAPT` with style, quality/risk/target multipliers, minimum quality, regime-history sample count and win rate.
- Regime metadata is stored in decision snapshots for future auditing and learning.
- V3.10.19 Live Readiness, Shadow Mode, Correlation Guard, Expectancy, Smart Routing and all prior safeguards remain intact.


## V3.10.21 — Dynamic Exit Manager

- Upgrades the existing dynamic-exit logic from simple percentage locks to an R/ATR/structure-aware manager.
- Uses the original entry-to-SL distance as `1R` so exit decisions scale naturally across FOREX, crypto, metals and other instruments.
- Mode-aware stages:
  - Break-even after a meaningful favorable R move.
  - Profit lock after a stronger move.
  - ATR + swing-structure trailing once the trade has earned enough room.
- Scalping, Intraday and Swing use different break-even/trailing thresholds.
- `RANGE_DEFENSIVE` and `TRANSITION_DEFENSIVE` regimes protect profit sooner; strong `TREND_FOLLOWING` regimes give the trade more room.
- SL is monotonic and can never be loosened away from protection.
- Broker stop-distance rules are respected before any SL update.
- TP extension is now selective: it can extend primarily when trend regime + evidence still support a larger target. Defensive regimes do not chase a farther TP.
- Logs expose exit stage, current R multiple, mode, regime, structure and old/new SL/TP.
- Entry snapshots track the managed SL/TP, exit stage and latest R multiple.
- Existing broker-side SL/TP remain the fail-safe if Python/AI stops running.
- Partial close is intentionally not included yet; it remains suitable for V3.10.22.


## V3.10.22 — Partial Close / Scale-Out Manager

- Adds broker-side partial close for profitable positions without flattening the entire trade.
- Scale-out thresholds are R-based and mode-aware:
  - Scalping: first scale near 1R, second near 1.65R.
  - Intraday: first near 1.25R, second near 2.10R.
  - Swing: first near 1.50R, second near 2.75R.
- Defensive regimes bank profit slightly earlier; strong trend-following regimes keep more size for continuation.
- Stage 1 generally closes about 25–40% depending on mode/regime.
- Stage 2 closes another bounded fraction of the remaining position.
- Broker volume minimum/step are respected and scale-out is skipped rather than accidentally closing the full position.
- A successful first scale-out activates at least break-even protection for the remaining position.
- Scale-out state prevents the same milestone from firing repeatedly every loop.
- Logs show `PARTIAL CLOSE [STAGE1]`, `PARTIAL CLOSE [STAGE2]`, skip reasons and broker failures.
- Entry snapshots track whether each scale-out stage executed and how much volume was realized.
- No martingale or exposure increase is introduced: partial close can only reduce an already-open position.
- V3.10.21 Dynamic Exit Manager continues managing the remaining SL/TP after scale-out.


## V3.10.23 — Real Correlation Engine

- Adds rolling return correlation from actual MT5 candle data on top of the deterministic V3.10.17 exposure guard.
- The engine compares a proposed symbol against currently open bot positions using recent M15 percentage returns.
- Positive correlation reinforces same-direction trades; negative correlation reinforces opposite-direction trades.
- Correlation requires a minimum history window and fails open to the existing deterministic guard when data is unavailable.
- A single strongly reinforcing relationship (`|corr| >= 0.75`) produces a caution.
- Two or more strongly reinforcing relationships can block a new entry as concentrated real-market exposure.
- Correlation calculations are cached briefly to avoid repeatedly fetching the same candle series every loop.
- Logs expose `RealCorrelationGuard CAUTION` and `RealCorrelationGuard BLOCKED` with the compared symbols and coefficients.
- This does not replace currency/factor exposure logic; both deterministic concentration and measured rolling correlation must pass.
- V3.10.22 Scale-Out, V3.10.21 Dynamic Exit and all previous risk/Council protections remain intact.


## V3.10.24 — AI Council Consensus Score

- Adds a quantitative Council agreement score across Scout, Technical, Critic and Chief.
- Each valid role contributes with a bounded role weight and its own confidence.
- Abstentions reduce coverage instead of being treated as directional votes.
- Technical deterministic direction contributes only a small independent sanity vote.
- Council consensus is graded as `VERY_HIGH`, `HIGH`, `MODERATE`, `MIXED`, `LOW`, or `CONFLICT`.
- Consensus can only modestly modify quality/risk (`quality ~0.90x–1.05x`, risk ~0.80x–1.05x).
- It cannot override a Critic hard veto, RegimeGuard, CorrelationGuard, MarginGuard, Live Readiness, spread guard or broker preflight.
- A clearly conflicting directional Council (`score < 0.32` with >=50% role coverage) is blocked by `ConsensusGuard`.
- Low role coverage does not hard-block by itself; it primarily de-risks the setup.
- Logs show consensus score, grade, role coverage, abstentions/disagreements and applied quality/risk multipliers.
- Decision snapshots retain consensus score/grade/modifiers for later expectancy analysis.
- V3.10.23 Real Correlation, V3.10.22 Scale-Out, V3.10.21 Dynamic Exit and all previous safeguards remain intact.


## V3.10.25 — Council Calibration Memory

- Closes the feedback loop introduced by V3.10.24: completed bot trades now preserve Council consensus metadata in decision/trade features.
- Historical consensus buckets (`VERY_HIGH/HIGH/MODERATE/MIXED/LOW/CONFLICT`) are measured against actual completed-trade outcomes.
- The calibration engine checks whether HIGH-consensus trades are genuinely outperforming LOW/CONFLICT trades instead of assuming the Council score is always useful.
- Requires meaningful completed-trade evidence before affecting risk; while samples are insufficient the state is `LEARNING` and multiplier remains 1.00x.
- Calibration grades: `LEARNING`, `CALIBRATED`, `NEUTRAL`, `INVERTED`.
- Risk influence is deliberately asymmetric and bounded to about `0.85x–1.03x`: bad calibration can de-risk more than good calibration can increase exposure.
- Small samples are shrunk strongly toward neutral to reduce overfitting.
- Symbol/timeframe/side-local performance is also collected for diagnostics, while the global high-vs-low separation drives the bounded calibration score.
- Logs expose HIGH/LOW win rates, evidence sample count, calibration score/grade and applied risk multiplier.
- Decision snapshots now preserve consensus + calibration metadata so broker-history synchronization can learn from the actual entry context.
- Calibration cannot create BUY/SELL signals or override Critic veto, ConsensusGuard, RegimeGuard, CorrelationGuard, MarginGuard, Live Readiness or broker preflight.


## V3.10.26 — Backtest / Historical Replay Engine

- Adds `HistoricalReplayEngine` for candle-by-candle historical simulation.
- `FAST` replay reuses the live indicator pipeline (`add_indicators`, `snapshot`, `technical_score`) without calling Ollama.
- `FULL_AI` replay accepts a bounded `ai_decider` callback for targeted AI validation; without one it safely behaves like FAST.
- No replay path calls `order_send`; historical testing is isolated from live execution.
- Uses a conservative same-candle rule: if simulated SL and TP are both touched, SL is assumed first.
- Risk is equity-relative and expressed in R, making results comparable across symbols.
- Reports trades, win rate, profit factor, expectancy in R, return, max drawdown and longest losing streak.
- Produces per-regime breakdowns so TRENDING/RANGING/TRANSITION behavior can be compared.
- Keeps a full trade log and equity curve in the returned result for later A/B tests and dashboarding.
- `replay_from_mt5(...)` is a convenience function for pulling historical MT5 candles and running a replay without placing any trade.
- This is a research/validation layer; historical performance does not guarantee live results.


## V3.10.27 — Council Circuit Breaker + Fast Adjudication

- Adds a per-role/per-model circuit breaker for local Ollama Council calls.
- Two consecutive infrastructure failures open that circuit for 10 minutes.
- OPEN circuits fail immediately; after cooldown one HALF_OPEN recovery probe is allowed.
- Timeouts, connection errors and incomplete/empty responses count as infrastructure failures; valid HOLD/REJECT opinions do not.
- A failed/unavailable Critic no longer automatically triggers a 75s Chief + 45s fallback chain.
- Conservative Fast Adjudication requires strong alignment between Scout, Technical and deterministic evidence; otherwise it returns HOLD.
- Directional fast-adjudication confidence is capped at 0.72 and all normal downstream guards still apply.
- Valid Critic REJECT remains an absolute hard veto.
- Chief circuit degradation can skip the repeated fallback wait and use the same conservative adjudication path.
- Logs expose CIRCUIT_OPEN, CIRCUIT_OPENED and COUNCIL FAST ADJUDICATION states.


## V3.10.28 — ZPI Endpoint Resilience

- ZPI failures are isolated per endpoint instead of collapsing the whole intelligence context.
- Adds endpoint-specific timeouts; `fear-greed/crypto` is intentionally shorter so a slow upstream does not stall the trading loop for ~15 seconds.
- HTTP 500/502/503/504 and request timeouts are tracked per endpoint.
- Two repeated endpoint failures open a short endpoint circuit with exponential backoff (30s, 60s, 120s, capped at 300s).
- While an endpoint circuit is open, only that endpoint is skipped; healthy TradingView/Binance/calendar/news endpoints continue normally.
- Successful responses close/reset the endpoint circuit.
- Existing successful data is served as `STALE_CACHE_ENDPOINT` when a degraded endpoint fails or is cooling down.
- 429 remains account-wide because ZPI quota is shared across API keys/endpoints; the existing global 429 cooldown is preserved.
- No rapid automatic retry is performed inside one trading cycle, reducing quota waste and loop latency.
- Logs distinguish `ZPI ENDPOINT TIMEOUT`, `ZPI ENDPOINT DEGRADED`, endpoint circuit cooldown, and account-wide `ZPI RATE LIMIT 429`.
- ZPI consumers mark stale/degraded transport as `DEGRADED` rather than pretending the data was freshly fetched.
- Adds `endpoint_health_snapshot()` for future dashboard health visualization.
