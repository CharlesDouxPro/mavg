"""Agent réalisateur : lit les skills du repo, puis écrit le plan de tournage.

Rien n'est figé ici. Le sujet, le style, l'avatar, les bornes éditoriales, les
réglages de rendu et les modèles à joindre viennent tous de la `TaskConfig`.
Ce fichier ne porte que la méthode : découvrir les skills, écrire les plans,
les valider, les rendre.
"""

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy
from langchain.tools import tool
from langchain_anthropic import ChatAnthropic
from pydantic import ValidationError

from providers import scraper, storage
from providers.editing import concat_clips, extract_frame, probe_duration
from providers.subtitles import add_subtitles
from providers.minimax_h3 import (
    MAX_SECONDS,
    MIN_SECONDS,
    MinimaxShot,
    Publication,
    VideoPlan,
    build_payload,
    generate_videos,
    identity_lock,
)
from providers.storage import slugify
from task_config import (
    LANGUAGE_NAMES,
    PlanConstraints,
    PublicationConstraints,
    TaskConfig,
    load_task,
)

# Disposition du repo, pas un réglage.
ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
HUMANIZER_MODULE = SKILLS_DIR / "writing/humanizer/includes/humanizer.py"

# Contrat du format de prompt H3 : ce n'est pas un réglage, c'est l'API du moteur.
REQUIRED_SECTIONS = [
    "summary:",
    "detailed_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
]
INJECTED_SECTIONS = ["subject_definitions:", "retention_analysis:"]


# --- Chargement des skills ---


def _parse_skill(path: Path) -> dict:
    """Sépare le frontmatter YAML du corps d'un SKILL.md."""
    import yaml

    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = yaml.safe_load(front) or {}
    else:
        meta, body = {}, raw
    return {
        "name": meta.get("name", path.parent.name),
        "description": (meta.get("description") or "").strip(),
        "body": body.strip(),
        "dir": path.parent,
    }


def load_skills(skills_dir: Path = SKILLS_DIR) -> dict[str, dict]:
    return {s["name"]: s for s in (_parse_skill(p) for p in skills_dir.rglob("SKILL.md"))}


