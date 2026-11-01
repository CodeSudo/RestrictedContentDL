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

# Multi-Cloud & DB Helpers
from gdrive_helper import upload_to_drive_user, get_user_credentials, generate_auth_url, authorize_user
from onedrive_helper import upload_to_onedrive_user, get_valid_token, get_onedrive_auth_url, authorize_onedrive_user
from temp_cloud_helper import upload_to_temp_cloud
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

# ✅ State Management
PENDING_DOWNLOADS = {}
AWAITING_AUTH = {}
AWAITING_FOLDER_NAME = {}

# ✅ Batch Queue
BDL_QUEUE = asyncio.Queue()

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    task.add_done_callback(lambda _: RUNNING_TASKS.discard(task))
    return task

# --- BACKGROUND BATCH WORKER ---
async def batch_queue_worker():
    while True:
        task_data = await BDL_QUEUE.get()
        bot_client, msg, urls, dest, folder = task_data
        try:
            await execute_batch_download(bot_client, msg, urls, dest, folder)
        except Exception as e:
            LOGGER(__name__).error(f"Batch Queue Error: {e}")
        finally:
            BDL_QUEUE.task_done()

# --- ADMIN COMMANDS ---
@bot.on_message(filters.command("admin") & filters.private)
async def admin_dashboard(_, message: Message):
    if get_user_role(message.from_user.id) != "admin":
        return await message.reply("⛔️ **Access Denied.** You are not an admin.")
    
    users = get_all_users()
    dash_text = (
        "👑 **Admin Dashboard**\n\n"
        f"👥 **Total Registered Users:** `{len(users)}`\n"
        f"⭐️ **VIP Users:** `{sum(1 for r in users.values() if r == 'vip')}`\n"
        f"🚫 **Banned Users:** `{sum(1 for r in users.values() if r == 'banned')}`\n\n"
        "**Admin Commands:**\n"
        "`/promote <user_id>` - Grant VIP status\n"
        "`/demote <user_id>` - Remove VIP status\n"
        "`/ban <user_id>` - Block user from bot\n"
        "`/unban <user_id>` - Restore access"
    )
    await message.reply(dash_text)

@bot.on_message(filters.command(["promote", "demote", "ban", "unban"]) & filters.private)
async def manage_users(_, message: Message):
    if get_user_role(message.from_user.id) != "admin": return
    if len(message.command) < 2: return await message.reply("⚠️ Provide User ID. Example: `/promote 123456789`")

    target_id, action = message.command[1], message.command[0]
    roles = {"promote": "vip", "demote": "user", "ban": "banned", "unban": "user"}
    
    if action in roles:
        set_user_role(target_id, roles[action])
        await message.reply(f"✅ User `{target_id}` updated to **{roles[action].upper()}**.")

# --- CORE COMMANDS ---
@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    if get_user_role(message.from_user.id) == "banned": return await message.reply("🚫 Banned.")
    welcome_text = (
        "👋 **Welcome to the Advanced Media Downloader Bot!**\n\n"
        "Send me a link or use `/bdl <link>` for batches."
    )
    await message.reply(welcome_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]))

@bot.on_message(filters.command("cleanup") & filters.private)
async def cleanup_storage(_, message: Message):
    if get_user_role(message.from_user.id) == "banned": return
    try:
        files_removed, bytes_freed = cleanup_downloads_root()
        await message.reply(f"🧹 **Cleanup complete:** removed `{files_removed}` file(s), freed `{get_readable_file_size(bytes_freed)}`.")
    except Exception:
        await message.reply("❌ **Cleanup failed.** Check logs.")

