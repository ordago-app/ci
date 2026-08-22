#!/usr/bin/env bash
set -euo pipefail

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 1
  fi
}

require_env RUNNER_REPOSITORY
require_env RUNNER_NAME
require_env RUNNER_LABELS
require_env RUNNER_WORKDIR

require_app_creds() {
  require_env GITHUB_RUNNER_APP_ID
  require_env GITHUB_RUNNER_APP_PRIVATE_KEY_B64
}

# App creds are only needed to MINT tokens. When the controller supplies a
# pre-minted registration token (the dynamic ci-controller path), the App
# private key never enters this lane.
if [ -z "${RUNNER_REGISTRATION_TOKEN:-}" ]; then
  require_app_creds
fi

app_id="${GITHUB_RUNNER_APP_ID:-}"
app_private_key_b64="${GITHUB_RUNNER_APP_PRIVATE_KEY_B64:-}"
unset GITHUB_RUNNER_APP_ID GITHUB_RUNNER_APP_PRIVATE_KEY_B64

github_api="https://api.github.com/repos/${RUNNER_REPOSITORY}/actions/runners"
github_repo_installation_api="https://api.github.com/repos/${RUNNER_REPOSITORY}/installation"
github_url="https://github.com/${RUNNER_REPOSITORY}"

base64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

mint_app_jwt() {
  local now header payload signing_input private_key_file signature

  now="$(date +%s)"
  header="$(printf '{"alg":"RS256","typ":"JWT"}' | base64url)"
  payload="$(
    jq -nc \
      --argjson iat "$((now - 60))" \
      --argjson exp "$((now + 540))" \
      --arg iss "${app_id}" \
      '{iat: $iat, exp: $exp, iss: $iss}' \
      | base64url
  )"
  signing_input="${header}.${payload}"
  private_key_file="$(mktemp)"
  trap 'rm -f "${private_key_file}"' RETURN
  printf '%s' "${app_private_key_b64}" | base64 -d > "${private_key_file}"
  signature="$(
    printf '%s' "${signing_input}" \
      | openssl dgst -sha256 -sign "${private_key_file}" -binary \
      | base64url
  )"
  rm -f "${private_key_file}"
  trap - RETURN

  printf '%s.%s\n' "${signing_input}" "${signature}"
}

# Resolved from RUNNER_REPOSITORY rather than configured: the same App installed
# on a personal account and on an org has a different installation id under each,
# so a configured id pins the whole runner pool to one owner.
#
# Fails by return status, never `exit`: this runs two command substitutions deep
# (mint_token, then its caller) and `set -e` does not propagate out of nested
# substitutions — an `exit` here prints its error and is then discarded, leaving
# the caller to register with an empty token. Every call site checks explicitly.
mint_installation_token() {
  local app_jwt lookup http_code installation_id

  app_jwt="$(mint_app_jwt)"
  # Deliberately not `-f`: the status code is the diagnosis. A missing
  # installation (404) names an operator action; a 401 from a bad App id or
  # revoked key, a rate limit, or a GitHub 5xx name entirely different ones, and
  # collapsing them into "not installed" sends the operator to the wrong page
  # during the one migration this guard exists to make legible.
  lookup="$(
    curl -sSL -w '\n%{http_code}' \
      -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer ${app_jwt}" \
      -H "X-GitHub-Api-Version: 2026-03-10" \
      "${github_repo_installation_api}"
  )" || {
    echo "::error::could not reach GitHub to look up the installation for ${RUNNER_REPOSITORY}" >&2
    return 1
  }
  http_code="${lookup##*$'\n'}"
  case "${http_code}" in
    200) ;;
    404)
      echo "::error::the GitHub App is not installed on the account owning ${RUNNER_REPOSITORY}" >&2
      return 1
      ;;
    *)
      echo "::error::installation lookup for ${RUNNER_REPOSITORY} failed with HTTP ${http_code}." \
           "This is NOT a missing installation — check GITHUB_RUNNER_APP_ID and the App private" \
           "key first, then GitHub status." >&2
      return 1
      ;;
  esac
  installation_id="$(printf '%s' "${lookup%$'\n'*}" | jq -r '.id')"
  curl -fsSL \
    -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${app_jwt}" \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "https://api.github.com/app/installations/${installation_id}/access_tokens" \
    | jq -r '.token'
}

mint_token() {
  local endpoint="$1"
  local installation_token

  installation_token="$(mint_installation_token)" || return 1
  curl -fsSL \
    -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${installation_token}" \
    -H "X-GitHub-Api-Version: 2026-03-10" \
    "${github_api}/${endpoint}" \
    | jq -r '.token'
}