def _load_module(path: Path):
    """Importe un .py d'un dossier includes/ pour réutiliser ses listes."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Draft:
    """Ce que les outils construisent au fil de la boucle."""

    def __init__(self) -> None:
        self.treatment: dict | None = None
        self.shots: dict[int, MinimaxShot] = {}
        self.publication: Publication | None = None
        self.loaded: list[str] = []


# --- Outils ---


def build_tools(task: TaskConfig, draft: Draft, skills: dict[str, dict]) -> list:
    """Fabrique les outils en fermant sur le contexte de la tâche.

    Les outils lisent les contraintes de la config, jamais de constantes de
    module : changer une borne dans la task ne demande pas de redéploiement.
    """
    config = task.agent_config
    limits = config.plan
    humanizer = _load_module(HUMANIZER_MODULE) if HUMANIZER_MODULE.is_file() else None

    @tool
    def list_skills() -> str:
        """Liste les skills disponibles, avec leur description.

        À appeler en premier pour savoir quelles méthodes de production existent.
        """
        print("  [tool] list_skills()")
        lines = []
        for skill in skills.values():
            rel = skill["dir"].relative_to(SKILLS_DIR)
            includes = [p.name for p in (skill["dir"] / "includes").glob("*")]
            lines.append(
                f"- {skill['name']} ({rel})\n  {skill['description']}"
                + (f"\n  includes: {', '.join(includes)}" if includes else "")
            )
        return "\n".join(lines)

    @tool
    def load_skill(name: str) -> str:
        """Charge le contenu complet d'un skill par son nom.

        Charge le skill agent qui décrit ton rôle, le skill de style qui
        correspond au brief, et tout skill transverse qui contraint l'écriture.
        Leurs règles priment sur tes habitudes.
        """
        print(f"  [tool] load_skill({name!r})")
        key = name if name in skills else name.rstrip("/").split("/")[-1]
        if key not in skills:
            return f"Skill inconnu. Disponibles : {', '.join(skills)}"
        draft.loaded.append(key)
        return skills[key]["body"]

    @tool
    def load_skill_include(skill: str, filename: str) -> str:
        """Charge un fichier annexe d'un skill (dossier includes/).

        C'est là que vivent les références de format de prompt.
        """
        print(f"  [tool] load_skill_include({skill!r}, {filename!r})")
        key = skill if skill in skills else skill.rstrip("/").split("/")[-1]
        if key not in skills:
            return f"Skill inconnu. Disponibles : {', '.join(skills)}"
        path = skills[key]["dir"] / "includes" / filename
        if not path.is_file():
            available = [p.name for p in (skills[key]["dir"] / "includes").glob("*")]
            return f"Fichier introuvable. Disponibles : {', '.join(available) or 'aucun'}"
        draft.loaded.append(f"{key}/{filename}")
        return path.read_text(encoding="utf-8")

    @tool
    def fetch_source(url: str) -> str:
        """Récupère le contenu d'une page web, en markdown.

        À utiliser quand le sujet de la vidéo pointe vers une page à lire.
        Si la vidéo part d'une idée et non d'une source, n'appelle pas cet outil.
        Renvoie le texte de la page, ou la raison de l'échec.
        """
        print(f"  [tool] fetch_source({url})")
        provided = config.get("source_text")
        if provided:
            return provided
        content = scraper.fetch(config.scraper, url)
        print(f"           -> {len(content)} caractères")
        return content

    @tool
    def save_treatment(angle: str, language: str, key_points: list[str]) -> str:
        """Enregistre l'angle de la vidéo et les points qu'elle a le droit d'affirmer.

        angle: l'idée unique sur laquelle la vidéo est construite, en une phrase.
        language: langue parlée (code ISO, ex 'fr').
        key_points: les faits ou étapes que la vidéo peut affirmer. Quand la
            vidéo part d'une source, ils viennent de la source et d'elle seule.
        """
        print(f"  [tool] save_treatment({len(key_points)} points)")
        draft.treatment = {
            "angle": angle,
            "language": language,
            "key_points": key_points,
        }
        return "Traitement enregistré."

    @tool
    def add_shot(
        index: int, role: str, seconds: int, spoken_line: str, prompt: str,
        camera_motion: Literal["stable", "punchy_zoom"] = "stable",
    ) -> str:
        """Ajoute (ou remplace) un plan du storyboard.

        index: position dans la timeline, à partir de 1.
        role: rôle du plan dans l'arc du style suivi.
        seconds: durée du clip.
        spoken_line: la réplique, dans la langue du traitement.
        prompt: le prompt H3 full-reference, en anglais, sections `summary:`,
            `detailed_description:`, `overall_soundscape:`, `non_diegetic_music:`.
            N'écris pas `subject_definitions:` ni `retention_analysis:`. Le moteur
            n'a PAS de prompt négatif : dis ce que tu veux voir, pas ce à éviter.
        camera_motion: "stable" (défaut) = plan fixe, aucun zoom — obligatoire
            dès que l'avatar parle (context, explication, core). "punchy_zoom" =
            zoom vif et bref, réservé aux plans qui doivent accrocher l'œil
            (hook, punch), à décrire aussi dans le mouvement de caméra du prompt.
        """
        try:
            shot = MinimaxShot(
                index=index,
                role=role,
                seconds=seconds,
                spoken_line=spoken_line,
                prompt=prompt,
                camera_motion=camera_motion,
            )
        except ValidationError as exc:
            print(f"  [tool] add_shot({index}) REFUSE")
            return f"Plan refusé par le schéma MiniMax :\n{exc}"
        print(
            f"  [tool] add_shot({index}, {role}, {seconds}s, {camera_motion}, "
            f"{len(prompt.split())} mots de prompt)"
        )
        draft.shots[index] = shot
        return f"Plan {index} enregistré."

    @tool
    def save_publication(title: str, description: str, hashtags: list[str]) -> str:
        """Enregistre le titre et le texte de publication de la vidéo.

        title: le titre de la vidéo, dans la langue de la vidéo. Court et
            factuel : il sert à nommer et retrouver la vidéo une fois archivée,
            ce n'est pas l'accroche.
        description: la légende, dans la langue de la vidéo. C'est toi qui
            décides quoi y mettre : l'accroche, ce qu'on apprend, un appel aux
            commentaires. Si le brief demande d'y faire figurer quelque chose
            (un lien, une mention, une phrase), fais-le figurer.
        hashtags: les hashtags avec leur #, du plus spécifique au plus large.
        """
        print(f"  [tool] save_publication({title!r}, {len(description)} car., "
              f"{len(hashtags)} hashtags)")
        draft.publication = Publication(
            title=title, description=description, hashtags=hashtags
        )
        return "Publication enregistrée."

    @tool
    def validate_plan() -> str:
        """Vérifie le plan contre les contraintes de production et le format H3.

        Renvoie la liste des problèmes à corriger, ou 'PLAN VALIDE'.
        À appeler après avoir posé tous les plans, et après chaque correction.
        """
        errors = check_plan(
            draft, limits, config.publication, humanizer, config.language
        )
        result = "PLAN VALIDE" if not errors else "PROBLEMES:\n- " + "\n- ".join(errors)
        print(
            f"  [tool] validate_plan() -> {result.splitlines()[0]} "
            f"({len(errors)} erreur(s))"
        )
        return result

    return [
        list_skills,
        load_skill,
        load_skill_include,
        fetch_source,
        save_treatment,
        add_shot,
        save_publication,
        validate_plan,
    ]


def check_plan(
    draft: Draft,
    limits: PlanConstraints,
    publication: PublicationConstraints,
    humanizer,
    language: str = "",
) -> list[str]:
    """Les problèmes du plan courant, en clair, dans l'ordre où les corriger."""
    errors: list[str] = []

    if draft.treatment is None:
        errors.append("Aucun traitement : appelle save_treatment d'abord.")
    elif language and draft.treatment["language"].lower() != language.lower():
        errors.append(
            f"Langue imposée '{language}', traitement enregistré en "
            f"'{draft.treatment['language']}'. Reprends save_treatment."
        )
    if not draft.shots:
        errors.append("Aucun plan enregistré.")
        return errors

    indexes = sorted(draft.shots)
    if indexes != list(range(1, len(indexes) + 1)):
        errors.append(f"Index non contigus depuis 1 : {indexes}.")

    total = sum(s.seconds for s in draft.shots.values())
    if not limits.min_total_seconds <= total <= limits.max_total_seconds:
        errors.append(
            f"Durée totale {total}s hors cible "
            f"{limits.min_total_seconds}-{limits.max_total_seconds}s."
        )

    roles = [draft.shots[i].role for i in indexes]
    if limits.arc:
        unknown = [r for r in roles if r not in limits.arc]
        if unknown:
            errors.append(
                f"Rôles hors de l'arc {limits.arc} : {sorted(set(unknown))}."
            )
        if roles and roles[0] != limits.arc[0]:
            errors.append(f"Le plan 1 doit être '{limits.arc[0]}', reçu '{roles[0]}'.")
        if roles and roles[-1] != limits.arc[-1]:
            errors.append(
                f"Le dernier plan doit être '{limits.arc[-1]}', reçu '{roles[-1]}'."
            )

    for i in indexes:
        shot = draft.shots[i]
        prompt, low = shot.prompt, shot.prompt.lower()
        tag = f"Plan {i}"

        if not limits.min_shot_seconds <= shot.seconds <= limits.max_shot_seconds:
            errors.append(
                f"{tag}: durée {shot.seconds}s hors bornes "
                f"{limits.min_shot_seconds}-{limits.max_shot_seconds}s "
                f"(le moteur accepte {MIN_SECONDS}-{MAX_SECONDS})."
            )

        missing = [s for s in REQUIRED_SECTIONS if s not in low]
        if missing:
            errors.append(f"{tag}: sections H3 manquantes : {', '.join(missing)}.")

        present = [s for s in INJECTED_SECTIONS if s in low]
        if present:
            errors.append(
                f"{tag}: ne rédige pas {', '.join(present)} — le verrou d'identité "
                f"est préfixé au rendu. Commence à `summary:`."
            )

        if "<subject 1>" not in low:
            errors.append(f"{tag}: le sujet doit être désigné par <Subject 1>.")
        if "(s1)" not in low:
            errors.append(f"{tag}: l'ID de locuteur (S1) est absent.")

        dialogue = re.findall(r"<d>\[(\w+)\](.*?)</d>", prompt, re.DOTALL)
        if not dialogue:
            errors.append(
                f"{tag}: la réplique doit être dans detailed_description, "
                f"balisée <d>[Langue] ... </d>."
            )
        else:
            expected_tag = LANGUAGE_NAMES.get(language.lower()) if language else None
            wrong = {d[0] for d in dialogue if expected_tag and d[0] != expected_tag}
            if wrong:
                errors.append(
                    f"{tag}: balises <d>[{'/'.join(sorted(wrong))}] alors que la "
                    f"langue imposée est '{language}' — écris <d>[{expected_tag}] … </d> "
                    f"et la réplique dans cette langue."
                )
            if shot.spoken_line.strip() not in prompt:
                errors.append(
                    f"{tag}: la réplique de <d> ne reprend pas spoken_line mot pour mot."
                )
            words = len(" ".join(d[1].strip() for d in dialogue).split())
            budget = shot.seconds * limits.words_per_second
            if words > budget * 1.35:
                errors.append(
                    f"{tag}: {words} mots parlés pour {shot.seconds}s "
                    f"(~{budget:.0f} max à {limits.words_per_second} mots/s). "
                    f"Raccourcis ou découpe."
                )

        if humanizer is not None:
            tells = humanizer.check(shot.spoken_line)
            if tells:
                errors.append(
                    f"{tag}: la réplique porte des tells d'écriture IA "
                    f"({'; '.join(tells)}). Applique le skill humanizer et réécris-la."
                )

        described = re.search(
            r"detailed_description:(.*?)(?=overall_soundscape:|$)", prompt, re.DOTALL | re.IGNORECASE
        )
        n_words = len(described.group(1).split()) if described else 0
        if n_words < limits.min_description_words:
            errors.append(
                f"{tag}: detailed_description trop pauvre ({n_words} mots, min "
                f"{limits.min_description_words}). Détaille composition, action, "
                f"expression, geste, mouvement caméra et son."
            )

        leaked = [w for w in limits.forbidden_appearance_words if w in low]
        if leaked:
            errors.append(
                f"{tag}: le prompt décrit l'apparence du sujet "
                f"({', '.join(leaked)}). L'identité est verrouillée en amont : "
                f"décris l'action autour, pas le physique."
            )

    errors += check_publication(draft.publication, publication, humanizer)
    return errors


