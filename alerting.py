"""Un e-mail par vidéo terminée, envoyé à l'adresse de sa chaîne (réussie ou en échec)."""

import os
import smtplib
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


def build_message(sender: str, result: VideoResult) -> EmailMessage:
    """L'e-mail d'une seule vidéo, adressé à la chaîne qui l'a produite."""
    channel = result.task.channel_config
    if result.error:
        subject = f"[{channel.channel_name}] Échec : {result.task.task_id}"
    else:
        title = result.publication.title if result.publication else result.task.task_id
        subject = f"[{channel.channel_name}] Vidéo prête : {title}"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = channel.email
    message.set_content(video_block(result, datetime.now() + LINK_TTL))
    return message


def _smtp_password() -> str:
    """Le mot de passe SMTP, espaces retirés.

    Gmail affiche les mots de passe d'application en quatre groupes séparés par
    des espaces (« abcd efgh ijkl mnop »), mais le mot de passe réel n'en
    contient pas : on les retire pour qu'un copier-coller fonctionne quand même.
    """
    return os.getenv("SMTP_PASSWORD", "").replace(" ", "")


def send_report(result: VideoResult) -> None:
    """Envoie l'e-mail d'une vidéo terminée, à l'adresse de sa chaîne.

    Appelé dès qu'une vidéo est traitée — une par e-mail, jamais de rapport
    groupé. Un envoi raté n'arrête pas le worker : la vidéo reste dans le bucket.
    """
    sender = os.getenv("SMTP_USER", "")
    message = build_message(sender, result)
    try:
        with smtplib.SMTP_SSL(
            os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))
        ) as smtp:
            smtp.login(sender, _smtp_password())
            smtp.send_message(message)
            print(f"E-mail envoyé à {message['To']} : {message['Subject']}")
    except (smtplib.SMTPException, OSError) as exc:
        print(f"E-mail non envoyé ({result.task.task_id}) : {exc}")