cleanup() {
  set +e
  # Ephemeral runners auto-deregister after one job, and have no App creds to
  # mint a remove-token. Only the static pool path removes explicitly.
  #
  # Reachable only before `exec ./run.sh` replaces this shell and takes the trap
  # with it — i.e. a failure during configure, or a signal caught while setting
  # up. Once run.sh is exec'd, deregistration is its business, not ours.
  if [ "${RUNNER_EPHEMERAL:-0}" != "1" ] && [ -f .runner ]; then
    echo "Removing GitHub Actions runner ${RUNNER_NAME}..."
    if remove_token="$(mint_token remove-token)"; then
      ./config.sh remove --unattended --token "${remove_token}"
    fi
  fi
  # The ci-controller binds a shared work-dir base and each ephemeral lane creates
  # its own per-lane subdir (RUNNER_WORKDIR) inside it. The container auto-removes,
  # but that host subdir would persist and accumulate (filled the SSD over time).
  # Reap it here — runs as the runner uid that owns the files. Only for ephemeral
  # lanes; the static pool reuses its work dir across jobs.
  if [ "${RUNNER_EPHEMERAL:-0}" = "1" ] && [ -n "${RUNNER_WORKDIR:-}" ]; then
    echo "Reaping ephemeral work dir ${RUNNER_WORKDIR}..."
    rm -rf "${RUNNER_WORKDIR}"
  fi
}

trap cleanup EXIT INT TERM

mkdir -p "${RUNNER_WORKDIR}"

if [ "${SKIP_ANDROID_SDK:-0}" != "1" ] && [ ! -x "${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager" ]; then
  # Images built with WITH_ANDROID=0 carry no seed (lane hosts allow only `light`,
  # which sets SKIP_ANDROID_SDK=1 and never gets here). If an SDK-needing job ever
  # does land on one, say so plainly -- the bare `cp` failure reads as a corrupt
  # image rather than a job scheduled onto the wrong host.
  # Overridable only so the tests can exercise both branches for real; the image
  # always bakes the seed at the default path.
  android_sdk_seed="${ANDROID_SDK_SEED:-/opt/android-sdk-seed}"
  if [ ! -d "${android_sdk_seed}" ]; then
    echo "::error::this runner image was built without the Android SDK (WITH_ANDROID=0)," \
         "but the job's class needs it. Check allowed_classes and runner_image for this" \
         "host in personal/ci-controller.yml." >&2
    exit 1
  fi
  echo "Seeding persistent Android SDK cache at ${ANDROID_SDK_ROOT}..."
  mkdir -p "${ANDROID_SDK_ROOT}"
  cp -a "${android_sdk_seed}/." "${ANDROID_SDK_ROOT}/"
fi

export PATH="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:${ANDROID_SDK_ROOT}/platform-tools:${ANDROID_SDK_ROOT}/emulator:${PATH}"

# Ephemeral runners register for a single job; after that job (or a crash) the
# registration is already consumed/deleted server-side, but the local config
# files persist on the container layer. With them present, run.sh starts with a
# dead registration and fails ("registration has been deleted, please
# re-configure"), so a restart:unless-stopped container crash-loops instead of
# recycling. Clear the stale config so an ephemeral runner always re-registers
# fresh on (re)start. Long-lived (non-ephemeral) runners keep their config.
if [ "${RUNNER_EPHEMERAL:-0}" = "1" ]; then
  rm -f .runner .credentials .credentials_rsaparams
fi

if [ ! -f .runner ]; then
  echo "Configuring GitHub Actions runner ${RUNNER_NAME} for ${RUNNER_REPOSITORY}..."
  if [ -n "${RUNNER_REGISTRATION_TOKEN:-}" ]; then
    registration_token="${RUNNER_REGISTRATION_TOKEN}"
  else
    registration_token="$(mint_token registration-token)" || exit 1
  fi
  config_args=(
    --unattended
    --replace
    --url "${github_url}"
    --token "${registration_token}"
    --name "${RUNNER_NAME}"
    --labels "${RUNNER_LABELS}"
    --work "${RUNNER_WORKDIR}"
  )
  if [ "${RUNNER_EPHEMERAL:-0}" = "1" ]; then
    config_args+=(--ephemeral)
    # An ephemeral runner lives for exactly one job; a self-update triggered
    # mid-job deadlocks ("waiting for current job to finish" vs "job needs the
    # update first"). Disable auto-update — keep the image's runner version
    # current instead (ACTIONS_RUNNER_VERSION in the Dockerfile).
    config_args+=(--disableupdate)
  fi
  ./config.sh "${config_args[@]}"
fi

exec ./run.sh
