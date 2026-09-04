# Code, by Groww — Smart Market Watchlist

## Event
- Hackathon: "Code, by Groww" (HackerEarth), Sep 4–7 2026, solo (team size 1), women-only.
- Submission: source (zip or git) + README with setup instructions + a 100-word product pitch. Must actually run.
- Graded on: engineering depth, problem interpretation, resilience/edge cases, code quality, simplicity, originality of thought — explicitly NOT feature count.
- Judges say outright: AI tools are fine to use; what's graded is what the AI *can't* decide for you — architecture, judgement, trade-offs. Every choice must be defensible when asked "why."
- Path: Build → Top 40 virtual presentations → Top 20 at Groww HQ Bengaluru → possible 6-month internship (₹1L/month) → possible PPO.

## Theme (verbatim intent)
Build a smart market watchlist that helps users understand what has **"meaningfully changed"** since they last checked — not just track prices. Minimum bar: create/manage a watchlist, view latest market info, return later and see what changed. You decide: what counts as meaningful, what to surface, how state persists across sessions/devices, how to handle stale/delayed/conflicting data, how it scales, where to keep it simple vs add complexity.

## Standing directives from the user (apply to every response in this project)
- **Be brutally honest. Never hallucinate capabilities, data sources, or numbers.** If something isn't verified, say so explicitly rather than presenting it as fact.
- Avoid the "obvious AI answer" (fixed % thresholds, generic price/volume/news scoring rubric) — that's the median submission every team will independently arrive at. Differentiate on architecture and judgement, not on more features.
- Think like the CEO of an actual fintech (Groww), not just a hackathon judge: regulatory/compliance risk (SEBI), cost of data licensing, brand trust — tie decisions back to Groww's own stated value: **"Responsible — building trust while managing users' hard-earned money."**
- Design for what a real Groww production system would need, not just what's gradeable in 72 hours — but be explicit in the README/pitch about which parts are actually built vs. which are documented as the production-scaling narrative. Never claim to have load-tested or built something that wasn't actually built.
- User will be questioned directly on every design choice ("why did you do X") — every decision needs a one-sentence, technically honest justification, not a hand-wave.

## Core architecture decisions (locked in)

**1. Diff-since-last-visit, not snapshot-on-load.** Per-user `last_seen_at` checkpoint per watchlist item. Returning after N days replays a compressed summary of everything that happened in the gap (event count, biggest single event, net drift) — not just "current price vs whatever." This directly answers the brief's own framing.

**2. Event-sourced significance log, computed once per symbol, not once per user.** A `SignificantEvent` ("commit") is written when a symbol's move is statistically significant. Ingestion/detection is symbol-centric; the diff a user sees is just a cheap read of events since their checkpoint. This is also the scaling answer: cost is bounded by the number of *distinct symbols* watched (~2,000 NSE-listed equities, hard ceiling), not by user count.

**3. Volatility-normalized significance (z-score), not a fixed % rubric.** `z = (today's return − trailing mean return) / trailing stddev return`, over a rolling window (e.g. 20d). A low-vol bluechip and a high-vol small-cap get different bars automatically. Defensible answer to "why 3%?" — because there's no arbitrary 3%.

**4. Push-based cache, not pull-based compute.** The ingestion worker computes and caches `symbol_summary:{symbol}` the moment new data lands (Redis, or an in-proc dict if Docker time is tight for the demo). API reads are O(1) cache lookups + a cheap per-user timestamp filter — never a live DB aggregation per request. This is what actually lets concurrent reads scale.

**5. Market-hours-aware, batched, bounded polling.** No polling outside NSE trading hours (9:15–15:30 IST) — cuts ingestion to near-zero for ~17 of 24 hours, free win, no infra. Batch API calls across symbols (e.g. `yf.download([...])`) instead of one call per symbol — real risk here is the upstream data source rate-limiting/blocking you during dev, not theoretical user-scale.

**6. Stateless API tier, single (or symbol-sharded) ingestion worker.** API instances can be replicated behind a load balancer freely (no server-side session state). The ingestion worker must NOT be naively replicated — duplicate polling wastes rate-limit budget and can produce duplicate/conflicting events. If ever sharded, partition by `hash(symbol) % num_workers`, not by user.

