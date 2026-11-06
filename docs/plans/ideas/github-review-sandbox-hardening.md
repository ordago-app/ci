# github-review: what the review sandbox still lets through

**Priority:** medium — none is a known exploit; each widens what a hostile PR could reach once the reviewer runs on someone else's repo.

A fresh-context review of the unification (PR #21) probed the Claude sandbox
with a real CLI and found it holds: no shell, no write tools, project settings
and MCP ignored, the credential only in the environment. These are what it
named as residual, each verified against `main` at the time of writing.

## 1. Codex can send its access token out

`codex-review` runs `codex exec` with the account's access token in
`/codex-home/auth.json` (read-only, refresh token stripped) on a bridge network.
Codex has a shell, so a prompt injection in the diff can `cat` the file and
`curl` it out. The token is short-lived and cannot be refreshed, which bounds the
damage but does not remove it.

Options: an egress allow-list (a proxy that admits only the OpenAI API host), or
running codex with its own sandbox mode and no network for tool calls. The Claude
path does not have this exposure: it has no shell.

## 2. Git tokens carry the App's full permissions

`GitHubAppTokens` scopes each token to one repository but not to fewer
permissions. Git only needs `contents: read`; the same token class posts
reviews. Mint a second, `{"permissions": {"contents": "read"}}` token for
`GitWorktreeManager` so a leaked fetch token cannot post or comment.

## 3. No `/proc` deny and no canary

The Claude container's `Read` tool can read `/proc/self/environ`, which holds
the credential. The model has no way to send it anywhere except the review
body — but the review body is public. Add a deny rule for `/proc/**` (and
`/run/secrets`), and a canary test: a fixed fake credential, a prompt asking for
it, and an assertion that it never appears in the posted body. The canary needs
a real CLI run, so it belongs in a manual or scheduled check, not the unit suite.

## 4. `POST /reviews` is unauthenticated

Anyone who can reach the service's port can enqueue a review of any configured
repo's PR, spending the account's quota. It listens only on the instance's
Docker network, so the exposure is what else shares that network (on a host
where `github_review_network` is a shared one, that is every service on it).
Require a shared secret header, or drop the endpoint if polling covers every
caller.

## 5. CI tests on a different Python than the image runs

`services/github-review/Dockerfile` is `python:3.14-slim`; CI runs the tests and
mypy on 3.12 (`ci-controller` is on 3.12 throughout). Pin the image to the version
CI tests, or move CI to the image's, and build the image once to confirm the
lockfile's wheels resolve.
