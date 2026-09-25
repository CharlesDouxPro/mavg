"""Construit le catalogue de voix de référence dans le bucket.

Chaque voix est un échantillon de parole généré une fois par ElevenLabs (Voice Design),
puis rangé pour toujours sous `voices/<langue>/<sexe>/<âge>/<prénom>.wav`, à côté de sa
fiche `.json`. Un avatar la désigne par son URI ; MiniMax-H3 la reçoit en référence audio
à chaque plan, et c'est ce qui garde la même voix d'un plan à l'autre.

Le script ne génère que ce qui manque : relancer après avoir ajouté une langue ne touche
pas aux voix existantes. Une voix n'est jamais régénérée sous le même nom : un avatar qui
la référence changerait de voix.

    uv run python build_voices.py                  # tout le catalogue
    uv run python build_voices.py --language fr    # une langue
    uv run python build_voices.py --limit 1        # un seul dossier, pour essayer
    uv run python build_voices.py --dry-run        # ce qui serait généré, sans appel payant
"""

import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from providers import storage
from task_config import StorageConfig

load_dotenv(".env.local")

PREFIX = "voices"
DESIGN_URL = "https://api.elevenlabs.io/v1/text-to-voice/design"
OUTPUT_FORMAT = "mp3_44100_192"
VARIANTS = 3
"""Voice Design renvoie 3 variantes d'une description : chacune devient une voix."""
MIN_SECONDS, MAX_SECONDS = 2.0, 15.0
"""Bornes d'une référence audio pour MiniMax-H3 ref2va."""
SAMPLE_RATE = 32000
"""Fréquence native de l'audio H3 : le moteur n'aura rien à rééchantillonner."""

AGE_RANGES = {
    "20-30": "twenties",
    "30-40": "thirties",
    "40-50": "forties",
    "50-60": "fifties",
    "60-70": "sixties",
}

