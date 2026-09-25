# syntax=docker/dockerfile:1
# L'agent : prend une tâche dans Mongo, planifie, fait rendre les clips par MiniMax-H3,
# monte, sous-titre et publie. Pas de GPU : faster-whisper tourne sur CPU (`device: auto`).
FROM ghcr.io/astral-sh/uv:0.11.22-python3.13-trixie-slim

# ffmpeg de Debian est compilé avec libass, dont l'incrustation des sous-titres a besoin.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Les dépendances d'abord : cette couche reste en cache tant que uv.lock ne change pas.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
CMD ["python", "-m", "main"]
