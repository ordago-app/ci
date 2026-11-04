# What ClaudeReviewProvider runs a review in. The version is pinned because this
# CLI's JSON output is what the provider parses.
FROM node:22-slim

ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN npm install -g @anthropic-ai/claude-code@2.1.307

USER node
WORKDIR /workspace
