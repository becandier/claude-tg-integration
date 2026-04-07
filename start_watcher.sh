#!/bin/bash
# Wrapper для LaunchAgent — загружает окружение пользователя
source ~/.zprofile 2>/dev/null
source ~/.zshrc 2>/dev/null
exec python3 -u ~/.claude/tg-integration/tg_reply_watcher.py
