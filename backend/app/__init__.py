"""Package init — loads .env before any submodule reads os.getenv().

Several modules (auth.py, db.py, narrator.py, scheduler.py) read their
configuration at import time. This runs first, so a local .env file is
picked up automatically without every developer having to export vars by
hand or paste secrets onto a command line (where they'd leak into shell
history and the process list).

.env is gitignored. Production platforms like Render inject real
environment variables directly, in which case there's no .env file and
this is a no-op — env vars already set always win over the file.
"""

from pathlib import Path

try:
    from dotenv import load_dotenv

    # backend/.env — one level up from backend/app/
    _ENV_PATH = Path(__file__).parent.parent / ".env"
    if _ENV_PATH.is_file():
        load_dotenv(_ENV_PATH, override=False)   # real env vars take precedence
except ImportError:                              # pragma: no cover - optional dep
    pass
