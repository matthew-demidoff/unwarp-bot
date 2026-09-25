import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import LinkPreviewOptions, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatMemberStatus, MessageOriginType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
TZ = ZoneInfo(os.environ["TZ"]) if os.getenv("TZ") else None
CONFIG = BASE / "config.json"
REPO = "https://github.com/matthew-demidoff/unwarp-bot"
DENIED = (
    "You are not permitted to use this service.\n"
    f"Host yours from [source code]({REPO}) or apply for whitelist by contacting @vevollo"
)
MAX_ITEMS = 5
IMAGE_CHANCE = 0.7
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
PERIODS = {"daily": 1, "weekly": 7}
TICK_SECONDS = 30
NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MAX_TOKENS = 4096
NIM_TIMEOUT = 120
NIM_ASK = "Write the next post."
PLACEHOLDERS, NIM = "Placeholders", "NVIDIA NIM"

MODE, MESSAGES, KEY, MODEL, PROMPT, IMAGES, CHAT, WINDOW, PERIOD = range(9)

log = logging.getLogger("unwarp")
cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {"whitelist": [], "users": {}}
whitelist = filters.User(user_id=[ADMIN_ID, *cfg["whitelist"]])
fresh = filters.UpdateType.MESSAGE
private = filters.ChatType.PRIVATE & fresh
text_only = fresh & filters.TEXT & ~filters.COMMAND


def save():
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def hhmm(minutes):
    return f"{minutes // 60:02}:{minutes % 60:02}"


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")


def pick_time(window, day, not_before):
    midnight = datetime.combine(day, datetime.min.time(), TZ)
    start = midnight + timedelta(minutes=window[0])
    end = midnight + timedelta(minutes=window[1])
    if end <= start:
        end += timedelta(days=1)
    start = max(start, not_before)
    if start >= end:
        return None
    return random.uniform(start.timestamp(), end.timestamp())


def next_post(window, day):
    now = datetime.now(TZ)
    while (ts := pick_time(window, day, now)) is None:
        day += timedelta(days=1)
    return ts


def parse_window(text):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text.strip())
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59 or (h1, m1) == (h2, m2):
        return None
    return [h1 * 60 + m1, h2 * 60 + m2]


def window_day(u):
    # windows past midnight belong to the day they opened
    return (datetime.fromtimestamp(u["next_post"], TZ) - timedelta(minutes=u["window"][0])).date()


def describe(u):
    text = f"NIM {u['nim']['model']}" if u.get("nim") else f"{len(u['messages'])} placeholders"
    return (
        f"Chat: {u['chat_title']} ({u['chat_id']})\n"
        f"Text: {text}, images: {len(u['images'])}\n"
        f"Window: {hhmm(u['window'][0])}-{hhmm(u['window'][1])}\n"
        f"Every {u['period']} day(s)\n"
        f"Next post: {fmt_ts(u['next_post'])}"
    )


async def nim_chat(key, model, messages, max_tokens=NIM_MAX_TOKENS):
    async with httpx.AsyncClient(timeout=NIM_TIMEOUT) as client:
        r = await client.post(
            NIM_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 1.0},
        )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


async def post(bot, u):
    if nim := u.get("nim"):
        messages = [{"role": "system", "content": nim["prompt"]}, {"role": "user", "content": NIM_ASK}]
        text, mode = (await nim_chat(nim["key"], nim["model"], messages))[:MESSAGE_LIMIT], None
    else:
        text, mode = random.choice(u["messages"]), ParseMode.HTML
    if u["images"] and random.random() < IMAGE_CHANCE:
        photo = random.choice(u["images"])
        if len(text) <= CAPTION_LIMIT:
            await bot.send_photo(u["chat_id"], photo, caption=text, parse_mode=mode)
            return
        await bot.send_photo(u["chat_id"], photo)
    await bot.send_message(u["chat_id"], text, parse_mode=mode)


async def poster(bot):
    while True:
        now = datetime.now(TZ).timestamp()
        for uid, u in list(cfg["users"].items()):
            if u["next_post"] > now:
                continue
            try:
                await post(bot, u)
            except Exception:
                log.exception("post failed for %s", uid)
            u["next_post"] = next_post(u["window"], window_day(u) + timedelta(days=u["period"]))
            save()
        await asyncio.sleep(TICK_SECONDS)


