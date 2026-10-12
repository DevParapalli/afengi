# syntax=docker/dockerfile:1

# Hardened, portable image for the agent app:
#   - multi-stage, distroless final layer (no shell, no package manager)
#   - non-root numeric UID; tolerates an arbitrary assigned UID (OpenShift)
#   - config is 100% environment variables (see .env.example); nothing baked in
# Companions: Dockerfile.dev (local, has a shell + uv) and compose.yaml.
#
# Base images are ARGs so they can point at a mirror / pull-through registry
# (e.g. an internally proxied ghcr.io / gcr.io). Pin them by @sha256 digest in
# CI (tags move under you); a scanner / Renovate bumps them.

ARG PYTHON_VERSION=3.14
ARG UV_IMAGE=ghcr.io/astral-sh/uv:bookworm-slim
ARG RUNTIME_IMAGE=gcr.io/distroless/base-debian12:nonroot

# --- build: a standalone interpreter + venv the distroless stage can run ---
FROM ${UV_IMAGE} AS build
ARG PYTHON_VERSION
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed
WORKDIR /app

# Portable interpreter (distroless ships none); copied to the final stage. On a
# locked-down network set UV_PYTHON_INSTALL_MIRROR to your python-build-standalone
# mirror.
RUN uv python install ${PYTHON_VERSION}

# Dependencies before app code so source edits don't re-resolve (layer cache).
# Generated projects ship no uv.lock -> resolve fresh; add a uv.lock and switch to
# `uv sync --frozen` for byte-reproducible builds.
# NOTE: LiteLLM ships compiled wheels for Linux glibc only. This glibc (bookworm)
# base uses that wheel -- no build tools needed. A musl/Alpine or unsupported-arch
# base would build it from source and require a Rust toolchain (cargo) in this stage.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --no-install-project

# App source, then finalize the environment.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# Grant group + other the owner's read/traverse (no write) so ANY assigned UID --
# and any UID in the root group -- can run this. Numeric-UID / arbitrary-UID rule.
RUN chmod -R go=rX /python /app

# --- runtime: distroless base (glibc + libssl + ca-certs + tzdata) ---
FROM ${RUNTIME_IMAGE} AS runtime
ARG APP_VERSION=0.0.0
LABEL org.opencontainers.image.title="agent-app" \
      org.opencontainers.image.description="Agent framework HTTP service (A2A / decide)." \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ADK_ENABLE_MCP_MODERN_PROTOCOL=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app
# Files land owned root:root (gid 0); with the go=rX perms set above, a non-root
# UID reads them via "other" and an arbitrary UID-in-group-0 via "group".
COPY --from=build /python /python
COPY --from=build /app /app

# Numeric UID: runAsNonRoot cannot resolve a name. 65532 = distroless "nonroot".
USER 65532
EXPOSE 8080

# K8s / OpenShift ignore HEALTHCHECK (they use probes -- wire the same /ready there).
# /ready is the DEEP check: it verifies the DB when one is configured.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/ready').status==200 else 1)"]

# Exec form: python is PID 1 and uvicorn installs its own SIGTERM handler.
CMD ["python", "main.py"]
