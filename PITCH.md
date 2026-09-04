# 100-word product pitch

Most watchlists show a red/green ticker and stop. This one answers three
different questions: *what changed since I last checked* (a per-user
checkpoint replays a compressed diff), *is this move statistically real*
(scored against each stock's own trailing volatility, not a fixed 3%), and
*can I trust the number at all* (independent sources — Yahoo and BSE —
fanned out concurrently, resolved by median, gated by a decomposed
confidence score). A `verified` label is enforced in code to require
cross-verification, never a single source. Ground-truth detection stays
deterministic; AI only ever explains an already-confirmed move, always
cited, never deciding one.
