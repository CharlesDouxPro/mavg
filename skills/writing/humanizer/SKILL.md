---
name: humanizer
description: |
  Anti-« écriture IA » pour les scripts PARLÉS de shorts verticaux (TikTok / Reels / Shorts).
  Adapté de Wikipedia "Signs of AI writing" (WikiProject AI Cleanup) au cas d'usage réel :
  on garde les tells qui s'entendent à l'oral, on laisse tomber les règles propres à la prose
  encyclopédique. À charger par tout agent qui écrit des lignes parlées, quelle que soit la
  langue de sortie.
compatibility: "Transverse. Se combine avec n'importe quel skill de style (tiktok-news-article-presentation, etc.) et avec script_writer. Ne remplace pas le style. Il contraint l'écriture des lignes parlées, pas la direction ni le cadrage."
metadata:
  trigger-words: [humanizer, écriture naturelle, anti IA, ton humain, script parlé, voix off, sonne faux, sent l'IA]
---

# Humanizer

Les lignes parlées doivent sonner comme quelqu'un qui parle, pas comme un texte généré lu à voix
haute. À l'oral, les tics d'écriture IA s'entendent encore plus qu'à l'écrit : le spectateur ne
saurait pas les nommer, mais il décroche.

Ce skill contraint **les lignes parlées uniquement**. Le cadrage, le mouvement de caméra et le
sound design ne sont pas concernés.

## Guide

Écris comme un humain, pas comme une IA. Un script qui « sent » l'IA fait fuir. Évite ces tells :

- **VOCABULAIRE IA** : bannis les mots-tics « plonger/plongeons », « il est important de noter »,
  « souligne/met en lumière », « témoigne de », « paysage » (au figuré), « riche », « vibrant »,
  « fascinant », « incontournable », « au cœur de », « véritable », « à l'ère de ». Dis les choses
  simplement.
- **PAS D'EMPHASE CREUSE** sur l'importance ou l'héritage : pas de « marque un tournant »,
  « restera dans les mémoires », « change la donne », « plus qu'un X, c'est un Y ». Donne le fait,
  pas son auréole.
- **PAS DE PARTICIPES EMPILÉS** pour faire profond : évite les fins de phrase en
  « ..., soulignant... », « ..., rappelant... », « ..., témoignant de... ». Fais une vraie phrase.
- **PAS DE RÈGLE DE TROIS** automatique : n'aligne pas systématiquement trois adjectifs ou trois
  idées pour faire complet. Deux suffisent souvent ; une seule, bien choisie, frappe plus fort.
- **VARIE LA LONGUEUR DES PHRASES.** Alterne court et long. Une IA écrit des phrases toutes de la
  même taille. Mais n'enchaîne pas non plus les phrases hyper courtes en rafale pour dramatiser :
  une punchline isolée, oui ; dix d'affilée, non.
- **PAS DE PARALLÉLISMES NÉGATIFS** ni de négations en fin de phrase : évite
  « ce n'est pas seulement..., c'est... » et les bouts collés du genre « sans chichi »,
  « sans détour ».
- **PAS D'ATTRIBUTIONS VAGUES** : pas de « les experts affirment », « selon certaines sources »,
  « des observateurs notent ». Nomme la source réelle de l'article, ou n'attribue rien.
- **PAS DE CONCLUSION BATEAU** ni de « signposting » : ne finis pas sur « l'avenir s'annonce
  radieux » / « une chose est sûre... », et n'annonce pas ce que tu vas dire
  (« découvrons ensemble », « voici ce qu'il faut savoir »). Dis-le, c'est tout.
- **PAS D'INVENTION** : aucun fait, chiffre, date, nom ou citation qui ne soit pas dans l'article.
- **TON HUMAIN** : une vraie voix, un point de vue, un peu d'aspérité. Guillemets droits, pas
  d'emoji.
- **ZÉRO TIRET CADRATIN (—) ni tiret demi-cadratin (–)** : c'est le tell IA le plus reconnaissable.
  Remplace par un point, une virgule, deux-points ou une parenthèse. Relis et supprime-les tous.

## Contrôle automatique

`includes/humanizer.py` expose les listes exploitables par un validateur : `BANNED_PHRASES`
(expressions à bannir), `BANNED_CHARS` (ponctuation interdite) et `HUMANIZER_GUIDE` (le bloc
ci-dessus, à injecter tel quel dans un system prompt d'écriture).

Les tells non mécanisables (règle de trois, longueur de phrase homogène, conclusion bateau) se
relisent à l'oreille : lis la ligne à voix haute. Si elle sonne comme un communiqué, réécris-la.