# --- DOWNLOAD LOGIC ---
async def handle_download(bot: Client, message: Message, post_url: str, destination: str = "tg", custom_folder: str = None, processed_groups: set = None):
    global forward_chat_id
    async with download_semaphore:
        if "?" in post_url: post_url = post_url.split("?", 1)[0]

        try:
            effective_forward_chat_id = forward_chat_id if forward_chat_id and (await check_forward_permission(bot, forward_chat_id))[0] else None
            chat_id, message_id = getChatMsgID(post_url)
            chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)

            if chat_message.document or chat_message.video or chat_message.audio:
                file_size = chat_message.document.file_size if chat_message.document else chat_message.video.file_size if chat_message.video else chat_message.audio.file_size
                if not await fileSizeLimit(file_size, message, "download", user.me.is_premium): return

            raw_caption, raw_caption_entities = get_raw_text(chat_message.caption, chat_message.caption_entities)
            raw_text, raw_text_entities = get_raw_text(chat_message.text, chat_message.entities)

            if chat_message.media_group_id:
                if processed_groups is not None:
                    if chat_message.media_group_id in processed_groups: return
                    processed_groups.add(chat_message.media_group_id)
                if not await processMediaGroup(chat_message, bot, message, forward_chat_id=effective_forward_chat_id):
                    await message.reply(f"**Could not extract media from album: {post_url}**")
                return

            if bool(chat_message.photo or chat_message.video or chat_message.audio or chat_message.document or chat_message.voice or chat_message.animation):
                start_time = time()
                progress_message = await message.reply(f"**📥 Downloading:** `{post_url.split('/')[-1]}`")
                media_path = await chat_message.download(
                    file_name=get_download_path(message.id, get_file_name(message_id, chat_message)),
                    progress=Leaves.progress_for_pyrogram,
                    progress_args=progressArgs("📥 Downloading", progress_message, start_time),
                )

                if not media_path: return await progress_message.edit("**❌ Download failed**")

                await progress_message.edit(f"**☁️ Uploading to {destination.upper()}...**")
                try:
                    loop = asyncio.get_event_loop()
                    link = None
                    actual_user_id = message.chat.id 
                    
                    if destination == "gdrive":
                        link = await loop.run_in_executor(None, upload_to_drive_user, actual_user_id, media_path, custom_folder)
                    elif destination == "onedrive":
                        link = await loop.run_in_executor(None, upload_to_onedrive_user, actual_user_id, media_path, custom_folder)
                    elif destination == "tempcloud":
                        link = await loop.run_in_executor(None, upload_to_temp_cloud, media_path)
                    else:
                        media_type = "photo" if chat_message.photo else "video" if chat_message.video else "audio" if chat_message.audio else "document"
                        await send_media(bot, message, media_path, media_type, raw_caption, raw_caption_entities, progress_message, start_time, forward_chat_id=effective_forward_chat_id)

                    if link:
                        folder_txt = f"\n📁 **Folder:** `{custom_folder}`" if custom_folder else ""
                        await message.reply(f"✅ **Saved to {destination.upper()}!**{folder_txt}\n\n[🔗 View File Here]({link})")
                    await progress_message.delete()
                except Exception as e:
                    await progress_message.edit(f"❌ **Upload Failed:** `{e}`")

                cleanup_download(media_path)
            elif chat_message.text or chat_message.caption:
                try: await message.reply(raw_text or raw_caption, entities=raw_text_entities if raw_text else raw_caption_entities or None)
                except BadRequest: await message.reply(raw_text or raw_caption)

        except Exception as e:
            await message.reply(f"**❌ Error processing link:** `{e}`")

async def execute_batch_download(bot: Client, msg: Message, urls: list, dest: str, folder: str = None):
    loading = await msg.reply(f"📥 **Processing Batch of {len(urls)} files from Queue...**")
    processed_groups = set()
    for i, url in enumerate(urls):
        await handle_download(bot, msg, url, destination=dest, custom_folder=folder, processed_groups=processed_groups)
        if i % 100 == 0: await asyncio.sleep(0.5)
    await loading.edit(f"**✅ Batch Complete for {len(urls)} links!**")

