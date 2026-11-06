import base64
import json
import time

from src.job_store import ReviewJobStore
from src.worker import ReviewWorker


def tick_and_drain(worker: ReviewWorker) -> None:
    """One poll, then every job claimable as of now, in this thread: what the
    runner threads do after a tick, made deterministic for a test."""
    worker.tick()
    as_of = time.time()
    while worker.run_next(as_of):
        pass


def jwt(exp: float) -> str:
    """An unsigned JWT carrying only `exp`: all the auth owner reads from it."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).rstrip(b"=")
    return f"e30.{payload.decode()}.sig"


def login(expires_in: float = 10 * 24 * 3600, refresh_token: str = "rt-1") -> str:
    """auth.json as `codex login` writes it for a ChatGPT account."""
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": "id-1",
                "access_token": jwt(time.time() + expires_in),
                "refresh_token": refresh_token,
                "account_id": "acct",
            },
            "last_refresh": "2026-10-10T00:00:00Z",
        }
    )


def begin_attempt(store: ReviewJobStore, job_id: int) -> None:
    """Put a job into `running` as a runner would, for a test that is setting up
    history rather than exercising the claim."""
    assert store.claim(job_id, max_attempts=99), f"job {job_id} was not claimable"
