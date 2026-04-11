# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import shutil
import psutil
import asyncio
from time import time

from pyleaves import Leaves
from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, BadRequest, FloodWait
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from helpers.utils import processMediaGroup, progressArgs, send_media
from helpers.forward import check_forward_permission, resolve_forward_chat_id
from helpers.files import (
    get_download_path, fileSizeLimit, get_readable_file_size,
    get_readable_time, cleanup_download, cleanup_downloads_root
)
from helpers.msg import getChatMsgID, get_file_name, get_raw_text
from config import PyroConf
from logger import LOGGER

from gdrive_helper import upload_to_drive_user, get_user_credentials, generate_auth_url, authorize_user
from db_helper import get_user_role, set_user_role, get_all_users

bot = Client(
    "media_bot", api_id=PyroConf.API_ID, api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN, workers=100, parse_mode=ParseMode.MARKDOWN,
    max_concurrent_transmissions=1, sleep_threshold=30,
)

user = Client(
    "user_session", workers=100, session_string=PyroConf.SESSION_STRING,
    max_concurrent_transmissions=1, sleep_threshold=30,
)

RUNNING_TASKS = set()
download_semaphore = None
forward_chat_id = None

PENDING_DOWNLOADS = {}
AWAITING_AUTH = {}

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    def _remove(_):
        RUNNING_TASKS.discard(task)
    task.add_done_callback(_remove)
    return task

# ✅ --- ADMIN DASHBOARD ---
@bot.on_message(filters.command("admin") & filters.private)
async def admin_dashboard(_, message: Message):
    if get_user_role(message.from_user.id) != "admin":
        return await message.reply("⛔️ **Access Denied.** You are not an admin.")
    
    users = get_all_users()
    total_users = len(users)
    vips = sum(1 for role in users.values() if role == "vip")
    banned = sum(1 for role in users.values() if role == "banned")

    dash_text = (
        "👑 **Admin Dashboard**\n\n"
        f"👥 **Total Registered Users:** `{total_users}`\n"
        f"⭐️ **VIP Users:** `{vips}`\n"
        f"🚫 **Banned Users:** `{banned}`\n\n"
        "**Admin Commands:**\n"
        "`/promote <user_id>` - Grant VIP status\n"
        "`/demote <user_id>` - Remove VIP status\n"
        "`/ban <user_id>` - Block user from bot\n"
        "`/unban <user_id>` - Restore access"
    )
    await message.reply(dash_text)

@bot.on_message(filters.command(["promote", "demote", "ban", "unban"]) & filters.private)
async def manage_users(_, message: Message):
    if get_user_role(message.from_user.id) != "admin":
        return

    if len(message.command) < 2:
        return await message.reply("⚠️ Please provide a User ID. Example: `/promote 123456789`")

    target_id = message.command[1]
    action = message.command[0]

    if action == "promote":
        set_user_role(target_id, "vip")
        await message.reply(f"✅ User `{target_id}` promoted to **VIP**.")
    elif action == "demote" or action == "unban":
        set_user_role(target_id, "user")
        await message.reply(f"✅ User `{target_id}` reverted to standard **User**.")
    elif action == "ban":
        set_user_role(target_id, "banned")
        await message.reply(f"🚫 User `{target_id}` has been **Banned**.")

# ✅ --- CORE COMMANDS ---
@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    if get_user_role(message.from_user.id) == "banned":
        return await message.reply("🚫 You are banned from using this bot.")
        
    welcome_text = (
        "👋 **Welcome to Media Downloader Bot!**\n\n"
        "I can grab photos, videos, audio, and documents from any Telegram post.\n"
        "Just send me a link (paste it directly or use `/dl <link>`),\n"
        "or reply to a message with `/dl`.\n\n"
        "Ready? Send me a Telegram post link!"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]])
    await message.reply(welcome_text, reply_markup=markup, disable_web_page_preview=True)

