# Smart Market Watchlist — Backend

FastAPI service implementing the diff-since-last-visit, multi-source-reconciled
watchlist described in the architecture blueprint.

## Layout

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI entry (later)
│   ├── db.py                # SQLAlchemy session/engine (later)
│   ├── models.py            # ORM models (later)
│   ├── schemas.py           # Pydantic response models (later)
│   ├── cache.py             # Redis wrapper w/ in-proc fallback (later)
│   ├── market_data.py       # Multi-source fetch + reconciler + confidence
│   ├── significance.py      # z-score detector + tier-gated event emission (later)
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── yahoo.py         # Yahoo Finance adapter (later)
│   │   └── nse.py           # NSE India direct-JSON adapter (later)
│   └── routers/             # (later)
├── tests/
└── requirements.txt
```

## Design references

Every non-trivial decision here is anchored to a section in the architecture
blueprint (`watchlist-architecture.html`). Search for `§N.M` comments to trace
back the "why."

## Run (once main.py exists)

```
pip install -r requirements.txt
uvicorn app.main:app --reload
```
