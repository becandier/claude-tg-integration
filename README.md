# Claude Code Telegram Integration

Двусторонний мост между Telegram и [Claude Code](https://docs.anthropic.com/en/docs/claude-code) через tmux-сессии. Управляй Claude Code с телефона — отправляй задачи, получай результаты, подтверждай действия.

## Возможности

- **Мульти-сессии** — несколько Claude Code сессий параллельно, каждая в своём tmux pane
- **Голосовой ввод** — голосовые сообщения распознаются офлайн через Whisper
- **Permission prompts** — инлайн-кнопки ✅/❌ для подтверждения действий Claude
- **Навигация по проектам** — drill-down браузер с инлайн-кнопками
- **Форматирование** — Markdown → HTML: код, таблицы, bold/italic
- **Терминал** — автоматически открывает iTerm2/Terminal.app с tmux attach

## Как работает

```
Telegram ──reply──→ tg_reply_watcher.py ──tmux send-keys──→ Claude Code (tmux pane)
Telegram ←─msg────── tg_stop_hook.sh ←───── stop hook ←──── Claude Code завершил
Telegram ←─msg────── tg_notify_hook.sh ←─── notify hook ←── Claude Code ждёт ввода
```

## Установка

### 1. Создать Telegram-бота

Через [@BotFather](https://t.me/BotFather) → `/newbot` → получить токен.

### 2. Конфигурация

```bash
cp tg_config.sh.example tg_config.sh
```

Заполнить `tg_config.sh`:

```bash
TG_BOT_TOKEN="your_bot_token"
TG_CHAT_ID="your_chat_id"        # узнать: @userinfobot
TG_USER_ID="your_user_id"
TERMINAL_MODE="tab"               # "tab", "window" или "none"
TERMINAL_APP="iterm"              # "iterm" или "terminal"
```

### 3. Зависимости

```bash
brew install tmux ffmpeg

# Для голосовых сообщений (опционально)
brew install whisper-cpp
mkdir -p whisper-models
# Скачать модель: https://huggingface.co/ggerganov/whisper.cpp/tree/main
```

### 4. Хуки Claude Code

В `~/.claude/settings.json` добавить:

```json
{
  "hooks": {
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/tg-integration/tg_notify_hook.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/tg-integration/tg_stop_hook.sh"
          }
        ]
      }
    ]
  }
}
```

### 5. Запуск

```bash
python3 -u ~/.claude/tg-integration/tg_reply_watcher.py
```

Или через LaunchAgent для автозапуска — см. `start_watcher.sh`.

## Команды бота

| Команда | Описание |
|---------|----------|
| `/newstart [проект]` | Запустить Claude Code в новой tmux-сессии |
| `/projects` | Выбрать проект из ~/projects |
| `/sessions` | Список активных сессий |
| `/close` | Закрыть сессию |

## Использование

1. Отправь `/newstart my-project` или выбери проект через `/projects`
2. Claude Code запустится в tmux — бот пришлёт подтверждение
3. Отвечай **reply-ем** на сообщения бота — текст уходит в Claude
4. Кнопки ✅/❌ появляются когда Claude спрашивает разрешение
5. Короткие ответы (`да`, `y`, `ok`) автоматически подтверждают permission prompts

Голосовые сообщения распознаются и отправляются как текст.

## Отключение интеграции

Для запуска Claude Code без уведомлений в Telegram:

```bash
CLAUDE_NO_TG=1 claude
```
