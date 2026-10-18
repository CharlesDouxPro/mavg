"""Le LLM, joint par la route Anthropic (`/v1/messages`) de son serveur SGLang.

SGLang traduit vers son API OpenAI : `thinking` active le raisonnement de Qwen,
`output_config.effort` devient `reasoning_effort`, et les outils passent par le parseur
`qwen3_coder`. Il ignore en revanche `output_config.format` : une sortie structurée
passe par un outil (`ToolStrategy`), jamais par `ProviderStrategy`.
"""

import warnings

from langchain_anthropic import ChatAnthropic

from task_config import ModelConfig

# Avec le thinking, langchain-anthropic retire le tool_choice forcé de ToolStrategy et le
# signale à chaque appel du modèle. C'est attendu : plan_video relance le modèle qui
# s'arrête avant d'avoir validé son plan.
warnings.filterwarnings("ignore", message="tool_choice is forced but thinking is enabled")

QWEN_EFFORTS = {"low": "low", "medium": "medium"}
"""Les efforts que le chat template de Qwen3.8 accepte, en plus de son défaut `xhigh`.

Tout autre valeur fait échouer la requête (HTTP 500), et SGLang traduit `xhigh` en `max`
au passage : `high`, `xhigh` et `max` partent donc sans effort, et Qwen prend `xhigh`.
"""

TEMPERATURE = 0.6
"""Celle que la fiche du modèle conseille pour l'appel d'outils. À 1.0, son défaut, Qwen
boucle dans la planification : il réécrit le même plan sans revalider jusqu'à la limite."""


def chat_model(model: ModelConfig, *, effort: str | None = None, **settings) -> ChatAnthropic:
    """Le client du modèle.

    `effort` suit l'échelle de `LLMSettings` ; il est ramené à ce que Qwen accepte. Le
    serveur ne demande pas de clé, mais le SDK Anthropic refuse de partir sans : un
    token vide devient une valeur que SGLang ignore.
    """
    if effort in QWEN_EFFORTS:
        settings["output_config"] = {"effort": QWEN_EFFORTS[effort]}
    settings.setdefault("temperature", TEMPERATURE)
    return ChatAnthropic(
        model=model.model_name,
        base_url=model.base_url,
        api_key=model.token or "sglang",
        **settings,
    )
