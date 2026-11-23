"""Entry point for ``python -m freshcheck`` (dev server)."""

from __future__ import annotations

import os

from .app import app
from .db import init_db


def main() -> None:
    init_db(app.config.get("DB_PATH"))
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        debug=False,
    )


if __name__ == "__main__":
    main()
