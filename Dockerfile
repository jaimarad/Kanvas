# Kanvas with agents: the board plus everything its agents need (git, gh, Claude Code).
# Used for local development (docker-compose.yml) and for the VPS.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git gnupg nodejs chromium tini \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# Agents run as an ordinary user: Claude Code refuses to skip permission prompts as root,
# and a container user cannot touch the host.
RUN useradd --create-home --shell /bin/bash kanvas
USER kanvas
WORKDIR /home/kanvas
RUN curl -fsSL https://claude.ai/install.sh | bash

# Logins live in volumes so they survive rebuilds: Claude Code's in CLAUDE_CONFIG_DIR, gh's in GH_CONFIG_DIR,
# and git's settings (identity, `gh auth setup-git`) in GIT_CONFIG_GLOBAL.
ENV PATH="/home/kanvas/.local/bin:${PATH}" \
    CLAUDE_CONFIG_DIR=/home/kanvas/auth/claude \
    GH_CONFIG_DIR=/home/kanvas/auth/gh \
    GIT_CONFIG_GLOBAL=/home/kanvas/auth/gitconfig \
    KANVAS_STATE_DIR=/home/kanvas/auth/kanvas \
    KANVAS_TEST_CHROME=chromium

# The source is copied for the VPS image; docker-compose.yml mounts the working copy over it instead.
COPY --chown=kanvas:kanvas . /opt/kanvas
# Kanvas runs from its own virtualenv: in this image Python itself lives in /usr/local, and Kanvas
# would take a `kanvas` command there for a temporary uvx one.
USER root
RUN python3 -m venv /opt/venv \
 && printf '#!/bin/sh\nexec /opt/venv/bin/python /opt/kanvas/kanvas.py "$@"\n' > /usr/local/bin/kanvas && chmod +x /usr/local/bin/kanvas \
 && mkdir -p /workspace /home/kanvas/auth/claude /home/kanvas/auth/gh /home/kanvas/auth/kanvas \
 && chown -R kanvas:kanvas /workspace /home/kanvas/auth
ENV PATH="/opt/venv/bin:${PATH}"
USER kanvas

WORKDIR /workspace
EXPOSE 8765
ENTRYPOINT ["tini", "--"]
CMD ["kanvas", "serve", "--saved", "--host", "0.0.0.0", "--no-open"]
