"""Les personnages inventés : leur numéro, leur fiche dans le verrou, le casting qui les
fige, et ce que le plan doit en faire."""

from pathlib import Path

import pytest

from agents.video_planner import Draft, check_plan
from providers.minimax_h3 import Character, MinimaxShot, build_payload, casting_payload, identity_lock
from task_config import (
    AgentConfig,
    Avatar,
    Brief,
    PlanConstraints,
    PublicationConstraints,
    ReferenceImage,
    RenderSettings,
    cast_number,
)

AVATAR = Avatar(name="Nova", avatar_url="s3://b/avatars/nova.mp4", description="", appearance="woman, navy blazer")
HEDGEHOG = ReferenceImage(name="heros", image_url="s3://b/references/herisson.png", description="le héros")
GIRL = Character(
    name="la fillette",
    appearance="A seven-year-old girl with a round face, freckles and a messy brown bob, in faded-mint star pyjamas.",
    voice="a high, sleepy seven-year-old girl's voice",
)
ARM = Character(name="le bras robot", appearance="A slim white robotic arm with three joints and a soft rubber fist.")
CASTING = PlanConstraints(max_characters=2, min_appearance_words=10)


def shot(prompt: str, index: int = 1) -> MinimaxShot:
    return MinimaxShot(index=index, role="hook", seconds=8, spoken_line="Salut.", prompt=prompt)


@pytest.fixture
def files(tmp_path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("frame.png", "herisson.png", "girl.png", "arm.png", "room.wav")}
    for path in paths.values():
        path.write_bytes(b"x")
    return paths


def test_without_an_avatar_the_first_character_is_subject_1():
    assert [cast_number(i, has_avatar=False, references=0) for i in range(2)] == [1, 2]
    lock = identity_lock(None, cast=[GIRL, ARM])
    assert lock.startswith(
        "subject_definitions:\n<Subject 1> is the on-camera subject from <Picture 1>. "
        f"Fixed, invariant appearance (matches the reference exactly): {GIRL.appearance} "
        f"<Subject 1> (S1) speaks with one fixed voice in every shot: {GIRL.voice}\n"
    )
    assert f"<Subject 2> is the subject shown in <Picture 2>. Fixed, invariant appearance (matches <Picture 2> exactly): {ARM.appearance}\n" in lock
    assert "<Subject 2> (appears in every shot that features it): fully_preserved" in lock


def test_supplied_references_keep_their_number():
    # L'agent a lu « <Subject 2> : heros » avant d'inventer qui que ce soit.
    assert [cast_number(i, has_avatar=False, references=1) for i in range(2)] == [1, 3]
    assert [cast_number(i, has_avatar=True, references=1) for i in range(2)] == [3, 4]
    lock = identity_lock(AVATAR, references=1, cast=[GIRL])
    assert "appearance (matches the reference exactly): woman, navy blazer" in lock
    assert "<Subject 2> is the subject shown in <Picture 2>. Fixed, invariant appearance: it matches" in lock
    assert f"<Subject 3> is the subject shown in <Picture 3>." in lock and GIRL.appearance in lock
    assert f"Whenever it speaks, it uses one fixed voice: {GIRL.voice}" in lock


def test_cast_frames_take_their_picture_number(files):
    payload = build_payload(
        shot("summary: x"),
        model_name="MiniMaxAI/MiniMax-H3",
        avatar=None,
        render=RenderSettings(),
        reference=None,
        references=[files["herisson.png"]],
        cast=[GIRL, ARM],
        cast_frames=[files["girl.png"], files["arm.png"]],
    )
    assert [Path(c["uri"]).name for c in payload["conditions"]] == ["girl.png", "herisson.png", "arm.png"]
    assert GIRL.appearance in payload["prompt"] and ARM.appearance in payload["prompt"]
    assert "<Subject 3> is the subject shown in <Picture 3>" in payload["prompt"]


def test_shots_wait_for_the_casting(files):
    with pytest.raises(ValueError, match="casting doit être rendu"):
        build_payload(
            shot("summary: x"),
            model_name="m",
            avatar=None,
            render=RenderSettings(),
            reference=None,
            cast=[GIRL],
            cast_frames=[],
        )


def test_the_casting_shot_shows_the_fiche_and_nobody_speaks(files):
    payload = casting_payload(GIRL, model_name="m", render=RenderSettings(), ambience=files["room.wav"])
    assert payload["task"] == "ref2va" and payload["seconds"] == 5
    # Ni image ni voix : le fond sonore est la seule référence, et le moteur en exige une.
    assert [(c["type"], Path(c["uri"]).name) for c in payload["conditions"]] == [("audio", "room.wav")]
    prompt = payload["prompt"]
    assert GIRL.appearance in prompt and "nobody speaks" in prompt
    assert "(S1)" not in prompt and "<d>" not in prompt and GIRL.voice not in prompt