@bot.on_message(filters.command("cleanup") & filters.private)
async def cleanup_storage(_, message: Message):
    if get_user_role(message.from_user.id) == "banned": return
    try:
        files_removed, bytes_freed = cleanup_downloads_root()
        if files_removed == 0:
            return await message.reply("🧹 **Cleanup complete:** no local downloads found.")
        return await message.reply(f"🧹 **Cleanup complete:** removed `{files_removed}` file(s), freed `{get_readable_file_size(bytes_freed)}`.")
    except Exception as e:
        LOGGER(__name__).error(f"Cleanup failed: {e}")
        return await message.reply("❌ **Cleanup failed.** Check logs for details.")

# ✅ --- DOWNLOAD LOGIC ---
async def handle_download(bot: Client, message: Message, post_url: str, destination: str = "tg"):
    global forward_chat_id
    async with download_semaphore:
        if "?" in post_url: post_url = post_url.split("?", 1)[0]

        try:
            effective_forward_chat_id = None
            if forward_chat_id:
                ok, err_msg = await check_forward_permission(bot, forward_chat_id)
                if ok: effective_forward_chat_id = forward_chat_id

            chat_id, message_id = getChatMsgID(post_url)
            chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)

            if chat_message.document or chat_message.video or chat_message.audio:
                file_size = (
                    chat_message.document.file_size if chat_message.document
                    else chat_message.video.file_size if chat_message.video
                    else chat_message.audio.file_size
                )
                if not await fileSizeLimit(file_size, message, "download", user.me.is_premium): return

            raw_caption, raw_caption_entities = get_raw_text(chat_message.caption, chat_message.caption_entities)
            raw_text, raw_text_entities = get_raw_text(chat_message.text, chat_message.entities)

            if chat_message.media_group_id:
                if destination == "gdrive":
                    await message.reply("⚠️ **Notice:** Google Drive upload does not natively support Telegram Albums. Uploading Album to Telegram instead.")
                if not await processMediaGroup(chat_message, bot, message, forward_chat_id=effective_forward_chat_id):
                    await message.reply("**Could not extract any valid media from the media group.**")
                return

            has_downloadable_media = (
                chat_message.photo or chat_message.video or chat_message.audio or 
                chat_message.document or chat_message.voice or chat_message.video_note or 
                chat_message.animation or chat_message.sticker
            )

            if has_downloadable_media:
                start_time = time()
                progress_message = await message.reply("**📥 Downloading Progress...**")

                filename = get_file_name(message_id, chat_message)
                download_path = get_download_path(message.id, filename)

                media_path = None
                for attempt in range(2):
                    try:
                        media_path = await chat_message.download(
                            file_name=download_path,
                            progress=Leaves.progress_for_pyrogram,
                            progress_args=progressArgs("📥 Downloading Progress", progress_message, start_time),
                        )
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        if wait_s > 0 and attempt == 0:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise

                if not media_path or not os.path.exists(media_path):
                    return await progress_message.edit("**❌ Download failed: File not saved properly**")

                # ✅ Multi-Cloud Router
                await progress_message.edit(f"**☁️ Uploading securely to {destination.upper()}...**")
                try:
                    loop = asyncio.get_event_loop()
                    link = None
                    
                    if destination == "gdrive":
                        link = await loop.run_in_executor(None, upload_to_drive_user, message.from_user.id, media_path)
                    
                    # Placeholder for future dropbox logic
                    # elif destination == "dropbox":
                    #    link = await loop.run_in_executor(None, upload_to_dropbox, message.from_user.id, media_path)
                    
                    elif destination == "tg":
                        media_type = ("photo" if chat_message.photo else "video" if chat_message.video else "audio" if chat_message.audio else "document")
                        await send_media(
                            bot, message, media_path, media_type, raw_caption,
                            raw_caption_entities, progress_message, start_time,
                            forward_chat_id=effective_forward_chat_id,
                        )

                    if link:
                        await message.reply(f"✅ **Saved to your {destination.upper()}!**\n\n[🔗 View File Here]({link})", disable_web_page_preview=True)
                    
                    await progress_message.delete()
                        
                except Exception as e:
                    await progress_message.edit(f"❌ **Upload Failed:** `{e}`")

                cleanup_download(media_path)

            elif chat_message.text or chat_message.caption:
                txt = raw_text or raw_caption
                ents = raw_text_entities if raw_text else raw_caption_entities
                if destination != "tg":
                    await message.reply("📝 **Notice:** Texts cannot be saved to Cloud. Forwarding to Telegram instead.")
                try:
                    await message.reply(txt, entities=ents or None)
                except BadRequest:
                    await message.reply(txt)

        except Exception as e:
            await message.reply(f"**❌ Error processing link:** `{e}`")

