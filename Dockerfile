# Optional image overlay. Supply an explicit digest-pinned base image.
# A resulting image needs its own digest and fresh recipe qualification.
ARG VLLM_BASE
FROM ${VLLM_BASE}
ARG VLLM_BASE
RUN case "$VLLM_BASE" in *@sha256:*) ;; *) echo "base image must be digest-pinned" >&2; exit 1 ;; esac
LABEL org.opencontainers.image.title="pulsar-inference-stack" \
      org.opencontainers.image.description="Exact-spec vLLM serving for NVIDIA DGX Spark" \
      org.opencontainers.image.source="https://github.com/luisefigueroa/pulsar-inference-stack"
RUN [ "$(uname -m)" = "aarch64" ] || { echo "image requires aarch64" >&2; exit 1; }
RUN command -v curl >/dev/null || (apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*)
