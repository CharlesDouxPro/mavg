"""Schéma des plans MiniMax-H3 et transport vers le serveur SGLang.

Ce déploiement ne sert que la tâche `ref2va` : l'avatar est la référence
d'identité, le modèle rend l'image ET son audio en une passe. Les répliques
parlées et le sound design vivent DANS le prompt.

La référence d'identité — comme l'image de chaque autre sujet — est un fichier
LOCAL : le bucket qui héberge les avatars est privé, et le moteur télécharge
ses conditions par un GET sans authentification. `providers/storage.py` la
ramène, `providers/editing.py` en tire la frame, et c'est son chemin qui
arrive ici.

Un personnage inventé par l'agent (`Character`) n'a pas d'image à télécharger :
sa fiche sert d'abord à un plan de casting, rendu ici comme les autres, et c'est
la frame tirée de ce plan qui devient sa référence.

Découpage : `MinimaxShot` porte ce que l'agent décide (le prompt, la durée, la
réplique). Tout ce qui doit rester constant d'un plan à l'autre — la seed,
l'avatar, la résolution, les pas de diffusion — vient de `RenderSettings` et
n'est jamais choisi par le modèle. Une règle de prompt est une suggestion ; un
paramètre absent du schéma est une garantie.

Ce fichier ne porte que les contraintes DURES du moteur : la tâche `ref2va` et
les bornes 5-15 s. Tout ce qui se règle sans redéploiement vit dans
`task_config.RenderSettings`.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from task_config import (
    Avatar,
    ModelConfig,
    RenderSettings,
    cast_number,
    reference_number,
)

# Contraintes du moteur : les changer casse l'appel. Le serveur EXIGE `task` à
# chaque requête (tâches supportées : fl2va, ref2va, t2va) ; ce déploiement ne
# sert que ref2va — l'image de référence conditionne l'identité.
TASK = "ref2va"
MIN_SECONDS = 5
MAX_SECONDS = 15
MAX_PICTURES = 9
"""Images de référence que ref2va accepte sur une requête : avatar, références et
personnages inventés compris."""
CASTING_SECONDS = MIN_SECONDS
"""Le plan de casting ne sert qu'à en tirer une frame : le plus court que le moteur rende."""


class MinimaxShot(BaseModel):
    """Un plan tel que l'agent le rend."""

    index: int = Field(
        ge=1, description="Position du plan dans la timeline, à partir de 1."
    )
    role: str = Field(
        description="Rôle du plan dans l'arc narratif du style suivi "
        "(ex. hook, context, core, escalation, punch)."
    )
    seconds: int = Field(
        ge=MIN_SECONDS,
        le=MAX_SECONDS,
        description=f"Durée du clip. Le moteur accepte {MIN_SECONDS}-{MAX_SECONDS} s ; "
        f"le style peut resserrer.",
    )
    spoken_line: str = Field(
        description="La réplique, dans la langue demandée. Doit apparaître mot "
        "pour mot dans <d>[...]</d> à l'intérieur du prompt. Vide pour un plan "
        "muet, si le channel en autorise : l'avatar est à l'écran sans parler."
    )
    prompt: str = Field(
        description="Prompt H3 full-reference, en anglais, sections `summary:`, "
        "`detailed_description:`, `overall_soundscape:`, `non_diegetic_music:`. "
        "Ne rédige PAS `subject_definitions:` ni `retention_analysis:` : le verrou "
        "d'identité est préfixé au rendu."
    )
    camera_motion: Literal["stable", "punchy_zoom"] = Field(
        default="stable",
        description="Mouvement de caméra du plan. 'stable' = plan fixe, aucun "
        "zoom : c'est OBLIGATOIRE dès que l'avatar parle (context, explication, "
        "core), et c'est la valeur par défaut. 'punchy_zoom' = un zoom vif et "
        "bref, réservé aux plans qui doivent accrocher l'œil (hook, punch). En "
        "'stable', une consigne de caméra fixe est préfixée au prompt (le moteur "
        "n'a pas de prompt négatif) ; en 'punchy_zoom', décris le zoom dans le "
        "mouvement de caméra du prompt.",
    )

    @property
    def silent(self) -> bool:
        """Un plan sans réplique : l'avatar y joue la scène sans parler."""
        return not self.spoken_line.strip()