**7. Multi-source reconciliation with a confidence score (anti-hallucination layer for the data itself).**
- Sources actually available and honestly assessed (verified live via `backend/smoke.py`):
  - **Yahoo Finance** (`yfinance`, `.NS` suffix) — free, unofficial, sometimes ~15min delayed for India. **Live-tested working**: reliable primary source, ~200ms–2s latency.
  - **NSE India's own public JSON endpoints** — closest to "official," free, no key, but undocumented/unofficial (scraping the exchange's own site backend), needs session cookies + browser TLS fingerprint. **Live-tested**: returning HTTP 403 even with `curl_cffi` TLS impersonation + XHR headers — NSE has multi-layer bot detection (IP reputation + behavioral) that a plain adapter can't reliably beat. Documented as a known limitation; production would use a residential proxy or licensed feed.
  - **BSE India** direct JSON at `api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w` — uses numeric scrip codes (RELIANCE=500325 etc.), a small hardcoded map covers the demo set. **Live-tested working**: less aggressive bot detection than NSE, works out of the box with `curl_cffi`. Provides the second-source cross-verification that drives quotes to `[VERIFIED]` tier even when NSE fails.
  - **Twelve Data** (MCP connector available in this environment) — verified by direct test: works for global symbols (e.g. AAPL), but NSE quotes return "available starting with the Grow or Venture plan" on the current tier. Not used in production adapter — kept in reserve as documented fallback.
- **Current 3-source configuration**: Yahoo + NSE + BSE. Yahoo + BSE alone produce `[VERIFIED]` when both agree; NSE failure demotes to `[BEST_AVAILABLE]` only if BSE also drops out. That's the redundancy the multi-source design was for.
- Confidence formula: `confidence = 0.35·coverage + 0.45·agreement + 0.20·freshness`, where `coverage = sources_responded/configured`, `agreement = 1 − min(spread/spread_cap,1)` (spread = (max−min)/median price across responding sources), `freshness = 1 − min(max_staleness_sec/staleness_cap,1)`.
- Resolved price = **median** across sources, not mean (robust to one broken/stale source).
- Threshold gates behavior, not just a badge: ≥0.8 "Verified" (normal, can trigger events) · 0.5–0.8 "Best available" (shown, but requires a stronger z-score to fire a significance event) · <0.5 "Unconfirmed" (never fires an event off this reading; show last confirmed value with an explicit degraded-data badge instead of a fresh possibly-wrong number).

**8. Where AI/agents are and are not allowed to touch the pipeline.**
- Never for ground truth: no LLM decides a price or whether a move is "significant" — must stay deterministic, auditable, reproducible, cheap enough to run per tick across thousands of symbols.
- Allowed, and only downstream of a confirmed significant event: an LLM/agent synthesizes the human-readable "why" from news/filings, always labeled as AI-generated, always with a source link the user can verify. Never presented as fact.
- Concurrent fetch-from-N-sources with per-source retry/timeout/circuit-breaker is a standard fan-out/fan-in distributed-systems pattern — fine to badge as "agentic ingestion" for the pitch, but be honest under questioning that there is no LLM making the per-tick reconciliation decision.

## Brief-interpretation resolution (locked)

The theme is "build the smart watchlist that *should exist*," not "build a feature for Groww's existing app." The brief explicitly lists "How to handle stale, delayed or conflicting data" as a decision point, which directly justifies the multi-source reconciliation and confidence score. **Keep both the confidence score and the AI narrator** — they answer the brief verbatim. Under CEO-style questioning ("would this ship at Groww?"), the honest answer is: "The confidence-score surface wouldn't ship at Groww because Groww is a primary-source broker; in that context the same math moves server-side as an ops data-quality signal. The brief asked me to solve conflicting data, so I built the full user-facing pattern."

## Notification distribution / coalescing tier (scaling answer for user-side fanout)

**Problem**: a significant event on a widely-watched symbol fans out to potentially millions of user watchlists — naive `for user in watchers: push()` is O(N) per event, causes notification fatigue during volatile sessions, and burns real money through FCM/APNs.

