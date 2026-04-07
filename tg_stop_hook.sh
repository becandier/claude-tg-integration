#!/bin/bash
# Claude Code Stop hook — итог работы в Telegram + reply_to + маршрутизация

[ -n "$CLAUDE_NO_TG" ] && exit 0

source "$HOME/.claude/tg-integration/tg_config.sh"
PROJECT=$(basename "$PWD")

INPUT=$(cat)

SUMMARY=$(printf '%s' "$INPUT" | python3 -c '
import sys, json, re

def esc(text):
    """Экранирование HTML-спецсимволов."""
    return text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def convert_md_to_tg_html(msg):
    # --- 1. Извлекаем блоки кода, заменяем плейсхолдерами ---
    code_blocks = []
    def save_code_block(m):
        lang = m.group(1) or ""
        code = m.group(2)
        escaped = esc(code.strip("\n"))
        if lang:
            block = f"<pre><code class=\"language-{esc(lang)}\">{escaped}</code></pre>"
        else:
            block = f"<pre><code>{escaped}</code></pre>"
        idx = len(code_blocks)
        code_blocks.append(block)
        return f"\x00CODEBLOCK{idx}\x00"
    msg = re.sub(r"```(\w*)\n?([\s\S]*?)```", save_code_block, msg)

    # --- 2. Извлекаем inline code ---
    inline_codes = []
    def save_inline(m):
        code = m.group(1)
        idx = len(inline_codes)
        inline_codes.append(f"<code>{esc(code)}</code>")
        return f"\x00INLINE{idx}\x00"
    msg = re.sub(r"`([^`\n]+)`", save_inline, msg)

    # --- 3. Экранируем HTML в оставшемся тексте ---
    msg = esc(msg)

    # --- 4. Таблицы: конвертируем в структурированный список ---
    def format_table(m):
        block = m.group(0)
        rows = []
        for line in block.strip().split("\n"):
            line = line.strip()
            if not line.startswith("|"):
                continue
            if re.match(r"^\|[-:\s|]+\|$", line):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append(cells)
        if len(rows) < 2:
            return block
        header = rows[0]
        data = rows[1:]
        lines = []
        num_cols = len(header)
        for row in data:
            if num_cols == 2:
                col1 = row[0] if len(row) > 0 else ""
                col2 = row[1] if len(row) > 1 else ""
                if col2:
                    lines.append(f"\u25b8 {col1} \u2014 {col2}")
                else:
                    lines.append(f"\u25b8 {col1}")
            else:
                parts = []
                for i, cell in enumerate(row):
                    if cell:
                        h = header[i] if i < len(header) else ""
                        if h:
                            parts.append(f"{h}: {cell}")
                        else:
                            parts.append(cell)
                sep = ", "
                lines.append("\u25b8 " + sep.join(parts))
        content = "\n".join(lines)
        content = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", content)
        content = re.sub(r"__(.+?)__", r"<b>\1</b>", content)
        content = re.sub(r"(?<!\w)\*([^\*\n]+?)\*(?!\w)", r"<i>\1</i>", content)
        content = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", content)
        idx = len(code_blocks)
        code_blocks.append(f"<blockquote>{content}</blockquote>")
        return f"\x00CODEBLOCK{idx}\x00"
    msg = re.sub(r"(^\|.+\|$\n?){2,}", format_table, msg, flags=re.MULTILINE)

    # --- 5. Горизонтальные линии ---
    msg = re.sub(r"^-{3,}$", "", msg, flags=re.MULTILINE)

    # --- 6. Заголовки -> bold ---
    msg = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", msg, flags=re.MULTILINE)

    # --- 7. Bold и italic ---
    msg = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", msg)
    msg = re.sub(r"__(.+?)__", r"<b>\1</b>", msg)
    msg = re.sub(r"(?<!\w)\*([^\*\n]+?)\*(?!\w)", r"<i>\1</i>", msg)
    msg = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", msg)

    # --- 8. Ссылки [text](url) -> text (url) ---
    msg = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"\1 (\2)", msg)

    # --- 10. Убираем избыточные пустые строки ---
    msg = re.sub(r"\n{3,}", "\n\n", msg)

    # --- 11. Возвращаем code blocks и inline codes ---
    for idx, block in enumerate(code_blocks):
        msg = msg.replace(f"\x00CODEBLOCK{idx}\x00", block)
    for idx, code in enumerate(inline_codes):
        msg = msg.replace(f"\x00INLINE{idx}\x00", code)

    return msg.strip()

d = json.load(sys.stdin)
msg = d.get("last_assistant_message", "")
if not msg:
    sys.exit(0)

result = convert_md_to_tg_html(msg)
# Лимит 3500 символов с корректным обрезанием (не ломаем HTML-теги)
if len(result) > 3500:
    result = result[:3500]
    last_open = result.rfind("<")
    last_close = result.rfind(">")
    if last_open > last_close:
        result = result[:last_open]
    result += "\n..."
print(result)
' 2>/dev/null)

if [ -z "$SUMMARY" ]; then
    touch /tmp/claude_code_stopped
    exit 0
fi

ESC_PROJECT=$(printf '%s' "$PROJECT" | sed 's/&/\&amp;/g;s/</\&lt;/g;s/>/\&gt;/g')
if [ -n "$TMUX" ]; then
    ICON="✅"
else
    ICON="⚠️"
fi
MSG=$(printf '(<b>%s</b>) %s\n\n%s' "$ESC_PROJECT" "$ICON" "$SUMMARY")

touch /tmp/claude_code_stopped

# reply_to если в tmux
REPLY_TO=""
PANE_ID="${TMUX_PANE}"
if [ -n "$TMUX" ] && [ -n "$PANE_ID" ]; then
    REPLY_TO=$(python3 "$HELPER" get_reply_to "$PANE_ID" 2>/dev/null)
fi

CURL_ARGS=(
    -s --connect-timeout 5 --max-time 10
    -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage"
    -d "chat_id=${TG_CHAT_ID}"
    -d "parse_mode=HTML"
    --data-urlencode "text=$MSG"
)

# Без tmux — тихое уведомление
if [ -z "$TMUX" ]; then
    CURL_ARGS+=(-d "disable_notification=true")
fi

if [ -n "$REPLY_TO" ]; then
    CURL_ARGS+=(-d "reply_to_message_id=${REPLY_TO}" -d "allow_sending_without_reply=true")
fi

RESPONSE=$(curl "${CURL_ARGS[@]}")

# Сохраняем маршрут
if [ -n "$TMUX" ] && [ -n "$PANE_ID" ]; then
    MSG_ID=$(printf '%s' "$RESPONSE" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("result", {}).get("message_id", ""))
' 2>/dev/null)

    if [ -n "$MSG_ID" ]; then
        python3 "$HELPER" save_route "$MSG_ID" "$PANE_ID" 2>/dev/null
    fi
fi