class Character(BaseModel):
    """Un personnage inventé pour UNE vidéo, qu'aucune image ne montre.

    Sa fiche le crée : elle sert d'abord au plan de casting, dont on tire son image
    de référence, puis elle est recopiée mot pour mot dans le verrou d'identité de
    chaque plan. Même fiche et même image d'un plan à l'autre : même personnage.
    """

    name: str = Field(
        description="Comment tu le désignes dans tes notes (« la fillette », « le bras "
        "robot »). Le moteur ne le lit pas."
    )
    appearance: str = Field(
        description="Sa fiche, en anglais, assez précise pour qu'un inconnu le redessine "
        "à l'identique : âge apparent, morphologie, carnation, visage, coiffure, tenue "
        "complète (coupes, couleurs, matières), accessoires portés ; pour un objet, une "
        "machine ou une créature : forme, taille, matières, couleurs, détails distinctifs. "
        "Le style de rendu s'il n'est pas photoréaliste. Rien que du visible."
    )
    voice: str = Field(
        default="",
        description="Sa voix, en anglais, s'il parle : âge, hauteur, timbre, débit, "
        "accent. Vide s'il ne parle pas.",
    )


class Publication(BaseModel):
    """Le texte qui accompagne la vidéo une fois publiée."""

    title: str = Field(
        description="Le titre de la vidéo, dans la langue de la vidéo. Court et "
        "factuel : il nomme la vidéo dans l'archive, ce n'est pas l'accroche."
    )
    description: str = Field(
        description="La légende de publication, dans la langue de la vidéo. "
        "Contient ce que le brief demande d'y mettre."
    )
    hashtags: list[str] = Field(
        description="Les hashtags, avec leur #, du plus spécifique au plus large."
    )


class VideoPlan(BaseModel):
    """Le plan de tournage complet, prêt à être rendu clip par clip."""

    angle: str = Field(description="L'angle de la vidéo en une phrase.")
    language: str = Field(description="Langue parlée, code ISO (ex. 'fr').")
    skills_used: list[str] = Field(
        description="Les skills chargés pour produire ce plan."
    )
    total_duration_s: int
    shots: list[MinimaxShot]
    publication: Publication
    cast: list[Character] = Field(
        default_factory=list,
        description="Les personnages inventés pour cette vidéo, dans l'ordre du casting.",
    )


# Liaison de la référence audio, au format H3 (`h3_ref_format.txt`) : sans elle, le
# moteur ne sait pas si `<Audio 1>` est une voix, une musique ou une piste à recopier.
VOICE_DEFINITION = "<Audio 1> is the voice-timbre reference for <Subject 1> (S1)."
VOICE_RETENTION = (
    "<Audio 1>: reference - <Subject 1> (S1) speaks every line with <Audio 1>'s voice "
    "timbre, accent and delivery, identically in every shot, without copying the "
    "original signal."
)


def _preserved(number: int) -> str:
    """La ligne de `retention_analysis` d'un sujet secondaire, vu dans `<Picture N>`."""
    return (
        f"<Subject {number}> (appears in every shot that features it): fully_preserved - "
        f"its species or kind, shape, proportions, colours, markings, outfit and "
        f"accessories match <Picture {number}> exactly, identically in every shot. Do "
        f"NOT redesign, restyle or swap it for a similar-looking subject."
    )


