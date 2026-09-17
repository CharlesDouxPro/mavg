"""Récupération de pages web.

Comme `providers/minimax_h3.py`, ce fichier dispatche sur `ScraperConfig.provider` :
changer de service de scraping = changer un champ en base, pas une ligne de code.
"""

from linkup import LinkupClient

from task_config import ScraperConfig

TRUNCATION_NOTE = "[…tronqué à {limit} caractères]"


def fetch(config: ScraperConfig, url: str) -> str:
    """Renvoie le contenu de `url` en markdown, ou la raison de l'échec.

    Ne lève pas : l'appelant est un outil d'agent, et un message d'erreur lisible
    lui permet de se rabattre sur une autre source. Une exception, elle, tuerait
    la boucle.
    """
    if config.provider != "linkup":
        return f"Provider de scraping inconnu : {config.provider!r}."
    if not config.token:
        return (
            "Aucun token de scraping configuré (agent_config.scraper.token). "
            "Travaille à partir du brief et des paramètres."
        )

    try:
        response = LinkupClient(api_key=config.token, base_url=config.base_url).fetch(
            url=url,
            mode=config.mode,
            render_js=config.render_js,
            extract_images=config.extract_images,
            include_raw_html=False,
            timeout=config.timeout_s,
        )
    except Exception as exc:
        return f"Échec du scraping de {url} : {type(exc).__name__} — {exc}"

    content = (response.markdown or "").strip()
    if not content:
        return f"{url} n'a renvoyé aucun texte exploitable."
    if len(content) > config.max_chars:
        note = TRUNCATION_NOTE.format(limit=config.max_chars)
        content = f"{content[: config.max_chars]}\n\n{note}"
    return content
