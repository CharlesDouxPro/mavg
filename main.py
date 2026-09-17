"""Le point d'entrée : prend une tâche, lance le réalisateur, range le résultat."""

import os
import time

from dotenv import load_dotenv

from agents.video_planner import plan_video, produce_video, publish_video
from providers.mongo_db_provider import MongoDB
from task_config import TaskConfig, load_task

load_dotenv(".env.local")

MONGO_CONNECTION_STRING = os.getenv("MONGO_CONNECTION_STRING", "")
DB_NAME = os.getenv("MONGO_PLATFORM_DATABASE_NAME", "")


def pull_task(client: MongoDB) -> TaskConfig:
    """Attend qu'une tâche apparaisse en base et la renvoie.

    `load_last_task` rend le document Mongo brut ; `load_task` le valide et
    résout les ${VAR} des secrets. Une config invalide échoue ici, avant le
    moindre appel payant.
    """
    while True:
        document = client.load_last_task("tasks")
        if document:
            return load_task(document)
        time.sleep(5)


def main() -> None:
    mongodb = MongoDB(MONGO_CONNECTION_STRING, DB_NAME)
    task = pull_task(mongodb)

    plan, _, _ = plan_video(task)
    clips, final = produce_video(task, plan)
    print(f"{len(clips)} clips montés dans {final}")
    print(f"publiée sur {publish_video(task, plan, final)}")


if __name__ == "__main__":
    main()
