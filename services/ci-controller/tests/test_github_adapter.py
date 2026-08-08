import datetime as dt

import httpx
import pytest
import respx
from src.github_adapter import GitHubAdapter

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
