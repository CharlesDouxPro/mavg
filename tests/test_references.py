"""Les images de référence : ce qui part au moteur, le verrou d'identité, et ce que le
plan doit en faire."""

from pathlib import Path

import pytest

from agents.video_planner import Draft, check_plan
from providers.minimax_h3 import MinimaxShot, build_payload, identity_lock
from task_config import (
    AgentConfig,
    Avatar,
    Brief,
    PlanConstraints,
    PublicationConstraints,
    ReferenceImage,
    RenderSettings,
)

AVATAR = Avatar(name="Nova", avatar_url="s3://b/avatars/nova.mp4", description="", appearance="woman, navy blazer")
HEDGEHOG = ReferenceImage(name="heros", image_url="s3://b/references/herisson.png", description="le héros")


def shot(prompt: str) -> MinimaxShot:
    return MinimaxShot(index=1, role="hook", seconds=8, spoken_line="Salut.", prompt=prompt)


@pytest.fixture
def files(tmp_path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("frame.png", "herisson.png", "four.webp", "voice.wav")}
    for path in paths.values():
        path.write_bytes(b"x")
    return paths


def test_references_follow_the_avatar_image_and_precede_the_voice(files):
    payload = build_payload(
        shot("summary: x"),
        model_name="MiniMaxAI/MiniMax-H3",
        avatar=AVATAR,
        render=RenderSettings(),
        reference=files["frame.png"],
        voice=files["voice.wav"],
        references=[files["herisson.png"], files["four.webp"]],
    )
    # Le moteur numérote ses images dans l'ordre d'arrivée : <Picture 1> est l'avatar.
    assert [(c["type"], Path(c["uri"]).name) for c in payload["conditions"]] == [
        ("image", "frame.png"),
        ("image", "herisson.png"),
        ("image", "four.webp"),
        ("audio", "voice.wav"),
    ]
    prompt = payload["prompt"]
    assert "<Subject 1> is the on-camera subject from <Picture 1>." in prompt
    assert "<Subject 2> is the subject shown in <Picture 2>." in prompt
    assert "<Subject 3> (appears in every shot that features it): fully_preserved" in prompt
    assert prompt.index("<Subject 3>") < prompt.index("<Audio 1>") < prompt.index("summary: x")


def test_lock_without_references_is_unchanged():
    lock = identity_lock(AVATAR)
    assert "<Subject 1> is the on-camera subject from the reference image. Fixed" in lock
    assert "<Picture" not in lock and "<Subject 2>" not in lock


def test_references_lock_subject_1_even_without_an_appearance():
    bare = AVATAR.model_copy(update={"appearance": ""})
    assert identity_lock(bare) == ""
    assert identity_lock(bare, references=1).startswith(
        "subject_definitions:\n<Subject 1> is the on-camera subject from <Picture 1>.\n<Subject 2>"
    )


def problems(prompts: list[str], references: list[ReferenceImage]) -> list[str]:
    draft = Draft()
    draft.shots = {i: shot(p).model_copy(update={"index": i}) for i, p in enumerate(prompts, start=1)}
    return check_plan(draft, PlanConstraints(), PublicationConstraints(), None, references=references)


def test_a_subject_without_an_image_is_refused():
    found = problems(["<Subject 1> (S1) talks to <Subject 2> and <Subject 3>."], [HEDGEHOG])
    assert any("<Subject 3> n'a pas d'image" in e for e in found)
    assert not any("<Subject 2> n'a pas" in e for e in found)


def test_every_reference_must_be_staged():
    unused = problems(["<Subject 1> (S1) talks."], [HEDGEHOG])
    assert any("<Subject 2> (heros) n'apparaît dans aucun plan" in e for e in unused)
    staged = problems(["<Subject 1> (S1) talks.", "<Subject 2> kneads dough."], [HEDGEHOG])
    assert not any("n'apparaît dans aucun plan" in e for e in staged)


def test_agent_is_told_who_is_who_only_when_there_are_references():
    base = {"models": {}, "brief": Brief(prompt="Raconte."), "avatar": AVATAR}
    config = AgentConfig.model_construct(**base, params={}, skill="", language="", references=[HEDGEHOG])
    message = config.user_message()
    assert "RÉFÉRENCES VISUELLES" in message
    assert "- <Subject 1> (<Picture 1>) : l'avatar (Nova)." in message
    assert "- <Subject 2> (<Picture 2>) : heros — le héros." in message
    without = AgentConfig.model_construct(**base, params={}, skill="", language="", references=[])
    assert "RÉFÉRENCES" not in without.user_message()


@pytest.mark.parametrize("name", ["herisson.jpg", "herisson.png", "herisson.webp"])
def test_an_image_avatar_is_sent_as_is(tmp_path, monkeypatch, name):
    # Certains JPEG se lisent comme une « vidéo » d'une frame de 0,04 s : en tirer la
    # frame du milieu tombait après elle, et le rendu plantait.
    from agents import video_planner

    image = tmp_path / name
    image.write_bytes(b"x")
    monkeypatch.setattr(video_planner.storage, "download", lambda config, uri: image)
    monkeypatch.setattr(video_planner, "extract_frame", lambda *a, **k: pytest.fail("pas de frame à extraire"))
    task = AgentConfig.model_construct(avatar=AVATAR.model_copy(update={"avatar_url": f"s3://b/references/{name}"}), storage=None)
    assert video_planner.prepare_avatar(type("Task", (), {"agent_config": task})()) == image


def test_a_video_avatar_still_gives_a_frame(tmp_path, monkeypatch):
    from agents import video_planner

    video = tmp_path / "nova.mp4"
    video.write_bytes(b"x")
    extracted = []
    monkeypatch.setattr(video_planner.storage, "download", lambda config, uri: video)
    monkeypatch.setattr(video_planner, "extract_frame", lambda src, out, at_s=None: extracted.append(out) or Path(out).write_bytes(b"f"))
    task = AgentConfig.model_construct(avatar=AVATAR, storage=None)
    frame = video_planner.prepare_avatar(type("Task", (), {"agent_config": task})())
    assert extracted == [frame] and frame.name == "nova_reference_mid.png"
