#!/bin/bash
# Отправка файла в Telegram-топик текущей сессии Claude Code.
# Использование: tg_send_file.sh <файл> [подпись]
# Лимит Bot API — 50 МБ. Файл уходит как документ (без сжатия).

source "$HOME/.claude/tg-integration/tg_config.sh"

FILE="$1"
CAPTION="${2:-}"

if [ -z "$FILE" ]; then
    echo "Использование: tg_send_file.sh <файл> [подпись]" >&2
    exit 1
fi

if [ ! -f "$FILE" ] || [ ! -r "$FILE" ]; then
    echo "Файл не найден или недоступен: $FILE" >&2
    exit 1
fi

# Абсолютный путь — curl -F с относительным путём зависит от CWD;
# "--" защищает от путей, начинающихся с "-"
FILE=$(cd -- "$(dirname -- "$FILE")" && pwd)/$(basename -- "$FILE")

# -L: размер цели симлинка, а не самой ссылки
MAX_SIZE=$((50 * 1024 * 1024))
SIZE=$(stat -Lf%z "$FILE" 2>/dev/null || stat -Lc%s "$FILE" 2>/dev/null)
if [ -n "$SIZE" ] && [ "$SIZE" -gt "$MAX_SIZE" ]; then
    echo "Файл больше 50 МБ ($((SIZE / 1024 / 1024)) МБ) — Bot API не пропустит. Сожми или разбей на части." >&2
    exit 1
fi

# Лимит подписи Telegram — 1024 UTF-16 code units (не байты и не code points)
if [ -n "$CAPTION" ]; then
    CAPTION=$(printf '%s' "$CAPTION" | python3 -c '
import sys
c = sys.stdin.read()
u16 = lambda s: len(s.encode("utf-16-le")) // 2
if u16(c) > 1024:
    while c and u16(c) > 1021:
        c = c[:-1]
    c += "..."
sys.stdout.write(c)
')
fi

# Топик текущей сессии (по tmux pane); без tmux — в General
TOPIC_ID=""
if [ -n "$TMUX" ] && [ -n "$TMUX_PANE" ]; then
    TOPIC_ID=$(python3 "$HELPER" get_topic "$TMUX_PANE" 2>/dev/null)
fi

# Экранируем \ и " в пути: curl -F понимает @"..." с \\ и \"
FILE_ESCAPED=${FILE//\\/\\\\}
FILE_ESCAPED=${FILE_ESCAPED//\"/\\\"}

# --form-string — значения уходят литерально (иначе curl трактует ";", "@", "<")
CURL_ARGS=(
    -s --connect-timeout 10 --max-time 300
    -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendDocument"
    --form-string "chat_id=${TG_CHAT_ID}"
    -F "document=@\"${FILE_ESCAPED}\""
)
[ -n "$TOPIC_ID" ] && CURL_ARGS+=(--form-string "message_thread_id=${TOPIC_ID}")
[ -n "$CAPTION" ] && CURL_ARGS+=(--form-string "caption=${CAPTION}")

RESPONSE=$(curl "${CURL_ARGS[@]}")
CURL_EXIT=$?
if [ "$CURL_EXIT" -ne 0 ]; then
    echo "curl завершился с кодом ${CURL_EXIT} (сеть/таймаут/файл)" >&2
    exit 1
fi

printf '%s' "$RESPONSE" | python3 -c '
import sys, json
try:
    r = json.load(sys.stdin)
except Exception:
    print("Не удалось разобрать ответ Telegram API", file=sys.stderr)
    sys.exit(1)
if r.get("ok"):
    print("Отправлено: message_id =", r["result"]["message_id"])
else:
    print("Ошибка Telegram API [%s]: %s" % (r.get("error_code"), r.get("description")), file=sys.stderr)
    sys.exit(1)
'