def identity_lock(
    avatar: Avatar | None,
    voiced: bool = False,
    references: int = 0,
    cast: Sequence[Character] = (),
) -> str:
    """Bloc `subject_definitions` + `retention_analysis` figeant l'apparence et la voix.

    Préfixé à l'identique sur CHAQUE plan : c'est ce qui empêche la coupe, la
    tenue ou les accessoires de dériver d'un clip à l'autre. Les plans écrits
    par l'agent n'ont donc pas à décrire le personnage, et ne doivent pas le
    faire — ils décrivent l'action autour d'une apparence déjà verrouillée.
    `voiced` : une référence audio accompagne le plan, on la lie à la voix du sujet.
    `references` : le nombre d'images envoyées après celle de l'avatar ; chacune
    définit son `<Subject N>`, vu dans `<Picture N>` (`reference_number`).
    `cast` : les personnages inventés ; sans avatar, le premier tient son rôle. Leur
    fiche, seule description qu'ils aient, est recopiée ici à côté de leur image.
    """
    lead = cast[0] if avatar is None and cast else None
    if avatar is not None:
        look = (avatar.appearance or avatar.description or "").strip()
    else:
        look = lead.appearance.strip() if lead else ""
    definitions, retention = [], []
    if look or references or cast:
        # Dès qu'il y a plusieurs images, « l'image de référence » ne désigne plus
        # rien : on nomme celle de l'avatar.
        source = "<Picture 1>" if references or cast else "the reference image"
        definition = f"<Subject 1> is the on-camera subject from {source}."
        if look:
            definition += f" Fixed, invariant appearance (matches the reference exactly): {look}"
        if lead and lead.voice.strip():
            definition += (
                f" <Subject 1> (S1) speaks with one fixed voice in every shot: {lead.voice.strip()}"
            )
        definitions.append(definition)
        retention.append(
            "<Subject 1>'s appearance is FULLY PRESERVED and UNCHANGED in every shot - "
            "face, age, hair, facial hair, skin tone, wardrobe and any worn accessories "
            "match the reference and the definition above exactly. Do NOT re-age, "
            "restyle, change the outfit, or add/remove props (glasses, hat, headphones, "
            "microphone) unless the shot description below explicitly requires it."
        )
    for position in range(references):
        number = reference_number(position)
        definitions.append(
            f"<Subject {number}> is the subject shown in <Picture {number}>. Fixed, "
            f"invariant appearance: it matches <Picture {number}> exactly."
        )
        retention.append(_preserved(number))
    for position, character in enumerate(cast):
        number = cast_number(position, avatar is not None, references)
        if number == 1:
            continue
        definition = (
            f"<Subject {number}> is the subject shown in <Picture {number}>. Fixed, "
            f"invariant appearance (matches <Picture {number}> exactly): "
            f"{character.appearance.strip()}"
        )
        if character.voice.strip():
            definition += f" Whenever it speaks, it uses one fixed voice: {character.voice.strip()}"
        definitions.append(definition)
        retention.append(_preserved(number))
    if voiced:
        definitions.append(VOICE_DEFINITION)
        retention.append(VOICE_RETENTION)
    if not definitions:
        return ""
    return (
        "subject_definitions:\n" + "\n".join(definitions) + "\n\n"
        "retention_analysis:\n" + "\n".join(retention) + "\n\n"
    )


def reference_path(reference: Path, what: str = "Frame de référence") -> str:
    """Le CHEMIN LOCAL absolu d'une référence (frame, voix), pour `conditions[].uri`.

    Le loader de matériel H3 lit l'`uri` directement sur le disque : ce process
    doit donc tourner sur la même machine que le serveur (ou monter le même
    dossier). On renvoie un chemin nu plutôt qu'un `file://` : le loader accepte
    les deux, mais le chemin nu évite l'encodage des espaces et accents du nom.
    """
    reference = Path(reference)
    if not reference.is_file():
        raise FileNotFoundError(f"{what} introuvable : {reference}")
    return str(reference.resolve())


# Consigne de caméra fixe, préfixée au prompt d'un plan `stable`. Le checkpoint
# est distillé CFG (une seule branche positive, pas de prompt négatif) : la
# stabilité doit donc être formulée EN POSITIF, comme le verrou d'identité.
STABLE_CAMERA_LOCK = (
    "camera_direction:\n"
    "The camera is static and locked off on a tripod for the entire shot: no "
    "zoom in or out, no push-in, no dolly, no pan, no tilt, no handheld drift. "
    "The framing stays completely fixed for the whole shot.\n\n"
)


