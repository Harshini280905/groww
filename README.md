# Smart Market Watchlist

Built for **Code, by Groww 2026** (HackerEarth), Sep 4–7 2026 · solo · 72-hour build.

A stock watchlist that treats **"what changed"** as a diff problem, **"is this real"** as a statistics problem, and **"why did it happen"** as the only place AI is allowed near the truth.

**📐 Architecture blueprint:** https://claude.ai/code/artifact/c629a769-6ed1-4d93-812a-efb7449a9286

---

## What it does differently

Most watchlists show a red/green ticker and stop there. This one:

1. **Diff since your last visit.** A per-user, per-symbol checkpoint (`last_seen_at`) is bumped every time you view the list. Return after 3 days and the card shows a compressed summary of the gap — event count, biggest single move, net drift — not just today's number.
2. **Volatility-normalized significance.** A move is scored in standard deviations against *that stock's own trailing volatility*, not a fixed percentage. A 2% move on a bluechip and a 2% move on a small-cap trigger different responses automatically. Defensible answer to "why 3%?" — because there is no arbitrary 3%.
3. **Multi-source reconciliation with a confidence score.** Three genuinely independent sources (Yahoo Finance, NSE India, BSE India) are fanned out concurrently, resolved via **median** (robust to one broken source), and gated by a decomposed confidence score (`coverage` + `agreement` + `freshness`). A single-source quote can never be labeled `VERIFIED` — the invariant is enforced in code (see `market_data.py::tier_for`).
4. **AI is walled off from ground truth — and now actually built.** No LLM ever decides a price or a significance verdict; that stays fully deterministic. `POST /api/stocks/{symbol}/events/{id}/narrate` calls an LLM *only* to explain an event that's already confirmed and persisted — it fetches real news via yfinance, and if `ANTHROPIC_API_KEY` is set, asks Claude to synthesize a short cited explanation from those headlines (with an explicit instruction to say "no clear cause found" rather than invent one). Without a key, it still returns a real, cited headline — just labeled honestly as `headline-fallback`, not pretending to be AI-generated. See [backend/app/narrator.py](backend/app/narrator.py).
5. **A real background poller, not just a demo button.** APScheduler runs the identical pipeline (`pipeline.poll_and_detect`) on a 10-minute interval, gated to NSE trading hours (09:15–15:30 IST, Mon–Fri) — near-zero ingestion cost outside market hours, and cost bounded by *distinct symbols watched*, not by user count.
6. **Live push notifications, not just a page you have to refresh.** A confirmed significant event fans out over WebSocket to every watcher of that symbol, tagged with a priority (P0 immediate / P1 batched / P2 digest) derived from the event's z-score and the watcher's own intent tag.
7. **Real JWT auth, cross-device by construction.** Every watchlist row is scoped to a signed-in user via a real bcrypt-free (pbkdf2_sha256) password hash + JWT — not a hardcoded demo user id. A `demo-login` convenience route removes signup friction for judges without being a security bypass: it issues a token through the exact same code path as a real login.
8. **Honest failure modes.** When a source 403s or times out, the tier is honestly demoted rather than the price fabricated. NSE currently returns 403 in this environment (their bot detection wins against a scraper adapter) — but Yahoo + BSE alone cross-verify to `VERIFIED` tier, live, in the demo.

---

## Setup

Requires Python 3.11+.

```bash
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Then open **http://127.0.0.1:8765/** in a browser. Auto-generated API docs live at http://127.0.0.1:8765/docs.

The frontend is served by the same FastAPI process — no separate Node/npm build.

Optional environment variables (see [backend/.env.example](backend/.env.example)):

| Variable | Default | What it does |
|---|---|---|
| `JWT_SECRET_KEY` | insecure dev default (warns loudly) | Signs auth tokens. Set a real value before deploying anywhere reachable. |
| `DATABASE_URL` | `sqlite:///./watchlist.db` | Swap to Postgres with zero code changes. |
| `SCHEDULER_ENABLED` | `1` | Background poller (§07). Set `0` to rely only on the manual dev trigger. |
| `POLL_INTERVAL_MINUTES` | `10` | How often the scheduler polls, during market hours only. |
| `DEV_ROUTES` | `1` | Exposes `/api/dev/*` manual-trigger endpoints. Set `0` for a hardened deploy. |
| `ANTHROPIC_API_KEY` | unset | Optional. Enables real LLM synthesis in the event narrator. Without it, narration still works and still cites real news — it just says so plainly instead of pretending. |
| `ANTHROPIC_NARRATOR_MODEL` | `claude-haiku-4-5-20251001` | Which Claude model narrates events, if a key is set. |

