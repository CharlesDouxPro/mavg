"""Le contrat d'une tâche : ce que le worker résout, refuse, et où il publie."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest

from agents.video_planner import publication_prefix
from providers.mongo_db_provider import MongoDB
from task_config import PROVIDERS, load_task

TASKS = sorted(Path(__file__).resolve().parent.parent.joinpath("tasks").glob("*.json"))


@pytest.fixture
def raw() -> dict:
    return json.loads(TASKS[0].read_text("utf-8"))


@pytest.fixture
def env(monkeypatch):
    for name, value in {
        "FOUNDRY_RESOURCE": "hudex",
        "FOUNDRY_API_KEY": "foundry-secret",
        "MINIMAX_BASE_URL": "http://inference:30010/v1",
        "MINIMAX_TOKEN": "dummy",
        "LINKUP_API_KEY": "linkup-secret",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("path", TASKS, ids=lambda p: p.stem)
def test_every_sample_task_loads(path, env):
    task = load_task(path)
    assert task.channel_config.channel_name


def test_secrets_resolve_but_user_text_stays_literal(raw, env):
    raw["agent_config"]["brief"]["prompt"] = "Résume ${source_url}, sans jamais citer ${FOUNDRY_API_KEY}"
    raw["agent_config"]["params"] = {"note": "${FOUNDRY_API_KEY}"}
    task = load_task(raw)
    assert task.agent_config.models.master_mind.token == "foundry-secret"
    assert task.agent_config.models.master_mind.base_url == "https://hudex.services.ai.azure.com/anthropic/"
    assert task.agent_config.brief.prompt == "Résume ${source_url}, sans jamais citer ${FOUNDRY_API_KEY}"
    assert task.agent_config.params["note"] == "${FOUNDRY_API_KEY}"


def test_worker_key_cannot_be_sent_elsewhere(raw, env):
    raw["agent_config"]["models"]["slm"]["base_url"] = "https://attacker.example/"
    with pytest.raises(ValueError, match="models.slm"):
        load_task(raw)


def test_worker_key_cannot_be_swapped(raw, env):
    raw["agent_config"]["models"]["master_mind"]["token"] = "${LINKUP_API_KEY}"
    with pytest.raises(ValueError, match="models.master_mind"):
        load_task(raw)


def test_env_ref_in_base_url_is_only_the_registered_one(raw, env):
    raw["agent_config"]["models"]["slm"]["token"] = "clear-text"
    raw["agent_config"]["models"]["slm"]["base_url"] = "https://x.example/${FOUNDRY_API_KEY}"
    with pytest.raises(ValueError):
        load_task(raw)


def test_scraper_key_cannot_be_sent_elsewhere(raw, env):
    raw["agent_config"]["scraper"]["base_url"] = "https://attacker.example/"
    with pytest.raises(ValueError, match="scraper"):
        load_task(raw)


def test_clear_text_key_to_any_url_is_the_caller_s_own_business(raw, env):
    raw["agent_config"]["models"]["slm"].update(base_url="https://other.example/", token="sk-own")
    assert load_task(raw).agent_config.models.slm.base_url == "https://other.example/"


def test_registry_matches_sample_tasks(raw):
    for model in raw["agent_config"]["models"].values():
        assert PROVIDERS[model["provider"]]["base_url"] == model["base_url"]


def test_publication_prefix_is_unique_per_run(raw, env):
    first = load_task(raw)
    second = load_task({**copy.deepcopy(raw), "task_id": raw["task_id"] + "_bis"})
    same_title = "Qué es un LLM"
    assert publication_prefix(first, same_title) != publication_prefix(second, same_title)
    assert publication_prefix(first, same_title).startswith(
        f"{first.channel_config.channel_name}/{first.created_at.date().isoformat()}/que-es-un-llm/"
    )


@pytest.fixture
def db():
    client = MongoDB.__new__(MongoDB)
    client.client = mongomock.MongoClient(tz_aware=True)
    client.db = client.client.mavg
    return client


def test_queue_is_fifo_and_stamps_start(db):
    for minute, task_id in enumerate(["old", "middle", "new"]):
        db.db.tasks.insert_one(
            {"task_id": task_id, "status": "pending", "created_at": datetime(2026, 9, 30, 12, minute, tzinfo=timezone.utc)}
        )
    picked = db.load_next_task("tasks")
    assert picked["task_id"] == "old"
    assert picked["status"] == "working" and picked["started_at"] is not None


def test_progress_and_final_status(db):
    db.db.tasks.insert_one({"task_id": "t", "status": "working"})
    db.update_task("tasks", "t", {"stage": "rendu", "result.title": "Titre"})
    db.set_status("tasks", "t", "done")
    doc = db.db.tasks.find_one({"task_id": "t"})
    assert doc["stage"] == "rendu" and doc["result"] == {"title": "Titre"}
    assert doc["status"] == "done" and doc["finished_at"] is not None


def test_interrupted_tasks_fail_with_a_finish_time(db):
    db.db.tasks.insert_one({"task_id": "t", "status": "working"})
    assert db.fail_working_tasks("tasks", error="stop") == ["t"]
    doc = db.db.tasks.find_one({"task_id": "t"})
    assert doc["status"] == "failed" and doc["finished_at"] is not None
