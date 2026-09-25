"""Découpe et mise en forme pour Telegram : limite 4096, repli texte brut."""
from __future__ import annotations

import html

# Marge sous la limite Telegram (4096) pour préfixes et erreurs.
MAX_CHUNK = 3800
MAX_MESSAGES = 6


def split_message(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Découpe aux frontières de paragraphe puis de ligne, en dernier
    recours au milieu du texte."""
    text = text.strip()
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if len(parts) > MAX_MESSAGES:
        parts = parts[:MAX_MESSAGES]
        parts.append("… (réponse tronquée)")
    return parts


def plain(text: str) -> str:
    """Texte Telegram sûr : entités HTML échappées, aucun parse mode requis."""
    return html.escape(text)