# Préfixé à un plan muet, après la caméra : faute de prompt négatif, on dit ce qu'on
# entend (l'ambiance) plutôt que ce qu'on ne veut pas (la parole).
SILENT_LOCK = (
    "audio_direction:\n"
    "<Subject 1> keeps their lips closed for the entire shot. The soundtrack carries "
    "only the ambient sound of the scene and the music.\n\n"
)


def camera_lock(camera_motion: str) -> str:
    """Le bloc de caméra préfixé au prompt, selon `camera_motion`.

    Sur un plan `stable`, on impose une caméra fixe : le plan ne doit pas bouger,
    que l'avatar parle ou non. Sur `punchy_zoom`, rien n'est préfixé — le zoom vif est
    décrit par l'agent dans le prompt du plan.
    """
    return STABLE_CAMERA_LOCK if camera_motion == "stable" else ""


def pictures(
    reference: Path | None, references: Sequence[Path] = (), cast_frames: Sequence[Path] = ()
) -> list[Path]:
    """Les images d'un plan, dans l'ordre de leurs `<Picture N>`.

    `reference` est la frame de l'avatar ; sans avatar, la frame de casting du premier
    personnage prend sa place. Les références fournies suivent, puis les autres
    personnages : l'ordre de `reference_number` et `cast_number`.
    """
    if reference is None:
        return [*cast_frames[:1], *references, *cast_frames[1:]]
    return [reference, *references, *cast_frames]


def _request(prompt: str, conditions: list[dict], seconds: float, *, model_name: str, render: RenderSettings) -> dict:
    """Le corps de `POST /v1/videos`, commun aux plans et aux castings."""
    duration = float(max(MIN_SECONDS, min(MAX_SECONDS, seconds)))
    return {
        "model": model_name,
        "task": TASK,
        "prompt": prompt,
        "conditions": conditions,
        "seconds": int(round(duration)),
        "target": {
            "short_edge": render.short_edge,
            "aspect_ratio": render.aspect_ratio,
            "duration_seconds": duration,
        },
        "num_outputs_per_prompt": 1,
        "num_inference_steps": render.num_inference_steps,
        "flow_shift": render.flow_shift,
        "audio_flow_shift": render.audio_flow_shift,
        "seed": render.seed,
    }


def build_payload(
    shot: MinimaxShot,
    *,
    model_name: str,
    avatar: Avatar | None,
    render: RenderSettings,
    reference: Path | None,
    voice: Path | None = None,
    references: Sequence[Path] = (),
    cast: Sequence[Character] = (),
    cast_frames: Sequence[Path] = (),
) -> dict:
    """Traduit un plan en payload JSON `POST /v1/videos` pour le serveur SGLang.

    Champs alignés sur le validateur canonique MiniMax H3 (`request_validation.py`).
    Le serveur EXIGE `task`, `prompt`, `conditions` (≥1 entrée pour ref2va) et
    `target` ; il REFUSE `negative_prompt` (checkpoint distillé CFG, une seule
    branche positive). `target.short_edge` DOIT valoir 768.

    Les références passent par `conditions`, en `uri` = CHEMIN LOCAL nu (le
    loader H3 sait lire un chemin local ou `file://`, mais pas `s3://` ; le chemin
    nu évite l'encodage des espaces/accents du nom de fichier) : la frame porte
    l'identité visuelle, `references` les autres sujets, `cast_frames` la frame de
    casting de chaque personnage de `cast` (rangées par `pictures` : le moteur
    numérote ses images `<Picture N>` comme elles arrivent), `voice` (optionnelle)
    la voix, la même à chaque plan parlé. Le prompt est préfixé par le verrou
    d'identité PUIS le verrou de caméra — la stabilité passe par le positif, faute
    de prompt négatif.

    Un plan muet part sans la voix (une référence audio liée à « S1 speaks every
    line » pousserait le moteur à faire parler l'avatar quand même) et avec
    `SILENT_LOCK`, qui dit en positif que l'avatar se tait.
    """
    if len(cast_frames) != len(cast):
        raise ValueError(
            f"{len(cast)} personnages pour {len(cast_frames)} frames de casting : le casting "
            "doit être rendu avant les plans."
        )
    images = pictures(reference, references, cast_frames)
    if not images:
        raise ValueError("Aucune image pour <Subject 1> : ni avatar, ni personnage inventé.")
    if shot.silent:
        voice = None
    conditions = [
        {
            "type": "image",
            "uri": reference_path(image, "Frame de référence" if not n else "Image de référence"),
            "role": "reference",
        }
        for n, image in enumerate(images)
    ]
    if voice is not None:
        conditions.append(
            {"type": "audio", "uri": reference_path(voice, "Voix de référence"), "role": "reference"}
        )
    lock = identity_lock(avatar, voiced=voice is not None, references=len(references), cast=cast)
    silence = SILENT_LOCK if shot.silent else ""
    prompt = lock + camera_lock(shot.camera_motion) + silence + shot.prompt
    return _request(prompt, conditions, shot.seconds, model_name=model_name, render=render)