def check_publication(
    published: "Publication | None",
    limits: PublicationConstraints,
    humanizer,
) -> list[str]:
    """Les problèmes du texte de publication."""
    if published is None:
        return ["Aucune publication : appelle save_publication."]

    errors: list[str] = []
    text, tags = published.description, published.hashtags

    title = published.title.strip()
    if not limits.min_title_chars <= len(title) <= limits.max_title_chars:
        errors.append(
            f"Titre de {len(title)} caractères, hors bornes "
            f"{limits.min_title_chars}-{limits.max_title_chars}."
        )
    elif not slugify(title):
        errors.append(
            f"Titre {title!r} inutilisable : il ne donne aucun nom de dossier "
            f"(que de la ponctuation ou des emojis). Écris-le en mots."
        )

    if not limits.min_chars <= len(text) <= limits.max_chars:
        errors.append(
            f"Description de {len(text)} caractères, hors bornes "
            f"{limits.min_chars}-{limits.max_chars}."
        )
    if not limits.min_hashtags <= len(tags) <= limits.max_hashtags:
        errors.append(
            f"{len(tags)} hashtags, hors bornes "
            f"{limits.min_hashtags}-{limits.max_hashtags}."
        )

    malformed = [t for t in tags if not t.startswith("#") or " " in t.strip()]
    if malformed:
        errors.append(
            f"Hashtags mal formés (un # en tête, pas d'espace) : {malformed}."
        )

    missing = [needle for needle in limits.must_include if needle not in text]
    if missing:
        errors.append(
            f"La description doit contenir : {missing}. Relis le brief et ajoute-les."
        )

    if humanizer is not None:
        tells = humanizer.check(text)
        if tells:
            errors.append(
                f"La description porte des tells d'écriture IA ({'; '.join(tells)}). "
                f"Applique le skill humanizer et réécris-la."
            )

    return errors


