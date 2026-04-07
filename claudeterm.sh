claudeterm() {
    local projects_root="$HOME/projects"
    local project_dir=""

    # Собираем список вариантов
    local -a items
    items=("📂 Текущая директория ($(basename "$PWD"))")
    if [ -d "$projects_root" ]; then
        for d in "$projects_root"/*(N/); do
            items+=("$(basename "$d")")
        done
    fi
    items+=("✏️  Ввести путь вручную")

    # fzf выбор
    local choice
    choice=$(printf '%s\n' "${items[@]}" | fzf --height=~40% --reverse --prompt="Проект: " --header="Выбери проект для Claude") || return 0

    case "$choice" in
        "📂 Текущая директория"*)
            project_dir="$PWD"
            ;;
        "✏️  Ввести путь вручную")
            echo -n "Путь к проекту: "
            read -r project_dir
            project_dir="${project_dir/#\~/$HOME}"
            if [ ! -d "$project_dir" ]; then
                echo "Директория не найдена: $project_dir"
                return 1
            fi
            ;;
        *)
            project_dir="$projects_root/$choice"
            ;;
    esac

    local name="claude-$$"
    local watcher="$HOME/.claude/tg-integration/tg_reply_watcher.py"
    local pidfile="/tmp/claude_tg_watcher.pid"

    # Запускаем watcher если не запущен (проверка по PID-файлу)
    local need_start=0
    if [ -f "$pidfile" ]; then
        local pid=$(cat "$pidfile" 2>/dev/null)
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pidfile"
            need_start=1
        fi
    else
        need_start=1
    fi

    if [ "$need_start" -eq 1 ]; then
        python3 -u "$watcher" > /tmp/claude_tg_watcher.log 2>&1 &
        disown
    fi

    tmux new-session -d -s "$name" -c "$project_dir" \
        "claude --dangerously-skip-permissions; tmux wait-for -S $name-done"

    # Отправляем стартовое сообщение в Telegram с маршрутизацией
    source "$HOME/.claude/tg-integration/tg_config.sh"
    local project_name
    project_name="$(basename "$project_dir")"
    local pane_id
    pane_id=$(tmux list-panes -t "$name" -F '#{pane_id}' 2>/dev/null | head -1)

    if [ -n "$pane_id" ]; then
        local response
        response=$(curl -s --connect-timeout 5 --max-time 10 \
            -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TG_CHAT_ID}" \
            --data-urlencode "text=🚀 Claude Code запущен — проект: ${project_name}")

        local msg_id
        msg_id=$(printf '%s' "$response" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(d.get("result", {}).get("message_id", ""))
' 2>/dev/null)

        if [ -n "$msg_id" ]; then
            python3 "$HELPER" save_route "$msg_id" "$pane_id" 2>/dev/null
        fi
    fi

    tmux attach -t "$name"
}
