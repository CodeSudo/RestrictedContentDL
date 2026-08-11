# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import shutil
import psutil
import asyncio
import re
from time import time

from pyleaves import Leaves
from pyrogram.enums import ParseMode
from pyrogram import Client, filters, compose
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

# Import our new per-user GDrive logic
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
        "For Batch downloads, use a range: `/bdl https://t.me/channel/10-150`\n"
        "Or bridge two links: `/bdl https://t.me/c/10 https://t.me/c/150`\n\n"
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

async def handle_download(bot: Client, message: Message, post_url: str, destination: str = "tg", processed_groups: set = None):
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
                    pass # Only warn once during batch, handled elsewhere to prevent spam
                
                # Check if this album was already processed by another link in a batch
                if processed_groups is not None:
                    if chat_message.media_group_id in processed_groups:
                        return # Skip downloading duplicate albums
                    processed_groups.add(chat_message.media_group_id)

                if not await processMediaGroup(chat_message, bot, message, forward_chat_id=effective_forward_chat_id):
                    await message.reply(f"**Could not extract any valid media from album link: {post_url}**")
                return

            has_downloadable_media = (
                chat_message.photo or chat_message.video or chat_message.audio or 
                chat_message.document or chat_message.voice or chat_message.video_note or 
                chat_message.animation or chat_message.sticker
            )

            if has_downloadable_media:
                start_time = time()
                progress_message = await message.reply(f"**📥 Downloading:** `{post_url.split('/')[-1]}`")

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
                    await progress_message.edit(f"**❌ Download failed for {post_url}: File not saved properly**")
                    return

                # --- GOOGLE DRIVE UPLOAD LOGIC ---
                if destination == "gdrive":
                    await progress_message.edit(f"**☁️ Uploading `{post_url.split('/')[-1]}` securely to your Google Drive...**")
                    try:
                        loop = asyncio.get_running_loop() 
                        drive_link = await loop.run_in_executor(None, upload_to_drive_user, message.from_user.id, media_path)
                        
                        await message.reply(f"✅ **Saved to your Google Drive!**\n\n[🔗 View File Here]({drive_link})", disable_web_page_preview=True)
                        await progress_message.delete()
                    except Exception as e:
                        await progress_message.edit(f"❌ **Google Drive Upload Failed:** `{e}`")
                
                # --- TELEGRAM UPLOAD LOGIC ---
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
                try:
                    await message.reply(txt, entities=ents or None)
                except BadRequest:
                    await message.reply(txt)

        except Exception as e:
            await message.reply(f"**❌ Error processing link `{post_url}`:** `{e}`")