# ✅ --- CALLBACK INTERFACE ---
@bot.on_callback_query(filters.regex(r"^dest_"))
async def process_dest_choice(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    choice = callback_query.data.split("_")[1]

    if user_id not in PENDING_DOWNLOADS:
        return await callback_query.answer("No pending downloads found. Please send the link again.", show_alert=True)

    data = PENDING_DOWNLOADS[user_id]
    url = data["url"]
    msg = data["message"]

    if choice == "gdrive":
        if not get_user_credentials(user_id):
            auth_url = generate_auth_url(user_id)
            AWAITING_AUTH[user_id] = True
            
            auth_message = (
                "⚠️ **Google Drive is not linked!**\n\n"
                f"1. [Click Here to Login securely via Google]({auth_url})\n"
                "2. Choose your account and grant permission.\n"
                "3. Your browser will eventually redirect to a page that says **'Site cannot be reached'**.\n"
                "4. **Copy that ENTIRE URL** from your browser's address bar and paste it as a message here."
            )
            return await callback_query.message.edit_text(auth_message, disable_web_page_preview=True)

    PENDING_DOWNLOADS.pop(user_id)
    await callback_query.message.delete()
    await track_task(handle_download(bot, msg, url, destination=choice))

# ✅ --- BATCH DOWNLOAD (VIP ONLY) ---
@bot.on_message(filters.command("bdl") & filters.private)
async def download_range(bot: Client, message: Message):
    role = get_user_role(message.from_user.id)
    if role not in ["vip", "admin"]:
        return await message.reply(
            "⭐️ **VIP Feature Only**\n\n"
            "Batch downloading is restricted to VIP members to conserve server bandwidth. "
            "Please contact the admin to upgrade your account."
        )

    args = message.text.split()
    if len(args) != 3 or not all(arg.startswith("https://t.me/") for arg in args[1:]):
        return await message.reply("🚀 **Batch Download Process**\n`/bdl start_link end_link`")

    try:
        start_chat, start_id = getChatMsgID(args[1])
        end_chat,   end_id   = getChatMsgID(args[2])
    except Exception as e:
        return await message.reply(f"**❌ Error parsing links:\n{e}**")

    prefix = args[1].rsplit("/", 1)[0]
    loading = await message.reply(f"📥 **Downloading posts {start_id}–{end_id}…**")

    downloaded = skipped = failed = 0
    batch_tasks = []

    for msg_id in range(start_id, end_id + 1):
        url = f"{prefix}/{msg_id}"
        try:
            task = track_task(handle_download(bot, message, url, destination="tg"))
            batch_tasks.append(task)

            if len(batch_tasks) >= PyroConf.BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception): failed += 1
                    else: downloaded += 1
                batch_tasks.clear()
                await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)

        except Exception as e:
            failed += 1

    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception): failed += 1
            else: downloaded += 1

    await loading.delete()
    await message.reply(f"**✅ Batch Complete!**\n📥 **Downloaded**: `{downloaded}`\n❌ **Failed**: `{failed}`")

