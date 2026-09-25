"""Le rapport de fin de lot : un email par chaîne, envoyé à l'adresse de sa ChannelConfig."""

import os
import smtplib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage

from langchain_anthropic import ChatAnthropic

from agents.video_planner import publication_text
from providers import storage
from providers.minimax_h3 import Publication
from task_config import TaskConfig

LINK_TTL = timedelta(days=7)
MAX_ERROR_CHARS = 6000
SEPARATOR = "\n" + "-" * 40 + "\n\n"

EXPLAIN_PROMPT = """Une tâche du pipeline de génération vidéo a échoué pendant l'étape « {stage} ».
Explique en français, en trois phrases au plus, à quelqu'un qui n'a pas le code sous les yeux :
ce qui s'est passé, la cause probable, et ce qu'il faut faire.
Réponds en texte brut, sans Markdown ni titre.

Tâche : {task_id} (chaîne {channel})

Traceback :
{error}"""


@dataclass
class VideoResult:
    task: TaskConfig
    publication: Publication | None = None
    video_uri: str = ""
    stage: str = ""
    error: str = ""


def explain_error(result: VideoResult) -> str:
    model = result.task.agent_config.models.slm
    prompt = EXPLAIN_PROMPT.format(
        stage=result.stage,
        task_id=result.task.task_id,
        channel=result.task.channel_config.channel_name,
        error=result.error[-MAX_ERROR_CHARS:],
    )
    try:
        llm = ChatAnthropic(
            model=model.model_name,
            base_url=model.base_url,
            api_key=model.token,
            max_tokens=1024,
        )
        return llm.invoke(prompt).text
    except Exception as exc:
        return f"Explication indisponible : {exc}"


def video_block(result: VideoResult, expires: datetime) -> str:
    publication = result.publication
    if result.error:
        name = f"{publication.title} ({result.task.task_id})" if publication else result.task.task_id
        cause = result.error.strip().splitlines()[-1]
        return (
            f"ÉCHEC : {name}\n"
            f"Étape : {result.stage}\n"
            f"Erreur : {cause}\n\n"
            f"{explain_error(result)}\n"
        )

    url = storage.presigned_url(
        result.task.agent_config.storage,
        result.video_uri,
        filename=f"{storage.slugify(publication.title) or 'video'}.mp4",
        expires_s=int(LINK_TTL.total_seconds()),
    )
    return (
        f"{publication_text(publication)}\n"
        f"Télécharger la vidéo (lien valable jusqu'au {expires:%d/%m}) :\n{url}\n"
    )


def build_report(sender: str, channel: str, email: str, results: list[VideoResult]) -> EmailMessage:
    expires = datetime.now() + LINK_TTL
    ready = sum(1 for result in results if not result.error)
    failed = len(results) - ready

    subject = f"[{channel}] {ready} vidéo(s) prête(s)"
    if failed:
        subject += f", {failed} en échec"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = email
    message.set_content(SEPARATOR.join(video_block(result, expires) for result in results))
    return message


def send_reports(results: list[VideoResult]) -> None:
    groups: dict[tuple[str, str], list[VideoResult]] = defaultdict(list)
    for result in results:
        channel = result.task.channel_config
        groups[(channel.channel_name, channel.email)].append(result)

    sender = os.getenv("SMTP_USER", "")
    messages = [
        build_report(sender, channel, email, items)
        for (channel, email), items in groups.items()
    ]

    # Un envoi raté ne doit pas arrêter le worker : les vidéos restent dans le bucket.
    try:
        with smtplib.SMTP_SSL(
            os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))
        ) as smtp:
            smtp.login(sender, os.getenv("SMTP_PASSWORD", ""))
            for message in messages:
                smtp.send_message(message)
                print(f"Rapport envoyé à {message['To']} : {message['Subject']}")
    except (smtplib.SMTPException, OSError) as exc:
        print(f"Rapports non envoyés : {exc}")
