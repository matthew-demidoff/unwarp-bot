# unWARP

Posts a random placeholder to a Telegram channel or group every N days, at a random time in a set window. 70% of posts get a random image.

Whitelist only. Admin id goes in `.env`, other users are added from the bot with `/add <id>`.

```
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

`/setup` walks through everything: messages (up to 5), images (up to 5), chat, window like `10:00-18:00`, period in days (`1`, `2`, `7`, `daily`, `weekly`). `/status` shows the next post.

Settings are kept in `config.json`. Times are in `TZ` if set, otherwise host time.