SYSTEM = """\
Tu réalises des vidéos verticales courtes. Les méthodes de production de la
maison sont documentées dans des skills : tu les lis, tu ne les inventes pas.

Commence toujours par list_skills, et lis les descriptions en entier. Charge
ensuite avec load_skill : le skill agent qui décrit ton rôle, le skill de style
qui correspond au brief, et TOUT skill transverse qui contraint l'écriture des
répliques. Si un skill renvoie vers un fichier annexe décrivant un format ou
des listes, charge-le avec load_skill_include AVANT d'écrire quoi que ce soit.

Une fois documenté, applique la méthode du style : réunir la matière (avec
fetch_source si le sujet pointe vers une page), dégager l'angle
(save_treatment), puis poser les plans avec add_shot en suivant l'arc du style.

Écris ensuite le texte de publication avec save_publication. C'est toi qui
décides de son contenu : ce qui donne envie de regarder, ce qu'on apprend, un
appel aux commentaires. Mais si le brief demande d'y faire figurer quelque
chose — un lien, une mention, une phrase précise — c'est un ordre, pas une
suggestion.

Les répliques passent un contrôle anti-écriture-IA automatique. Écris-les comme
quelqu'un qui parle.

Chaque prompt doit être riche et explicite : composition, position du sujet,
environnement et lumière, actions et changements d'état, mouvement de caméra,
son, et la réplique exacte. Un résumé d'intention ne suffit pas.

Caméra : par défaut le plan est STABLE (camera_motion="stable"), caméra fixe,
aucun zoom — c'est la règle dès que l'avatar parle (contexte, explication,
core). Ne réserve camera_motion="punchy_zoom" qu'aux plans qui doivent
accrocher l'œil (hook, punch), et décris alors ce zoom vif dans le mouvement de
caméra du prompt. Le moteur n'a pas de prompt négatif : formule tout en positif
(ce que la caméra fait, pas ce qu'elle évite).

Contraintes de ce tournage :
{constraints}

Termine par validate_plan. S'il renvoie des problèmes, corrige avec add_shot
puis revalide. Ne rends ta réponse structurée qu'une fois PLAN VALIDE obtenu.
"""


