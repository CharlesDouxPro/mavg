#!/usr/bin/env bash
# Construit et pousse une image du side project vers son registre Scaleway.
#
#   docker/push.sh              l'agent, tagué au SHA du commit
#   docker/push.sh inference    le serveur d'inférence, tagué sglang-<version>-<SHA>
#
# Le tag dit exactement quel code tourne : on refuse donc de construire depuis des
# fichiers modifiés et non commités. Une image déjà présente dans le registre n'est pas
# reconstruite (l'inférence pèse plus de 10 Go).
#
# Connexion : `docker login rg.fr-par.scw.cloud -u nologin --password-stdin` une fois,
# ou SCW_SECRET_KEY dans l'environnement.
set -euo pipefail
cd "$(dirname "$0")/.."

REGISTRY=rg.fr-par.scw.cloud/mavg-container-registery # même valeur que dans docker-compose.yml
TARGET="${1:-agent}"

case "$TARGET" in
  agent)
    DOCKERFILE=docker/agent/Dockerfile
    CONTEXT=.
    # Ce qui entre dans l'image : tout le repo, sauf ce que .dockerignore écarte.
    WATCHED=(. ':!docker' ':!.vscode')
    ;;
  inference)
    DOCKERFILE=docker/inference/Dockerfile
    CONTEXT=docker/inference
    WATCHED=(docker/inference)
    ;;
  *)
    echo "usage : $0 [agent|inference]" >&2
    exit 2
    ;;
esac

if [[ -n "$(git status --porcelain -- "${WATCHED[@]}")" ]]; then
  echo "Fichiers modifiés ou non suivis dans ce qui entre dans l'image $TARGET :" >&2
  git status --short -- "${WATCHED[@]}" >&2
  echo "Commite-les d'abord : sinon le tag ne décrirait pas le contenu de l'image." >&2
  exit 1
fi

if [[ "$TARGET" == agent ]]; then
  TAG=$(git rev-parse --short=12 HEAD)
else
  SGLANG=$(sed -n '/^name = "sglang"$/{n;s/^version = "\(.*\)"$/\1/p}' docker/inference/uv.lock)
  TAG="sglang-${SGLANG}-$(git log -1 --format=%h --abbrev=12 -- docker/inference)"
fi
IMAGE="$REGISTRY/mavg-$TARGET:$TAG"

if [[ -n "${SCW_SECRET_KEY:-}" ]]; then
  docker login rg.fr-par.scw.cloud -u nologin --password-stdin <<< "$SCW_SECRET_KEY" > /dev/null
fi

if docker manifest inspect "$IMAGE" > /dev/null 2>&1; then
  echo "$IMAGE est déjà dans le registre : rien à faire."
  exit 0
fi

docker build -f "$DOCKERFILE" -t "$IMAGE" "$CONTEXT"
docker push "$IMAGE"
echo "Poussée : $IMAGE"
