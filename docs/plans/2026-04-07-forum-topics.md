# Forum Topics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести бота с плоского приватного чата на супергруппу с Forum Topics — каждый `/newstart` создаёт отдельный топик для проекта.

**Architecture:** Добавляем `pane_to_topic` маппинг в tg_sessions.py. При `/newstart` бот вызывает `createForumTopic`, сохраняет `message_thread_id`, и все дальнейшие сообщения (включая hook-и) отправляются в этот топик. Общие команды (`/projects`, `/sessions`, `/close`) отвечают в тот же топик, откуда были вызваны.

**Tech Stack:** Python 3, Telegram Bot API (Forum Topics), bash, tmux

---

### Task 1: Расширить tg_sessions.py — добавить pane_to_topic

**Files:**
- Modify: `tg_sessions.py`

- [ ] **Step 1: Добавить `save_topic`, `get_topic` и обновить `cleanup_pane`**

В `tg_sessions.py` добавить третий dict `pane_to_topic` в структуру данных, и две функции + CLI-команды:

```python
def save_topic(pane_id, topic_id):
    def _do(data):
        data.setdefault("pane_to_topic", {})
        data["pane_to_topic"][pane_id] = int(topic_id)
    _with_lock(_do)


def get_topic(pane_id):
    data = _load()
    return data.get("pane_to_topic", {}).get(pane_id, "")
```

В `_with_lock` добавить `data.setdefault("pane_to_topic", {})`.

В `cleanup_pane` добавить: `data.get("pane_to_topic", {}).pop(pane_id, None)`.

CLI: добавить `save_topic` и `get_topic` в `if __name__ == "__main__"` блок.

- [ ] **Step 2: Проверить вручную**

```bash
python3 tg_sessions.py save_topic "%0.100" "42"
python3 tg_sessions.py get_topic "%0.100"
# Ожидание: 42
python3 tg_sessions.py cleanup_pane "%0.100"
python3 tg_sessions.py get_topic "%0.100"
# Ожидание: пусто
```

- [ ] **Step 3: Commit**

```bash
git add tg_sessions.py
git commit -m "Add pane_to_topic mapping to session storage"
```

---

### Task 2: Обновить tg_reply_watcher.py — создание топиков и маршрутизация

**Files:**
- Modify: `tg_reply_watcher.py`

- [ ] **Step 1: Добавить helper `create_forum_topic`**

После функции `tg_react` (~строка 91):

```python
# Палитра цветов для топиков (ротация)
TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]
_topic_color_idx = 0

def create_forum_topic(name):
    """Создаёт Forum Topic в супергруппе, возвращает message_thread_id или None."""
    global _topic_color_idx
    color = TOPIC_COLORS[_topic_color_idx % len(TOPIC_COLORS)]
    _topic_color_idx += 1
    resp = tg_api("createForumTopic", {
        "chat_id": CHAT_ID,
        "name": name[:128],
        "icon_color": color,
    })
    if resp.get("ok"):
        return resp["result"]["message_thread_id"]
    logger.error(f"[Topic] Failed to create: {resp}")
    return None
```

- [ ] **Step 2: Обновить `tg_api` — поддержка `message_thread_id` в sendMessage**

Добавить helper для отправки в топик. Заменить все прямые вызовы `tg_api("sendMessage", {...})` будет муторно, поэтому создаём обёртку:

```python
def tg_send(text, topic_id=None, reply_to=None, parse_mode=None, reply_markup=None):
    """Отправка сообщения с поддержкой Forum Topics."""
    params = {"chat_id": CHAT_ID, "text": text}
    if topic_id:
        params["message_thread_id"] = topic_id
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = "true"
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup:
        params["reply_markup"] = reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup)
    return tg_api("sendMessage", params)
```

- [ ] **Step 3: Обновить `handle_newstart` — создавать топик**

Ключевые изменения в `handle_newstart`:

1. После определения `project_name` — создать топик:
```python
topic_id = create_forum_topic(f"🚀 {project_name}")
if not topic_id:
    tg_send("❌ Не удалось создать топик", reply_to=user_msg_id)
    return
```

2. Заменить `tg_api("sendMessage", {...})` на `tg_send(...)` с `topic_id`.

3. После получения `pane_id` — сохранить маппинг:
```python
tg_sessions.save_topic(pane_id, topic_id)
```

4. Сообщение "🚀 Claude Code запущен" отправлять в созданный топик.

- [ ] **Step 4: Обновить `handle_projects`, `handle_sessions`, `handle_close`**

Эти команды должны отвечать в тот топик, из которого вызваны. Извлекаем `topic_id` из входящего сообщения:

