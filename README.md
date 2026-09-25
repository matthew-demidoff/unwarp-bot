# unWARP

Posts a random placeholder (or a fresh post from an NVIDIA NIM model) to a Telegram channel or group every N days, at a random time in a set window. 70% of posts get a random image.

Whitelist only. Admin id goes in `.env`, other users are added from the bot with `/add <id>`.

```
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

`/setup` walks through everything: placeholder messages (up to 5) or NIM key, model and system prompt, images (up to 5), chat, window like `10:00-18:00`, period in days (`1`, `2`, `7`, `daily`, `weekly`). `/status` shows the next post, `/pushnow` posts right away without touching the schedule.

Settings, including NIM keys, are kept in `config.json`. Times are in `TZ` if set, otherwise host time.
