"""Sous-titres incrustés, mot à mot, transcrits en local.

Aucune API : faster-whisper tourne sur la machine, GPU CUDA s'il y en a un,
CPU sinon. On transcrit l'audio RÉEL du montage plutôt que de caler le script
écrit — MiniMax-H3 génère la voix et peut phraser une réplique autrement, si
bien qu'un alignement sur le script dériverait.

Ce fichier porte le format des sous-titres (SRT, ASS) ; ce qui se règle sans
redéploiement vit dans `task_config.SubtitleSettings`.
"""

import threading
from pathlib import Path

from providers.editing import extract_audio, probe_size, run
from task_config import SubtitleSettings

# Le modèle coûte cher à charger : une fois par process, partagé entre les runs.
_MODEL = None
_MODEL_KEY: tuple[str, str, str] | None = None
_LOCK = threading.Lock()


def _get_model(settings: SubtitleSettings):
    """Charge le modèle, ou rend celui déjà en mémoire s'il a les mêmes réglages."""
    global _MODEL, _MODEL_KEY
    key = (settings.model, settings.device, settings.compute_type)
    if _MODEL is None or _MODEL_KEY != key:
        with _LOCK:
            if _MODEL is None or _MODEL_KEY != key:
                from faster_whisper import WhisperModel

                _MODEL = WhisperModel(
                    settings.model,
                    device=settings.device,
                    compute_type=settings.compute_type,
                )
                _MODEL_KEY = key
    return _MODEL


def transcribe_words(audio: Path, settings: SubtitleSettings, language: str = "") -> list[dict]:
    """L'audio -> une liste de mots horodatés [{text, start, end}].

    `vad_filter` coupe les silences : sans lui, Whisper invente du texte sur les
    passages muets entre deux plans.
    """
    segments, _info = _get_model(settings).transcribe(
        str(audio),
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
    )
    return [
        {"text": word.word.strip(), "start": word.start, "end": word.end}
        for segment in segments
        for word in (segment.words or [])
        if word.word.strip()
    ]


def group_captions(words: list[dict], settings: SubtitleSettings) -> list[list[dict]]:
    """Regroupe les mots en légendes courtes, format social.

    On coupe sur quatre signaux : trop de caractères, légende trop longue,
    silence trop marqué, ou ponctuation forte — une phrase finie ne déborde pas
    sur la suivante.
    """
    captions: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        if current:
            chars = sum(len(w["text"]) + 1 for w in current) + len(word["text"])
            duration = word["end"] - current[0]["start"]
            gap = word["start"] - current[-1]["end"]
            ends_sentence = current[-1]["text"][-1:] in ".!?…"
            if (
                chars > settings.max_chars
                or duration > settings.max_duration_s
                or gap > settings.max_gap_s
                or ends_sentence
            ):
                captions.append(current)
                current = []
        current.append(word)
    if current:
        captions.append(current)
    return captions


def _srt_timestamp(seconds: float) -> str:
    """Secondes -> 'HH:MM:SS,mmm'."""
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _ass_timestamp(seconds: float) -> str:
    """Secondes -> 'H:MM:SS.cc' (centisecondes, format ASS)."""
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        secs, centis = secs + 1, 0
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_color(hex_color: str) -> str:
    """'RRGGBB' -> '&H00BBGGRR' : ASS écrit en BGR, alpha 00 = opaque."""
    value = (hex_color or "").lstrip("#").strip()
    if len(value) != 6:
        value = "FFFFFF"
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}".upper()


def write_srt(captions: list[list[dict]], out: Path) -> Path:
    """Légendes -> .srt, en majuscules. Pas de mot colorié : le SRT ne sait pas."""
    blocks = []
    for number, caption in enumerate(captions, start=1):
        text = " ".join(w["text"] for w in caption).strip().upper()
        start, end = _srt_timestamp(caption[0]["start"]), _srt_timestamp(caption[-1]["end"])
        blocks.append(f"{number}\n{start} --> {end}\n{text}\n")
    out.write_text("\n".join(blocks), encoding="utf-8")
    return out