```python
def get_msg_topic(msg):
    """Извлекает message_thread_id из входящего сообщения (если из топика)."""
    return msg.get("message_thread_id")
```

Во всех трёх функциях: заменить `tg_api("sendMessage", {...})` на `tg_send(text, topic_id=get_msg_topic(msg), ...)`.

- [ ] **Step 5: Обновить `handle_callback`**

В callback_query сообщение тоже содержит `message_thread_id`. Пробрасывать его в `tg_api("editMessageText")` и в ответные сообщения.

Для `start:` callback (запуск проекта) — `handle_newstart` уже создаст свой топик.

Для `approve:`/`reject:` — `editMessageText` не требует `message_thread_id` (работает по `chat_id` + `message_id`).

Для `close:` — аналогично, `editMessageText` работает без topic.

- [ ] **Step 6: Обновить main loop — извлекать topic из входящих**

В основном цикле (после строки 808) при обработке reply-сообщений ничего менять не нужно — маршрутизация идёт по `reply_to_message_id` → `pane_id`, а topic уже привязан к pane.

- [ ] **Step 7: Commit**

```bash
git add tg_reply_watcher.py
git commit -m "Add Forum Topics support — each session gets its own topic"
```

---

### Task 3: Обновить tg_notify_hook.sh — отправка в топик

**Files:**
- Modify: `tg_notify_hook.sh`

- [ ] **Step 1: Получить topic_id по TMUX_PANE**

После строки с `REPLY_TO` (строка 35) добавить:

```bash
TOPIC_ID=""
if [ -n "$PANE_ID" ]; then
    TOPIC_ID=$(python3 "$HELPER" get_topic "$PANE_ID" 2>/dev/null)
fi
```

- [ ] **Step 2: Добавить message_thread_id в curl**

В блок CURL_ARGS (строка 39+) добавить:

```bash
if [ -n "$TOPIC_ID" ]; then
    CURL_ARGS+=(-d "message_thread_id=${TOPIC_ID}")
fi
```

Также в ветку "не в tmux" (строка 24) — без topic (General).

- [ ] **Step 3: Commit**

```bash
git add tg_notify_hook.sh
git commit -m "Send notify hook messages to project's forum topic"
```

---

### Task 4: Обновить tg_stop_hook.sh — отправка в топик

**Files:**
- Modify: `tg_stop_hook.sh`

- [ ] **Step 1: Получить topic_id по TMUX_PANE**

После строки с `REPLY_TO` (строка 155) добавить:

```bash
TOPIC_ID=""
if [ -n "$PANE_ID" ]; then
    TOPIC_ID=$(python3 "$HELPER" get_topic "$PANE_ID" 2>/dev/null)
fi
```

- [ ] **Step 2: Добавить message_thread_id в curl**

В `CURL_ARGS` (строка 158+) добавить:

```bash
if [ -n "$TOPIC_ID" ]; then
    CURL_ARGS+=(-d "message_thread_id=${TOPIC_ID}")
fi
```

- [ ] **Step 3: Commit**

```bash
git add tg_stop_hook.sh
git commit -m "Send stop hook messages to project's forum topic"
```

---

### Task 5: Обновить конфиг и документацию

**Files:**
- Modify: `tg_config.sh.example`
- Modify: `README.md`

- [ ] **Step 1: Обновить tg_config.sh.example**

Добавить комментарий что `TG_CHAT_ID` теперь — ID супергруппы:

```bash
# ID супергруппы с Forum Topics (отрицательное число, например -1001234567890)
TG_CHAT_ID="YOUR_SUPERGROUP_ID"
```

- [ ] **Step 2: Обновить README.md**

В секции "Установка" добавить шаг по созданию супергруппы с Topics.

- [ ] **Step 3: Commit + push**

```bash
git add tg_config.sh.example README.md
git commit -m "Update config and docs for Forum Topics setup"
git push
```

---

### Task 6: Ручное тестирование

- [ ] **Step 1:** Пользователь создаёт супергруппу, включает Topics, добавляет бота админом
- [ ] **Step 2:** Обновить `tg_config.sh` — прописать ID супергруппы
- [ ] **Step 3:** Перезапустить watcher: `python3 -u ~/.claude/tg-integration/tg_reply_watcher.py`
- [ ] **Step 4:** Отправить `/newstart project` — должен создаться топик
- [ ] **Step 5:** Ответить reply-ем в топике — текст должен уйти в Claude
- [ ] **Step 6:** Claude отвечает — сообщение должно прийти в тот же топик
- [ ] **Step 7:** Проверить `/sessions`, `/close` из General и из топика
