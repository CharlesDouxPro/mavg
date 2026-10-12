"""Les deux GPU, prêtés à tour de rôle au LLM et à MiniMax-H3.

Sur la machine GPU, les deux serveurs SGLang ne tiennent pas ensemble en mémoire : le
LLM s'est réservé 85 % de chaque carte (poids et cache KV), H3 en prend plus de la
moitié avant même ses activations. Un seul est donc éveillé à la fois ; l'autre a rendu
sa mémoire et ses poids attendent en RAM.

- Planification (et e-mail d'échec) : le LLM est éveillé, H3 dort.
- Rendu : H3 est éveillé, le LLM dort.

Le conteneur du LLM endort H3 avant de démarrer (docker/inference/llm-entrypoint.sh) :
c'est ce qui lui laisse les cartes entières pour dimensionner son cache.

Actif seulement avec SHARE_GPUS=1 (docker-compose.yml). Ailleurs, chaque serveur a ses
propres GPU et ce module ne fait rien.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

SWITCH_TIMEOUT_S = 600.0
"""Une bascule copie les poids entre RAM et GPU : quelques secondes d'habitude."""
PROBE_TIMEOUT_S = 30.0

_llm_awake: bool | None = None
"""Ce que ce worker sait du LLM. None : rien encore, il vient de démarrer."""


def _enabled() -> bool:
    return os.getenv("SHARE_GPUS") == "1"


def _root(variable: str) -> str:
    """L'adresse d'un serveur, sans le `/v1` de son API (`MINIMAX_BASE_URL` le porte)."""
    url = os.getenv(variable, "")
    if not url:
        raise RuntimeError(f"SHARE_GPUS=1 mais {variable} est vide : impossible de basculer les GPU.")
    return url.rstrip("/").removesuffix("/v1")


def _post(variable: str, path: str, body: dict | None = None) -> None:
    url = f"{_root(variable)}{path}"
    resp = httpx.post(url, json=body or {}, timeout=SWITCH_TIMEOUT_S)
    if resp.is_error:
        raise RuntimeError(f"{url} a échoué (HTTP {resp.status_code}) : {resp.text[:500]}")


def _llm_paused() -> bool:
    """Vrai si le LLM dort encore : un worker précédent a été tué en plein rendu.

    SGLang n'expose pas cet état. Mais endormi, il retient toute génération jusqu'à
    son réveil : une requête d'un token qui ne revient pas dit qu'il dort. Il faut le
    savoir, car réveiller un LLM déjà éveillé fait planter son scheduler.
    """
    try:
        httpx.post(
            f"{_root('LLM_BASE_URL')}/generate",
            json={"input_ids": [0], "sampling_params": {"max_new_tokens": 1}},
            timeout=PROBE_TIMEOUT_S,
        ).raise_for_status()
    except httpx.TimeoutException:
        return True
    return False


def _sleep_llm() -> None:
    """Rend toute la mémoire du LLM : cache KV, CUDA graphs, et poids (copiés en RAM).

    `pause_generation` d'abord : une requête qui arriverait pendant le rendu (l'assistant
    de l'interface) attend le réveil au lieu de lire une mémoire rendue, et celle qui
    tournerait encore est interrompue, sans quoi SGLang refuse de libérer.
    """
    global _llm_awake
    _post("LLM_BASE_URL", "/pause_generation", {"mode": "abort"})
    try:
        _post("LLM_BASE_URL", "/release_memory_occupation")
    except Exception:
        _post("LLM_BASE_URL", "/continue_generation")
        raise
    _llm_awake = False


def for_planning() -> None:
    """H3 endormi, LLM éveillé. Sans effet si ce worker les a déjà mis dans cet état."""
    global _llm_awake
    if not _enabled() or _llm_awake:
        return
    # H3 d'abord : le LLM reprend la mémoire qu'il lui laisse. Endormir H3 deux fois
    # ne fait rien, contrairement au LLM.
    _post("MINIMAX_BASE_URL", "/release_memory_occupation")
    asleep = _llm_paused() if _llm_awake is None else True
    # Un réveil qui échoue (le LLM a redémarré entre-temps…) laisse l'état inconnu : le
    # prochain appel sondera au lieu de réveiller à l'aveugle.
    _llm_awake = None
    if asleep:
        print("[gpu] réveil du LLM")
        _post("LLM_BASE_URL", "/resume_memory_occupation")
        _post("LLM_BASE_URL", "/continue_generation")
    _llm_awake = True


@contextmanager
def for_rendering() -> Iterator[None]:
    """Le temps du bloc, les GPU passent à H3. Ils reviennent au LLM à la sortie, même en erreur."""
    if not _enabled():
        yield
        return
    for_planning()  # part d'un état connu, même au premier rendu du worker
    print("[gpu] LLM endormi, réveil de MiniMax-H3")
    _sleep_llm()
    try:
        _post("MINIMAX_BASE_URL", "/resume_memory_occupation")
        yield
    finally:
        print("[gpu] MiniMax-H3 endormi, réveil du LLM")
        for_planning()