# Par langue : l'origine et l'accent décrits à Voice Design, le texte lu, et les prénoms,
# VARIANTS par tranche d'âge dans l'ordre de AGE_RANGES (des prénoms de la génération).
LANGUAGES: dict[str, dict[str, Any]] = {
    "fr": {
        "origin": "a native French speaker from Paris",
        "accent": "standard metropolitan French",
        "text": (
            "Bonjour à tous. Aujourd'hui, je vous explique simplement ce qui se passe, étape "
            "par étape, pour que vous puissiez vous faire votre propre avis. On commence tout "
            "de suite, et vous allez voir, c'est plus clair qu'il n'y paraît."
        ),
        "male": ["lucas", "hugo", "theo", "antoine", "julien", "mathieu", "nicolas",
                 "sebastien", "olivier", "laurent", "philippe", "eric", "bernard", "michel",
                 "alain"],
        "female": ["lea", "chloe", "manon", "camille", "julie", "marion", "sophie", "celine",
                   "aurelie", "isabelle", "nathalie", "sandrine", "monique", "francoise",
                   "catherine"],
    },
    "en": {
        "origin": "a native English speaker from the United States",
        "accent": "General American English",
        "text": (
            "Hi everyone. Today I'm going to walk you through what's really going on, step by "
            "step, so you can make up your own mind. Let's get right into it, and you'll see, "
            "it's much simpler than it looks."
        ),
        "male": ["ethan", "noah", "liam", "james", "ryan", "tyler", "michael", "david",
                 "christopher", "robert", "steven", "mark", "richard", "george", "william"],
        "female": ["emma", "olivia", "ava", "jessica", "ashley", "megan", "jennifer", "amanda",
                   "rachel", "susan", "karen", "linda", "margaret", "barbara", "dorothy"],
    },
    "es": {
        "origin": "a native Spanish speaker from Madrid, Spain",
        "accent": "standard Castilian Spanish",
        "text": (
            "Hola a todos. Hoy os explico de forma sencilla lo que está pasando, paso a paso, "
            "para que podáis sacar vuestras propias conclusiones. Empezamos ya mismo, y vais a "
            "ver que es más fácil de lo que parece."
        ),
        "male": ["pablo", "alvaro", "diego", "javier", "sergio", "raul", "carlos", "fernando",
                 "alberto", "jose", "manuel", "antonio", "francisco", "juan", "rafael"],
        "female": ["lucia", "paula", "alba", "laura", "marta", "cristina", "elena", "raquel",
                   "beatriz", "carmen", "pilar", "rosa", "dolores", "mercedes", "concepcion"],
    },
    "de": {
        "origin": "a native German speaker from Germany",
        "accent": "standard High German",
        "text": (
            "Hallo zusammen. Heute erkläre ich euch ganz einfach, was gerade passiert, Schritt "
            "für Schritt, damit ihr euch selbst eine Meinung bilden könnt. Wir legen direkt "
            "los, und ihr werdet sehen: Es ist einfacher, als es aussieht."
        ),
        "male": ["leon", "finn", "jonas", "lukas", "tobias", "florian", "stefan", "markus",
                 "andreas", "juergen", "frank", "ralf", "klaus", "dieter", "horst"],
        "female": ["mia", "hannah", "lena", "anna", "katharina", "julia", "sabine", "claudia",
                   "petra", "gabriele", "birgit", "ute", "ingrid", "helga", "ursula"],
    },
    "it": {
        "origin": "a native Italian speaker from Italy",
        "accent": "standard Italian",
        "text": (
            "Ciao a tutti. Oggi vi spiego in modo semplice cosa sta succedendo, passo dopo "
            "passo, così potete farvi un'idea tutta vostra. Cominciamo subito, e vedrete che "
            "è molto più semplice di quanto sembri."
        ),
        "male": ["lorenzo", "matteo", "leonardo", "marco", "luca", "andrea", "alessandro",
                 "stefano", "roberto", "giuseppe", "massimo", "paolo", "giovanni", "franco",
                 "bruno"],
        "female": ["giulia", "sofia", "aurora", "francesca", "chiara", "valentina", "federica",
                   "silvia", "elisa", "paola", "roberta", "antonella", "giuseppina", "rosaria",
                   "carla"],
    },
}

SEXES = {"male": ("man", "his", "His"), "female": ("woman", "her", "Her")}


def _check_catalog() -> None:
    """Un prénom = une voix, dans tout le catalogue : un doublon rendrait l'URI ambiguë."""
    names = [n for lang in LANGUAGES.values() for sex in SEXES for n in lang[sex]]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"Prénoms en double dans le catalogue : {duplicates}")
    for code, lang in LANGUAGES.items():
        for sex in SEXES:
            if len(lang[sex]) != len(AGE_RANGES) * VARIANTS:
                raise ValueError(f"{code}/{sex} : il faut {len(AGE_RANGES) * VARIANTS} prénoms.")


def describe(code: str, sex: str, decade: str) -> str:
    noun, pronoun, pronoun_cap = SEXES[sex]
    lang = LANGUAGES[code]
    return (
        f"A {noun} in {pronoun} {decade}, {lang['origin']}. {pronoun_cap} voice is natural "
        f"and clear, with a {lang['accent']} accent. Conversational, confident delivery, like "
        "a presenter explaining the news on social media. Clean studio recording, no "
        "background noise, no music."
    )