async def reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        DENIED, parse_mode=ParseMode.MARKDOWN, link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "unWARP posts a random placeholder or AI written post to your channel or group at a random time within your window.\n\n"
        "/setup - configure posts, chat and schedule\n"
        "/status - current settings\n"
        "/pushnow - post right now\n"
        "/cancel - abort setup"
    )
    if update.effective_user.id == ADMIN_ID:
        text += "\n\n/add <user id> - whitelist a user\n/remove <user id> - remove a user\n/users - list whitelist"
    await update.message.reply_text(text)


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = cfg["users"].get(str(update.effective_user.id))
    await update.message.reply_text(describe(u) if u else "Not set up yet, use /setup")


async def push_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = cfg["users"].get(str(update.effective_user.id))
    if not u:
        await update.message.reply_text("Not set up yet, use /setup")
        return
    try:
        await post(ctx.bot, u)
    except (TelegramError, httpx.HTTPError) as e:
        await update.message.reply_text(f"Post failed: {e}")
        return
    await update.message.reply_text(f"Posted. Next scheduled post: {fmt_ts(u['next_post'])}")


async def setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["draft"] = {"messages": [], "images": []}
    await update.message.reply_text(
        "Where should post text come from?",
        reply_markup=ReplyKeyboardMarkup([[PLACEHOLDERS, NIM]], one_time_keyboard=True, resize_keyboard=True),
    )
    return MODE


