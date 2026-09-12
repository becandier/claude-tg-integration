#!/usr/bin/env python3
"""Let either agent request a meaningful Telegram topic title for its own pane."""

import argparse
import os
import re
import unicodedata

import tg_sessions

# /model сохраняет алиас, который ввёл человек; в названии топика он должен
# выглядеть так же, как в CLI. Codex-модели (`gpt-5.6-sol`) уже читаемы.
_MODEL_ALIASES = {
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "fable": "Fable",
    "default": "",
}


def _clean(text):
    return " ".join("".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in text
    ).split())


def _truncate(text, units):
    return text.encode("utf-16-le")[:units * 2].decode("utf-16-le", errors="ignore")


def model_label(model):
    """Короткое имя модели для названия топика: `Opus 5 (1M context)` → `Opus 5`.

    Пустая строка означает, что модель ещё не определена и нужен запасной
    ярлык по типу агента.
    """
    name = _clean(re.sub(r"\s*\([^)]*\)", "", model or ""))
    if not name:
        return ""
    if name.islower() and " " not in name:
        return _MODEL_ALIASES.get(name, name)
    return _truncate(name, 30)


def format_topic_name(agent, project, title, model=""):
    """`🚀 Opus 5 · projects · Тема обсуждения` — модель вместо типа агента.

    Пустые части опускаются вместе с разделителем: у старых сессий проект или
    тема могут быть ещё не известны.
    """
    label = model_label(model) or ("Codex" if agent == "codex" else "Claude Code")
    prefix = "🚀 " + " · ".join(
        part for part in (label, _truncate(_clean(project), 40)) if part
    )
    title = _clean(title)
    if not title:
        return _truncate(prefix, 128)
    prefix += " · "
    remaining = 128 - len(prefix.encode("utf-16-le")) // 2
    return prefix + _truncate(title, remaining)


def set_title(pane_id, title):
    title = _truncate(_clean(title), 80)
    if not title:
        raise ValueError("Название не должно быть пустым")

    def update(data):
        if not pane_id or not data.get("pane_to_topic", {}).get(pane_id):
            raise ValueError("Для текущей сессии нет Telegram-топика")
        data.setdefault("pane_to_title", {})[pane_id] = title
    tg_sessions._with_lock(update)
    return title


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="Краткое название обсуждения, 3–6 слов")
    args = parser.parse_args()
    try:
        title = set_title(os.environ.get("TMUX_PANE", ""), args.title)
    except ValueError as error:
        parser.error(str(error))
    print(f"Название передано Telegram-мосту: {title}")


if __name__ == "__main__":
    main()
