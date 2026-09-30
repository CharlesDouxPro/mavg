from datetime import datetime, timezone

from pymongo import MongoClient, ReturnDocument

FINISHED = ("done", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MongoDB:
    def __init__(self, connection_string, db_name):
        self.client = MongoClient(connection_string)
        self.db = self.client[db_name]

    def create(self, collection_name, data):
        collection = self.db[collection_name]
        result = collection.insert_one(data)
        print("CREATE ->", result.inserted_id)

    def load_next_task(self, collection_name):
        """La plus ANCIENNE tâche en attente, passée en `working` (file FIFO).

        L'ordre affiché par l'interface est donc l'ordre de traitement : un run lancé
        après un autre ne passe jamais devant.
        """
        task = self.db[collection_name].find_one_and_update(
            filter={"status": "pending"},
            sort=[("created_at", 1)],
            update={"$set": {"status": "working", "started_at": _now()}},
            return_document=ReturnDocument.AFTER,
        )
        if task:
            print("New task received")
        return task

    def update_task(self, collection_name, task_id, fields: dict):
        """Écrit des champs d'avancement (`stage`, `result.title`…) sans toucher au reste."""
        self.db[collection_name].update_one(
            filter={"task_id": task_id}, update={"$set": fields}
        )

    def set_status(self, collection_name, task_id, status, error=None):
        fields = {"status": status, "error": error}
        if status in FINISHED:
            fields["finished_at"] = _now()
        self.update_task(collection_name, task_id, fields)

    def fail_working_tasks(self, collection_name, error):
        collection = self.db[collection_name]
        task_ids = [doc.get("task_id") for doc in collection.find({"status": "working"}, {"task_id": 1})]
        collection.update_many(
            filter={"status": "working"},
            update={"$set": {"status": "failed", "error": error, "finished_at": _now()}},
        )
        return task_ids
