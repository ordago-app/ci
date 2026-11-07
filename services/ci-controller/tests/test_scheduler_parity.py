from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from src.config import ControllerConfig
from src.ledger import Ledger
from src.models import BUDGET_FULL, AdmitDecision, DeferDecision, QueuedJob
from src.scheduler import LocalScheduler
from src.scheduler_api import create_scheduler_app
from src.scheduler_client import HttpScheduler

from tests.conftest import VALID_CONFIG

JOBS = [
    QueuedJob(job_id=i, repo="ordago-app/ordago-apps", labels=["ordago-ci"]) for i in range(1, 30)
]


class _TestClientTransport(httpx.BaseTransport):
    """Sync httpx transport that dispatches into a Starlette TestClient.

    Starlette's TestClient is itself httpx-based, so this is a thin forward rather
    than a reimplementation. It exists so HttpScheduler exercises the real FastAPI
    app — routing, validation, status codes — without binding a port."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
        )


@pytest.fixture()
def pair(write_config):
    """A LocalScheduler and an HttpScheduler backed by an independent LocalScheduler.

    Independent on purpose: sharing one ledger would let a bug that writes through only
    one path still produce matching reads."""
    path = write_config(VALID_CONFIG)
    local = LocalScheduler(ControllerConfig.load(path), Ledger())
    remote_backing = LocalScheduler(ControllerConfig.load(path), Ledger())
    app_client = TestClient(create_scheduler_app(remote_backing))
    remote = HttpScheduler(
        "http://sched",
        transport=_TestClientTransport(app_client),
        retries=1,
        backoff=0.0,
    )
    return local, remote


def test_same_decisions_over_a_full_admission_batch(pair):
    local, remote = pair

    local_decisions = local.plan(JOBS, {}, None, review_sessions=0, ram_offers={})
    remote_decisions = remote.plan(JOBS, {}, None, review_sessions=0, ram_offers={})

    assert [type(d).__name__ for d in local_decisions] == [
        type(d).__name__ for d in remote_decisions
    ]
    assert [getattr(d, "host", None) for d in local_decisions] == [
        getattr(d, "host", None) for d in remote_decisions
    ]
    assert [d.job.job_id for d in local_decisions] == [d.job.job_id for d in remote_decisions]


ELASTIC_CONFIG = (
    VALID_CONFIG
    + """\
hosts:
  powerserver:
    docker_endpoint: tcp://docker-socket-proxy:2375
  desktop:
    docker_endpoint: tcp://desktop:2375
    ram_offer_reserve_mb: 1000
"""
)


@pytest.mark.parametrize(
    ("offers", "admitted"),
    [({"desktop": 2500}, True), ({"desktop": 500}, False), ({}, False)],
    ids=["offer-fits", "offer-too-small", "no-offer"],
)
def test_offers_cross_the_wire_into_admission(write_config, offers, admitted):
    backing = LocalScheduler(ControllerConfig.load(write_config(ELASTIC_CONFIG)), Ledger())
    remote = HttpScheduler(
        "http://sched",
        transport=_TestClientTransport(TestClient(create_scheduler_app(backing))),
        retries=1,
        backoff=0.0,
    )

    (decision,) = remote.plan(JOBS[:1], {}, {"desktop"}, review_sessions=0, ram_offers=offers)

    assert isinstance(decision, AdmitDecision) is admitted
    if not admitted:
        assert isinstance(decision, DeferDecision)
        assert BUDGET_FULL in decision.reasons