def write_ass(
    captions: list[list[dict]], out: Path, settings: SubtitleSettings, size: tuple[int, int]
) -> Path:
    """Légendes -> .ass karaoké : le mot en cours est colorié, le reste non.

    Un event par mot, donc une légende de N mots produit N lignes qui se
    succèdent. Les tailles sont proportionnelles à la hauteur : libass raisonne
    en unités de script, pas en pixels.
    """
    width, height = size
    base, highlight = _ass_color(settings.base_color), _ass_color(settings.highlight_color)
    font_size = max(24, round(height * 0.05))
    outline = max(2, round(height * 0.004))
    margin_v = round(height * 0.12)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,Arial,{font_size},{base},{base},&H00000000,&H00000000,"
            f"-1,0,0,0,100,100,0,0,1,{outline},0,2,40,40,{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text",
    ]

    for caption in captions:
        # Les accolades délimitent les balises ASS : un mot qui en contient
        # casserait le parsing.
        shown = [w["text"].strip().upper().replace("{", "(").replace("}", ")") for w in caption]
        for position, word in enumerate(caption):
            start = word["start"]
            end = caption[position + 1]["start"] if position + 1 < len(caption) else caption[-1]["end"]
            if end <= start:
                end = start + 0.05
            parts = [
                f"{{\\c{highlight}&}}{text}{{\\c{base}&}}" if index == position else text
                for index, text in enumerate(shown)
            ]
            lines.append(
                f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},"
                f"Default,,0,0,,{' '.join(parts)}"
            )

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


FULL_BUILDS = [
    "ffmpeg",
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
]
"""Binaires essayés dans l'ordre quand aucun n'est imposé. Le ffmpeg par défaut
de Homebrew est compilé sans libass ; `ffmpeg-full` l'a."""

_FILTERS: dict[str, set[str]] = {}


def available_filters(ffmpeg_bin: str) -> set[str]:
    """Les filtres que ce binaire ffmpeg sait appliquer, mis en cache."""
    if ffmpeg_bin not in _FILTERS:
        proc = run([ffmpeg_bin, "-hide_banner", "-filters"])
        _FILTERS[ffmpeg_bin] = {
            line.split()[1]
            for line in proc.stdout.splitlines()
            if len(line.split()) > 2 and line.startswith(" ")
        }
    return _FILTERS[ffmpeg_bin]


def resolve_ffmpeg(filter_name: str, preferred: str = "") -> str:
    """Le premier ffmpeg qui sait appliquer `filter_name`.

    Un binaire imposé dans la config gagne, et son absence de filtre est une
    erreur : on ne se rabat pas silencieusement sur un autre build que celui
    demandé.
    """
    candidates = [preferred] if preferred else FULL_BUILDS
    tried = []
    for binary in candidates:
        try:
            if filter_name in available_filters(binary):
                return binary
        except RuntimeError:
            tried.append(f"{binary} (introuvable)")
            continue
        tried.append(f"{binary} (sans {filter_name})")
    raise RuntimeError(
        f"Aucun ffmpeg ne sait appliquer le filtre '{filter_name}' — essayés : "
        f"{', '.join(tried)}. Le build Homebrew par défaut est compilé sans libass ; "
        f"`brew install ffmpeg-full` en fournit un, ou renseigne son chemin dans "
        f"agent_config.subtitles.ffmpeg_bin."
    )


def _burn(video: Path, subtitles: Path, out: Path, filter_name: str, ffmpeg_bin: str) -> Path:
    """Incruste le fichier de sous-titres dans l'image. L'audio est recopié tel quel."""
    ffmpeg_bin = resolve_ffmpeg(filter_name, ffmpeg_bin)
    # Le filtre relit son argument : ':' et '\\' d'un chemin cassent le parsing.
    escaped = str(subtitles).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    run(
        [
            ffmpeg_bin, "-y",
            "-i", str(video),
            "-vf", f"{filter_name}=filename='{escaped}'",
            "-c:a", "copy",
            str(out),
        ]
    )
    return out


def add_subtitles(
    video: Path, settings: SubtitleSettings, language: str = ""
) -> tuple[Path, int]:
    """Incruste les sous-titres et renvoie (vidéo finale, nombre de mots).

    Désactivé, ou aucun mot transcrit : la vidéo d'entrée est renvoyée telle
    quelle. Un montage sans sous-titres reste un livrable ; échouer ici ne doit
    pas jeter la vidéo.
    """
    if not settings.enabled:
        return video, 0

    video = Path(video)
    audio = extract_audio(video, video.with_name("subs_audio.wav"))
    try:
        words = transcribe_words(audio, settings, language)
    finally:
        audio.unlink(missing_ok=True)

    if not words:
        return video, 0

    captions = group_captions(words, settings)
    out = video.with_name(f"{video.stem}_subtitled.mp4")
    if settings.style == "karaoke":
        path = write_ass(captions, video.with_name("subs.ass"), settings, probe_size(video))
        return _burn(video, path, out, "ass", settings.ffmpeg_bin), len(words)
    path = write_srt(captions, video.with_name("subs.srt"))
    return _burn(video, path, out, "subtitles", settings.ffmpeg_bin), len(words)