def describe_constraints(
    limits: PlanConstraints,
    published: PublicationConstraints,
    language_instruction: str = "",
) -> str:
    return "\n".join(
        [
            f"- Durée totale visée : {limits.min_total_seconds}-{limits.max_total_seconds} s.",
            f"- Durée par plan : {limits.min_shot_seconds}-{limits.max_shot_seconds} s.",
            f"- Débit de parole : environ {limits.words_per_second} mots par seconde.",
            f"- detailed_description : au moins {limits.min_description_words} mots.",
            f"- Arc narratif : {' → '.join(limits.arc)}." if limits.arc else "",
            "- Ne décris jamais l'apparence du sujet : elle est verrouillée en amont.",
            (f"- Titre de la vidéo : {published.min_title_chars}-"
            f"{published.max_title_chars} caractères."),
            (f"- Description de publication : {published.min_chars}-{published.max_chars} "
            f"caractères, {published.min_hashtags}-{published.max_hashtags} hashtags."),
            f"- Langue : {language_instruction}" if language_instruction else "",
        ]
    ).strip()


# --- Boucle agentique ---


def plan_video(task: TaskConfig) -> tuple[VideoPlan, Draft, int]:
    """Fait tourner l'agent et renvoie le plan validé."""
    config = task.agent_config
    model = config.models.master_mind
    draft = Draft()

    thinking = {"type": "adaptive"} if config.llm.thinking else {"type": "disabled"}
    llm = ChatAnthropic(
        model=model.model_name,
        base_url=model.base_url,
        api_key=model.token,
        max_tokens=config.llm.max_tokens,
        thinking=thinking,
        output_config={"effort": config.llm.effort},
    )

    agent = create_agent(
        model=llm,
        tools=build_tools(task, draft, load_skills()),
        system_prompt=SYSTEM.format(
            constraints=describe_constraints(
                config.plan,
                config.publication,
                config.language_instruction() if config.language else "",
            )
        ),
        response_format=ProviderStrategy(VideoPlan),
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": config.user_message()}]},
        config={"recursion_limit": config.llm.max_iterations},
    )
    return result["structured_response"], draft, len(result["messages"])


