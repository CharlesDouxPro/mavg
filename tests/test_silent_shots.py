"""Les plans muets : l'avatar reste à l'écran sans parler, si le channel le permet."""

from pathlib import Path

import pytest

from agents.video_planner import Draft, check_plan, describe_constraints, validated
from providers.minimax_h3 import (
    SILENT_LOCK,
    STABLE_CAMERA_LOCK,
    MinimaxShot,
    Publication,
    VideoPlan,
    build_payload,
)
from task_config import Avatar, PlanConstraints, PublicationConstraints, ReferenceImage, RenderSettings

AVATAR = Avatar(name="Pico", avatar_url="s3://b/avatars/pico.png", description="", appearance="a small hedgehog")
GUEST = ReferenceImage(name="client", image_url="s3://b/references/renard.png", description="le client")

SPOKEN = "summary: x\ndetailed_description: <Subject 1> (S1) says <d>[French] Bonjour à tous.</d>"
SILENT = "summary: x\ndetailed_description: <Subject 1> kneads the dough in silence, ovens humming."


def shot(index: int, prompt: str, line: str = "Bonjour à tous.") -> MinimaxShot:
    return MinimaxShot(index=index, role="hook", seconds=6, spoken_line=line, prompt=prompt)


def problems(shots: list[MinimaxShot], limits: PlanConstraints, references=()) -> list[str]:
    draft = Draft()
    draft.shots = {s.index: s for s in shots}
    return check_plan(draft, limits, PublicationConstraints(), None, "fr", references=list(references))


def about(errors: list[str], plan: int) -> list[str]:
    return [error for error in errors if error.startswith(f"Plan {plan}:")]


def test_by_default_the_avatar_speaks_in_every_shot():
    errors = problems([shot(1, SPOKEN), shot(2, SILENT, line="")], PlanConstraints())
    assert any("Plans sans réplique : [2]" in e and "parle dans chaque plan" in e for e in errors)


def test_an_allowed_silent_shot_needs_no_line_nor_speaker():
    errors = problems([shot(1, SPOKEN), shot(2, SILENT, line="")], PlanConstraints(max_silent_shots=1))
    assert not any("Plans sans réplique" in e for e in errors)
    silent = about(errors, 2)
    assert not any("(S1)" in e or "<d>" in e or "réplique" in e or "mots parlés" in e for e in silent)
    # Le plan muet reste un plan de l'avatar.
    assert not any("<Subject 1>" in e for e in silent)


def test_silent_shots_are_capped_and_one_shot_must_speak():
    two = problems([shot(1, SPOKEN), shot(2, SILENT, ""), shot(3, SILENT, "")], PlanConstraints(max_silent_shots=1))
    assert any("Plans sans réplique : [2, 3]" in e and "en autorise 1" in e for e in two)
    mute = problems([shot(1, SILENT, ""), shot(2, SILENT, "")], PlanConstraints(max_silent_shots=3))
    assert any("Aucun plan ne parle" in e for e in mute)


@pytest.mark.parametrize(
    "prompt",
    [
        SPOKEN,
        "summary: x\ndetailed_description: <Subject 1> (S1) murmurs to the oven.",
        "summary: x\ndetailed_description: <Subject 1> sighs <d>Merci.</d>",
        "summary: x\ndetailed_description: <Subject 1> sighs <d> [French] Merci.</d>",
    ],
)
def test_a_silent_shot_cannot_hide_speech_in_its_prompt(prompt):
    # Le plan part sans la voix : une réplique, même mal balisée, sortirait avec une voix inventée.
    errors = about(problems([shot(1, SPOKEN), shot(2, prompt, line="")], PlanConstraints(max_silent_shots=1)), 2)
    assert any("le prompt fait parler l'avatar" in e and "retire (S1)" in e for e in errors)


def test_without_silent_shots_an_empty_line_is_sent_back_to_spoken_line():
    # Sans plan muet permis, « retire la réplique » mènerait à une impasse : on la recopie.
    errors = about(problems([shot(1, SPOKEN), shot(2, SPOKEN, line="")], PlanConstraints()), 2)
    assert any("recopie la réplique dans spoken_line" in e for e in errors)
    assert not any("retire (S1)" in e for e in errors)


def test_a_silent_shot_still_shows_the_avatar_and_can_stage_a_reference():
    away = "summary: x\ndetailed_description: <Subject 2> sniffs the bread on the counter."
    errors = problems([shot(1, SPOKEN), shot(2, away, line="")], PlanConstraints(max_silent_shots=1), [GUEST])
    assert any("<Subject 1>" in e for e in about(errors, 2))
    # Le plan muet est celui qui montre le client : la référence est mise en scène.
    assert not any("n'apparaît dans aucun plan" in e for e in errors)


@pytest.fixture
def files(tmp_path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("frame.png", "renard.png", "voice.wav")}
    for path in paths.values():
        path.write_bytes(b"x")
    return paths


def payload(files, line: str) -> dict:
    return build_payload(
        shot(1, SILENT if not line else SPOKEN, line=line),
        model_name="MiniMaxAI/MiniMax-H3",
        avatar=AVATAR,
        render=RenderSettings(),
        reference=files["frame.png"],
        voice=files["voice.wav"],
        references=[files["renard.png"]],
    )


def test_a_silent_shot_is_rendered_without_the_voice(files):
    silent, spoken = payload(files, ""), payload(files, "Bonjour à tous.")
    # Seule la voix part : les images restent, dans le même ordre (<Picture 1>, <Picture 2>).
    assert [(c["type"], Path(c["uri"]).name) for c in silent["conditions"]] == [
        ("image", "frame.png"),
        ("image", "renard.png"),
    ]
    assert [c["type"] for c in spoken["conditions"]] == ["image", "image", "audio"]
    assert "<Audio 1>" not in silent["prompt"] and "<Audio 1>" in spoken["prompt"]
    assert "<Subject 2> is the subject shown in <Picture 2>." in silent["prompt"]
    # Le silence est dit en positif, juste avant le plan ; un plan parlé n'a pas ce bloc.
    assert silent["prompt"].index(SILENT_LOCK) + len(SILENT_LOCK) == silent["prompt"].index("summary:")
    assert SILENT_LOCK not in spoken["prompt"]


def test_the_camera_lock_does_not_assume_the_avatar_speaks():
    assert "speaks" not in STABLE_CAMERA_LOCK


def test_the_director_is_told_how_many_shots_may_be_silent():
    told = describe_constraints(PlanConstraints(max_silent_shots=2), PublicationConstraints())
    assert "Plans muets : au plus 2" in told
    assert "L'avatar parle dans chaque plan." in describe_constraints(PlanConstraints(), PublicationConstraints())


def test_the_render_uses_the_validated_shots_not_the_rewritten_ones():
    draft = Draft()
    draft.shots = {1: shot(1, SPOKEN), 2: shot(2, SILENT, line="")}
    draft.publication = Publication(title="Pico et le pain", description="d", hashtags=["#pico"])
    # En sortie, le modèle a réécrit la réplique du plan 1 à vide et inversé l'ordre.
    rewritten = VideoPlan(
        angle="a", language="fr", skills_used=[], total_duration_s=99,
        shots=[shot(2, SILENT, line="(silence)"), shot(1, SPOKEN, line="")],
        publication=Publication(title="autre", description="d", hashtags=[]),
    )
    plan = validated(rewritten, draft)
    assert [(s.index, s.silent) for s in plan.shots] == [(1, False), (2, True)]
    assert plan.total_duration_s == 12 and plan.publication.title == "Pico et le pain"
