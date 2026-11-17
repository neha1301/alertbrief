from __future__ import annotations

import uvicorn

from .logging_setup import configure_logging
from .settings import Settings


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    uvicorn.run(
        "alert_enrichment.main:app",
        host="0.0.0.0",
        port=8080,
        log_config=None,
    )


if __name__ == "__main__":
    main()
