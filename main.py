import os
import logging
import threading
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import pg8000.dbapi as pg
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN        = os.environ["BOT_TOKEN"]
SOURCE_GROUP_ID  = int(os.environ["SOURCE_GROUP_ID"])
ARCHIVE_GROUP_ID = int(os.environ["ARCHIVE_GROUP_ID"])
DATABASE_URL     = os.environ["DATABASE_URL"]
PORT             = int(os.environ.get("PORT", 8080))


# ─── Database ────────────────────────────────────────────────────────────────

def get_conn():
    url = urlparse(DATABASE_URL)
    return pg.connect(
        host=url.hostname,
        database=url.path[1:],
        user=url.username,
        password=url.password,
        port=url.port or 5432,
        ssl_context=True
    )

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS message_map (
            orig_id    INTEGER PRIMARY KEY,
            archive_id INTEGER NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("✅ DB ready")

def save_mapping(orig_id: int, archive_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO message_map VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (orig_id, archive_id)
    )
    conn.commit()
    cur.close()
    conn.close()

def get_archive_id(orig_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT archive_id FROM message_map WHERE orig_id = %s", (orig_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


# ─── Health Server ────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Archive Bot OK")
    def log_message(self, *_): pass

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


# ─── Bot Handler ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != SOURCE_GROUP_ID:
        return

    orig_id = msg.message_id

    name = "ناشناس"
    if msg.from_user:
        name = msg.from_user.full_name
    elif msg.sender_chat:
        name = msg.sender_chat.title

    reply_to = None
    if msg.reply_to_message:
        reply_to = get_archive_id(msg.reply_to_message.message_id)

    async def do_archive(with_reply: bool):
        rp = reply_to if with_reply else None

        if msg.text:
            return await context.bot.send_message(
                chat_id=ARCHIVE_GROUP_ID,
                text=f"<b>👤 {escape(name)}:</b>\n{escape(msg.text)}",
                reply_to_message_id=rp,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        else:
            extra = {}
            if not msg.sticker and not msg.dice:
                cap = escape(msg.caption or "")
                extra["caption"]    = f"<b>👤 {escape(name)}</b>\n{cap}".strip()
                extra["parse_mode"] = "HTML"

            return await context.bot.copy_message(
                chat_id=ARCHIVE_GROUP_ID,
                from_chat_id=SOURCE_GROUP_ID,
                message_id=orig_id,
                reply_to_message_id=rp,
                **extra
            )

    try:
        sent = await do_archive(with_reply=True)
    except Exception as e1:
        logger.warning(f"⚠️ retry without reply for {orig_id}: {e1}")
        try:
            sent = await do_archive(with_reply=False)
        except Exception as e2:
            logger.error(f"❌ failed {orig_id}: {e2}")
            return

    save_mapping(orig_id, sent.message_id)
    logger.info(f"✅ {orig_id} → {sent.message_id} | {name}")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info(f"🌐 Health check on :{PORT}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))

    logger.info("🤖 Polling started...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)