---
name: tiktok-news-article-presentation
description: |
  Turn a news article (URL or text) into a vertical 9:16 TikTok where ONE fixed avatar presents the news straight to camera as an urgent scoop — not a flat recap. Built for MiniMax-H3 ref2va: the channel avatar is the identity reference, the model speaks the lines with NATIVE audio (no TTS, no lip-sync), the look stays locked across every clip, and word-synced subtitles are burned in. Use it for news/sports/tech "breaking" presenter shorts. Not for faceless b-roll explainers, product ads, MVs, or fictional/animated stories.
compatibility: Repo-native. Drives this agent's MiniMax-H3 tools (generate_minimax_video, generate_minimax_image, add_media_clip, assemble_video, add_subtitles) plus scrape_article / fetch_url for sourcing. The video_generator role of the channel must be MiniMax-H3 (ref2va). Not a MiniMax Hub / canvas skill.
metadata:
  trigger-words: [tiktok news, presenter news short, avatar news, breaking news short, scoop video, news recap tiktok, actualité tiktok, présentateur, résumé actu, vidéo scoop, journal vertical]
---

# TikTok News Article Presentation (avatar, MiniMax-H3)

Use this Skill to turn a **news article** into a **vertical 9:16 TikTok** in which a **single recurring avatar** presents the news **to camera**, MrBeast-tight and scroll-stopping, treated as a **breaking scoop** rather than a soft summary. The presenter carries the whole video: they launch, push, and embody the information with the energy and authority of a journalist dropping an exclusive.

Core principle: **source the facts first, pick a sharp angle, write the spoken script in the article's language, then render the avatar clip-by-clip with MiniMax-H3 ref2va (native audio), keeping ONE locked identity and one coherent setting across every clip, and finish with word-synced burned-in subtitles.**

MiniMax-H3 here is **ref2va only**: every clip is generated from the avatar reference image, the MODEL generates the video **and its audio in a single pass** (the spoken lines and soundscape are written INSIDE the prompt — there is NO separate TTS and NO lip-sync step). Never use `add_talking_clip` or `add_broll_clip` (those inject TTS / lip-sync and would replace the native audio).

## When to use / not use

- **Use** when the brief is: a presenter/host breaking down a real news story (sports, tech, politics, culture) as a punchy vertical short, one person facing camera.
- **Do not use** for: faceless b-roll explainers, product ads (use `minimalist-product-ad-generator`), music videos, or fictional/animated stories. If there is no usable avatar and no way to create one, stop and say so.

## Start Gate

Run one lightweight pass before writing or generating. Confirm only what production needs; if the user says "you decide / just do it", adopt the recommended defaults and show them as the resolved start gate.

1. **Article source** — a channel URL (use `scrape_article`, which also dedups already-treated articles), a specific URL (`fetch_url`), or pasted text. If several are available, confirm which one drives this video.
2. **Avatar / presenter** — the channel `character` that has an image (the identity reference). If several characters exist, confirm which one presents. If none has an image, offer to create one with `generate_minimax_image` and reuse its URL as `reference_image` (but a stable channel avatar is strongly preferred for cross-video consistency).
3. **Language** — the spoken language of the presenter, defaulting to the **article's language** (usually FR). Subtitles follow the same language.
4. **Target duration** — ~30-60 s default (news short). Allow 20-90 s. This sets the number of clips.
5. **Angle** — the single scoop/hook the video is built around. Recommend one from the article; confirm or let the user override.

Aspect ratio is always **9:16**. Video model is always **MiniMax-H3** (ref2va). Do not ask about those.

## Operating Principles

1. **Identity is locked — do not re-invent the presenter.** Every clip passes the SAME `character` (or the same `reference_image`). `generate_minimax_video` **auto-injects** the `subject_definitions` + `retention_analysis` sections (the avatar is `<Subject 1>`, appearance fully preserved) at the top of the prompt, identically on every clip. So **do NOT write those two sections yourself**, and do NOT restyle the presenter, change their wardrobe, hair, facial hair, or add/remove props (glasses, mic, headphones) from clip to clip. You only write what changes: framing, action, camera, and the spoken line. Keep the SAME seed (the tool handles this) — never randomize it per clip.

2. **One presenter, one voice, one mouth.** Never show two people talking at once. The presenter speaks alone to camera. Native H3 audio only.

3. **Setting and light stay coherent across clips.** Same studio/room, same key light, same palette, from the first clip to the last. Vary the SHOT (tight face close-up, chest shot, slight angle change) to create rhythm — never break the decor or lighting continuity.

4. **News integrity is non-negotiable.** Only state what the article supports. Attribute claims ("selon…", "d'après…") when they are not established fact. Keep names, numbers, clubs, and dates accurate. No fabrication, no invented quotes, no defamation. If the article is thin or ambiguous, narrow the claim rather than inflate it.

5. **Scoop energy, not a recap.** High energy, sharp cuts, short clips (each MiniMax clip 5-15 s; prefer 6-10 s), direct eye-to-camera authority, hand gestures that punctuate. The MOOD drives the directing.

6. **Progress is visible.** After sourcing and after the script, show what was produced and the next decision. Do not silently run the whole pipeline end to end without a checkpoint on the facts and the script.

## Narrative Arc (build the script on this)

1. **Hook (2-3 s)** — the presenter leans in, brows up, hand punctuating, and drops the info as a question or a statement that stops the scroll. No slow intro, no "salut à tous". Start on the shock.
2. **Context (quick)** — the minimum background needed to understand the scoop.
3. **Core info** — THE detail that makes people react, stated plainly and confidently.
4. **Escalation** — the presenter argues, nuances, gets fired up, smiles; adds the angle/opinion.
5. **Open punch / CTA** — an open-ended closer that invites comments ("t'en penses quoi ?"). End on the presenter, never on a card or grid.

