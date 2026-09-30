"""Le point d'entrée : traite les tâches une à une, un e-mail par vidéo terminée.

Chaque vidéo (réussie ou en échec) déclenche son propre e-mail dès qu'elle est
traitée. En mode lot (`EXIT_WHEN_EMPTY=1`, l'instance cloud), le worker s'arrête
une fois la file vidée. Sinon, il attend les tâches suivantes.

L'avancement (`stage`, titre, lien de la vidéo) est écrit dans la tâche au fil de
l'eau : c'est ce que l'interface web affiche pendant le rendu.
"""

import os
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo.errors import PyMongoError

from agents.video_planner import (
    PUBLISHED_VIDEO,
    plan_video,
    produce_video,
    publish_video,
)
from alerting import VideoResult, send_report
from providers.mongo_db_provider import MongoDB
from task_config import SCHEMA_VERSION, TaskConfig, load_task

load_dotenv(".env.local")

MONGO_CONNECTION_STRING = os.getenv("MONGO_CONNECTION_STRING", "")
DB_NAME = os.getenv("MONGO_PLATFORM_DATABASE_NAME", "")
COLLECTION = "tasks"
POLL_INTERVAL_S = 5
EXIT_WHEN_EMPTY = os.getenv("EXIT_WHEN_EMPTY") == "1"
INTERRUPTED = "Interrupted : The worker stoped during its treatment"


def fail_interrupted(client: MongoDB) -> None:
    for task_id in client.fail_working_tasks(COLLECTION, error=INTERRUPTED):
        print(f"Task {task_id} set to failed")


def mark(client: MongoDB, task_id: str, **fields) -> None:
    """Enregistre l'avancement pour l'interface.

    Au mieux : une base qui hoquette ne doit pas faire échouer une vidéo à moitié
    rendue. Seul le statut final (`set_status`) compte vraiment.
    """
    try:
        client.update_task(COLLECTION, task_id, fields)
    except PyMongoError as exc:
        print(f"Avancement non enregistré ({task_id}) : {exc}")


def pull_task(client: MongoDB) -> TaskConfig | None:
    while document := client.load_next_task(COLLECTION):
        task_id = document.get("task_id")
        version = document.get("schema_version")
        if version is not None and version != SCHEMA_VERSION:
            print(
                f"ATTENTION {task_id} : schéma v{version}, le worker est en v{SCHEMA_VERSION}. "
                "Resynchroniser task_config.py entre le worker et l'interface."
            )
        try:
            return load_task(document)
        except ValueError as exc:  # ValidationError en hérite
            print(f"task {task_id} invalid :\n{exc}")
            mark(client, task_id, stage="validation")
            client.set_status(COLLECTION, task_id, "failed", error=str(exc))
    return None


def run_task(client: MongoDB, task: TaskConfig) -> VideoResult:
    result = VideoResult(task)

    def stage(name: str) -> None:
        result.stage = name
        mark(client, task.task_id, stage=name, stage_at=datetime.now(timezone.utc))

    try:
        stage("planification")
        plan, _, _ = plan_video(task)
        result.publication = plan.publication
        # Le titre est connu bien avant la vidéo : l'interface l'affiche pendant le rendu.
        mark(
            client,
            task.task_id,
            **{
                "result.title": plan.publication.title,
                "result.description": plan.publication.description,
                "result.hashtags": plan.publication.hashtags,
            },
        )

        stage("rendu")
        clips, final = produce_video(task, plan)
        print(f"{len(clips)} clips montés dans {final}")

        stage("publication")
        prefix = publish_video(task, plan, final)
        print(f"Published on {prefix}")
        result.video_uri = f"{prefix}/{PUBLISHED_VIDEO}"
        mark(client, task.task_id, **{"result.video_uri": result.video_uri})
    except Exception:
        result.error = traceback.format_exc()
        print(result.error)
        client.set_status(COLLECTION, task.task_id, "failed", error=result.error)
    else:
        client.set_status(COLLECTION, task.task_id, "done")
    return result


def main() -> None:
    mongodb = MongoDB(MONGO_CONNECTION_STRING, DB_NAME)
    fail_interrupted(mongodb)
    while True:
        task = pull_task(mongodb)
        if task:
            send_report(run_task(mongodb, task))
            continue
        if EXIT_WHEN_EMPTY:
            print("Empty queue")
            return
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
