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

# ✅ Import our new per-user GDrive logic
from gdrive_helper import upload_to_drive_user, get_user_credentials, generate_auth_url, authorize_user

# Initialize the bot client
bot = Client(
    "media_bot", api_id=PyroConf.API_ID, api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN, workers=100, parse_mode=ParseMode.MARKDOWN,
    max_concurrent_transmissions=1, sleep_threshold=30,
)

# Client for user session
user = Client(
    "user_session", workers=100, session_string=PyroConf.SESSION_STRING,
    max_concurrent_transmissions=1, sleep_threshold=30,
)

RUNNING_TASKS = set()
download_semaphore = None
forward_chat_id = None

# State tracking dictionaries
PENDING_DOWNLOADS = {}
AWAITING_AUTH = {}

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    def _remove(_):
        RUNNING_TASKS.discard(task)
    task.add_done_callback(_remove)
    return task

@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
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
    try:
        files_removed, bytes_freed = cleanup_downloads_root()
        if files_removed == 0:
            return await message.reply("🧹 **Cleanup complete:** no local downloads found.")
        return await message.reply(f"🧹 **Cleanup complete:** removed `{files_removed}` file(s), freed `{get_readable_file_size(bytes_freed)}`.")
    except Exception as e:
        LOGGER(__name__).error(f"Cleanup failed: {e}")
        return await message.reply("❌ **Cleanup failed.** Check logs for details.")

async def handle_download(bot: Client, message: Message, post_url: str, destination: str = "tg"):
    global forward_chat_id
    async with download_semaphore:
        if "?" in post_url:
            post_url = post_url.split("?", 1)[0]

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
                if not await fileSizeLimit(file_size, message, "download", user.me.is_premium):
                    return

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
                    await progress_message.edit("**❌ Download failed: File not saved properly**")
                    return

                # ✅ --- GOOGLE DRIVE UPLOAD LOGIC ---
                if destination == "gdrive":
                    await progress_message.edit("**☁️ Uploading securely to your Google Drive...**")
                    try:
                        loop = asyncio.get_event_loop()
                        # Pass the specific user_id to the Drive function
                        drive_link = await loop.run_in_executor(None, upload_to_drive_user, message.from_user.id, media_path)
                        
                        await message.reply(f"✅ **Saved to your Google Drive in 'Telegram downloads' folder!**\n\n[🔗 View File Here]({drive_link})", disable_web_page_preview=True)
                        await progress_message.delete()
                    except Exception as e:
                        await progress_message.edit(f"❌ **Google Drive Upload Failed:** `{e}`")
                
                # ✅ --- TELEGRAM UPLOAD LOGIC ---
                else:
                    media_type = ("photo" if chat_message.photo else "video" if chat_message.video else "audio" if chat_message.audio else "document")
                    await send_media(
                        bot, message, media_path, media_type, raw_caption,
                        raw_caption_entities, progress_message, start_time,
                        forward_chat_id=effective_forward_chat_id,
                    )
                    await progress_message.delete()

                cleanup_download(media_path)

            elif chat_message.text or chat_message.caption:
                txt = raw_text or raw_caption
                ents = raw_text_entities if raw_text else raw_caption_entities
                if destination == "gdrive":
                    await message.reply("📝 **Notice:** Texts cannot be saved to Drive. Forwarding to Telegram instead.")
                try:
                    await message.reply(txt, entities=ents or None)
                except BadRequest:
                    await message.reply(txt)

        except Exception as e:
            await message.reply(f"**❌ Error processing link:** `{e}`")


@bot.on_callback_query(filters.regex(r"^dest_(tg|gdrive)$"))
async def process_dest_choice(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    choice = callback_query.data.split("_")[1]

    if user_id not in PENDING_DOWNLOADS:
        return await callback_query.answer("No pending downloads found. Please send the link again.", show_alert=True)

    data = PENDING_DOWNLOADS[user_id]
    url = data["url"]
    msg = data["message"]

    # ✅ IF G-DRIVE: Check if user has linked their account
    if choice == "gdrive":
        if not get_user_credentials(user_id):
            auth_url = generate_auth_url()
            AWAITING_AUTH[user_id] = True
            
            auth_message = (
                "⚠️ **Google Drive is not linked!**\n\n"
                f"1. [Click Here to Login securely via Google]({auth_url})\n"
                "2. Choose your account and grant permission.\n"
                "3. Your browser will eventually redirect to a page that says **'Site cannot be reached'** (the URL will start with `http://localhost...`).\n"
                "4. **Copy that ENTIRE URL** from your browser's address bar and paste it as a message here to complete the link."
            )
            return await callback_query.message.edit_text(auth_message, disable_web_page_preview=True)

    # Remove buttons and proceed
    PENDING_DOWNLOADS.pop(user_id)
    await callback_query.message.delete()
    await track_task(handle_download(bot, msg, url, destination=choice))


# ✅ --- CATCH ALL MESSAGES & AUTH CODES ---
@bot.on_message(filters.private & ~filters.command(["start", "help", "cleanup", "cancel"]))
async def handle_any_message(bot: Client, message: Message):
    user_id = message.from_user.id
    text = message.text or ""

    # If the bot is waiting for the user to paste their localhost Auth link
    if AWAITING_AUTH.get(user_id):
        if text.startswith("http://localhost") or text.startswith("http://127.0.0.1"):
            try:
                msg = await message.reply("⏳ Verifying your Google Drive connection...")
                authorize_user(user_id, text)  # Process the pasted URL
                
                del AWAITING_AUTH[user_id]
                await msg.edit_text("✅ **Successfully linked!** Your Google Drive is ready.")
                
                # Automatically resume their download since they successfully connected
                if user_id in PENDING_DOWNLOADS:
                    data = PENDING_DOWNLOADS.pop(user_id)
                    await track_task(handle_download(bot, data["message"], data["url"], destination="gdrive"))
            except Exception as e:
                await message.reply(f"❌ **Failed to authorize:** `{e}`\n\nPlease click the login link again or send `/cancel` to stop.")
            return
        else:
            return await message.reply("⚠️ **You are currently linking Google Drive.**\n\nPlease paste the `http://localhost...` link you got from your browser, or send `/cancel` to abort.")

    # Normal user sending a telegram post link
    if text and not text.startswith("/"):
        PENDING_DOWNLOADS[user_id] = {"url": text, "message": message}
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Send here on Telegram", callback_data="dest_tg")],
            [InlineKeyboardButton("☁️ Upload to my Google Drive", callback_data="dest_gdrive")]
        ])
        await message.reply("Where would you like to save this file?", reply_markup=keyboard)


@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_auth(_, message: Message):
    user_id = message.from_user.id
    if AWAITING_AUTH.pop(user_id, None):
        PENDING_DOWNLOADS.pop(user_id, None)
        await message.reply("✅ Google Drive linking cancelled.")
    else:
        await message.reply("Nothing to cancel.")


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
