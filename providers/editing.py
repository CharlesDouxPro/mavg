"""Le montage : tout ce qui passe par ffmpeg.

Un seul point d'entrée vers le sous-processus (`run`), pour que l'erreur soit
lisible quand ffmpeg échoue — sinon on récupère un code de retour nu.

Ce fichier ne porte que les contraintes dures de l'outil (le format de la liste
du démultiplexeur `concat`, les codecs d'un MP4 lisible partout). Ce qui se
règle sans redéploiement — où écrire, sous quel nom — vient de
`task_config.RenderSettings`.
"""

import shutil
import subprocess
from pathlib import Path

FFMPEG_TIMEOUT = 600
"""Plafond d'un appel ffmpeg. Un concat de 60 s de vidéo prend quelques
secondes ; au-delà de dix minutes, c'est un blocage, pas une lenteur."""


def run(cmd: list[str], timeout: int = FFMPEG_TIMEOUT) -> subprocess.CompletedProcess:
    """Lance une commande et lève une erreur lisible si elle échoue."""
    if shutil.which(cmd[0]) is None:
        raise RuntimeError(f"{cmd[0]} introuvable dans le PATH (brew install ffmpeg).")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timeout ({timeout}s) : {' '.join(cmd[:3])}...") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"échec : {' '.join(cmd[:3])}...\n{proc.stderr[-800:]}")
    return proc


def probe_duration(path: Path | str) -> float:
    """Durée d'un média en secondes, 0.0 si ffprobe ne sait pas la lire."""
    proc = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nokey=1:noprint_wrappers=1",
            str(path),
        ]
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def probe_size(path: Path | str) -> tuple[int, int]:
    """(largeur, hauteur) en px du premier flux vidéo, (720, 1280) par défaut.

    Sert à dimensionner les sous-titres : libass raisonne en unités de script,
    pas en pixels, donc la taille de police se calcule sur la hauteur réelle.
    """
    proc = run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ]
    )
    try:
        width, height = proc.stdout.strip().split("x")[:2]
        return int(width), int(height)
    except ValueError:
        return 720, 1280


def extract_audio(video: Path | str, out: Path | str) -> Path:
    """Extrait la piste audio en mono 16 kHz : le format attendu par Whisper."""
    out = Path(out)
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(out)])
    return out


def _quote(path: Path) -> str:
    """Un chemin tel que le démultiplexeur `concat` l'accepte.

    Chemin absolu (la liste peut vivre ailleurs que les clips), entre
    apostrophes, une apostrophe littérale s'écrivant `'\\''`.
    """
    return "'" + str(path).replace("'", "'\\''") + "'"


def concat_clips(clips: list[Path | str], out: Path | str) -> Path:
    """Assemble les clips dans l'ordre de la liste et renvoie le chemin final.

    L'ordre vient de la liste, pas d'un index : il ne peut pas être faux.
    On réencode plutôt que de copier les flux — `-c copy` exige des paramètres
    strictement identiques d'un clip à l'autre, et un seul clip qui sort avec un
    profil différent casse tout le montage.

    `-fflags +genpts` régénère les horodatages : les clips arrivent chacun avec
    leur propre base de temps, et sans ça l'audio dérive à partir de la
    deuxième jonction.
    """
    if not clips:
        raise ValueError("Aucun clip à assembler.")

    missing = [str(c) for c in clips if not Path(c).is_file()]
    if missing:
        raise FileNotFoundError(f"Clips introuvables : {', '.join(missing)}")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    list_path = out.parent / "_concat_list.txt"
    list_path.write_text(
        "".join(f"file {_quote(Path(c).resolve())}\n" for c in clips), encoding="utf-8"
    )

    run(
        [
            "ffmpeg", "-y",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            str(out),
        ]
    )
    list_path.unlink(missing_ok=True)
    return out


def extract_frame(video: Path | str, out: Path | str, at_s: float | None = None) -> Path:
    """Tire une image fixe de `video` et renvoie son chemin.

    `at_s` vide : le milieu du clip. Le début d'une vidéo d'avatar est souvent un
    fondu ou une pose de démarrage, et une référence d'identité floue fait
    dériver tous les plans qui en dépendent.

    `-ss` avant `-i` : ffmpeg cherche la keyframe au lieu de décoder depuis le
    début, et `-accurate_seek` recale ensuite sur l'instant demandé.
    """
    video, out = Path(video), Path(out)
    if not video.is_file():
        raise FileNotFoundError(f"Vidéo de référence introuvable : {video}")

    duration = probe_duration(video)
    if at_s is None:
        at_s = duration / 2
    if duration and not 0 <= at_s < duration:
        raise ValueError(
            f"Frame demandée à {at_s:g}s, hors de la vidéo ({duration:.1f}s)."
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg", "-y",
            "-accurate_seek", "-ss", f"{at_s:.3f}",
            "-i", str(video),
            "-frames:v", "1", "-q:v", "2",
            str(out),
        ]
    )
    if not out.is_file() or not out.stat().st_size:
        raise RuntimeError(f"Aucune frame extraite de {video} à {at_s:g}s.")
    return out
