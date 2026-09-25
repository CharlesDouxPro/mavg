#!/usr/bin/env bash
# Lance sglang sur MiniMax-H3 ref2va + LoRA fusionnée, depuis les poids montés.
# Mêmes réglages que inference_engine/run_combo.sh ; les chemins viennent du profil
# de déploiement (docker/onprem.env ou docker/scaleway.env).
set -euo pipefail

: "${H3_MODEL_PATH:?H3_MODEL_PATH manquant : lance compose avec --env-file docker/<profil>.env}"
LORA="/loras/${H3_LORA_FILE:?H3_LORA_FILE manquant}"

# Échouer ici avec un message clair, plutôt qu'au milieu du chargement de 135 Go.
if [[ ! -d "$H3_MODEL_PATH/Ref2VA" ]]; then
  echo "Poids introuvables : $H3_MODEL_PATH/Ref2VA n'existe pas dans le conteneur." >&2
  echo "Vérifie WEIGHTS_DIR et H3_MODEL_PATH dans le profil de déploiement." >&2
  exit 1
fi
if [[ ! -f "$LORA" ]]; then
  echo "LoRA introuvable : $LORA. Vérifie LORA_DIR et H3_LORA_FILE." >&2
  exit 1
fi

echo "==> MiniMax-H3 ref2va depuis $H3_MODEL_PATH, LoRA $LORA"
exec sglang serve \
  --model-path "$H3_MODEL_PATH" \
  --model-variant ref2va \
  --num-gpus "${H3_NUM_GPUS:-2}" \
  --tp-size "${H3_TP_SIZE:-2}" \
  --performance-mode speed \
  --lora-path "$LORA" \
  --lora-nickname combo \
  --lora-scale 1.0 \
  --lora-merge-mode merge \
  --host 0.0.0.0 \
  --port 30010