# ✅ --- MAIN MESSAGE ROUTER & AUTH LISTENER ---
@bot.on_message(filters.private & ~filters.command(["start", "help", "cleanup", "cancel", "admin", "promote", "demote", "ban", "unban", "bdl", "dl", "stats", "logs", "killall"]))
async def handle_any_message(bot: Client, message: Message):
    user_id = message.from_user.id
    if get_user_role(user_id) == "banned": return
    
    text = message.text or ""

    if AWAITING_AUTH.get(user_id):
        if text.startswith("http://localhost") or text.startswith("http://127.0.0.1"):
            try:
                msg = await message.reply("⏳ Verifying your Google Drive connection...")
                authorize_user(user_id, text)
                
                del AWAITING_AUTH[user_id]
                await msg.edit_text("✅ **Successfully linked!** Your Google Drive is ready.")
                
                if user_id in PENDING_DOWNLOADS:
                    data = PENDING_DOWNLOADS.pop(user_id)
                    await track_task(handle_download(bot, data["message"], data["url"], destination="gdrive"))
            except Exception as e:
                await message.reply(f"❌ **Failed to authorize:** `{e}`\n\nPlease send `/cancel` and try again.")
            return
        else:
            return await message.reply("⚠️ **You are currently linking Google Drive.**\nPlease paste the `http://localhost...` link, or send `/cancel`.")

    if text and not text.startswith("/"):
        PENDING_DOWNLOADS[user_id] = {"url": text, "message": message}
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Send here on Telegram", callback_data="dest_tg")],
            [InlineKeyboardButton("☁️ Google Drive", callback_data="dest_gdrive")]
            # [InlineKeyboardButton("🟦 Dropbox", callback_data="dest_dropbox")] # Uncomment when ready
        ])
        await message.reply("Where would you like to save this file?", reply_markup=keyboard)

@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_auth(_, message: Message):
    user_id = message.from_user.id
    if AWAITING_AUTH.pop(user_id, None):
        PENDING_DOWNLOADS.pop(user_id, None)
        await message.reply("✅ Process cancelled.")
    else:
        await message.reply("Nothing to cancel.")

@bot.on_message(filters.command("stats") & filters.private)
async def stats(_, message: Message):
    try:
        start_time = getattr(PyroConf, "BOT_START_TIME", time())
        currentTime = get_readable_time(time() - start_time)
        
        total, used, free = shutil.disk_usage(".")
        try:
            sent = get_readable_file_size(psutil.net_io_counters().bytes_sent)
            recv = get_readable_file_size(psutil.net_io_counters().bytes_recv)
        except Exception:
            sent = recv = "Restricted"
            
        cpuUsage = psutil.cpu_percent(interval=0.5)
        memory = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
        process = psutil.Process(os.getpid())
        memory_mb = round(process.memory_info()[0] / 1024**2)

        stats_msg = (
            "**≧◉◡◉≦ Bot is Up and Running.**\n\n"
            f"**➜ Uptime:** `{currentTime}`\n"
            f"**➜ Disk:** `{get_readable_file_size(used)}` / `{get_readable_file_size(total)}`\n"
            f"**➜ Memory:** `{memory_mb} MiB`\n\n"
            f"**➜ CPU:** `{cpuUsage}%` | **➜ RAM:** `{memory}%` | **➜ DISK:** `{disk}%`"
        )
        await message.reply(stats_msg)
    except Exception as e:
        await message.reply(f"❌ **Stats Error:** `{e}`")

@bot.on_message(filters.command("logs") & filters.private)
async def logs(_, message: Message):
    if os.path.exists("logs.txt"): await message.reply_document(document="logs.txt")
    else: await message.reply("**Not exists**")

@bot.on_message(filters.command("killall") & filters.private)
async def cancel_all_tasks(_, message: Message):
    cancelled = 0
    for task in list(RUNNING_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    await message.reply(f"**Cancelled {cancelled} running task(s).**")

async def initialize():
    global download_semaphore, forward_chat_id
    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)
    if PyroConf.FORWARD_CHAT_ID:
        forward_chat_id = await resolve_forward_chat_id(PyroConf.FORWARD_CHAT_ID)
        LOGGER(__name__).info(f"Auto-forward enabled. Target chat: {forward_chat_id}")

if __name__ == "__main__":
    try:
        LOGGER(__name__).info("Bot Started!")
        asyncio.get_event_loop().run_until_complete(initialize())
        user.start()
        bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
    finally:
        LOGGER(__name__).info("Bot Stopped")