# Smart Market Watchlist

**Live demo → https://smart-market-watchlist-f2s6.onrender.com/**
*(free Render tier — first load after idle takes ~40–60s to wake, then it's instant)*

Built for **Code, by Groww 2026** (HackerEarth) · solo · 72-hour build.

A stock watchlist that treats **"what changed"** as a diff problem, **"is this real"** as a statistics problem, and **"why did it happen"** as the only place AI is allowed near the truth.

📐 **Architecture blueprint:** https://claude.ai/code/artifact/c629a769-6ed1-4d93-812a-efb7449a9286

---

## Try it in 30 seconds

1. Open the [live link](https://smart-market-watchlist-f2s6.onrender.com/) — it signs you in automatically (real JWT, fixed demo account, no signup friction).
2. Search **`HDFC`** — it resolves to HDFCBANK. Try `tata` or `bank` too.
3. Add it. The price appears labelled, with today's move and a **`verified`** badge.
4. Click **"price confirmed by 2 independent sources"** to see which sources agreed and how fast.
5. If a stock has moved unusually, hit **"Why did this happen?"** for an AI explanation built only from cited news headlines.

---

## What it does differently

Most watchlists show a red/green ticker and stop there. This one:

1. **Diff since your last visit.** A per-user, per-symbol checkpoint (`last_seen_at`) bumps every time you view the list. Return after 3 days and the card summarises the gap — event count, biggest move, net drift — not just today's number.
2. **Volatility-normalized significance.** A move is scored in standard deviations against *that stock's own trailing volatility*, not a fixed percentage. Defensible answer to "why 3%?" — because there is no arbitrary 3%. Surfaced to users in plain language: *"2.4× this stock's normal daily move."*
3. **Multi-source reconciliation with a confidence score.** Independent sources fan out concurrently, resolve via **median** (robust to one broken source), and are gated by a decomposed score (`coverage` + `agreement` + `freshness`). A single-source quote can **never** be labelled `verified` — that invariant is enforced in code and guarded by a test.
4. **AI is walled off from ground truth.** No LLM ever decides a price or a significance verdict. `POST /api/stocks/{symbol}/events/{id}/narrate` only ever explains an event that's *already confirmed and persisted*, using real news headlines, and always returns `generated_by` so a headline lookup is never passed off as AI synthesis.
5. **A real background poller.** APScheduler runs the identical pipeline on a 10-minute interval, gated to NSE trading hours — near-zero ingestion cost outside market hours, and cost bounded by *distinct symbols watched*, not user count.
6. **Live push notifications.** A confirmed event fans out over WebSocket to every watcher, priority-tagged (P0/P1/P2) from the z-score and that watcher's intent tag.
7. **Real JWT auth, cross-device by construction.** Every watchlist row is scoped to a signed-in user, not a hardcoded id.
8. **Honest failure modes.** When a source fails or a symbol can't be resolved, the app says *why* — it never fabricates a price to fill the gap.

---

## Setup

Requires Python 3.11+.

```bash
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765/**. API docs at `/docs`. The frontend is served by the same FastAPI process — no Node, no build step.

Optional config (see [backend/.env.example](backend/.env.example)) — a gitignored `backend/.env` is auto-loaded:

| Variable | Default | What it does |
|---|---|---|
| `JWT_SECRET_KEY` | insecure dev default (warns loudly) | Signs auth tokens. Set a real value in production. |
| `DATABASE_URL` | `sqlite:///./watchlist.db` | Swap to Postgres with zero code changes. |
| `SCHEDULER_ENABLED` | `1` | Background poller. `0` to use only the manual trigger. |
| `POLL_INTERVAL_MINUTES` | `10` | Poll cadence, during market hours only. |
| `ENABLE_NSE` | `0` | NSE adapter. Off by default — see limitations. |
| `GROQ_API_KEY` | unset | Free tier at [console.groq.com](https://console.groq.com), no credit card. Enables AI narration. |
| `GROQ_NARRATOR_MODEL` | `llama-3.3-70b-versatile` | ⚠️ Groq rotates models — `openai/gpt-oss-120b` works as of this build. |
| `ANTHROPIC_API_KEY` | unset | Alternative provider (paid API credits). |

`GET /api/health` reports the resolved `narrator_provider` and active `sources`, so config is verifiable rather than guessed.

### Verify it yourself

```bash
cd backend
python smoke.py                          # live multi-source fetch, no server needed
python -m unittest discover -s tests     # 91 unit tests
```

---

## Code map (read in this order)

| File | What it is |
|---|---|
| [app/market_data.py](backend/app/market_data.py) | **Start here.** Multi-source reconciler, confidence score, the `verified` invariant, per-source circuit breakers, bounded queues, exchange-circuit awareness. |
| [app/significance.py](backend/app/significance.py) | Z-score detector with tier-gated thresholds. Pure — no I/O. |
| [app/pipeline.py](backend/app/pipeline.py) | The shared poll → reconcile → detect → persist → notify cycle. One code path for both the manual trigger and the scheduler — no drift between "the demo button" and "the real poller". |
| [app/catalog.py](backend/app/catalog.py) | Instrument catalog + ranked search, and the single source of BSE scrip codes — so anything autocomplete offers is guaranteed resolvable. |
| [app/scheduler.py](backend/app/scheduler.py) | Background poller. `market_is_open()` is pure and unit-tested. |
| [app/notifications.py](backend/app/notifications.py) | Reverse index (symbol → watchers), priority classification, per-user WebSocket manager. |
| [app/narrator.py](backend/app/narrator.py) | The AI boundary in code. Provider-agnostic (Groq / Anthropic / honest fallback). |
| [app/auth.py](backend/app/auth.py) + [routers/auth.py](backend/app/routers/auth.py) | JWT issuance, password hashing, register/login/demo-login. |
| [app/sources/](backend/app/sources/) | Source adapters sharing one `MarketSource` protocol. **Adapters never raise** — failures return as data. |
| [static/index.html](backend/static/index.html) | Frontend — vanilla JS, dark/light themes, typeahead search, live toasts. No build step. |
| [tests/](backend/tests/) | 91 tests. Notable: `TierInvariants` (single source can't be `verified`), `CitationIntegrity` (every prompted headline must be returned as a source), `MarketHours`, `ConnectionManagerBehavior`. |
| [CLAUDE.md](CLAUDE.md) | Full design context — a fresh session opened in this repo inherits every decision. |

---

## What's built vs. what's roadmap

**Built, deployed, and demonstrable live:**
- Multi-source reconciliation (Yahoo + BSE live; NSE implemented but disabled — see below)
- Tier-gated confidence score with the "≥2 sources to be verified" invariant
- Diff-since-last-visit, per authenticated user
- Volatility-normalized significance detection, thresholds gated by data confidence
- Exchange-circuit awareness, per-source circuit breakers with exponential backoff
- APScheduler background poller, market-hours gated
- Live WebSocket notifications with priority tiers
- Real JWT auth (register / login / demo-login)
- AI event narrator — provider-agnostic, cited, honestly labelled
- Searchable instrument catalog with ranked typeahead
- Full frontend: dark/light themes, profile menu, labelled prices, onboarding legend
- 91 passing unit tests
- Deployed on Render via [render.yaml](render.yaml)

**Documented but not built (72-hour scope):**
- Horizontal load-testing at real concurrency
- Licensed vendor feeds (what production Groww would use instead of free sources)
- Redis-backed notification inbox with true time-window coalescing

---

## Honest limitations

- **NSE is disabled by default.** Their bot detection blocks datacenter IPs outright — every request from Render returns 403. `curl_cffi` TLS impersonation plus browser-shaped XHR headers gets partway; beating it needs a residential proxy or a licensed feed. Shipping a permanently-failing source is worse than omitting it: it dragged `coverage` to 2/3, lowering every quote's confidence for a reason no user could act on. The adapter and its tests stay in the tree and work from a residential IP (`ENABLE_NSE=1`). This is a deployment switch, not a deletion.
- **Catalog covers 40 major NSE stocks.** Only instruments with a verified BSE scrip code are included — inventing codes would produce symbols that autocomplete happily then fail to fetch. Production would seed the full list from BSE's own securities endpoint.
- **Indian equities only.** Global tickers (`AAPL`, `SAP`) won't resolve, and the app now says so explicitly rather than showing a blank card.
- **SQLite on Render's free tier is ephemeral.** Data resets on redeploy or after idle spin-down. Point `DATABASE_URL` at Postgres for real persistence — no code changes needed.
- **Load testing found a real bottleneck — see the section below.** It's been measured and partly fixed, not hand-waved.
- **Notification coalescing is simplified.** Priority tagging and reverse-index fanout are real; time-window batching is documented, not built.

---

## Load testing — and the bottleneck it found

`loadtest.py` hammers `GET /api/watchlist` (the endpoint whose cost actually grows with users) against a locally-seeded database. External market APIs are never called, so the numbers measure *this* system rather than yfinance's latency.

**The finding.** Throughput flatlined at ~47 rps regardless of concurrency while latency grew linearly — the signature of requests serializing behind a single resource:

| Concurrent users | rps | p50 | p99 |
|---:|---:|---:|---:|
| 1 | 47 | 21 ms | 26 ms |
| 5 | 47 | 101 ms | 155 ms |
| 10 | 45 | 209 ms | 455 ms |
| 25 | 12 | 458 ms | **30,719 ms** |
| 50 | — | — | **collapsed, 1000 errors** |

**The controlled experiment.** `loadtest_readonly.py` hits a purely read-only endpoint on the same server and database. It sustained **170–197 rps and survived 50 concurrent users with zero errors** — isolating the cause to the write, not the framework or the machine.

**The cause.** `GET /api/watchlist` performed a *write* on every read: it advanced each item's `last_seen_at` checkpoint. SQLite serializes writers, so every read queued behind a write lock. I had assumed this was negligible; it was the single biggest constraint in the system.

**The fix** is semantic rather than a trick. `last_seen_at` records what the user has *seen* — so if a view surfaced nothing, there is nothing to acknowledge and the checkpoint can stay put. The next genuine event still lands after it either way, so the diff a user sees is unchanged. Since ~91% of views surface no events (measured independently by `prove_detection.py`), this removes the write from the overwhelming majority of requests.

| At 10 concurrent users | Before | After |
|---|---:|---:|
| Throughput | 44.8 rps | **92.2 rps** |
| p50 | 209 ms | **104 ms** |
| p99 | 455 ms | **155 ms** |

**What I have NOT proven:** that the ceiling is gone at 25+ users. That run timed out before producing a clean number, and I'm not going to claim a result I didn't measure. The writes that *do* happen still serialize on SQLite, so a ceiling certainly remains — just a higher one. The real production answer is Postgres (already supported via `DATABASE_URL`, no code change), which has proper concurrent writers.

## Two bugs worth mentioning

Both were found by testing against live services, not by reading code:

1. **Citation integrity.** The narrator prompted the model with 5 headlines but returned only 3 in the response. When the model legitimately cited headline #5, it *looked* like a hallucination with no way to verify it — silently breaking the "always cited" guarantee. The model was correct the whole time; the serialization was hiding the evidence. Fixed, with a regression test asserting the prompted set and returned set match.
2. **`localStorage` throws.** In private browsing and some embedded contexts the accessor raises rather than returning null, which killed the entire frontend with an unrecoverable error. Every access is now wrapped with an in-memory fallback — degraded, not dead.

---

## Deploying

[render.yaml](render.yaml) is a ready-to-use Blueprint. On [render.com](https://render.com): **New → Blueprint**, connect the repo, deploy. Then add `GROQ_API_KEY` and `GROQ_NARRATOR_MODEL` in the Environment tab to enable AI narration.

## Tech stack

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy 2.0, SQLite (Postgres-ready via `DATABASE_URL`)
- **Auth:** `python-jose` (JWT), `passlib` with `pbkdf2_sha256` (no native bcrypt — sidesteps a known version-compat footgun)
- **Scheduling:** `APScheduler`
- **Ingestion:** `yfinance`, `curl_cffi` (browser TLS impersonation for BSE), `asyncio` fan-out
- **AI:** provider-agnostic via OpenAI-compatible transport (Groq default) or Anthropic SDK
- **Realtime:** native FastAPI WebSocket
- **Frontend:** vanilla JS + CSS custom properties, no build step
- **Testing:** `unittest`, no external runner

Full dependency list in [backend/requirements.txt](backend/requirements.txt).
