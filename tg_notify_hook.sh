#!/bin/bash
# Claude Code Notification hook — уведомление + reply_to + маршрутизация
# Если stop-hook уже отправил ответ — шлём короткое "ждёт внимания"
# Если stop-hook не сработал — шлём "ответь чтобы продолжить"

[ -n "$CLAUDE_NO_TG" ] && exit 0

source "$HOME/.claude/tg-integration/tg_config.sh"
PROJECT=$(basename "$PWD")

# stop-hook ставит этот флаг при успешной отправке
STOP_FLAG="/tmp/claude_code_stopped"

# Текст зависит от того, дошёл ли ответ
if [ -f "$STOP_FLAG" ]; then
    rm -f "$STOP_FLAG"
    TEXT="($PROJECT) ✅ Ответь reply-ем чтобы продолжить"
else
    TEXT="($PROJECT) ⏳ Claude Code ждёт ввода — ответь reply-ем"
fi

# Если не в tmux — тихое уведомление без маршрутизации
if [ -z "$TMUX" ]; then
    curl -s --connect-timeout 5 --max-time 10 \
        -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT_ID}" \
        -d "disable_notification=true" \
        --data-urlencode "text=$TEXT" > /dev/null
    exit 0
fi

PANE_ID="$TMUX_PANE"

# reply_to — последнее сообщение пользователя из TG
REPLY_TO=$(python3 "$HELPER" get_reply_to "$PANE_ID" 2>/dev/null)

KEYBOARD="{\"inline_keyboard\":[[{\"text\":\"✅ Принять\",\"callback_data\":\"approve:${PANE_ID}\"},{\"text\":\"❌ Отклонить\",\"callback_data\":\"reject:${PANE_ID}\"}]]}"

CURL_ARGS=(
    -s --connect-timeout 5 --max-time 10
    -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage"
    -d "chat_id=${TG_CHAT_ID}"
    --data-urlencode "text=$TEXT"
    --data-urlencode "reply_markup=$KEYBOARD"
)

if [ -n "$REPLY_TO" ]; then
    CURL_ARGS+=(-d "reply_to_message_id=${REPLY_TO}" -d "allow_sending_without_reply=true")
fi

RESPONSE=$(curl "${CURL_ARGS[@]}")

# Сохраняем маршрут: message_id → pane_id
MSG_ID=$(printf '%s' "$RESPONSE" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("result", {}).get("message_id", ""))
' 2>/dev/null)

if [ -n "$MSG_ID" ] && [ -n "$PANE_ID" ]; then
    python3 "$HELPER" save_route "$MSG_ID" "$PANE_ID" 2>/dev/null
fi
