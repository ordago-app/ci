from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn

from src.config import ControllerConfig
from src.ledger import Ledger
from src.scheduler import LocalScheduler
from src.scheduler_api import create_scheduler_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config_path = Path(
        os.environ.get("CI_CONTROLLER_CONFIG", "/etc/ci-controller/ci-controller.yml")
    )
    config = ControllerConfig.load(config_path)
    app = create_scheduler_app(LocalScheduler(config=config, ledger=Ledger()))
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")


if __name__ == "__main__":
    main()