def design(description: str, text: str) -> list[dict]:
    """Les variantes générées par Voice Design, audio en base64."""
    for attempt in range(3):
        resp = requests.post(
            DESIGN_URL,
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
            params={"output_format": OUTPUT_FORMAT},
            json={"voice_description": description, "text": text},
            timeout=180,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        if not resp.ok:
            raise RuntimeError(f"ElevenLabs HTTP {resp.status_code} : {resp.text[:300]}")
        return resp.json()["previews"]
    raise RuntimeError(f"ElevenLabs indisponible (HTTP {resp.status_code}) après 3 essais.")


def to_reference_wav(mp3: Path, wav: Path) -> float:
    """Convertit en WAV mono 32 kHz et renvoie sa durée en secondes."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-c:a", "pcm_s16le", str(wav)],
        check=True,
    )
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(wav)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def build_folder(config: StorageConfig, existing: set[str], code: str, sex: str,
                 age_range: str, workdir: Path) -> int:
    """Génère les voix manquantes d'un dossier ; renvoie le nombre de voix ajoutées."""
    index = list(AGE_RANGES).index(age_range)
    names = LANGUAGES[code][sex][index * VARIANTS:(index + 1) * VARIANTS]
    folder = f"{PREFIX}/{code}/{sex}/{age_range}"
    missing = [n for n in names if f"{folder}/{n}.wav" not in existing]
    if not missing:
        return 0

    description = describe(code, sex, AGE_RANGES[age_range])
    text = LANGUAGES[code]["text"]
    print(f"\n{folder} : {', '.join(missing)}")
    previews = design(description, text)

    added = 0
    for name, preview in zip(missing, previews):
        mp3, wav = workdir / f"{name}.mp3", workdir / f"{name}.wav"
        mp3.write_bytes(base64.b64decode(preview["audio_base_64"]))
        duration = to_reference_wav(mp3, wav)
        if not MIN_SECONDS <= duration <= MAX_SECONDS:
            print(f"  !! {name} : {duration:.1f}s hors [{MIN_SECONDS:g}, {MAX_SECONDS:g}] s, écartée")
            continue
        key = f"{folder}/{name}.wav"
        sheet = workdir / f"{name}.json"
        sheet.write_text(json.dumps({
            "name": name,
            "language": code,
            "sex": sex,
            "age_range": age_range,
            "uri": f"s3://{config.bucket}/{key}",
            "duration_s": round(duration, 2),
            "description": description,
            "text": text,
            "source": "elevenlabs-voice-design",
            "generated_voice_id": preview.get("generated_voice_id"),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        # La fiche d'abord : un .wav présent dans le bucket a toujours la sienne.
        storage.upload(config, sheet, f"{folder}/{name}.json")
        storage.upload(config, wav, key)
        existing.add(key)
        added += 1
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--language", action="append", choices=sorted(LANGUAGES),
                        help="Langue(s) à construire ; toutes par défaut.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Nombre maximal de dossiers à générer (0 : pas de limite).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Liste ce qui serait généré, sans appeler ElevenLabs.")
    args = parser.parse_args()

    _check_catalog()
    config = StorageConfig()
    existing = storage.list_keys(config, f"{PREFIX}/")
    folders = [
        (code, sex, age_range)
        for code in args.language or LANGUAGES
        for sex in SEXES
        for age_range in AGE_RANGES
    ]

    if args.dry_run:
        for code, sex, age_range in folders:
            index = list(AGE_RANGES).index(age_range)
            names = LANGUAGES[code][sex][index * VARIANTS:(index + 1) * VARIANTS]
            folder = f"{PREFIX}/{code}/{sex}/{age_range}"
            todo = [n for n in names if f"{folder}/{n}.wav" not in existing]
            print(f"{folder:28} {'à générer : ' + ', '.join(todo) if todo else 'complet'}")
        return

    added = processed = failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for code, sex, age_range in folders:
            if args.limit and processed >= args.limit:
                break
            try:
                count = build_folder(config, existing, code, sex, age_range, Path(tmp))
            except Exception as exc:  # un dossier raté ne doit pas arrêter le catalogue
                print(f"  !! {code}/{sex}/{age_range} : {exc}")
                failed += 1
                processed += 1
                continue
            if count:
                processed += 1
                added += count
    print(f"\n{added} voix ajoutées, {failed} dossier(s) en échec. "
          f"Catalogue : s3://{config.bucket}/{PREFIX}/")


if __name__ == "__main__":
    main()
