"""Le relais des GPU entre le LLM et MiniMax-H3 : quels appels, dans quel ordre.

Réveiller un LLM déjà éveillé fait planter son scheduler : chaque test vérifie que le
réveil n'arrive qu'après un sommeil.
"""

import httpx
import pytest

from providers import gpu_share

LLM, H3 = "http://llm:30000", "http://inference:30010"


class Servers:
    """Les deux serveurs, réduits au journal des appels reçus."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.llm_paused = False
        self.failing: set[str] = set()

    def post(self, url: str, json=None, timeout=None) -> httpx.Response:
        call = f"llm {url.removeprefix(LLM)}" if url.startswith(LLM) else f"h3 {url.removeprefix(H3)}"
        if call == "llm /generate" and self.llm_paused:
            raise httpx.ReadTimeout("le LLM retient la requête")
        self.calls.append(call)
        return httpx.Response(500 if call in self.failing else 200, request=httpx.Request("POST", url))


@pytest.fixture
def servers(monkeypatch) -> Servers:
    fake = Servers()
    monkeypatch.setenv("SHARE_GPUS", "1")
    monkeypatch.setenv("LLM_BASE_URL", LLM)
    monkeypatch.setenv("MINIMAX_BASE_URL", f"{H3}/v1")
    monkeypatch.setattr(gpu_share.httpx, "post", fake.post)
    monkeypatch.setattr(gpu_share, "_llm_awake", None)
    return fake


HAND_TO_H3 = [
    "llm /pause_generation",
    "llm /release_memory_occupation",
    "h3 /resume_memory_occupation",
]
HAND_BACK = [
    "h3 /release_memory_occupation",
    "llm /resume_memory_occupation",
    "llm /continue_generation",
]


def test_nothing_happens_without_share_gpus(servers, monkeypatch):
    monkeypatch.delenv("SHARE_GPUS")
    gpu_share.for_planning()
    with gpu_share.for_rendering():
        pass
    assert servers.calls == []


def test_first_planning_puts_h3_to_sleep_and_checks_the_llm(servers):
    gpu_share.for_planning()
    gpu_share.for_planning()
    assert servers.calls == ["h3 /release_memory_occupation", "llm /generate"]


def test_llm_left_asleep_by_a_killed_worker_is_woken(servers):
    servers.llm_paused = True
    gpu_share.for_planning()
    assert servers.calls == HAND_BACK


def test_render_hands_the_gpus_to_h3_then_back(servers):
    gpu_share.for_planning()
    servers.calls.clear()
    with gpu_share.for_rendering():
        assert servers.calls == HAND_TO_H3
    assert servers.calls == HAND_TO_H3 + HAND_BACK
    servers.calls.clear()
    gpu_share.for_planning()
    assert servers.calls == []


def test_failed_render_still_gives_the_gpus_back(servers):
    gpu_share.for_planning()
    servers.calls.clear()
    with pytest.raises(RuntimeError, match="rendu"):
        with gpu_share.for_rendering():
            raise RuntimeError("rendu en échec")
    assert servers.calls == HAND_TO_H3 + HAND_BACK


def test_llm_that_cannot_sleep_keeps_serving(servers):
    gpu_share.for_planning()
    servers.calls.clear()
    servers.failing.add("llm /release_memory_occupation")
    with pytest.raises(RuntimeError, match="release_memory_occupation"):
        with gpu_share.for_rendering():
            pytest.fail("H3 ne doit pas se réveiller tant que le LLM tient les GPU")
    assert servers.calls == [
        "llm /pause_generation",
        "llm /release_memory_occupation",
        "llm /continue_generation",
    ]
    servers.calls.clear()
    gpu_share.for_planning()
    assert servers.calls == []


def test_failed_wake_is_checked_before_the_next_try(servers):
    gpu_share.for_planning()
    servers.failing.add("llm /resume_memory_occupation")
    with pytest.raises(RuntimeError, match="resume_memory_occupation"):
        with gpu_share.for_rendering():
            pass
    # Le LLM a pu redémarrer entre-temps : on sonde, on ne réveille pas à l'aveugle.
    servers.failing.clear()
    servers.calls.clear()
    gpu_share.for_planning()
    assert servers.calls == ["h3 /release_memory_occupation", "llm /generate"]