# --- CALLBACK ROUTERS ---
@bot.on_callback_query(filters.regex(r"^dest_"))
async def process_dest_choice(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    choice = cb.data.split("_")[1]

    if user_id not in PENDING_DOWNLOADS: return await cb.answer("Session expired.", show_alert=True)
    data = PENDING_DOWNLOADS[user_id]
    
    if choice == "gdrive" and not get_user_credentials(user_id):
        AWAITING_AUTH[user_id] = "gdrive"
        return await cb.message.edit_text(f"⚠️ [Click to Login to GDrive]({generate_auth_url(user_id)})\nPaste localhost URL here.")
    elif choice == "onedrive" and not get_valid_token(user_id):
        AWAITING_AUTH[user_id] = "onedrive"
        return await cb.message.edit_text(f"⚠️ [Click to Login to OneDrive]({get_onedrive_auth_url(user_id)})\nPaste localhost URL here.")

    if choice in ["gdrive", "onedrive"]:
        AWAITING_FOLDER_NAME[user_id] = {"choice": choice, "data": data}
        return await cb.message.edit_text("Where should this be saved?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 Default (Telegram downloads)", callback_data="folder_default")],
            [InlineKeyboardButton("➕ Create New Folder", callback_data="folder_custom")]
        ]))

    PENDING_DOWNLOADS.pop(user_id)
    await cb.message.delete()
    
    if len(data["urls"]) > 1:
        await cb.message.reply(f"⏳ **Added to Batch Queue!** Position: {BDL_QUEUE.qsize() + 1}")
        await BDL_QUEUE.put((bot, data["message"], data["urls"], choice, None))
    else:
        await track_task(handle_download(bot, data["message"], data["urls"][0], destination=choice))

