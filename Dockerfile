# ── Stage 1: build the rmapi binary (reMarkable Cloud client) ──────────────
# Built from source so we don't depend on the fork's Go module path or on the
# exact name of a prebuilt release asset.
FROM golang:1.22-bookworm AS rmapi

ARG RMAPI_REPO=https://github.com/ddvk/rmapi
ARG RMAPI_REF=v0.0.34
RUN git clone --depth 1 --branch "${RMAPI_REF}" "${RMAPI_REPO}" /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -o /out/rmapi .

# ── Stage 2: Python runtime ────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RMAPI_CONFIG=/data/rmapi.conf \
    STATE_PATH=/data/state.json \
    WORK_DIR=/data/work

COPY --from=rmapi /out/rmapi /usr/local/bin/rmapi

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Token, sync state and scratch PDFs all live here — mount it as a volume.
RUN mkdir -p /data
VOLUME ["/data"]

ENTRYPOINT ["rem-readwise"]
CMD ["run"]