### First-run walkthrough

1. **Open the app.** It signs you in automatically via `/api/auth/demo-login` — a real JWT, issued through the same path as a normal login, just against a fixed documented account (`demo@watchlist.local`) so there's no signup friction. Prefer your own account? `POST /api/auth/register` with an email + password works identically.
2. **Add a symbol.** Try `TCS`, `RELIANCE`, `INFY`, `HDFCBANK`, or `SBIN`. The frontend automatically polls after adding.
3. **See the tier chip.** Yahoo + BSE responding → `verified`. Only one → `best-available`. None → `unconfirmed`.
4. **Open "why this tier?"** on any card to see per-source readings (which succeeded, latency, errors).
5. **Watch for a live toast.** If a significant event fires on a symbol you're watching — from the background scheduler or a manual poll — it pushes over WebSocket and shows as a toast in the corner, no refresh needed. The green dot next to your email shows the socket is live.
6. **Force an event now** rather than waiting for the 10-minute scheduler: open `http://127.0.0.1:8765/docs` and call `POST /api/dev/populate/{symbol}`, or click "Poll now" on any card.

### Verify the pipeline directly

```bash
cd backend
python smoke.py                          # live multi-source fetch, no server needed
python -m unittest discover -s tests     # 82 unit tests
```

---

## Code map (what to read, in order)

| File | What it is |
|---|---|
| [backend/app/market_data.py](backend/app/market_data.py) | Load-bearing: multi-source reconciler, confidence score, tier invariant, per-source circuit breakers, bounded queues, exchange-circuit awareness. |
| [backend/app/significance.py](backend/app/significance.py) | Z-score detector with tier-gated thresholds. Pure — no I/O. |
| [backend/app/pipeline.py](backend/app/pipeline.py) | The shared poll → reconcile → detect → persist → notify cycle. One code path for both the manual dev trigger and the scheduler — no drift between what the demo button does and what the real poller does. |
| [backend/app/scheduler.py](backend/app/scheduler.py) | APScheduler background poller. `market_is_open()` is pure and unit-tested directly. |
| [backend/app/notifications.py](backend/app/notifications.py) | §08 fanout: DB-backed reverse index (symbol → watchers), priority classification, per-user WebSocket `ConnectionManager`. |
| [backend/app/narrator.py](backend/app/narrator.py) | §11 AI boundary in code: fetches real news (yfinance, free), optionally calls Claude to synthesize a cited explanation of an *already-confirmed* event. Every response states `generated_by` so the UI never presents a headline lookup as if it were AI-generated. |
| [backend/app/auth.py](backend/app/auth.py) + [routers/auth.py](backend/app/routers/auth.py) | JWT issuance/verification, password hashing, register/login/demo-login. |
| [backend/app/sources/](backend/app/sources/) | Three source adapters: `yahoo.py`, `nse.py`, `bse.py`. All share the `MarketSource` protocol. Adapters never raise. |
| [backend/app/routers/](backend/app/routers/) | `watchlist.py` (diff engine, auth-scoped, bumps `last_seen_at`), `stocks.py` (per-symbol drill-down, unauthenticated — shared data), `dev.py` (manual pipeline trigger). |
| [backend/app/models.py](backend/app/models.py) | SQLAlchemy 2.0 models. |
| [backend/static/index.html](backend/static/index.html) | Frontend — vanilla JS, auth flow, live WebSocket toasts. |
| [backend/tests/](backend/tests/) | 73 unit tests. Notable: `TierInvariants` (single-source can't be `VERIFIED`), `MarketHours` (scheduler's gating logic), `ConnectionManagerBehavior` (dead-connection cleanup). |
| [CLAUDE.md](CLAUDE.md) | Standing directives and design context — a fresh Claude Code session opened in this folder inherits every decision made here. |

---

## What's built vs. what's roadmap