**Design**:
1. **Reverse index**: `symbol_watchers:{symbol}` as a Redis SET, maintained on add/remove-to-watchlist. Turns fanout into O(1) set lookup + parallel enqueue.
2. **Per-user inbox with coalescing**: `user_inbox:{user_id}` as a Redis sorted set (score = timestamp). Router writes events into inboxes; a scheduled flusher (every ~60s, or debounced) drains and delivers — 1 event = single push, 2+ events in the window = single digest push ("3 events on your watchlist").
3. **Priority tiers**: P0 immediate (circuit hits, |z|>4, user's `own_it`-tagged stocks with |z|>3) bypasses coalescing; P1 batched (normal significant events, |z|>2) uses the 5–10 min coalescing window; P2 digest (interesting-but-not-alerting) goes to hourly or morning digests only.
4. **User controls**: quiet hours, per-symbol notification level (Off / Digest / Real-time), global digest mode. These prevent the unsubscribe cliff.
5. **Hackathon-scoped substitution**: Redis pub/sub for the router (in-proc dict fallback), APScheduler for the flusher, WebSocket to the browser for demo-time live updates without needing real push infra.

## Backpressure: ingestion → reconciler under source flapping (resilience answer)

**Problem**: circuit-breaker events, breaking news, or a flaky upstream source can produce a burst of ticks. Unbounded queues = OOM or growing lag. Naive dropping loses the freshest (most valuable) data.

**Design — five layers, each independently valuable**:
1. **Bounded per-symbol queues with drop-*oldest* discipline**: `asyncio.Queue(maxsize=5)` per symbol; on full, `get_nowait()` the oldest before `put_nowait` the new. Newer ticks are strictly more valuable than 400ms-old ones for a live-price system — drop-oldest is the honest default.
2. **Per-symbol rate cap on reconciliation**: at most 1 reconciliation per symbol per second; within that second, use only the latest tick per source. Bounds downstream work regardless of upstream burst rate.
3. **Exchange-circuit awareness**: detect when live price is within ε of NSE's published upper/lower circuit bounds; while `IN_CIRCUIT`, suspend significance detection entirely (any "move" while circuited is regulatory-defined, not a market signal — flagging it would be a lie), emit a single `IN_CIRCUIT` event, keep recording raw ticks for post-hoc, resume normal processing on release.
4. **Per-source circuit breakers (CS-kind)**: wrap each source (Yahoo, NSE direct) in a circuit breaker tracking error rate + p95 latency + consecutive failures over a rolling 5-min window. On breach: open with exponential cooldown (30s → 1m → 5m), half-open probe after cooldown, close on success. Confidence score naturally reflects the drop through coverage and freshness terms.
5. **Global admission control (stretch)**: if aggregate ingest rate exceeds a system-wide cap, prioritize most-watched + most-volatile symbols and degrade lesser-watched ones to a slower cadence.

## Explicitly out of scope for the 72-hour build (document as roadmap, don't fake it)
- Real horizontal load-testing at scale (10k+ concurrent users) — not attempted, own that honestly rather than fabricate a benchmark.
- Actual multi-node Redis clusters / Kubernetes / real load balancer infra — not needed to prove the pattern for a hackathon demo.
- Licensed real-time vendor feeds (what a real Groww production system would use instead of scraped/free sources) — note in README as the production swap-in, not built here.

## Shipped since the design phase (status as of last session)

All four originally-deferred items are now built and tested:
- **BSE adapter** (`sources/bse.py`) — gives real 2-of-3 cross-verification live (Yahoo+BSE agree, NSE 403s but doesn't block `VERIFIED` tier).
- **Frontend** (`static/index.html`) — vanilla JS, diff cards, tier chips, source-readings drill-down, served directly by FastAPI (no build step).
- **APScheduler background poller** (`scheduler.py`) — market-hours-gated (09:15–15:30 IST, Mon–Fri), 10-min interval, calls the exact same `pipeline.poll_and_detect` the manual dev trigger uses. `market_is_open()` is pure and unit-tested.
- **Notification tier** (`notifications.py`) — §08 simplified to process-local structures: DB-backed reverse index (symbol→watchers), P0/P1/P2 priority classification (z-score + intent_tag), per-user `ConnectionManager` over WebSocket. Real-time delivery works; time-window coalescing for high-volume watchers is documented but not built (see module docstring for the Redis swap-in path).
- **Real JWT auth** (`auth.py` + `routers/auth.py`) — pbkdf2_sha256 password hashing (deliberately not bcrypt — sidesteps a known passlib/bcrypt version-compat footgun under time pressure), register/login/demo-login. `demo-login` is a zero-friction judge entry point that issues a token through the identical path as a real login — not a bypass.
- **`pipeline.py`** extracted as the shared poll→reconcile→detect→persist→notify cycle — both `routers/dev.py` and `scheduler.py` call it, so there's no drift between "what the demo button does" and "what the real poller does."
- 73 unit tests total (up from 46): `test_auth.py`, `test_scheduler.py`, `test_notifications.py` added.
- `render.yaml` + `.env.example` — deployment-ready config for Render's free tier. Actual account creation/deployment click-through was left to the user (creating third-party accounts on someone's behalf is out of scope for an assistant, regardless of convenience).

## Open decisions / next steps
- Deploy via Render (user-driven — see render.yaml + README "Deploying" section).
- Consider a Twelve Data plan upgrade for a genuine 3rd always-live NSE source, if NSE's bot detection remains unbeatable and the budget allows.
- Redis-backed notification coalescing (real time-window batching) — documented production upgrade, not built.
- Optional stretch (not committed): intent-tagged significance thresholds beyond notification priority (currently `own_it` only affects notification priority, not the significance z-score threshold itself); triage-inbox UX framing instead of a card grid.
