"""Schéma des plans MiniMax-H3 et transport vers le serveur SGLang.

Ce déploiement ne sert que la tâche `ref2va` : l'avatar est la référence
d'identité, le modèle rend l'image ET son audio en une passe. Les répliques
parlées et le sound design vivent DANS le prompt.

La référence d'identité est un fichier LOCAL : le bucket qui héberge les
avatars est privé, et le moteur télécharge ses conditions par un GET sans
authentification. `providers/storage.py` la ramène, `providers/editing.py` en
tire la frame, et c'est son chemin qui arrive ici.

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
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from task_config import Avatar, ModelConfig, RenderSettings

# Contraintes du moteur : les changer casse l'appel. Le serveur EXIGE `task` à
# chaque requête (tâches supportées : fl2va, ref2va, t2va) ; ce déploiement ne
# sert que ref2va — l'image de référence conditionne l'identité.
TASK = "ref2va"
MIN_SECONDS = 5
MAX_SECONDS = 15


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
        "pour mot dans <d>[...]</d> à l'intérieur du prompt."
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


# Liaison de la référence audio, au format H3 (`h3_ref_format.txt`) : sans elle, le
# moteur ne sait pas si `<Audio 1>` est une voix, une musique ou une piste à recopier.
VOICE_DEFINITION = "<Audio 1> is the voice-timbre reference for <Subject 1> (S1)."
VOICE_RETENTION = (
    "<Audio 1>: reference - <Subject 1> (S1) speaks every line with <Audio 1>'s voice "
    "timbre, accent and delivery, identically in every shot, without copying the "
    "original signal."
)


def identity_lock(avatar: Avatar, voiced: bool = False) -> str:
    """Bloc `subject_definitions` + `retention_analysis` figeant l'apparence et la voix.

    Préfixé à l'identique sur CHAQUE plan : c'est ce qui empêche la coupe, la
    tenue ou les accessoires de dériver d'un clip à l'autre. Les plans écrits
    par l'agent n'ont donc pas à décrire le personnage, et ne doivent pas le
    faire — ils décrivent l'action autour d'une apparence déjà verrouillée.
    `voiced` : une référence audio accompagne le plan, on la lie à la voix du sujet.
    """
    look = (avatar.appearance or avatar.description or "").strip()
    definitions, retention = [], []
    if look:
        definitions.append(
            "<Subject 1> is the on-camera subject from the reference image. Fixed, "
            f"invariant appearance (matches the reference exactly): {look}"
        )
        retention.append(
            "<Subject 1>'s appearance is FULLY PRESERVED and UNCHANGED in every shot - "
            "face, age, hair, facial hair, skin tone, wardrobe and any worn accessories "
            "match the reference and the definition above exactly. Do NOT re-age, "
            "restyle, change the outfit, or add/remove props (glasses, hat, headphones, "
            "microphone) unless the shot description below explicitly requires it."
        )
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
    "The framing stays completely fixed while the subject speaks.\n\n"
)


def camera_lock(camera_motion: str) -> str:
    """Le bloc de caméra préfixé au prompt, selon `camera_motion`.

    Sur un plan `stable`, on impose une caméra fixe : l'avatar parle, le plan ne
    doit pas bouger. Sur `punchy_zoom`, rien n'est préfixé — le zoom vif est
    décrit par l'agent dans le prompt du plan.
    """
    return STABLE_CAMERA_LOCK if camera_motion == "stable" else ""


def build_payload(
    shot: MinimaxShot,
    *,
    model_name: str,
    avatar: Avatar,
    render: RenderSettings,
    reference: Path,
    voice: Path | None = None,
) -> dict:
    """Traduit un plan en payload JSON `POST /v1/videos` pour le serveur SGLang.

    Champs alignés sur le validateur canonique MiniMax H3 (`request_validation.py`).
    Le serveur EXIGE `task`, `prompt`, `conditions` (≥1 entrée pour ref2va) et
    `target` ; il REFUSE `negative_prompt` (checkpoint distillé CFG, une seule
    branche positive). `target.short_edge` DOIT valoir 768.

    Les références passent par `conditions`, en `uri` = CHEMIN LOCAL nu (le
    loader H3 sait lire un chemin local ou `file://`, mais pas `s3://` ; le chemin
    nu évite l'encodage des espaces/accents du nom de fichier) : la frame porte
    l'identité visuelle, `voice` (optionnelle) la voix, la même à chaque plan. Le
    prompt est préfixé par le verrou d'identité PUIS le verrou de caméra — la
    stabilité passe par le positif, faute de prompt négatif.
    """
    duration = float(max(MIN_SECONDS, min(MAX_SECONDS, shot.seconds)))
    conditions = [{"type": "image", "uri": reference_path(reference), "role": "reference"}]
    if voice is not None:
        conditions.append(
            {"type": "audio", "uri": reference_path(voice, "Voix de référence"), "role": "reference"}
        )
    lock = identity_lock(avatar, voiced=voice is not None)
    return {
        "model": model_name,
        "task": TASK,
        "prompt": lock + camera_lock(shot.camera_motion) + shot.prompt,
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


async def generate_videos(
    plans: list[MinimaxShot],
    *,
    avatar: Avatar,
    reference: Path,
    voice: Path | None = None,
    model: ModelConfig,
    render: RenderSettings,
    output_dir: Path | str,
) -> list[Path]:
    """Rend chaque plan et renvoie les .mp4, dans l'ordre de la timeline.

    L'endpoint SGLang est asynchrone : on soumet le job, on poll son statut,
    puis on télécharge le contenu. Le sémaphore borne le nombre de clips en vol
    (le serveur sérialise de toute façon le rendu sur le GPU).
    """
    dest_dir = Path(output_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(render.concurrency)

    async with httpx.AsyncClient(
        base_url=model.base_url,
        headers={"Authorization": f"Bearer {model.token}"} if model.token else {},
        timeout=httpx.Timeout(3600.0, connect=90.0),
        limits=httpx.Limits(max_connections=render.concurrency),
    ) as client:

        async def submit(shot: MinimaxShot) -> str:
            payload = build_payload(
                shot,
                model_name=model.model_name,
                avatar=avatar,
                render=render,
                reference=reference,
                voice=voice,
            )
            resp = await client.post("/videos", json=payload)
            if resp.is_error:
                # raise_for_status masque le corps ; c'est là qu'est le motif
                # (« task is required », « negative_prompt is not supported »…).
                raise RuntimeError(
                    f"SGLang a refusé le plan {shot.index} "
                    f"(HTTP {resp.status_code}) : {resp.text[:500]}"
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

        async def run(shot: MinimaxShot) -> Path:
            async with sem:
                job_id = await submit(shot)
                await wait(job_id)
                return await download(job_id, dest_dir / f"minimax_{shot.index}.mp4")

        return list(await asyncio.gather(*(run(p) for p in plans)))