@bot.on_callback_query(filters.regex(r"^dest_(tg|gdrive)$"))
async def process_dest_choice(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    choice = callback_query.data.split("_")[1]

    if user_id not in PENDING_DOWNLOADS:
        return await callback_query.answer("No pending downloads found. Please send the link again.", show_alert=True)

    data = PENDING_DOWNLOADS[user_id]
    urls = data["urls"]
    msg = data["message"]

    # IF G-DRIVE: Check if user has linked their account
    if choice == "gdrive":
        if not get_user_credentials(user_id):
            auth_url = generate_auth_url(user_id)
            AWAITING_AUTH[user_id] = True
            
            auth_message = (
                "⚠️ **Google Drive is not linked!**\n\n"
                f"1. [Click Here to Login securely via Google]({auth_url})\n"
                "2. Choose your account and grant permission.\n"
                "3. Your browser will eventually redirect to a page that says **'Site cannot be reached'**.\n"
                "4. **Copy that ENTIRE URL** and paste it as a message here to complete the link."
            )
            return await callback_query.message.edit_text(auth_message, disable_web_page_preview=True)

    # Remove buttons and proceed
    PENDING_DOWNLOADS.pop(user_id)
    await callback_query.message.delete()
    
    if len(urls) > 1:
        await msg.reply(f"🔄 **Batch Mode:** Queuing `{len(urls)}` links...\nThey will be processed continuously in the background.")

    processed_groups = set()
    for i, url in enumerate(urls):
        await track_task(handle_download(bot, msg, url, destination=choice, processed_groups=processed_groups))
        # Yield to event loop occasionally to prevent blocking on massive batches
        if i % 100 == 0:
            await asyncio.sleep(0.1)


# --- CATCH ALL MESSAGES, LINKS & AUTH CODES ---
@bot.on_message((filters.private & ~filters.command(["start", "help", "cleanup", "cancel"])) | filters.command(["dl", "bdl"]))
async def handle_any_message(bot: Client, message: Message):
    user_id = message.from_user.id
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
                    urls = data["urls"]
                    msg_obj = data["message"]
                    
                    if len(urls) > 1:
                        await msg_obj.reply(f"🔄 **Batch Mode:** Queuing `{len(urls)}` links...")

                    processed_groups = set()
                    for i, url in enumerate(urls):
                        await track_task(handle_download(bot, msg_obj, url, destination="gdrive", processed_groups=processed_groups))
                        if i % 100 == 0:
                            await asyncio.sleep(0.1)
            except Exception as e:
                await message.reply(f"❌ **Failed to authorize:** `{e}`\n\nPlease click the login link again or send `/cancel` to stop.")
            return
        else:
            return await message.reply("⚠️ **You are currently linking Google Drive.**\n\nPlease paste the `http://localhost...` link, or send `/cancel` to abort.")

    target_text = text
    if message.reply_to_message and not "t.me/" in text:
        target_text = message.reply_to_message.text or ""

    urls = []
    pattern = r"(https?://t\.me/(?:c/)?[a-zA-Z0-9_]+/(?:[a-zA-Z0-9_]+/)*)(\d+)(?:-(\d+))?"
    matches = list(re.finditer(pattern, target_text))
    is_bdl_command = text.startswith(("/bdl", "bdl"))
    
    # 🌟 Bridging two separate links
    if is_bdl_command and len(matches) == 2 and matches[0].group(1) == matches[1].group(1) and not matches[0].group(3) and not matches[1].group(3):
        base_url = matches[0].group(1)
        start_id = int(matches[0].group(2))
        end_id = int(matches[1].group(2))
        
        if start_id > end_id:
            start_id, end_id = end_id, start_id 
            
        # Generous hard cap of 5000 to prevent bot freeze/crashes
        if end_id - start_id > 5000:
            await message.reply("⚠️ **Range is extremely large!** Capping at 5,000 files to prevent server crash.")
            end_id = start_id + 5000
        
        for msg_id in range(start_id, end_id + 1):
            urls.append(f"{base_url}{msg_id}")
            
    else:
        # Standard hyphen range processing
        for match in matches:
            base_url = match.group(1)
            start_id = int(match.group(2))
            end_id = match.group(3)
            
            if end_id:
                start, end = start_id, int(end_id)
                if start > end:
                    start, end = end, start 
                
                if end - start > 5000:
                    await message.reply("⚠️ **Range is extremely large!** Capping at 5,000 files to prevent server crash.")
                    end = start + 5000
                
                for msg_id in range(start, end + 1):
                    urls.append(f"{base_url}{msg_id}")
            else:
                urls.append(match.group(0))
            
    if not urls:
        urls = re.findall(r"(https?://t\.me/[^\s]+)", target_text)

    if urls:
        urls = list(dict.fromkeys(urls))
        PENDING_DOWNLOADS[user_id] = {"urls": urls, "message": message}
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📤 Send on Telegram ({len(urls)} files)", callback_data="dest_tg")],
            [InlineKeyboardButton(f"☁️ Upload to Google Drive ({len(urls)} files)", callback_data="dest_gdrive")]
        ])
        await message.reply(f"Where would you like to save this file batch?", reply_markup=keyboard)
    
    elif text.startswith(("/dl", "/bdl")) or text.lower().startswith(("dl ", "bdl ")):
        await message.reply("⚠️ **Please provide a valid Telegram link.**\nUse `/dl <link>` or `/bdl <link>`.")


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

async def main():
    await initialize()
    LOGGER(__name__).info("Starting Bot and User Clients...")
    await compose([bot, user])

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
    finally:
        LOGGER(__name__).info("Bot Stopped")
