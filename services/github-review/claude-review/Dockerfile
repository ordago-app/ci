# What ClaudeReviewProvider runs every Claude account's review in. The version is
# pinned because this CLI's stream-json output is what the provider parses, and
# because the flags that lock it down (--setting-sources, --strict-mcp-config,
# --tools, --permission-mode dontAsk) must mean what they meant when tested.
FROM node:22-slim

ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN npm install -g @anthropic-ai/claude-code@2.1.307

USER node
WORKDIR /workspace
