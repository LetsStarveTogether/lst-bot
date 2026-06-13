from __future__ import annotations

from enum import StrEnum


class HitokotoType(StrEnum):
    ANIME = "a"
    COMIC = "b"
    GAME = "c"
    LITERATURE = "d"
    ORIGINAL = "e"
    INTERNET = "f"
    OTHER = "g"
    FILM = "h"
    POETRY = "i"
    NETEASE_MUSIC = "j"
    PHILOSOPHY = "k"
    JOKE = "l"


__all__ = ["HitokotoType"]
