#!/usr/bin/env bash
# Lance sglang sur le LLM de l'agent (un Qwen3.8-27B), sur les GPU de MiniMax-H3. Le modèle
# vient du profil de déploiement : LLM_MODEL_PATH, LLM_SERVED_NAME, LLM_QUANTIZATION.
#
# Les deux serveurs ne tiennent pas ensemble en mémoire : le worker les réveille à tour de
# rôle (providers/gpu_share.py). D'où, en plus des réglages du modèle :
#   --tp-size 2                   le modèle réparti sur les deux cartes ;
#   --enable-memory-saver         active /release_memory_occupation et /resume_… ;
#   --enable-weights-cpu-backup   copie les poids en RAM pendant le sommeil. Sans lui, le
#                                 réveil rend une mémoire vide et le modèle dit n'importe quoi ;
#   H3 endormi avant le démarrage : --mem-fraction-static se calcule sur la mémoire libre à
#                                 cet instant, elle doit être celle des cartes entières.
set -euo pipefail

: "${LLM_MODEL_PATH:?LLM_MODEL_PATH manquant : lance compose avec --env-file docker/<profil>.env}"

# L'image le règle pour H3 (expandable_segments:True), mais torch_memory_saver refuse de
# démarrer avec : sans cette ligne, --enable-memory-saver fait échouer le lancement.
unset PYTORCH_CUDA_ALLOC_CONF

# Un chemin est vérifié ici ; un repo (`Qwen/…`) est résolu dans le cache HF, hors ligne.
if [[ "$LLM_MODEL_PATH" == /* && ! -f "$LLM_MODEL_PATH/config.json" ]]; then
  echo "Poids introuvables : $LLM_MODEL_PATH/config.json n'existe pas dans le conteneur." >&2
  echo "Vérifie LLM_WEIGHTS_DIR et LLM_MODEL_PATH dans le profil de déploiement." >&2
  exit 1
fi

# Le service démarre une fois H3 chargé (depends_on) : on l'endort pour prendre sa place.
echo "==> MiniMax-H3 endormi : le LLM dimensionne son cache sur les cartes entières"
curl -fsS -X POST "${H3_URL:?H3_URL manquant}/release_memory_occupation"
echo

# Un checkpoint BF16 (un fine-tune publié sans FP8) peut être quantifié au chargement :
# LLM_QUANTIZATION=fp8 lui donne la mémoire et la vitesse d'un FP8 publié.
EXTRA=()
if [[ -n "${LLM_QUANTIZATION:-}" ]]; then
  EXTRA+=(--quantization "$LLM_QUANTIZATION")
fi

echo "==> ${LLM_SERVED_NAME:?} depuis $LLM_MODEL_PATH ${EXTRA[*]}"
exec sglang serve \
  "${EXTRA[@]}" \
  --trust-remote-code \
  --model-path "$LLM_MODEL_PATH" \
  --served-model-name "$LLM_SERVED_NAME" \
  --tp-size "${LLM_TP_SIZE:-2}" \
  --kv-cache-dtype fp8_e4m3 \
  --mem-fraction-static 0.85 \
  --attention-backend flashinfer \
  --chunked-prefill-size 2048 \
  --reasoning-parser qwen3 \
  --tool-call-parser qwen3_coder \
  --mamba-full-memory-ratio 4.59 \
  --speculative-algorithm EAGLE \
  --speculative-num-steps 3 \
  --speculative-eagle-topk 1 \
  --speculative-num-draft-tokens 4 \
  --enable-linear-replayssm-spec \
  --mamba-radix-cache-strategy extra_buffer \
  --mamba-ssm-dtype float32 \
  --enable-memory-saver \
  --enable-weights-cpu-backup \
  --host 0.0.0.0 \
  --port 30000
