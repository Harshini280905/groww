# 100-word product pitch

Most watchlists show a red/green ticker and stop. This one answers three
different questions: *what changed since I last checked* (a per-user
`last_seen_at` checkpoint replays a compressed diff), *is this move
statistically real* (z-score against each stock's own trailing volatility,
not a fixed 3%), and *can I trust the number at all* (three independent
sources — Yahoo, NSE, BSE — fanned out concurrently, resolved by median,
gated by a decomposed confidence score). A `verified` label is enforced in
code to require cross-verification, never a single source. Ground-truth
detection stays deterministic; AI narration is walled off downstream.