def casting_prompt(character: Character) -> str:
    """Le prompt du plan de casting : le personnage seul, immobile, sur fond uni.

    Sa fiche est la seule chose qui le décrive ; c'est elle que l'image doit montrer, et
    elle que chaque plan relira dans le verrou d'identité. Fond uni et lumière égale :
    la frame servira de référence d'identité, le décor des plans ne doit pas en hériter.

    ref2va exige au moins une référence ; faute d'image et de voix, c'est un fond sonore
    de pièce calme (`<Audio 1>`) qui la tient. Personne ne parle.
    """
    return (
        "subject_definitions:\n"
        "<Audio 1> is a quiet studio room-tone reference; it carries no voice and no music.\n\n"
        "summary:\n"
        "[reference generation + audio reference] A character reference shot: one single "
        "subject, alone and almost still against a plain backdrop, shown clearly so that its "
        "appearance can be reproduced exactly in later shots.\n\n"
        "retention_analysis:\n"
        "<Audio 1>: weak_reference - only the quiet room-tone atmosphere is retained.\n\n"
        "detailed_description:\n"
        "The target video is a clean character reference shot with soft, even studio "
        "lighting and true-to-life colours.\n"
        "[Shot 1] The camera is static and locked off at eye level for the entire shot. A "
        "single subject is alone in the centre of the frame against a plain light-grey studio "
        "backdrop, framed as a medium shot so that it fills most of the frame and its face, "
        f"if it has one, is sharp and fully visible. The subject: {character.appearance.strip()} "
        "It stays almost still, facing the camera with a calm, neutral expression and its "
        "mouth closed, with only small idle movements such as breathing and blinking. Nothing "
        "else is in the frame and nobody speaks.\n\n"
        "overall_soundscape:\n"
        "Quiet studio room tone throughout, following <Audio 1>.\n\n"
        "non_diegetic_music:\n"
        "N/A"
    )


def casting_payload(
    character: Character, *, model_name: str, render: RenderSettings, ambience: Path
) -> dict:
    """La requête du plan de casting d'un personnage : sa fiche, et un fond sonore pour seule référence."""
    conditions = [
        {"type": "audio", "uri": reference_path(ambience, "Fond sonore du casting"), "role": "reference"}
    ]
    return _request(
        casting_prompt(character), conditions, CASTING_SECONDS, model_name=model_name, render=render
    )


