#!/usr/bin/env python3
"""Let either agent request a meaningful Telegram topic title for its own pane."""

import argparse
import os
import unicodedata

import tg_sessions


def _clean(text):
    return " ".join("".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in text
    ).split())


def _truncate(text, units):
    return text.encode("utf-16-le")[:units * 2].decode("utf-16-le", errors="ignore")


def format_topic_name(agent, project, title):
    label = "Codex" if agent == "codex" else "Claude Code"
    project = _truncate(_clean(project), 40)
    prefix = f"🚀 {label} · {project} · "
    remaining = 128 - len(prefix.encode("utf-16-le")) // 2
    return prefix + _truncate(_clean(title), remaining)


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