async def set_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == NIM:
        await update.message.reply_text("Send your NVIDIA NIM API key.", reply_markup=ReplyKeyboardRemove())
        return KEY
    if update.message.text == PLACEHOLDERS:
        await update.message.reply_text(
            f"Send up to {MAX_ITEMS} placeholder messages, one per message. /done when finished.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return MESSAGES
    await update.message.reply_text(f"Pick {PLACEHOLDERS} or {NIM}.")
    return MODE


async def add_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    messages = ctx.user_data["draft"]["messages"]
    messages.append(update.message.text_html)
    if len(messages) >= MAX_ITEMS:
        return await ask_images(update, ctx)
    await update.message.reply_text(f"Saved {len(messages)}/{MAX_ITEMS}. Send another or /done.")
    return MESSAGES


async def set_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["draft"]["nim"] = {"key": update.message.text.strip()}
    await update.message.delete()
    await update.message.reply_text(
        "Got the key and deleted your message. Which model? Model ID from https://build.nvidia.com/models, "
        "e.g. deepseek-ai/deepseek-v4.1-flash"
    )
    return MODEL


async def set_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nim = ctx.user_data["draft"]["nim"]
    model = update.message.text.strip()
    try:
        await nim_chat(nim["key"], model, [{"role": "user", "content": "hi"}], max_tokens=1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            await update.message.reply_text("NIM rejected the key, send it again.")
            return KEY
        await update.message.reply_text(f"Model doesn't work ({e.response.status_code}), send another one.")
        return MODEL
    except httpx.HTTPError as e:
        await update.message.reply_text(f"Can't reach NIM: {e}")
        return MODEL

    nim["model"] = model
    await update.message.reply_text("Now send the system prompt, it decides what the posts are about and how they read.")
    return PROMPT


async def set_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["draft"]["nim"]["prompt"] = update.message.text
    return await ask_images(update, ctx)


async def ask_images(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data["draft"]["messages"] and "nim" not in ctx.user_data["draft"]:
        await update.message.reply_text("Send at least one message first.")
        return MESSAGES
    await update.message.reply_text(f"Now send up to {MAX_ITEMS} placeholder images. /done when finished (or now to skip).")
    return IMAGES


async def add_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    images = ctx.user_data["draft"]["images"]
    images.append(update.message.photo[-1].file_id)
    if len(images) >= MAX_ITEMS:
        return await ask_chat(update, ctx)
    await update.message.reply_text(f"Saved {len(images)}/{MAX_ITEMS}. Send another or /done.")
    return IMAGES


async def ask_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Add me to your channel or group as admin, then send its ID "
        "(like -1001234567890 or @channelname) or forward me any post from the channel."
    )
    return CHAT


async def set_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    origin = update.message.forward_origin
    if origin and origin.type == MessageOriginType.CHANNEL:
        ref = origin.chat.id
    else:
        ref = (update.message.text or "").strip()
    if not ref:
        await update.message.reply_text("Send the chat ID or forward a post from the channel.")
        return CHAT

    try:
        chat = await ctx.bot.get_chat(ref)
        me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
        member = await ctx.bot.get_chat_member(chat.id, update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"Can't access that chat: {e.message}")
        return CHAT
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        await update.message.reply_text("I'm not an admin there yet. Promote me and send the ID again.")
        return CHAT
    # no posting into someone elses channel
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await update.message.reply_text("You need to be an admin of that chat too.")
        return CHAT

    ctx.user_data["draft"].update(chat_id=chat.id, chat_title=chat.title or chat.username or str(chat.id))
    await update.message.reply_text(f"Got {chat.title}. When should I post? Send a time window like 10:00-18:00.")
    return WINDOW


async def set_window(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    window = parse_window(update.message.text)
    if not window:
        await update.message.reply_text("Format is HH:MM-HH:MM, e.g. 10:00-18:00.")
        return WINDOW

    ctx.user_data["draft"]["window"] = window
    await update.message.reply_text(
        "How often? Number of days between posts (1 = every day, 2 = every other day, 7 = weekly), or daily/weekly."
    )
    return PERIOD


async def set_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip().lower()
    period = PERIODS.get(t) or (int(t) if t.isdigit() else 0)
    if period < 1:
        await update.message.reply_text("Send a positive number of days, or daily/weekly.")
        return PERIOD

    u = ctx.user_data.pop("draft")
    u["period"] = period
    u["next_post"] = next_post(u["window"], datetime.now(TZ).date())
    cfg["users"][str(update.effective_user.id)] = u
    save()
    await update.message.reply_text("All set.\n\n" + describe(u))
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("draft", None)
    await update.message.reply_text("Setup cancelled, previous settings kept.")
    return ConversationHandler.END


def arg_id(ctx):
    return int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None


async def add_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = arg_id(ctx)
    if uid is None:
        await update.message.reply_text("Usage: /add <user id>")
        return
    if uid not in cfg["whitelist"]:
        cfg["whitelist"].append(uid)
        save()
    whitelist.add_user_ids(uid)
    await update.message.reply_text(f"{uid} whitelisted.")


async def remove_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = arg_id(ctx)
    if uid is None or uid == ADMIN_ID:
        await update.message.reply_text("Usage: /remove <user id> (not the admin)")
        return
    if uid in cfg["whitelist"]:
        cfg["whitelist"].remove(uid)
    cfg["users"].pop(str(uid), None)
    save()
    whitelist.remove_user_ids(uid)
    await update.message.reply_text(f"{uid} removed, their posting stopped.")


async def list_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [f"{uid} {'configured' if str(uid) in cfg['users'] else 'not set up'}" for uid in cfg["whitelist"]]
    await update.message.reply_text("\n".join(lines) or "Whitelist is empty.")


async def post_init(app: Application):
    app.bot_data["poster"] = asyncio.create_task(poster(app.bot))


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    allowed = private & whitelist
    admin = private & filters.User(ADMIN_ID)

    app.add_handler(MessageHandler(private & ~whitelist, reject))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setup", setup, filters=allowed)],
        states={
            MODE: [MessageHandler(text_only, set_mode)],
            MESSAGES: [MessageHandler(text_only, add_message), CommandHandler("done", ask_images)],
            KEY: [MessageHandler(text_only, set_key)],
            MODEL: [MessageHandler(text_only, set_model)],
            PROMPT: [MessageHandler(text_only, set_prompt)],
            IMAGES: [MessageHandler(fresh & filters.PHOTO, add_image), CommandHandler("done", ask_chat)],
            CHAT: [MessageHandler(fresh & filters.FORWARDED | text_only, set_chat)],
            WINDOW: [MessageHandler(text_only, set_window)],
            PERIOD: [MessageHandler(text_only, set_period)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    ))
    app.add_handler(CommandHandler("start", start, filters=allowed))
    app.add_handler(CommandHandler("status", status, filters=allowed))
    app.add_handler(CommandHandler("pushnow", push_now, filters=allowed))
    app.add_handler(CommandHandler("add", add_user, filters=admin))
    app.add_handler(CommandHandler("remove", remove_user, filters=admin))
    app.add_handler(CommandHandler("users", list_users, filters=admin))
    app.run_polling()


if __name__ == "__main__":
    main()
