# 100-word product pitch

Most watchlists show a red/green ticker and stop. This one answers three
questions instead: *what changed since I last checked* (a per-user
checkpoint replays a compressed diff), *is this move statistically real*
(scored against each stock's own trailing volatility, not a fixed 3%), and
*can I trust the number* (independent sources reconciled by median, gated
by a decomposed confidence score). "Verified" is enforced in code to
require cross-verification, never one source. Chaos testing proves the
system degrades honestly rather than inventing a price under any failure.
Detection stays deterministic; AI only explains an already-confirmed move,
always cited, never deciding one.
