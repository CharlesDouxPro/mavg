"""La configuration d'une tâche, telle qu'elle arrive de la base.

Tout ce qui peut changer sans redéploiement vit ici. Le code ne porte que les
contraintes dures du moteur (une durée de clip hors de 5-15 s est refusée par
MiniMax-H3, pas par nous) et le format de prompt H3, qui est un contrat, pas un
réglage.

Règle de partage : si un opérateur peut vouloir le changer pour une vidéo, une
chaîne ou une campagne, c'est un champ de ce fichier. Si le changer casserait
l'appel au moteur, c'est une constante de `providers/`.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """Un modèle joignable : où, avec quelle clé, sous quel nom de déploiement.

    `model_name` est le nom côté serveur : un déploiement Azure Foundry
    (`claude-opus-4-7-1`) ou un repo servi par SGLang (`MiniMaxAI/MiniMax-H3`).
    """

    provider: str
    model_name: str
    base_url: str
    token: str


class ModelConfigs(BaseModel):
    master_mind: ModelConfig
    slm: ModelConfig
    video_generator: ModelConfig
    image_generator: ModelConfig


class LLMSettings(BaseModel):
    """Réglages d'inférence du modèle qui raisonne."""

    max_tokens: int = 32000
    thinking: bool = True
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_iterations: int = 40
    """Borne de la boucle agentique. Sans elle, un validateur trop strict fait
    tourner le modèle jusqu'à épuisement du budget."""


class Avatar(BaseModel):
    """Le personnage à l'écran. Son apparence est le verrou d'identité."""

    name: str
    avatar_url: str
    """La vidéo de référence de l'avatar, dans le stockage objet. S'écrit
    `s3://bucket/cle`, en URL du bucket, ou en clé nue (le bucket vient alors de
    `StorageConfig`). Une URL http(s) hors bucket est téléchargée telle quelle."""
    description: str
    appearance: str
    reference_frame_s: float | None = None
    voice_url: str = ""


class Brief(BaseModel):
    """Ce que l'utilisateur demande.

    `prompt` est la tâche en langage naturel : résumer une actu en vidéo,
    dérouler un DIY, animer un fruit. Le sujet précis arrive par
    `AgentConfig.params`.
    """

    prompt: str
    mood: str = ""


class RenderSettings(BaseModel):
    """Réglages du moteur vidéo, constants sur toute une vidéo.

    `seed` est fixe par construction : c'est lui qui empêche le visage de
    dériver d'un plan à l'autre. Le changer change l'avatar rendu.
    """

    seed: int = 42
    num_inference_steps: int = 9
    """Points de grille sigma ; évaluations = steps - 1 (LoRA turbo ref2v)."""
    flow_shift: float = 12.0
    audio_flow_shift: float = 3.0
    aspect_ratio: str = "9:16"
    short_edge: int = 768
    concurrency: int = 2
    """Clips en vol simultanément. Le serveur rend un clip à la fois : 2 suffit
    pour qu'un clip attende en file pendant que l'autre se rend (pas de temps
    mort GPU). Au-delà, les clips ne font qu'attendre et consomment `timeout_s`."""
    timeout_s: float = 1800.0
    poll_interval_s: float = 3.0
    output_dir: str = "runs"
    final_name: str = "final.mp4"
    """Nom du montage final, dans le dossier du run, à côté des clips."""


DEFAULT_FORBIDDEN_APPEARANCE = [
    "hair",
    "beard",
    "shirt",
    "jacket",
    "glasses",
    "hoodie",
    "wears",
    "wearing",
    "his face",
    "her face",
    "moustache",
    "young man",
    "young woman",
    "young male",
    "young female",
]


class PlanConstraints(BaseModel):
    """Contraintes éditoriales vérifiées avant le rendu.

    Elles sont volontairement séparées des contraintes moteur : le moteur
    accepte 5-15 s par clip, mais une chaîne donnée peut vouloir 6-10 s.
    """

    min_total_seconds: int = 30
    max_total_seconds: int = 60
    min_shot_seconds: int = 6
    max_shot_seconds: int = 10
    words_per_second: float = 2.5
    """Débit de parole servant à vérifier qu'une réplique tient dans son clip."""
    min_description_words: int = 90
    arc: list[str] = Field(
        default_factory=lambda: ["hook", "context", "core", "escalation", "punch"]
    )
    """Les rôles autorisés, dans l'ordre. Le premier ouvre, le dernier ferme.
    Dépend du style : un DIY n'a pas le même arc qu'une actu."""
    forbidden_appearance_words: list[str] = Field(
        default_factory=lambda: list(DEFAULT_FORBIDDEN_APPEARANCE)
    )
    """Vocabulaire interdit dans les prompts : l'apparence est déjà verrouillée
    en amont, la décrire à nouveau la fait dériver."""