async def render_jobs(
    jobs: Sequence[tuple[str, dict, Path]], *, model: ModelConfig, render: RenderSettings
) -> list[Path]:
    """Rend chaque requête `(nom, payload, destination)` et renvoie les .mp4, dans l'ordre de `jobs`.

    L'endpoint SGLang est asynchrone : on soumet le job, on poll son statut,
    puis on télécharge le contenu. Le sémaphore borne le nombre de clips en vol
    (le serveur sérialise de toute façon le rendu sur le GPU).
    """
    sem = asyncio.Semaphore(render.concurrency)

    async with httpx.AsyncClient(
        base_url=model.base_url,
        headers={"Authorization": f"Bearer {model.token}"} if model.token else {},
        timeout=httpx.Timeout(3600.0, connect=90.0),
        limits=httpx.Limits(max_connections=render.concurrency),
    ) as client:

        async def submit(name: str, payload: dict) -> str:
            resp = await client.post("/videos", json=payload)
            if resp.is_error:
                # raise_for_status masque le corps ; c'est là qu'est le motif
                # (« task is required », « negative_prompt is not supported »…).
                raise RuntimeError(
                    f"SGLang a refusé {name} (HTTP {resp.status_code}) : {resp.text[:500]}"
                )
            job_id = resp.json().get("id")
            if not job_id:
                raise RuntimeError(f"SGLang : réponse sans id ({resp.text[:200]})")
            return job_id

        async def wait(job_id: str) -> None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + render.timeout_s
            while True:
                resp = await client.get(f"/videos/{job_id}")
                resp.raise_for_status()
                status = resp.json().get("status")
                if status == "completed":
                    return
                if status == "failed":
                    raise RuntimeError(f"SGLang job {job_id} échoué : {resp.text[:300]}")
                if loop.time() > deadline:
                    raise TimeoutError(
                        f"SGLang job {job_id} : {render.timeout_s:g}s dépassées "
                        f"(dernier statut : {status})"
                    )
                await asyncio.sleep(render.poll_interval_s)

        async def download(job_id: str, dest: Path) -> Path:
            async with client.stream(
                "GET", f"/videos/{job_id}/content", follow_redirects=True
            ) as resp:
                resp.raise_for_status()
                with dest.open("wb") as handle:
                    async for chunk in resp.aiter_bytes(8192):
                        handle.write(chunk)
            return dest

        async def run(name: str, payload: dict, dest: Path) -> Path:
            async with sem:
                job_id = await submit(name, payload)
                await wait(job_id)
                return await download(job_id, dest)

        return list(await asyncio.gather(*(run(*job) for job in jobs)))


async def generate_videos(
    plans: list[MinimaxShot],
    *,
    avatar: Avatar | None,
    reference: Path | None,
    voice: Path | None = None,
    references: Sequence[Path] = (),
    cast: Sequence[Character] = (),
    cast_frames: Sequence[Path] = (),
    model: ModelConfig,
    render: RenderSettings,
    output_dir: Path | str,
) -> list[Path]:
    """Rend chaque plan et renvoie les .mp4, dans l'ordre de la timeline.

    Les payloads sont construits avant le premier envoi : une référence manquante
    arrête le rendu avant qu'un seul clip ne parte au GPU.
    """
    dest_dir = Path(output_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        (
            f"le plan {shot.index}",
            build_payload(
                shot,
                model_name=model.model_name,
                avatar=avatar,
                render=render,
                reference=reference,
                voice=voice,
                references=references,
                cast=cast,
                cast_frames=cast_frames,
            ),
            dest_dir / f"minimax_{shot.index}.mp4",
        )
        for shot in plans
    ]
    return await render_jobs(jobs, model=model, render=render)


async def generate_casting(
    cast: Sequence[Character],
    *,
    ambience: Path,
    model: ModelConfig,
    render: RenderSettings,
    output_dir: Path | str,
) -> list[Path]:
    """Rend le plan de casting de chaque personnage et renvoie les .mp4, dans l'ordre de `cast`."""
    dest_dir = Path(output_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        (
            f"le casting de {character.name}",
            casting_payload(character, model_name=model.model_name, render=render, ambience=ambience),
            dest_dir / f"casting_{position}.mp4",
        )
        for position, character in enumerate(cast, start=1)
    ]
    return await render_jobs(jobs, model=model, render=render)