## Script & Copy Rules

- Write the **spoken lines in the article's language** (FR by default); keep proper nouns as-is. Only the H3 prompt scaffolding (scene, camera, soundscape) is written in English.
- One coherent **segment per clip**. Keep each spoken segment to what fits the clip duration (roughly 2-4 short sentences for a 6-10 s clip). Don't cram.
- Punchy spoken register: short sentences, active voice, one idea per sentence. This is talking-head speech, not an article read-aloud.
- You may use `write_script` to draft the segmented script from the scraped article, then refine per the arc above.

## Subtitles

- Always finish with `add_subtitles` — it burns **word-synced** captions locally (faster-whisper, no API) onto the assembled video.
- Captions are short, punchy, uppercase, bottom-centered (social style). Keep the bottom third of the frame free of important action so subtitles never cover the presenter's mouth or key gestures.
- Subtitles transcribe the **actual** spoken audio (H3 native), so they stay in sync even if the model phrases a line slightly differently.

## Workflow

### STEP 1 — Source the article and summarize the facts
Pull the article: `scrape_article` (channel URL, auto-dedup) or `fetch_url` (specific URL) or use pasted text. Output a short fact summary: headline, the 2-4 hard facts, the key figures/names/dates, the source, and the single most reaction-worthy detail. If the material is too thin or unclear to build a factual short, stop and ask for a better source.

### STEP 2 — Pick the angle (the scoop)
State, in one sentence, the hook the whole video is built around. Example: "Angle: le transfert est bouclé à un prix record, et personne n'en parle encore."

### STEP 3 — Write the segmented spoken script
Write the presenter's lines, split into clips that follow the Narrative Arc. Label each segment with its role (hook / context / core / escalation / punch), its spoken line (article language), and its target duration. Read it back and get a quick confirmation on facts + script before generating.

### STEP 4 — Plan the shots (per clip)
For each segment define: framing (tight face CU for the hook, chest shot for context, slight angle change for variety), the presenter's action/gesture and expression, camera move (subtle — push-in, small handheld energy), and the ambience. Keep the SAME setting/light every clip.

### STEP 5 — Generate the avatar clips (MiniMax-H3 ref2va)
For each segment, call `generate_minimax_video(character=<the avatar>, prompt=…, seconds=…)`. Write the prompt in the H3 **full-reference format**, but **omit `subject_definitions` and `retention_analysis`** (the tool injects them). Provide:
- `summary`: one line — the shot's purpose and that `<Subject 1>` (the avatar) presents to camera.
- `detailed_description`: the composition, the presenter's action/expression/gesture, camera move, and **the exact spoken line verbatim** (in the article's language) that the model must speak.
- `overall_soundscape`: the presenter's clear voice up front + a discreet studio/urban ambience that supports the pace without covering the voice.
- `non_diegetic_music`: optional light bed; keep it under the voice.
Reference `<Subject 1>` for the person; never re-describe the avatar's looks. Keep the SAME setting wording across clips. See the injected `h3-prompt-writing` skill (`references/ref-en.txt`) for the field format.

### STEP 6 — Assemble
Bring each returned .mp4 into the timeline in order with `add_media_clip(source=<path>)` and **no `narration_text`** (this preserves the native H3 audio). Then `assemble_video` to concatenate.

### STEP 7 — Subtitles
Call `add_subtitles` on the assembled video (local word-synced burn-in).

### STEP 8 — Music (optional) & delivery
Only if a real track is provided or clearly wanted, `add_background_music` kept low under the voice. Then deliver: final path, duration, aspect ratio, language, the angle used, and one line of next-improvement (pacing, hook, shot variety).

## H3 Prompt Reminders (ref2va)

- Every clip MUST pass a `character` (or `reference_image`). Text-only generation is not available and will error.
- Put the SPOKEN LINES and the sound design INSIDE the prompt — that is what the model renders as audio. No TTS, no lip-sync.
- Do NOT invent props or restyle the subject in `detailed_description` (no new mic, hat, glasses, haircut, or outfit) unless the brief explicitly asks — describe the action around the LOCKED appearance.
- One clip = one continuous shot of the presenter. Never render split screens, grids, or two people talking.
- Clip duration 5-15 s (prefer 6-10 s). Keep the seed as-is (fixed) so the face does not drift.

## Failure Handling

- **Article too thin / unclear** → stop, ask for a better source; do not fabricate.
- **No avatar with an image** → offer to generate one with `generate_minimax_image` and reuse its URL; prefer pinning a stable channel avatar for cross-video consistency.
- **Face / wardrobe / props drift across clips** → verify every clip passes the SAME `character`, that the seed is fixed, and that `detailed_description` does not re-describe or restyle the presenter (identity lives in the auto-injected sections + the reference image).
- **Two mouths / second person appears** → rewrite the prompt to a single presenter facing camera.
- **Setting/light jumps between clips** → reuse the exact same setting wording and lighting description every clip.
- **Subtitles cover the presenter** → keep the bottom third clear; re-run `add_subtitles`.
- **Native audio replaced by silence/TTS** → ensure `add_media_clip` is called with NO `narration_text` on H3 clips.
- **Clip too long / rambling** → shorten the spoken segment; split into two clips.

## Trigger Examples

- "Fais une TikTok où mon présentateur annonce cette actu foot comme un scoop"
- "Résume cet article en vidéo verticale avec l'avatar face caméra"
- "Breaking-news short: have the avatar break down this article to camera"
- "Un présentateur qui balance cette info tech en 45 secondes, énergie haute"
- "Transforme cette page 20min en présentation TikTok par l'avatar"

Do not use this Skill for faceless explainers, product ads, music videos, or fictional/animated stories.