class SubtitleSettings(BaseModel):
    """Sous-titres incrustés, transcrits en local par faster-whisper.

    Rien ne part sur une API : le modèle tourne en local, sur l'audio réel du
    montage. C'est ce qui le rend fiable sur l'audio natif de MiniMax-H3, dont
    on ne connaît pas le transcript exact — le modèle peut phraser une réplique
    autrement que le script.
    """

    enabled: bool = True
    """False : le montage sort tel quel, aucune transcription n'est lancée."""
    model: str = "large-v3"
    """Poids faster-whisper. 'large-v3' pour la qualité, 'large-v3-turbo' plus
    rapide, 'base' ou 'tiny' pour un aller-retour de test."""
    device: str = "auto"
    """'auto' prend le GPU CUDA s'il y en a un, sinon le CPU. 'cuda' ou 'cpu'
    pour forcer. CTranslate2 ne gère pas Metal/MPS : sur un Mac Apple Silicon,
    'auto' résout toujours vers le CPU."""
    compute_type: str = "auto"
    """'auto' laisse CTranslate2 choisir la précision la plus rapide que la
    machine sait faire — float16 sur GPU, int8 sur CPU."""
    ffmpeg_bin: str = ""
    """Binaire ffmpeg servant à l'incrustation ; il lui faut libass. Vide : on
    prend le premier build du PATH qui sait le faire. Renseigné : celui-là et
    pas un autre."""
    style: Literal["karaoke", "plain"] = "karaoke"
    """'karaoke' colorie le mot en cours (.ass), 'plain' écrit des légendes
    blanches (.srt) — le SRT ne sait pas colorier un mot."""
    base_color: str = "FFFFFF"
    highlight_color: str = "F5E003"
    max_chars: int = 16
    """Une légende courte tient dans la largeur d'un 9:16 et se lit d'un coup."""
    max_duration_s: float = 1.6
    max_gap_s: float = 0.6
    """Au-delà de ce silence, on coupe la légende : la parole a repris ailleurs."""


class PublicationConstraints(BaseModel):
    """Contraintes du texte de publication qui accompagne la vidéo."""

    min_title_chars: int = 10
    max_title_chars: int = 100
    """Le titre range la vidéo dans le stockage : au-delà, le chemin devient
    illisible."""
    min_chars: int = 80
    max_chars: int = 2200
    """Limite de la légende TikTok. Instagram et YouTube Shorts sont plus larges."""
    min_hashtags: int = 3
    max_hashtags: int = 8
    must_include: list[str] = Field(default_factory=list)
    """Chaînes qui DOIVENT apparaître dans la description. Vide par défaut : le
    reste se demande en langage naturel dans le brief, que l'agent lit."""


class StorageConfig(BaseModel):
    """Le stockage objet où vivent les assets d'avatar.

    Même forme que `ScraperConfig` : changer de fournisseur = changer `provider`
    en base. Les clés ne vivent pas en clair dans la config, elles s'écrivent
    `${SCW_ACCESS_KEY}` et se résolvent depuis l'environnement au chargement ;
    laissées vides, elles sont lues dans l'environnement au moment de l'appel.
    """

    provider: str = "scaleway"
    endpoint_url: str = "https://s3.fr-par.scw.cloud"
    """Endpoint S3 de la région, SANS le nom du bucket (boto3 le préfixe lui-même)."""
    region: str = "fr-par"
    bucket: str = "mavg-object-storage"
    access_key: str = ""
    secret_key: str = ""
    cache_dir: str = "assets"
    """Où atterrissent les objets téléchargés, sous `<cache_dir>/<bucket>/<clé>`.
    Partagé entre les tâches : un avatar inchangé n'est pas retéléchargé."""
    timeout_s: float = 300.0


class ScraperConfig(BaseModel):
    """Le service qui récupère les pages web, et ses réglages.

    Même forme que `ModelConfig` : changer de scraper = changer `provider` en
    base. Le token ne vit pas en clair dans la config, il s'écrit `${LINKUP_API_KEY}`
    et se résout depuis l'environnement au chargement.
    """

    provider: str = "linkup"
    base_url: str = "https://api.linkup.so/v1"
    token: str = ""
    mode: Literal["standard", "pro"] = "standard"
    """'pro' réussit sur les pages difficiles, au prix d'une latence plus haute."""
    render_js: bool = False
    extract_images: bool = False
    timeout_s: float = 60.0
    max_chars: int = 20000
    """Au-delà, la page est tronquée : une page entière noie le contexte."""