**Built and demonstrable live:**
- Multi-source reconciliation with 3 real sources (Yahoo + BSE working live, NSE configured but bot-blocked — see below)
- Tier-gated confidence score (`verified` / `best-available` / `unconfirmed`)
- Diff-since-last-visit with server-side `last_seen_at` checkpoints, scoped per authenticated user
- Volatility-normalized (z-score) significance detection with tier-gated thresholds
- Exchange-circuit awareness (`is_in_exchange_circuit`) with observed RELIANCE bounds
- Per-source circuit breakers with exponential-cooldown backoff
- **APScheduler background poller** — market-hours-gated, 10-minute interval, same pipeline as the manual trigger
- **Live WebSocket notifications** — priority-tagged (P0/P1/P2), reverse-indexed by symbol, delivered to every open session for a user
- **Real JWT authentication** — register/login/demo-login, pbkdf2-hashed passwords, every watchlist row scoped to `current_user`, not a hardcoded id
- **AI event narrator** — real news fetch (yfinance, free, always on) + optional Claude synthesis (`ANTHROPIC_API_KEY`), only ever explaining an already-confirmed event, never deciding one; honestly labels which path produced the text
- 82 passing unit tests
- Frontend UI with diff cards, tier chips, source-readings drill-down, live toast notifications, "Explain this move" narration panel
- Render deployment config ([render.yaml](render.yaml)) — one click after connecting the repo

**Documented but not built for the 72-hour scope:**
- Real horizontal load-testing at concurrency
- Licensed vendor feeds (would replace scraped sources at production Groww)
- Redis-backed notification inbox with real coalescing windows (current implementation pushes immediately over WebSocket — the priority classification and reverse-index lookup are real, but true time-window batching for high-volume watchers is the documented production upgrade, see `notifications.py` module docstring)

---

## Honest limitations

- **NSE returns 403.** NSE has multi-layer bot detection (TLS fingerprint + IP reputation + behavioral) that a plain scraper adapter can't reliably beat. `curl_cffi` with Chrome TLS impersonation and browser-shaped XHR headers gets partway; the rest needs a residential proxy or licensed feed. **This is actually a feature of the demo**, not a bug — it lets you see the tier drop from `VERIFIED` to `BEST-AVAILABLE` honestly when a source dies, which is precisely what the confidence-gating design was built for.
- **BSE uses numeric scrip codes.** A small hardcoded map covers 40 top-liquidity names; symbols outside that map return `unknown_scrip_code` honestly rather than failing silently.
- **SQLite on the free Render tier is ephemeral.** Data resets on redeploy or after the free instance spins down from inactivity. Acceptable for a judged demo that gets populated live; swap `DATABASE_URL` to a Render Postgres instance for real persistence — no code changes needed.
- **No load-test.** I did not stress-test concurrent users in 72 hours. The architecture doesn't have a scaling cliff in it (ingestion is bounded by symbol count, reads are cache-shaped, the API tier is stateless), but I own that I haven't proven it under load. Pretending otherwise would look worse than saying so.
- **Notification coalescing is simplified.** Priority tagging and the reverse-index fanout are real; the time-window batching that would prevent a burst of 20 events becoming 20 separate pushes is documented but not built (see `notifications.py`).

---

## Deploying

[render.yaml](render.yaml) is a ready-to-use Render Blueprint. After pushing this repo to GitHub:

1. On [render.com](https://render.com), **New → Blueprint**, connect the repo.
2. Render reads `render.yaml` automatically — build command, start command, and env vars (including an auto-generated `JWT_SECRET_KEY`) are already configured.
3. First deploy takes a few minutes. The free tier spins down after 15 minutes of inactivity and cold-starts on the next request (~30–60s) — expected, not a bug.

## Tech stack

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy 2.0, SQLite (Postgres via `DATABASE_URL` env)
- **Auth:** `python-jose` (JWT), `passlib` with `pbkdf2_sha256` (no native bcrypt dependency — sidesteps a known passlib/bcrypt version-compat footgun)
- **Scheduling:** `APScheduler` (AsyncIO scheduler, in-process)
- **Ingestion:** `yfinance` (Yahoo), `curl_cffi` (NSE + BSE — browser TLS impersonation), `asyncio` fan-out
- **Realtime:** native FastAPI `WebSocket`
- **Frontend:** vanilla JS + inline CSS (no build step)
- **Testing:** `unittest`, no external test runner

Full requirements in [backend/requirements.txt](backend/requirements.txt).
