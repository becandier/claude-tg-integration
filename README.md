# Claude Code Telegram Integration

Двусторонний мост между Telegram и [Claude Code](https://docs.anthropic.com/en/docs/claude-code) через tmux-сессии. Управляй Claude Code с телефона — отправляй задачи, получай результаты, подтверждай действия.

## Возможности

- **Forum Topics** — каждая сессия в отдельном топике, удобная навигация между проектами
- **Мульти-сессии** — несколько Claude Code сессий параллельно, каждая в своём tmux pane
- **Голосовой ввод** — голосовые сообщения распознаются офлайн через Whisper
- **Permission prompts** — инлайн-кнопки ✅/❌ для подтверждения действий Claude
- **Навигация по проектам** — drill-down браузер с инлайн-кнопками
- **Форматирование** — Markdown → HTML: код, таблицы, bold/italic
- **Терминал** — автоматически открывает iTerm2/Terminal.app с tmux attach

## Как работает

```
Telegram (Forum Topic) ──→ tg_reply_watcher.py ──tmux send-keys──→ Claude Code (tmux pane)
Telegram (Forum Topic) ←── tg_stop_hook.sh ←───── stop hook ←──── Claude Code завершил
Telegram (Forum Topic) ←── tg_notify_hook.sh ←─── notify hook ←── Claude Code ждёт ввода
```

Каждая сессия живёт в отдельном Forum Topic — навигация между проектами в один тап.

## Установка

### 1. Создать Telegram-бота

Через [@BotFather](https://t.me/BotFather) → `/newbot` → получить токен.

Настройки бота (Bot Settings):
- **Group Privacy** → **Turn OFF** (бот должен видеть все сообщения в группе)
- **Allow Groups** → ON

### 2. Создать супергруппу с Forum Topics

1. Создать группу в Telegram (любое название, например "Claude Code")
2. Настройки группы → Topics → включить
3. Добавить бота в группу
4. Сделать бота администратором с правом **Manage Topics**
5. Узнать ID группы (переслать сообщение из группы в `@userinfobot`)

### 3. Конфигурация

```bash
cp tg_config.sh.example tg_config.sh
```

Заполнить `tg_config.sh`:

```bash
TG_BOT_TOKEN="your_bot_token"
TG_CHAT_ID="-1001234567890"       # ID супергруппы (отрицательное число)
TG_USER_ID="your_user_id"         # узнать: @userinfobot
TERMINAL_MODE="tab"               # "tab", "window" или "none"
TERMINAL_APP="iterm"              # "iterm" или "terminal"
```

### 4. Зависимости

```bash
brew install tmux ffmpeg

# Для голосовых сообщений (опционально)
brew install whisper-cpp
mkdir -p whisper-models
# Скачать модель: https://huggingface.co/ggerganov/whisper.cpp/tree/main
```

### 5. Хуки Claude Code

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

### 6. Запуск

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
| `/close` | Закрыть сессию (в топике — закрывает сессию + топик) |

## Использование

1. Отправь `/newstart my-project` или выбери проект через `/projects`
2. Бот создаст **Forum Topic** и запустит Claude Code в tmux
3. Пиши прямо в топике — текст автоматически уходит в Claude (reply не нужен)
4. Кнопки ✅/❌ появляются когда Claude спрашивает разрешение
5. Короткие ответы (`да`, `y`, `ok`) автоматически подтверждают permission prompts
6. `/close` в топике — закрывает сессию и топик

Голосовые сообщения распознаются и отправляются как текст.

## Отключение интеграции

Для запуска Claude Code без уведомлений в Telegram:

```bash
CLAUDE_NO_TG=1 claude
```
