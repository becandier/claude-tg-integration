# Claude Code + Codex Telegram Integration

Двусторонний мост между Telegram, Claude Code и Codex CLI через tmux-сессии. Управляй обоими coding-агентами с телефона: запускай проекты, отправляй задачи, файлы и голосовые сообщения, переключай модели и reasoning effort.

## Возможности

- **Forum Topics** — каждая сессия в отдельном топике, удобная навигация между проектами
- **Два агента** — Claude Code по умолчанию и Codex CLI по команде `/newstart codex`
- **Мульти-сессии** — несколько Claude/Codex-сессий параллельно, каждая в своём tmux pane
- **Фото и документы** — отправляй скриншоты, PDF и файлы кода прямо агенту
- **Файлы от агента** — файл до 50 МБ можно прислать в топик через `tg_send_file.sh`
- **Голосовой ввод** — голосовые сообщения распознаются офлайн через Whisper
- **Permission prompts Claude** — инлайн-кнопки ✅/❌ для подтверждений Claude
- **Навигация по проектам** — drill-down браузер с инлайн-кнопками
- **Мониторинг активности** — уведомления, если Claude или Codex работает долго или завис
- **Форматирование** — Markdown → HTML: код, таблицы, bold/italic
- **Терминал** — автоматически открывает iTerm2/Terminal.app с tmux attach

## Как работает

```
Telegram (Forum Topic) ──→ tg_reply_watcher.py ──tmux send-keys──→ Claude/Codex
Telegram (Forum Topic) ←── tg_stop_hook.sh ←───── Claude Stop hook
Telegram (Forum Topic) ←── tg_notify_hook.sh ←─── Claude Notification hook
Telegram (Forum Topic) ←── tg_codex_notify_hook.py ← Codex turn-complete notify
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

Установи и авторизуй нужные CLI:

```bash
claude --version
codex --version
```

Codex-сессии запускаются с `--dangerously-bypass-approvals-and-sandbox` и `--dangerously-bypass-hook-trust`. Это режим полного локального доступа без Telegram approval-кнопок. Используй его только на доверенной машине и в доверенных проектах.

Для каждой Codex-сессии watcher передаёт временный `notify` override, поэтому глобальный `~/.codex/config.toml` менять не требуется. После завершения turn Codex отправляет `last_assistant_message` через общий Telegram formatter в топик своей tmux-сессии.

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
| `/newstart codex [проект]` | Запустить Codex CLI в новой tmux-сессии |
| `/projects` | Выбрать проект из ~/projects |
| `/sessions` | Список активных сессий |
| `/close` | Закрыть сессию (в топике — закрывает сессию и удаляет топик) |
| `/cleanup` | Очистить все скачанные медиафайлы |
| `/effort [уровень]` | Reasoning effort активного агента; список зависит от Claude/Codex |
| `/model [алиас]` | Модель активного агента; список читается из живого `/model` picker |

## Использование

1. Отправь `/newstart my-project` для Claude или `/newstart codex my-project` для Codex
2. Без имени проекта бот покажет project picker и сохранит выбранный тип агента
3. Бот создаст **Forum Topic** и запустит агента в tmux
4. Пиши прямо в топике — текст автоматически уходит в нужную сессию (reply не нужен)
5. Отправляй фото и документы — агент получит путь к файлу
6. Кнопки ✅/❌ появляются только для permission prompts Claude; Codex работает в Full Access
7. Короткие ответы (`да`, `y`, `ok`) подтверждают только permission prompts Claude
8. Если агент работает дольше 2 минут — придёт уведомление "⏳ всё ещё работает"
9. `/close` в топике — закрывает сессию и удаляет топик

Голосовые сообщения распознаются и отправляются как текст.
Медиафайлы хранятся в `/tmp/claude_tg_media/` и автоматически удаляются при закрытии сессии.

## Отправка файлов из сессии

Claude, Codex или ты вручную можете отправить файл в Telegram:

```bash
~/.claude/tg-integration/tg_send_file.sh <файл> [подпись]
```

- Файл уходит как документ (`sendDocument`), без сжатия, лимит Bot API — 50 МБ
- Внутри tmux-сессии файл попадает в топик текущей сессии, иначе — в General
- Подпись обрезается до 1024 символов (лимит Telegram)

## Отключение интеграции

Для запуска Claude Code без уведомлений в Telegram:

```bash
CLAUDE_NO_TG=1 claude
```
