"""Le client du LLM : ce qu'il envoie à SGLang, que le chat template de Qwen accepte."""

import pytest

from providers.llm import chat_model
from task_config import ModelConfig

MODEL = ModelConfig(provider="sglang-llm", model_name="qwen", base_url="http://llm:30000", token="")


@pytest.mark.parametrize(
    ("effort", "sent"),
    [("low", {"effort": "low"}), ("medium", {"effort": "medium"}), ("high", None), ("xhigh", None), ("max", None)],
)
def test_effort_is_one_qwen_accepts(effort, sent):
    # high, le défaut des tâches, faisait répondre 500 : « Unexpected reasoning effort high ».
    assert chat_model(MODEL, effort=effort, max_tokens=10).output_config == sent


def test_empty_token_still_builds_a_client():
    assert chat_model(MODEL, max_tokens=10).anthropic_api_key.get_secret_value() == "sglang"
