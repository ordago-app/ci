FROM ubuntu:24.04

ARG ACTIONS_RUNNER_VERSION=2.335.1
ARG ANDROID_CMDLINE_TOOLS_VERSION=13114758
ARG MAESTRO_VERSION=1.39.0

ENV DEBIAN_FRONTEND=noninteractive
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV HOME=/home/runner
ENV PATH=/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/maestro/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV MAESTRO_CLI_NO_ANALYTICS=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      gnupg \
      jq \
      openssl \
      libicu74 \
      libkrb5-3 \
      liblttng-ust1 \
      libssl3t64 \
      libstdc++6 \
      libunwind8 \
      unzip \
      xz-utils \
      zip \
      openjdk-17-jdk-headless \
      qemu-kvm \
      libpulse0 \
      libglu1-mesa \
      libnss3 \
      libxcomposite1 \
      libxcursor1 \
      libxi6 \
      libxtst6 \
      libxrandr2 \
      libasound2t64 \
      libatk1.0-0t64 \
      libatk-bridge2.0-0t64 \
      libcups2t64 \
      libdrm2 \
      libgbm1 \
      libgtk-3-0t64 \
      libxdamage1 \
      libxkbcommon0 \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && corepack enable \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/actions-runner \
 && curl -fsSL "https://github.com/actions/runner/releases/download/v${ACTIONS_RUNNER_VERSION}/actions-runner-linux-x64-${ACTIONS_RUNNER_VERSION}.tar.gz" \
      -o /tmp/actions-runner.tar.gz \
 && tar -xzf /tmp/actions-runner.tar.gz -C /opt/actions-runner \
 && rm /tmp/actions-runner.tar.gz

RUN mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools" \
 && curl -fsSL "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS_VERSION}_latest.zip" \
      -o /tmp/android-commandline-tools.zip \
 && unzip -q /tmp/android-commandline-tools.zip -d /tmp/android-commandline-tools \
 && mv /tmp/android-commandline-tools/cmdline-tools "${ANDROID_SDK_ROOT}/cmdline-tools/latest" \
 && rm -rf /tmp/android-commandline-tools /tmp/android-commandline-tools.zip \
 && yes | sdkmanager --licenses >/dev/null \
 && sdkmanager \
      "platform-tools" \
      "emulator" \
      "platforms;android-33" \
      "platforms;android-35" \
      "platforms;android-36" \
      "build-tools;35.0.0" \
      "build-tools;36.0.0" \
      "system-images;android-33;google_apis;x86_64" \
 && mv "${ANDROID_SDK_ROOT}" /opt/android-sdk-seed \
 && mkdir -p "${ANDROID_SDK_ROOT}"

RUN curl -fsSL "https://github.com/mobile-dev-inc/maestro/releases/download/cli-${MAESTRO_VERSION}/maestro.zip" \
      -o /tmp/maestro.zip \
 && unzip -q /tmp/maestro.zip -d /opt \
 && rm /tmp/maestro.zip \
 && ln -sf /opt/maestro/bin/maestro /usr/local/bin/maestro

RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && rm -rf /var/lib/apt/lists/*

RUN install -d -o 1000 -g 1000 /home/runner /runner-work /cache /cache/gradle /cache/android-sdk /cache/android-avd /cache/pnpm \
 && chown 1000:1000 /opt/actions-runner \
 && chmod +x /opt/actions-runner/config.sh /opt/actions-runner/run.sh

# Point pnpm's content-addressed store at the mounted /cache/pnpm volume.
# PNPM_HOME only sets the global bin dir, not the store, so without this pnpm
# kept its store on the container layer and the mounted (persistent) volume sat
# empty — every job re-fetched dependencies. A user-level .npmrc is honored by
# pnpm regardless of the Node version quirks in the base image.
RUN printf 'store-dir=/cache/pnpm\n' > /home/runner/.npmrc \
 && chown 1000:1000 /home/runner/.npmrc

COPY entrypoint.sh /usr/local/bin/github-runner-entrypoint
RUN chmod +x /usr/local/bin/github-runner-entrypoint

USER 1000:1000
WORKDIR /opt/actions-runner

ENTRYPOINT ["/usr/local/bin/github-runner-entrypoint"]