PREVIEW_CHARS = 280


def _preview(value: Any) -> str:
    """Un paramètre long est résumé : l'agent va le chercher avec ses outils.

    Recopier un article entier dans le message et le reservir par `fetch_source`
    le ferait porter deux fois au contexte.
    """
    text = str(value).replace("\n", " ").strip()
    if len(text) <= PREVIEW_CHARS:
        return text
    return f"{text[:PREVIEW_CHARS]}… ({len(text)} caractères, à récupérer avec fetch_source)"


LANGUAGE_NAMES = {
    "fr": "French",
    "en": "English",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "tr": "Turkish",
    "sv": "Swedish",
    "ar": "Arabic",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "hi": "Hindi",
}
"""Code ISO -> nom anglais de la langue, tel que MiniMax-H3 l'attend dans les
balises `<d>[Language] ... </d>`. Les sections du prompt restent en anglais ;
seule la réplique change de langue."""


class AgentConfig(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    """Le sujet précis de cette vidéo : l'URL à scraper, l'idée de DIY, le fruit
    à animer. Remonté dans le message utilisateur par `user_message`."""
    models: ModelConfigs
    skill: str = ""
    """Le skill de style imposé. Vide : l'agent choisit d'après le brief."""
    language: str = ""
    """Code ISO de la langue parlée ET de la description (ex. 'fr', 'en').
    Vide : l'agent choisit, en général la langue de la source ou du brief."""
    brief: Brief
    avatar: Avatar
    llm: LLMSettings = Field(default_factory=LLMSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    plan: PlanConstraints = Field(default_factory=PlanConstraints)
    scraper: ScraperConfig = Field(default_factory=ScraperConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    publication: PublicationConstraints = Field(default_factory=PublicationConstraints)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def user_message(self) -> str:
        """La tâche telle que l'agent la reçoit.

        Les `params` sont remontés en clair et mis en avant : ils portent
        l'élément spécifique de cette vidéo, celui sans lequel le brief reste
        une consigne générique.
        """
        blocks = [f"TÂCHE\n{self.brief.prompt}"]
        if self.brief.mood:
            blocks.append(f"MOOD\n{self.brief.mood}")
        if self.params:
            lines = "\n".join(
                f"- {key} : {_preview(value)}" for key, value in self.params.items()
            )
            blocks.append(f"SUJET PRÉCIS DE CETTE VIDÉO\n{lines}")
        if self.skill:
            blocks.append(f"STYLE IMPOSÉ\n{self.skill}")
        if self.language:
            blocks.append(f"LANGUE IMPOSÉE\n{self.language_instruction()}")
        return "\n\n".join(blocks)

    def language_instruction(self) -> str:
        """La consigne de langue, avec le nom que H3 attend dans les balises <d>."""
        name = LANGUAGE_NAMES.get(self.language.lower())
        line = (
            f"Les répliques parlées et le texte de publication sont en "
            f"'{self.language}'"
        )
        if name:
            line += f" ({name}). Les balises de dialogue s'écrivent <d>[{name}] … </d>"
        line += (
            ". Ce n'est pas la langue de la source qui décide, c'est celle-ci. "
            "Les sections du prompt H3 restent en anglais."
        )
        return line


class ChannelConfig(BaseModel):
    channel_name: str
    email: str


class TaskConfig(BaseModel):
    created_at: datetime
    task_id: str
    status: Literal["pending", "working", "failed", "done"]
    channel_config: ChannelConfig
    agent_config: AgentConfig


def expand_env(value: Any) -> Any:
    """Remplace ${VAR} par la variable d'environnement, récursivement.

    Les secrets restent hors des fichiers de config : ceux-ci ne portent que
    leur nom, et la valeur se résout au chargement.
    """
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def load_task(source: Path | str | dict) -> "TaskConfig":
    """Une TaskConfig depuis un fichier JSON ou un document déjà chargé.

    Accepte un document Mongo tel quel : les champs en trop (`_id`) sont ignorés.
    """
    raw = (
        source
        if isinstance(source, dict)
        else json.loads(Path(source).read_text("utf-8"))
    )
    return TaskConfig.model_validate(expand_env(raw))
