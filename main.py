"""Le point d'entrée : traite les tâches une à une, et envoie les rapports quand la file est vide.

En mode lot (`EXIT_WHEN_EMPTY=1`, l'instance cloud), le worker s'arrête une fois la file
vidée et les rapports envoyés. Sinon, il attend les tâches suivantes.
"""

import os
import time
import traceback

from dotenv import load_dotenv
from pydantic import ValidationError

from agents.video_planner import (
    PUBLISHED_VIDEO,
    plan_video,
    produce_video,
    publish_video,
)
from alerting import VideoResult, send_reports
from providers.mongo_db_provider import MongoDB
from task_config import TaskConfig, load_task

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


def pull_task(client: MongoDB) -> TaskConfig | None:
    while document := client.load_last_task(COLLECTION):
        try:
            return load_task(document)
        except ValidationError as exc:
            print(f"task {document.get('task_id')} invalid :\n{exc}")
            client.set_status(
                COLLECTION, document.get("task_id"), "failed", error=str(exc)
            )
    return None


def run_task(client: MongoDB, task: TaskConfig) -> VideoResult:
    result = VideoResult(task)
    try:
        result.stage = "planification"
        plan, _, _ = plan_video(task)
        result.publication = plan.publication

        result.stage = "rendu"
        clips, final = produce_video(task, plan)
        print(f"{len(clips)} clips montés dans {final}")

        result.stage = "publication"
        prefix = publish_video(task, plan, final)
        print(f"Published on {prefix}")
        result.video_uri = f"{prefix}/{PUBLISHED_VIDEO}"
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
    results: list[VideoResult] = []
    while True:
        task = pull_task(mongodb)
        if task:
            results.append(run_task(mongodb, task))
            continue
        if results:
            send_reports(results)
            results = []
        if EXIT_WHEN_EMPTY:
            print("Empty queue")
            return
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
