# syntax=docker/dockerfile:1
# Serveur d'inférence : sglang sert MiniMax-H3 (ref2va + LoRA fusionnée) sur /v1/videos.
#
# Les POIDS ne sont pas dans l'image : docker-compose.yml les monte en lecture seule.
# Les versions viennent de uv.lock, copié de inference_engine : mêmes torch et sglang que
# les tests en local. Pour monter de version, recopier pyproject.toml et uv.lock.
#
# torch 2.11 est compilé pour CUDA 13 : l'hôte doit avoir un driver NVIDIA >= 580.
# Image `devel` et non `runtime` : flashinfer compile ses kernels à la volée avec nvcc.
ARG CUDA_IMAGE=nvidia/cuda:13.0.0-devel-ubuntu24.04
FROM ${CUDA_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.22 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

ENV PATH="/opt/venv/bin:/usr/local/cuda/bin:${PATH}" \
    CUDA_HOME=/usr/local/cuda \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
EXPOSE 30010
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
