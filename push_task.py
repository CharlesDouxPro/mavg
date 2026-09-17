"""Pousse des TaskConfig en base, pour donner du travail au worker.

Le worker (`main.py`) prend la tâche `pending` la PLUS RÉCENTE. Pousser plusieurs
tâches les fait donc traiter en pile, de la dernière à la première : l'ordre des
arguments n'est pas l'ordre d'exécution.

Chaque fichier est validé avant l'insertion. Une config qui ne passe pas le
schéma échoue ici, pas trois minutes plus tard dans la boucle agentique.
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from providers.mongo_db_provider import MongoDB
from task_config import TaskConfig, load_task

load_dotenv(".env.local")

COLLECTION = "tasks"
"""La file que `main.py` consomme. `--collection` permet d'essayer ailleurs."""
TASKS_DIR = Path(__file__).resolve().parent / "tasks"


def connect() -> MongoDB:
    connection_string = os.environ["MONGO_CONNECTION_STRING"]
    database = os.environ["MONGO_PLATFORM_DATABASE_NAME"]
    return MongoDB(connection_string, database)


def prepare(path: Path, suffix: str) -> dict:
    """Le document à insérer : validé, daté de maintenant, en `pending`.

    L'identifiant reçoit un suffixe horodaté pour que repousser la même tâche
    n'écrase pas les sorties de la précédente — `runs/<task_id>/`.

    On insère le JSON d'origine, `${VAR}` compris : les secrets ne partent pas
    en base, ils se résolvent chez le worker au chargement.
    """
    task = load_task(path)  # lève si la config est invalide
    document = __import__("json").loads(path.read_text("utf-8"))
    document["task_id"] = f"{task.task_id}_{suffix}"
    document["created_at"] = datetime.now(timezone.utc)
    document["status"] = "pending"
    return document


def push(files: list[Path], dry_run: bool, collection: str = COLLECTION) -> None:
    client = None if dry_run else connect()
    suffix = datetime.now().strftime("%m%d_%H%M%S")
    for path in files:
        document = prepare(path, suffix)
        config = document["agent_config"]
        summary = (
            f"{document['task_id']:34s} {document['channel_name']:12s} "
            f"langue={config.get('language') or '(libre)'} "
            f"sous-titres={config.get('subtitles', {}).get('enabled', True)}"
        )
        if dry_run:
            print(f"[dry-run] {summary}")
            continue
        print(summary)
        client.create(collection, document)


def show_pending(collection: str = COLLECTION) -> None:
    client = connect()
    documents = list(
        client.db[collection].find({"status": "pending"}).sort("created_at", -1)
    )
    if not documents:
        print("Aucune tâche en attente.")
        return
    print(f"{len(documents)} tâche(s) en attente, de la prochaine servie à la dernière :")
    for document in documents:
        print(f"  {document['created_at']:%Y-%m-%d %H:%M:%S}  {document['task_id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tasks", nargs="*", type=Path, help="fichiers JSON à pousser (défaut : tous)"
    )
    parser.add_argument("--dry-run", action="store_true", help="valider sans insérer")
    parser.add_argument("--pending", action="store_true", help="lister les tâches en attente")
    parser.add_argument(
        "--collection", default=COLLECTION,
        help=f"collection cible (défaut : {COLLECTION})",
    )
    args = parser.parse_args()

    if args.pending:
        show_pending(args.collection)
        return

    files = args.tasks or sorted(TASKS_DIR.glob("*.json"))
    missing = [f for f in files if not f.is_file()]
    if missing:
        raise SystemExit(f"Introuvable : {', '.join(str(f) for f in missing)}")

    push(files, args.dry_run, args.collection)


if __name__ == "__main__":
    main()