def prepare_avatar(task: TaskConfig) -> Path:
    """Ramène la vidéo de l'avatar et renvoie la frame qui servira de référence.

    Fait à chaque tâche : le bucket est privé, le moteur ne peut pas y aller
    lui-même, et l'avatar a pu être remplacé depuis le dernier rendu. La frame
    est recalculée dès que la vidéo est plus récente qu'elle — `ref2va` ne
    conditionne que sur une image, c'est elle qui porte l'identité.
    """
    config = task.agent_config
    at = config.avatar.reference_frame_s
    moment = "mid" if at is None else f"{at:g}s".replace(".", "_")

    video = storage.download(config.storage, config.avatar.avatar_url)
    # L'instant est dans le nom : changer `reference_frame_s` donne une autre
    # frame, pas la précédente restée en cache.
    frame = video.with_name(f"{video.stem}_reference_{moment}.png")
    if not frame.is_file() or frame.stat().st_mtime < video.stat().st_mtime:
        extract_frame(video, frame, at_s=at)
        print(f"  [avatar] {config.avatar.name} : frame à {moment} -> {frame}")
    return frame


def render_video(task: TaskConfig, plan: VideoPlan) -> list[Path]:
    """Rend chaque plan sur le moteur vidéo du channel."""
    config = task.agent_config
    out = Path(config.render.output_dir) / task.task_id
    reference = prepare_avatar(task)
    lock = identity_lock(config.avatar)
    print(f"\n{'=' * 70}\nRendu de {len(plan.shots)} plans sur {config.models.video_generator.model_name}")
    print(f"Référence d'identité : {reference}")
    print(f"Verrou d'identité : {len(lock)} caractères préfixés à chaque plan")
    print("=" * 70)
    return asyncio.run(
        generate_videos(
            plan.shots,
            avatar=config.avatar,
            reference=reference,
            model=config.models.video_generator,
            render=config.render,
            output_dir=out,
        )
    )


def assemble_video(task: TaskConfig, clips: list[Path]) -> Path:
    """Colle les clips bout à bout et renvoie le montage final.

    L'ordre est celui de `clips`, que `generate_videos` renvoie aligné sur la
    timeline : le rendu est parallèle, la liste ne l'est pas.
    """
    config = task.agent_config
    out = Path(config.render.output_dir) / task.task_id / config.render.final_name
    print(f"\n{'=' * 70}\nMontage de {len(clips)} clips")
    print("=" * 70)
    final = concat_clips(clips, out)
    print(f"{final} — {probe_duration(final):.1f}s")
    return final


def subtitle_video(task: TaskConfig, video: Path) -> Path:
    """Incruste les sous-titres si la tâche les demande, sinon rend la vidéo telle quelle."""
    settings = task.agent_config.subtitles
    if not settings.enabled:
        print("\nSous-titres désactivés pour cette tâche.")
        return video
    print(f"\n{'=' * 70}\nSous-titres ({settings.model}, {settings.device}, {settings.style})")
    print("=" * 70)
    try:
        final, n_words = add_subtitles(video, settings, task.agent_config.language)
    except RuntimeError as exc:
        print(f"!! Sous-titres impossibles : {exc}")
        print(f"   Le montage reste livrable : {video}")
        return video
    if not n_words:
        print("Aucun mot transcrit : le montage sort sans sous-titres.")
    else:
        print(f"{n_words} mots incrustés -> {final}")
    return final


PUBLISHED_VIDEO = "video.mp4"
PUBLISHED_DESCRIPTION = "description.txt"


