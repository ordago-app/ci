# What CodexReviewProvider runs a review in. Built by this repo, so an instance
# needs no image from anywhere else.
#
# The CLI is pinned, not floating: an unpinned install bakes whatever was latest
# at build time, and the drift is only discovered when the CLI stops
# understanding the models API (2026-09-01: every review died on `unknown variant
# \`max\``, which read as a red CI gate rather than an outage). A rebuild does
# not upgrade it either; only changing this pin does.
FROM node:22-slim

ARG CODEX_CLI_PACKAGE="@openai/codex@0.153.4"

# git, because the review prompt sends codex to it: the checkout's `.git` is
# mounted read-only. No gh, and no GitHub credential of any kind: the service
# is the only poster, and a review container that can post makes duplicates.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "${CODEX_CLI_PACKAGE}"

# The checkout is mounted at /workspace and owned by the host's data-dir user,
# not this one; git would refuse it as "dubious ownership".
RUN git config --system safe.directory '*'

USER node
WORKDIR /workspace
