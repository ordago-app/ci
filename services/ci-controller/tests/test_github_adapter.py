import datetime as dt

import httpx
import pytest
import respx
from src.github_adapter import JOBS_PAGE_SIZE, MAX_RUNNER_PAGES, GitHubAdapter, RunnerInfo
from src.models import RunningJob

# RSA test key generated for tests only (not a real secret).
TEST_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQDokimLOmyVwdSX
k1Pqr1uNqUI++yfD16TYlyx3BvIv31FMGqxW0VnQV8NJFJ6b+vUa6xABzGLVOfWq
RTOxL5hCRtjavEhHVtTpXPqSsHOGDvaQ++o/cXTU1AH8XYSxHSkQv+Wy4LDdUUyS
ZOSglBBTVN2xtQaNmN8EMfwBWtzsWreLGAtPz4K9ER6SGvY23hDmQubVAbDBzHcS
1onmpi0K+71amssZ2sqPUdfD20BdeA53VbL4h2vGw8P6JkLPTZcEd6n1Hk3KklHe
Ai99Fx7FBjDHnc8P5G0BbvACpKd0ZAxxjcUK5Dt5Sbse/igWKwBV3R63r6ldlUre
Amr7VHhRAgMBAAECggEADXjD+8rlVdJtwl74HjDMtJrAmd3aCpPf+mTdYEKwfjnp
H2evInNLiNBAoWfWnTHvB0FlArmoYvIRyyxph2K6puIsNxVveWRr/l2SrTMX6gTN
Xw4cnlKv4hEq0UfIgyrtgUkgYNl3nUZTkWpTPQL+pBkXI29ZQxP/HSLZFmoBQJaP
YB7d4ceiPGmCjyc1zaqfDznKvdajRiwY2PB3gsncvlVtMIrG5W1c1XukCxBA2ntt
NNSKmy6y1kRL8JqWQ6XHqdSLaXHo17+8volgCKEZ9MvIO60QOaBvSFra1o3o65W6
McoytNyyZ2CTt3udHzUiWO109joVGFwxuKhJTT8rnQKBgQD9HvziNoxISaF5Oms/
tJERDeKQscLYSBRgfRQOZsoRM8wYpSshyVcVGu0Knkf4jmZ5zFCMwUMZ+1k9WXbR
MwfICnzNjphUGtcltii0ZZNqCppKwAJaO/EGQkdj5UoH/EuERHEkLdsbAXEUz1m+
8YDtdby9l0cIe0byXtw2CZApBQKBgQDrN1a46SWOTZ9KZT1P+4Dbxx97rF2jkZU5
F6bnpd4yc6hRExsI33nc0DmbhImXos/8zdVki18XAq/aGh32NJr1OLbR0FuSs9sr
u4p53GG61oaZ0GzqgYjTlYVevK+krcftpdgy8LuxjKsKWMqFcbmqE8/NVJmzC3Sg
W9AUCK4D3QKBgHrY0kT70mO3EJ5kgu69NPbA9WfiTj1n5jPaIKTIsGNe7zw61U8l
h5Ufp1HS9f0lJ4kPZzyZA3cVrP8Ab5EiojEtHdspzLZs/GQ6H1FGRyBdGvsSa1Hd
66FtA3bxLlfn08LS8NJtSvy1W2uNIvJwBXG6BatCQ2BTbGBvN5MmWwf9AoGAa9Y9
Hh1VqH4Rz2vGxkqJ8zjBSFPnwjvWbAxZ6s3yprK7sh/OPy0lk4SrRI9o/WoZbM95
S9VRzRzgPl/G6L+JY2+S8XJS6Vkn3E7o16Gf9KaxowcZSBIHBun/8UUUSa2agWuN
SR1xD59sMxwuDSvscPsQRBTLOnjACVzOcsDf9skCgYAVOBRTcyrU6GlAPsB2LkdX
ifknved7JTSqvVRVqs7rv7Z1hbGhS/qSRvf9v04T7nDHxJyLz7c40fP/olq4i1rj
eOOvO2WaQcAaAonfGt+TjuBvZC6JUF/F/AW0hXY4W84CliMKgUaLHha7ugb0fIhK
CJ/PQWoj+19wLKH49xXz5w==
-----END PRIVATE KEY-----"""


def _future_iso() -> str:
    return (dt.datetime(2099, 1, 1, tzinfo=dt.UTC)).isoformat().replace("+00:00", "Z")


@respx.mock
def test_mint_registration_token() -> None:
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    reg = respx.post("https://api.github.com/repos/o/r/actions/runners/registration-token").mock(
        return_value=httpx.Response(201, json={"token": "ARRT", "expires_at": _future_iso()})
    )

    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    token = gh.mint_registration_token("o/r")

    assert token == "ARRT"
    assert reg.calls.last.request.headers["Authorization"] == "Bearer ghs_inst"


@respx.mock
def test_installation_token_is_cached() -> None:
    inst = respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    assert gh.installation_token() == "ghs_inst"
    assert gh.installation_token() == "ghs_inst"
    assert inst.call_count == 1  # second call served from cache


@respx.mock
def test_list_queued_jobs() -> None:
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    respx.get(
        "https://api.github.com/repos/o/r/actions/runs",
        params={"status": "queued", "per_page": "50"},
    ).mock(return_value=httpx.Response(200, json={"workflow_runs": [{"id": 555}]}))
    respx.get("https://api.github.com/repos/o/r/actions/runs/555/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": 1, "status": "queued", "labels": ["self-hosted", "homelab"]},
                    {"id": 2, "status": "in_progress", "labels": ["self-hosted", "homelab"]},
                ]
            },
        )
    )

    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    jobs = gh.list_queued_jobs("o/r")

    assert len(jobs) == 1
    assert jobs[0].job_id == 1
    assert jobs[0].repo == "o/r"
    assert jobs[0].labels == ["self-hosted", "homelab"]


@respx.mock
def test_queued_jobs_carry_workflow_and_job_name() -> None:
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    respx.get(
        "https://api.github.com/repos/o/r/actions/runs",
        params={"status": "queued", "per_page": "50"},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"workflow_runs": [{"id": 5, "name": "CI", "path": ".github/workflows/ci.yml"}]},
        )
    )
    respx.get("https://api.github.com/repos/o/r/actions/runs/5/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 77,
                        "status": "queued",
                        "name": "build-android",
                        "labels": ["self-hosted", "ordago-ci"],
                    }
                ]
            },
        )
    )

    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    (job,) = gh.list_queued_jobs("o/r")

    assert job.job_id == 77
    assert job.job_name == "build-android"
    assert job.workflow == "CI"


@respx.mock
def test_job_conclusion_returns_the_terminal_value_when_present() -> None:
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    respx.get("https://api.github.com/repos/o/r/actions/jobs/42").mock(
        return_value=httpx.Response(
            200, json={"id": 42, "status": "completed", "conclusion": "success"}
        )
    )
    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    assert gh.job_conclusion("o/r", 42) == "success"


@respx.mock
def test_job_conclusion_is_none_when_the_conclusion_key_is_missing_entirely() -> None:
    """A job payload with no `conclusion` key at all (some in-progress responses omit it)."""
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    respx.get("https://api.github.com/repos/o/r/actions/jobs/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "status": "in_progress"})
    )
    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    assert gh.job_conclusion("o/r", 42) is None


@respx.mock
def test_job_conclusion_is_none_for_an_explicit_null_conclusion_on_an_in_progress_job() -> None:
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    respx.get("https://api.github.com/repos/o/r/actions/jobs/42").mock(
        return_value=httpx.Response(
            200, json={"id": 42, "status": "in_progress", "conclusion": None}
        )
    )
    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    assert gh.job_conclusion("o/r", 42) is None


@respx.mock
def test_job_conclusion_raises_on_a_non_2xx_response() -> None:
    """The caller (controller.reconcile) is responsible for catching this and writing the
    'lookup_failed' sentinel — job_conclusion itself must not swallow it."""
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )
    respx.get("https://api.github.com/repos/o/r/actions/jobs/42").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )
    gh = GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)
    with pytest.raises(httpx.HTTPStatusError):
        gh.job_conclusion("o/r", 42)


def _token() -> None:
    respx.post("https://api.github.com/app/installations/123/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_inst", "expires_at": _future_iso()})
    )


def _adapter() -> GitHubAdapter:
    return GitHubAdapter(app_id="9", installation_id="123", private_key_pem=TEST_KEY)


@respx.mock
def test_list_runners_reports_busy_state() -> None:
    _token()
    respx.get("https://api.github.com/repos/o/r/actions/runners").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 2,
                "runners": [
                    {"id": 11, "name": "powerserver-cici-1", "busy": True},
                    {"id": 12, "name": "powerserver-cici-2", "busy": False},
                ],
            },
        )
    )

    listing = _adapter().list_runners("o/r")

    assert listing.runners == [
        RunnerInfo(runner_id=11, name="powerserver-cici-1", busy=True),
        RunnerInfo(runner_id=12, name="powerserver-cici-2", busy=False),
    ]
    assert listing.complete is True


def _runner_page(n: int, start: int) -> list[dict]:
    return [
        {"id": i, "name": f"powerserver-cici-{i}", "busy": False} for i in range(start, start + n)
    ]


@respx.mock
def test_list_runners_follows_pagination_to_the_end() -> None:
    """A single page is not the whole truth: the reaper infers a lane's ABSENCE from this
    list, so a lane sitting on page 2 must never look gone."""
    _token()

    def _respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        runners = _runner_page(100, 1) if page == 1 else _runner_page(50, 101)
        return httpx.Response(200, json={"total_count": 150, "runners": runners})

    respx.get("https://api.github.com/repos/o/r/actions/runners").mock(side_effect=_respond)

    listing = _adapter().list_runners("o/r")

    assert len(listing.runners) == 150
    assert listing.complete is True
    assert listing.runners[-1].name == "powerserver-cici-150"


@respx.mock
def test_list_runners_reports_a_truncated_listing_instead_of_pretending_it_is_complete(
    caplog,
) -> None:
    """Pagination is bounded so a pathological repo cannot eat the API budget. When the
    bound is hit the listing is flagged incomplete and warned about, never passed off as
    evidence that a missing lane is gone."""
    _token()
    respx.get("https://api.github.com/repos/o/r/actions/runners").mock(
        return_value=httpx.Response(
            200, json={"total_count": 100_000, "runners": _runner_page(100, 1)}
        )
    )

    with caplog.at_level("WARNING"):
        listing = _adapter().list_runners("o/r")

    assert listing.complete is False
    assert len(listing.runners) == 100 * MAX_RUNNER_PAGES
    assert any("truncated" in rec.message for rec in caplog.records)


@respx.mock
def test_delete_runner_returns_true_on_success() -> None:
    _token()
    respx.delete("https://api.github.com/repos/o/r/actions/runners/11").mock(
        return_value=httpx.Response(204)
    )

    assert _adapter().delete_runner("o/r", 11) is True


@respx.mock
def test_delete_runner_returns_false_when_github_says_busy() -> None:
    """422 is the race guard: the runner took a job between our poll and this call."""
    _token()
    respx.delete("https://api.github.com/repos/o/r/actions/runners/11").mock(
        return_value=httpx.Response(422, json={"message": "runner is currently running a job"})
    )

    assert _adapter().delete_runner("o/r", 11) is False


@respx.mock
def test_delete_runner_treats_an_absent_runner_as_deregistered() -> None:
    _token()
    respx.delete("https://api.github.com/repos/o/r/actions/runners/11").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    assert _adapter().delete_runner("o/r", 11) is True


@respx.mock
def test_delete_runner_raises_on_a_real_error() -> None:
    _token()
    respx.delete("https://api.github.com/repos/o/r/actions/runners/11").mock(
        return_value=httpx.Response(500)
    )

    with pytest.raises(httpx.HTTPStatusError):
        _adapter().delete_runner("o/r", 11)


@respx.mock
def test_find_job_for_runner_matches_by_runner_name() -> None:
    _token()
    respx.get("https://api.github.com/repos/o/r/actions/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": [{"id": 500, "name": "CI"}]})
    )
    respx.get("https://api.github.com/repos/o/r/actions/runs/500/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 900,
                        "name": "other",
                        "status": "in_progress",
                        "runner_name": "powerserver-cici-99",
                    },
                    {
                        "id": 901,
                        "name": "e2e",
                        "status": "in_progress",
                        "runner_name": "powerserver-cici-1",
                    },
                ]
            },
        )
    )

    found = _adapter().find_job_for_runner("o/r", "powerserver-cici-1")

    assert found.job == RunningJob(job_id=901, workflow="CI", job_name="e2e")
    assert found.complete is True


@respx.mock
def test_find_job_for_runner_returns_none_when_no_job_matches() -> None:
    _token()
    respx.get("https://api.github.com/repos/o/r/actions/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": [{"id": 500, "name": "CI"}]})
    )
    respx.get("https://api.github.com/repos/o/r/actions/runs/500/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 900,
                        "name": "other",
                        "status": "in_progress",
                        "runner_name": "powerserver-cici-99",
                    }
                ]
            },
        )
    )

    found = _adapter().find_job_for_runner("o/r", "powerserver-cici-1")

    # A clean miss: every job list was read to its end and none named this runner.
    assert found.job is None
    assert found.complete is True


@respx.mock
def test_find_job_for_runner_only_queries_in_progress_runs() -> None:
    _token()
    runs = respx.get("https://api.github.com/repos/o/r/actions/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": []})
    )

    assert _adapter().find_job_for_runner("o/r", "powerserver-cici-1").job is None
    assert runs.calls.last.request.url.params["status"] == "in_progress"


@respx.mock
def test_find_job_for_runner_reads_past_the_first_jobs_page() -> None:
    """GitHub paginates /runs/{id}/jobs at 30 by default. Requesting it without per_page
    or pagination meant a lane whose job sat on page 2 never attributed: it stayed
    `booting`, kept its spawned-for claim, and eventually reaped unattributed."""
    _token()
    respx.get("https://api.github.com/repos/o/r/actions/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": [{"id": 500, "name": "CI"}]})
    )
    page_one = [
        {
            "id": 800 + i,
            "name": f"filler-{i}",
            "status": "in_progress",
            "runner_name": f"powerserver-cici-{i}",
        }
        for i in range(JOBS_PAGE_SIZE)
    ]
    page_two = [
        {
            "id": 901,
            "name": "e2e",
            "status": "in_progress",
            "runner_name": "powerserver-cici-target",
        }
    ]

    def _jobs(request):
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json={"jobs": page_one if page == 1 else page_two})

    respx.get("https://api.github.com/repos/o/r/actions/runs/500/jobs").mock(side_effect=_jobs)

    found = _adapter().find_job_for_runner("o/r", "powerserver-cici-target")

    assert found.job == RunningJob(job_id=901, workflow="CI", job_name="e2e")
    assert found.complete is True


@respx.mock
def test_a_truncated_jobs_scan_is_not_reported_as_not_running() -> None:
    """ "We stopped looking" must be distinguishable from "this runner has no job".

    Returning a bare None for both is what made the pagination bug invisible: the
    controller counted a truncated scan as a normal not-yet-visible run and retried
    forever without ever saying the scan itself was incomplete.
    """
    _token()
    respx.get("https://api.github.com/repos/o/r/actions/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": [{"id": 500, "name": "CI"}]})
    )
    full_page = [
        {
            "id": 800 + i,
            "name": f"filler-{i}",
            "status": "in_progress",
            "runner_name": f"powerserver-cici-{i}",
        }
        for i in range(JOBS_PAGE_SIZE)
    ]
    respx.get("https://api.github.com/repos/o/r/actions/runs/500/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": full_page})
    )

    found = _adapter().find_job_for_runner("o/r", "powerserver-cici-nowhere")

    assert found.job is None
    assert found.complete is False, "a scan cut short at the page bound is not a clean miss"