def problems(prompts: list[str], cast: list[Character], limits=CASTING, has_avatar=False, references=()) -> list[str]:
    draft = Draft()
    draft.cast = cast
    draft.shots = {i: shot(p, i) for i, p in enumerate(prompts, start=1)}
    return check_plan(
        draft, limits, PublicationConstraints(), None, references=list(references), has_avatar=has_avatar
    )


def test_a_channel_without_avatar_must_cast_before_shooting():
    found = problems(["<Subject 1> (S1) yawns."], [])
    assert any("Aucun personnage" in e and "cast_characters" in e for e in found)
    assert any("<Subject 1> n'a pas d'image" in e for e in found)
    assert not any("Aucun personnage" in e for e in problems(["<Subject 1> (S1) yawns."], [GIRL]))


def test_every_character_must_be_staged_and_defined():
    unused = problems(["<Subject 1> (S1) yawns."], [GIRL, ARM])
    assert any("<Subject 2> (le bras robot) n'apparaît dans aucun plan" in e for e in unused)
    staged = problems(["<Subject 1> (S1) yawns.", "<Subject 1> sleeps; <Subject 2> folds."], [GIRL, ARM])
    assert not any("n'apparaît dans aucun plan" in e for e in staged)
    undefined = problems(["<Subject 1> (S1) yawns next to <Subject 3>."], [GIRL, ARM])
    assert any("<Subject 3> n'a pas d'image" in e and "cast_characters" in e for e in undefined)


def test_the_cast_is_capped_and_each_fiche_must_say_enough():
    crowded = problems(["<Subject 1> (S1) yawns."], [GIRL, ARM, ARM], limits=PlanConstraints(max_characters=2))
    assert any("3 personnages inventés, le channel en autorise 2" in e for e in crowded)
    vague = problems(["<Subject 1> (S1) yawns."], [GIRL.model_copy(update={"appearance": "a girl"})])
    assert any("<Subject 1> (la fillette) : fiche trop courte (2 mots" in e for e in vague)


def test_a_task_without_avatar_must_let_the_agent_cast():
    base = {"models": {"master_mind": {}, "slm": {}, "video_generator": {}, "image_generator": {}}, "brief": {"prompt": "x"}}
    model = {"provider": "p", "model_name": "m", "base_url": "u", "token": ""}
    base["models"] = {role: model for role in base["models"]}
    with pytest.raises(ValueError, match="max_characters"):
        AgentConfig.model_validate(base)
    config = AgentConfig.model_validate({**base, "plan": {"max_characters": 1}})
    message = config.user_message()
    assert "CASTING" in message and "Invente-les (au plus 1)" in message


def test_the_agent_is_told_about_casting_only_when_it_may_cast():
    base = {"models": {}, "brief": Brief(prompt="Raconte."), "params": {}, "skill": "", "language": ""}
    with_avatar = AgentConfig.model_construct(**base, avatar=AVATAR, references=[HEDGEHOG], plan=PlanConstraints())
    assert "CASTING" not in with_avatar.user_message()
    alone = AgentConfig.model_construct(**base, avatar=None, references=[HEDGEHOG], plan=CASTING)
    message = alone.user_message()
    assert "- <Subject 1> (<Picture 1>) : le personnage principal, le premier que tu inventes." in message
    assert "Cette chaîne n'a pas d'avatar" in message


def test_casting_frames_land_where_the_engine_can_read_them(tmp_path, monkeypatch):
    from agents import video_planner
    from task_config import StorageConfig

    calls = {}

    async def fake_casting(cast, *, ambience, model, render, output_dir):
        calls.update(ambience=ambience, output_dir=Path(output_dir))
        return [Path(output_dir) / f"casting_{i}.mp4" for i in range(1, len(cast) + 1)]

    monkeypatch.setattr(video_planner, "room_tone", lambda out, seconds: Path(out))
    monkeypatch.setattr(video_planner, "generate_casting", fake_casting)
    monkeypatch.setattr(video_planner, "extract_frame", lambda clip, out, at_s=None: Path(out))
    config = AgentConfig.model_construct(
        storage=StorageConfig(cache_dir=str(tmp_path / "assets")),
        render=RenderSettings(output_dir=str(tmp_path / "runs")),
        models=type("Models", (), {"video_generator": None})(),
    )
    task = type("Task", (), {"agent_config": config, "task_id": "t1"})()
    plan = type("Plan", (), {"cast": [GIRL, ARM]})()
    frames = video_planner.render_casting(task, plan)
    # Le moteur ne voit que le cache partagé : ni le fond sonore ni les frames dans le run.
    assert calls["ambience"] == tmp_path / "assets/casting/room_tone_5s.wav"
    assert calls["output_dir"] == tmp_path / "runs/t1"
    assert frames == [tmp_path / "assets/casting/t1/casting_1.png", tmp_path / "assets/casting/t1/casting_2.png"]