def publication_text(published: Publication) -> str:
    """Le fichier texte qui accompagne la vidéo publiée.

    Le titre y figure en clair : le dossier ne porte que sa version sans
    accents ni majuscules, et c'est le titre d'origine qu'on republie.
    """
    return "\n\n".join(
        [published.title, published.description, " ".join(published.hashtags)]
    ) + "\n"


def publish_video(task: TaskConfig, plan: VideoPlan, final: Path) -> str:
    """Range la vidéo et sa description dans le stockage, et renvoie le préfixe.

    Disposition : `channel/date/titre/`. La date est celle de la tâche, pas
    celle du rendu : republier une tâche la remet au même endroit au lieu d'en
    semer une copie dans le dossier du jour.
    """
    config = task.agent_config
    channel = slugify(task.channel_config.channel_name)
    title = slugify(plan.publication.title)
    if not channel:
        raise ValueError(
            f"Nom de chaîne inutilisable comme dossier : {task.channel_config.channel_name!r}."
        )

    prefix = f"{channel}/{task.created_at.date().isoformat()}/{title}"
    notes = final.parent / PUBLISHED_DESCRIPTION
    notes.write_text(publication_text(plan.publication), encoding="utf-8")

    print(f"\n{'=' * 70}\nPublication vers s3://{config.storage.bucket}/{prefix}/")
    print("=" * 70)
    storage.upload(config.storage, final, f"{prefix}/{PUBLISHED_VIDEO}")
    storage.upload(config.storage, notes, f"{prefix}/{PUBLISHED_DESCRIPTION}")
    return f"s3://{config.storage.bucket}/{prefix}"


def produce_video(task: TaskConfig, plan: VideoPlan) -> tuple[list[Path], Path]:
    """Rend tous les plans, monte, puis sous-titre."""
    clips = render_video(task, plan)
    return clips, subtitle_video(task, assemble_video(task, clips))


# --- Chargement d'une tâche ---




def main() -> None:
    load_dotenv(".env.local")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path, help="fichier JSON d'une TaskConfig")
    parser.add_argument("--render", action="store_true", help="lancer le rendu vidéo")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="pousser la vidéo montée et sa description dans le stockage objet",
    )
    args = parser.parse_args()

    task = load_task(args.task)
    plan, draft, n_messages = plan_video(task)

    print(f"\n{'=' * 70}")
    print(f"{task.channel_config.channel_name} / {task.task_id}")
    print(f"{n_messages} messages | skills chargés : {draft.loaded}")
    print("=" * 70)
    print(f"Angle  : {plan.angle}")
    print(
        f"Langue : {plan.language} | Durée : {plan.total_duration_s}s "
        f"| {len(plan.shots)} plans"
    )
    for shot in plan.shots:
        print(f"\n{'-' * 70}")
        print(f"[{shot.index}] {shot.role.upper()} — {shot.seconds}s")
        print(f"Réplique : {shot.spoken_line}")
        print(f"\n{shot.prompt}")

    print(f"\n{'=' * 70}\nPUBLICATION\n{'=' * 70}")
    print(f"Titre : {plan.publication.title}\n")
    print(plan.publication.description)
    print()
    print(" ".join(plan.publication.hashtags))

    if not args.render:
        payload = build_payload(
            plan.shots[0],
            model_name=task.agent_config.models.video_generator.model_name,
            avatar=task.agent_config.avatar,
            render=task.agent_config.render,
            reference=prepare_avatar(task),
        )
        print(f"\n{'=' * 70}\nPayload du plan 1 (--render pour lancer le rendu)")
        print("=" * 70)
        print(
            json.dumps(
                {**payload, "prompt": payload["prompt"][:160] + " […]"},
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    clips, final = produce_video(task, plan)
    for shot, path in zip(plan.shots, clips):
        print(f"[{shot.index}] {shot.role:<12} {shot.seconds}s  {path}")
    print(f"\nVidéo finale : {final}")
    if args.publish:
        print(f"Publiée sur {publish_video(task, plan, final)}")


if __name__ == "__main__":
    main()
