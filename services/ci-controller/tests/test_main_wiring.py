from __future__ import annotations

from src.config import ControllerConfig
from src.main import build_scheduler
from src.scheduler import LocalScheduler
from src.scheduler_client import HttpScheduler

from tests.conftest import VALID_CONFIG


def _config(write_config) -> ControllerConfig:
    return ControllerConfig.load(write_config(VALID_CONFIG))


def test_scheduler_url_set_selects_http_scheduler(write_config):
    config = _config(write_config)

    scheduler = build_scheduler(config, "http://ci-scheduler:8001")

    assert isinstance(scheduler, HttpScheduler)


def test_scheduler_url_absent_selects_local_scheduler(write_config):
    config = _config(write_config)

    scheduler = build_scheduler(config, None)

    assert isinstance(scheduler, LocalScheduler)


def test_scheduler_url_empty_selects_local_scheduler(write_config):
    config = _config(write_config)

    scheduler = build_scheduler(config, "")

    assert isinstance(scheduler, LocalScheduler)
