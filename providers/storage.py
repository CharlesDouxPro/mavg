"""Le stockage objet : ramène en local les assets privés d'une tâche.

Le bucket est privé, et le moteur vidéo va chercher ses références par un GET
sans authentification : il ne peut donc pas lire le bucket lui-même. L'asset
descend ici, et c'est le fichier local qui part au rendu.

Comme `providers/scraper.py`, ce fichier dispatche sur `StorageConfig.provider` :
changer de fournisseur = changer un champ en base. Scaleway Object Storage parle
le protocole S3, d'où boto3 ; seuls l'endpoint et la région le distinguent d'AWS.
"""

import mimetypes
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from task_config import StorageConfig


def _credentials(config: StorageConfig) -> tuple[str, str]:
    """Les clés de la config, ou celles de l'environnement si elle n'en porte pas."""
    return (
        config.access_key or os.getenv("SCW_ACCESS_KEY", ""),
        config.secret_key or os.getenv("SCW_SECRET_KEY", ""),
    )


def _client(config: StorageConfig):
    access_key, secret_key = _credentials(config)
    if not (access_key and secret_key):
        raise RuntimeError(
            "Aucune clé de stockage : renseigne agent_config.storage.access_key / "
            "secret_key, ou SCW_ACCESS_KEY / SCW_SECRET_KEY dans l'environnement."
        )
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            read_timeout=config.timeout_s,
            connect_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def parse_uri(config: StorageConfig, uri: str) -> tuple[str, str] | None:
    """(bucket, clé) si `uri` désigne un objet du stockage, sinon None.

    Quatre écritures d'une même chose : `s3://bucket/clé`, l'URL du bucket
    (`https://bucket.s3.région…/clé`), l'URL de l'endpoint
    (`https://s3.région…/bucket/clé`), et la clé nue. Le reste est une URL
    publique, que l'appelant ira chercher en HTTP.
    """
    uri = uri.strip()
    if not uri:
        raise ValueError("avatar_url est vide : aucune référence à télécharger.")

    parsed = urlparse(uri)
    path = unquote(parsed.path).lstrip("/")

    if parsed.scheme == "s3":
        return parsed.netloc, path
    if not parsed.scheme:
        return config.bucket, path

    host = parsed.netloc.split(":")[0]
    endpoint_host = urlparse(config.endpoint_url).netloc.split(":")[0]
    if host == f"{config.bucket}.{endpoint_host}":
        return config.bucket, path
    if host == endpoint_host:
        bucket, _, key = path.partition("/")
        return (bucket, key) if key else None
    return None


def _is_fresh(dest: Path, size: int, modified: datetime) -> bool:
    """Le fichier local est-il déjà celui de l'objet distant ?

    Taille identique et copie locale plus récente que l'objet. Ni l'une ni
    l'autre ne prouve l'égalité octet pour octet, mais un avatar remplacé change
    de date, et c'est ce qu'on veut détecter.
    """
    if not dest.is_file():
        return False
    stat = dest.stat()
    local_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return stat.st_size == size and local_mtime >= modified


def download(config: StorageConfig, uri: str) -> Path:
    """Télécharge l'asset désigné par `uri` et renvoie son chemin local.

    Le cache est partagé entre les tâches : un avatar inchangé n'est téléchargé
    qu'une fois, mais il est revérifié à chaque tâche — un HEAD coûte moins cher
    qu'un rendu fait avec la mauvaise référence.
    """
    if config.provider != "scaleway":
        raise ValueError(f"Fournisseur de stockage inconnu : {config.provider!r}.")

    located = parse_uri(config, uri)
    if located is None:
        return _download_http(config, uri)

    bucket, key = located
    if not key:
        raise ValueError(f"{uri!r} ne désigne aucun objet (clé manquante).")

    dest = Path(config.cache_dir) / bucket / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    client = _client(config)

    try:
        head = client.head_object(Bucket=bucket, Key=key)
        if _is_fresh(dest, head["ContentLength"], head["LastModified"]):
            print(f"  [storage] s3://{bucket}/{key} déjà à jour ({dest})")
            return dest
        print(f"  [storage] s3://{bucket}/{key} -> {dest} "
              f"({head['ContentLength'] / 1e6:.1f} Mo)")
        client.download_file(bucket, key, str(dest))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NoSuchBucket"):
            raise FileNotFoundError(f"s3://{bucket}/{key} introuvable.") from exc
        if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            raise PermissionError(
                f"Accès refusé à s3://{bucket}/{key} : vérifie les clés Scaleway."
            ) from exc
        raise
    except BotoCoreError as exc:
        raise RuntimeError(f"Stockage injoignable ({config.endpoint_url}) : {exc}") from exc

    return dest


def _download_http(config: StorageConfig, url: str) -> Path:
    """Le cas d'un asset hébergé hors du bucket : un GET public, même cache."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Référence d'avatar illisible : {url!r}.")

    dest = Path(config.cache_dir) / parsed.netloc / unquote(parsed.path).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size:
        print(f"  [storage] {url} déjà en cache ({dest})")
        return dest

    print(f"  [storage] {url} -> {dest}")
    with httpx.stream(
        "GET", url, timeout=config.timeout_s, follow_redirects=True
    ) as resp:
        resp.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in resp.iter_bytes(8192):
                handle.write(chunk)
    return dest


MAX_SLUG_CHARS = 80


def slugify(text: str) -> str:
    """Un fragment de clé lisible, tiré d'un texte libre.

    Accents dépliés, tout ce qui n'est pas lettre ou chiffre devient un tiret :
    une clé d'objet se lit dans une console, se colle dans une URL et se tape à
    la main. Renvoie une chaîne vide si le texte ne donne aucun mot.
    """
    folded = unicodedata.normalize("NFKD", text)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()[
        :MAX_SLUG_CHARS
    ].strip("-")


def upload(config: StorageConfig, path: Path | str, key: str) -> str:
    """Envoie un fichier local sous `key` et renvoie son URI `s3://`.

    Le type MIME est déduit de l'extension et posé sur l'objet : sans lui,
    Scaleway sert le .mp4 en `binary/octet-stream`, que le navigateur télécharge
    au lieu de le lire.
    """
    if config.provider != "scaleway":
        raise ValueError(f"Fournisseur de stockage inconnu : {config.provider!r}.")

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Rien à publier : {path} n'existe pas.")
    key = key.strip("/")
    if not key:
        raise ValueError("Clé de publication vide.")

    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if content_type.startswith("text/"):
        content_type += "; charset=utf-8"

    print(f"  [storage] {path} -> s3://{config.bucket}/{key} "
          f"({path.stat().st_size / 1e6:.1f} Mo, {content_type})")
    try:
        _client(config).upload_file(
            str(path),
            config.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            raise PermissionError(
                f"Écriture refusée sur s3://{config.bucket}/{key} : "
                f"vérifie les droits des clés Scaleway."
            ) from exc
        raise
    except BotoCoreError as exc:
        raise RuntimeError(f"Stockage injoignable ({config.endpoint_url}) : {exc}") from exc

    return f"s3://{config.bucket}/{key}"


def presigned_url(config: StorageConfig, uri: str, filename: str, expires_s: int) -> str:
    """Un lien de téléchargement signé vers un objet du bucket privé."""
    located = parse_uri(config, uri)
    if located is None:
        raise ValueError(f"{uri!r} ne désigne aucun objet du stockage.")
    bucket, key = located
    return _client(config).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=expires_s,
    )