@bot.on_callback_query(filters.regex(r"^folder_"))
async def process_folder_choice(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    action = cb.data.split("_")[1]
    
    if user_id not in AWAITING_FOLDER_NAME: return await cb.answer("Session expired.", show_alert=True)
    state = AWAITING_FOLDER_NAME[user_id]
    
    if action == "default":
        AWAITING_FOLDER_NAME.pop(user_id)
        data = PENDING_DOWNLOADS.pop(user_id)
        await cb.message.delete()
        if len(data["urls"]) > 1:
            await cb.message.reply(f"⏳ **Added to Batch Queue!** Position: {BDL_QUEUE.qsize() + 1}")
            await BDL_QUEUE.put((bot, data["message"], data["urls"], state["choice"], None))
        else:
            await track_task(handle_download(bot, data["message"], data["urls"][0], destination=state["choice"]))
    else:
        state["waiting_for_text"] = True
        await cb.message.edit_text("✏️ **Send me the name of the new folder:**")

# --- MESSAGE ROUTER ---
@bot.on_message((filters.private & ~filters.command(["start", "cleanup", "cancel", "admin", "promote", "demote", "ban", "unban"])) | filters.command(["dl", "bdl"]))
async def handle_any_message(bot: Client, message: Message):
    user_id = message.from_user.id
    if get_user_role(user_id) == "banned": return
    text = message.text or ""

    if user_id in AWAITING_FOLDER_NAME and AWAITING_FOLDER_NAME[user_id].get("waiting_for_text"):
        state = AWAITING_FOLDER_NAME.pop(user_id)
        data = PENDING_DOWNLOADS.pop(user_id)
        folder = text.strip()
        await message.reply(f"📁 Will save to: **{folder}**")
        if len(data["urls"]) > 1:
            await message.reply(f"⏳ **Added to Batch Queue!** Position: {BDL_QUEUE.qsize() + 1}")
            return await BDL_QUEUE.put((bot, data["message"], data["urls"], state["choice"], folder))
        else:
            return await track_task(handle_download(bot, data["message"], data["urls"][0], destination=state["choice"], custom_folder=folder))

    auth_target = AWAITING_AUTH.get(user_id)
    if auth_target and (text.startswith("http://localhost") or text.startswith("http://127.0.0.1")):
        try:
            msg = await message.reply(f"⏳ Verifying {auth_target} connection...")
            if auth_target == "gdrive": authorize_user(user_id, text)
            else: authorize_onedrive_user(user_id, text)
            del AWAITING_AUTH[user_id]
            await msg.edit_text("✅ **Successfully linked!**")
            
            if user_id in PENDING_DOWNLOADS:
                data = PENDING_DOWNLOADS.pop(user_id)
                if len(data["urls"]) > 1:
                    await message.reply(f"⏳ **Added to Batch Queue!** Position: {BDL_QUEUE.qsize() + 1}")
                    await BDL_QUEUE.put((bot, data["message"], data["urls"], auth_target, None))
                else:
                    await track_task(handle_download(bot, data["message"], data["urls"][0], destination=auth_target))
        except Exception as e:
            await message.reply(f"❌ Auth failed: {e}")
        return
    elif auth_target:
        return await message.reply("⚠️ **Paste the localhost link or send /cancel.**")

    target_text = text if not message.reply_to_message or "t.me/" in text else message.reply_to_message.text or ""
    urls = []
    pattern = r"(https?://t\.me/(?:c/)?[a-zA-Z0-9_]+/(?:[a-zA-Z0-9_]+/)*)(\d+)(?:-(\d+))?"
    matches = list(re.finditer(pattern, target_text))
    is_bdl = text.startswith(("/bdl", "bdl"))

    if is_bdl and len(matches) == 2 and matches[0].group(1) == matches[1].group(1) and not matches[0].group(3) and not matches[1].group(3):
        start_id, end_id = int(matches[0].group(2)), int(matches[1].group(2))
        if start_id > end_id: start_id, end_id = end_id, start_id 
        if end_id - start_id > 5000: end_id = start_id + 5000
        for msg_id in range(start_id, end_id + 1): urls.append(f"{matches[0].group(1)}{msg_id}")
    else:
        for m in matches:
            if m.group(3):
                start, end = int(m.group(2)), int(m.group(3))
                if start > end: start, end = end, start 
                if end - start > 5000: end = start + 5000
                for msg_id in range(start, end + 1): urls.append(f"{m.group(1)}{msg_id}")
            else:
                urls.append(m.group(0))

    if not urls: urls = re.findall(r"(https?://t\.me/[^\s]+)", target_text)

    if urls:
        urls = list(dict.fromkeys(urls))
        # Enforce VIP for batches > 1
        if len(urls) > 1 and get_user_role(user_id) not in ["vip", "admin"]:
            return await message.reply("⭐️ **VIP Feature Only:** Batch downloading is restricted.")
            
        PENDING_DOWNLOADS[user_id] = {"urls": urls, "message": message}
        await message.reply(f"Save {len(urls)} file(s) to:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Telegram", callback_data="dest_tg"), InlineKeyboardButton("🔥 Temp Cloud", callback_data="dest_tempcloud")],
            [InlineKeyboardButton("☁️ Google Drive", callback_data="dest_gdrive"), InlineKeyboardButton("🟦 OneDrive", callback_data="dest_onedrive")]
        ]))
    elif text.startswith(("/dl", "/bdl")):
        await message.reply("⚠️ **Please provide a valid Telegram link.**")

@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_auth(_, message: Message):
    if AWAITING_AUTH.pop(message.from_user.id, None):
        PENDING_DOWNLOADS.pop(message.from_user.id, None)
        await message.reply("✅ Cancelled.")
    else:
        await message.reply("Nothing to cancel.")

async def main():
    global download_semaphore, forward_chat_id
    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)
    if PyroConf.FORWARD_CHAT_ID: forward_chat_id = await resolve_forward_chat_id(PyroConf.FORWARD_CHAT_ID)
    asyncio.create_task(batch_queue_worker())
    LOGGER(__name__).info("Starting...")
    await compose([bot, user])

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
