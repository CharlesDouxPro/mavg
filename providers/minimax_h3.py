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

import httpx
from pydantic import BaseModel, Field

from task_config import Avatar, ModelConfig, RenderSettings

# Contraintes du moteur : les changer casse l'appel.
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


def identity_lock(avatar: Avatar) -> str:
    """Bloc `subject_definitions` + `retention_analysis` figeant l'apparence.

    Préfixé à l'identique sur CHAQUE plan : c'est ce qui empêche la coupe, la
    tenue ou les accessoires de dériver d'un clip à l'autre. Les plans écrits
    par l'agent n'ont donc pas à décrire le personnage, et ne doivent pas le
    faire — ils décrivent l'action autour d'une apparence déjà verrouillée.
    """
    look = (avatar.appearance or avatar.description or "").strip()
    if not look:
        return ""
    return (
        "subject_definitions:\n"
        "<Subject 1> is the on-camera subject from the reference image. Fixed, "
        f"invariant appearance (matches the reference exactly): {look}\n\n"
        "retention_analysis:\n"
        "<Subject 1>'s appearance is FULLY PRESERVED and UNCHANGED in every shot - "
        "face, age, hair, facial hair, skin tone, wardrobe and any worn accessories "
        "match the reference and the definition above exactly. Do NOT re-age, "
        "restyle, change the outfit, or add/remove props (glasses, hat, headphones, "
        "microphone) unless the shot description below explicitly requires it.\n\n"
    )


def reference_uri(reference: Path) -> str:
    """Le chemin de la frame de référence, tel que le serveur l'accepte.

    `file://` en absolu : le serveur résout les conditions localement, il doit
    donc tourner sur la même machine que ce process (ou monter le même dossier).
    """
    reference = Path(reference)
    if not reference.is_file():
        raise FileNotFoundError(f"Frame de référence introuvable : {reference}")
    return reference.resolve().as_uri()


def build_payload(
    shot: MinimaxShot,
    *,
    model_name: str,
    avatar: Avatar,
    render: RenderSettings,
    reference: Path,
) -> dict:
    """Traduit un plan en payload `POST /v1/videos` pour le serveur SGLang.

    Le verrou d'identité est calculé ici, pas passé en argument : un plan rendu
    sans verrou n'est pas exprimable. `reference` est la frame tirée de la vidéo
    d'avatar ; `ref2va` ne conditionne que sur une image.
    """
    duration = float(max(MIN_SECONDS, min(MAX_SECONDS, shot.seconds)))
    return {
        "model": model_name,
        "prompt": identity_lock(avatar) + shot.prompt,
        "seconds": int(round(duration)),
        "task": TASK,
        "conditions": [
            {"type": "image", "uri": reference_uri(reference), "role": "reference"}
        ],
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
            )
            resp = await client.post("/videos", json=payload)
            resp.raise_for_status()
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
