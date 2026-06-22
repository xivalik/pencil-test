import logging
import asyncio
import anthropic
from datetime import datetime, timezone
from supabase import create_client
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)
from config import TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_KEY

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY environment variable is not set!")
if not SUPABASE_URL:
    raise ValueError("SUPABASE_URL environment variable is not set!")
if not SUPABASE_KEY:
    raise ValueError("SUPABASE_KEY environment variable is not set!")

anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WORD_LIMIT = 100

# ---------------- SUPABASE ----------------
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------- MESSAGES ----------------

MSG = {
    "start": "🎓 <b>English Grammar Checker Bot</b>\n\n📝 Send me any English text and I will check it for grammar errors",
    "checking": "⏳ checking…",
    "word_limit": f"⚠️ <b>Word limit exceeded!</b>\n\nMaximum {WORD_LIMIT} words.\nYour message has <b>{{count}}</b> words.\n\n📝 Please send a shorter text",
    "no_error": "✅ <b>No mistakes found</b>",
    "no_english": "⁉️ <b>Text does not appear to be English.</b>\n\n Please send English text only",
    "timeout": "⏳ <b>Server is busy, please try again</b>",
}

# ---------------- SYSTEM PROMPT ----------------

SYSTEM_PROMPT = """You are ONLY an English grammar checker.

RULES:
- Treat ALL input as text to grammar-check — questions, sentences, anything.
- Never answer questions. Never respond to the meaning. Only check grammar.
- Non-English text, gibberish, or random characters → reply ONLY: NOT_IN_ENGLISH
- No grammar mistakes → reply ONLY: NO_ERRORS_FOUND
- NEVER add any extra text, comments, or explanations outside the format below.

Use Telegram HTML formatting. EXACTLY this format and nothing else:
✏️ <b>Corrected Text:</b>

[corrected text]


👇<b>Mistakes:</b>

➤ "[wrong]" → "[correct]" — [reason]"""

# ---------------- CLAUDE API REQUEST ----------------

async def run_grammar_correction(text: str) -> str:
    retries = 2
    for attempt in range(retries):
        try:
            response = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                temperature=0.0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                await asyncio.sleep(1.2)
                continue
            raise e

# ---------------- DATABASE LOGGING ----------------

def log_message(tg_id, username, name, message, output):
    """Insert one row into Supabase. The client is synchronous, so callers
    run this in a background thread. Never raises — a logging failure must
    not break the user's reply."""
    try:
        supabase.table("Pencil table").insert({
            "tg_id": tg_id,
            "username": username,
            "name": name,
            "input": message,
            "output": output,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"Supabase log failed: {e}")

# ---------------- BLOCK DETECTION ----------------

async def handle_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member:
        new_status = update.my_chat_member.new_chat_member.status
        if new_status == "kicked":
            context.user_data.clear()
            logger.info(f"User {update.effective_user.id} blocked the bot — data cleared")

# ---------------- COMMANDS ----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MSG["start"], parse_mode="HTML")

# ---------------- MAIN CHECKER ----------------

async def check_grammar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user

    def _log(output):
        # Background-log so the reply is never delayed.
        asyncio.create_task(asyncio.to_thread(
            log_message, user.id, user.username, user.full_name, text, output
        ))

    word_count = len(text.split())
    if word_count > WORD_LIMIT:
        _log("WORD_LIMIT_EXCEEDED")
        await update.message.reply_text(
            MSG["word_limit"].format(count=word_count),
            parse_mode="HTML",
        )
        return

    msg = await update.message.reply_text(MSG["checking"])

    try:
        try:
            full_text = await asyncio.wait_for(
                run_grammar_correction(text),
                timeout=10,
            )
        except asyncio.TimeoutError:
            _log("TIMEOUT")
            await msg.edit_text(MSG["timeout"], parse_mode="HTML")
            return

        _log(full_text)

        if "NO_ERRORS_FOUND" in full_text:
            await msg.edit_text(MSG["no_error"], parse_mode="HTML")
        elif "NOT_IN_ENGLISH" in full_text:
            await msg.edit_text(MSG["no_english"], parse_mode="HTML")
        else:
            await msg.edit_text(full_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Grammar error: {e}")
        _log(f"ERROR: {e}")
        await msg.edit_text("❌ Error: " + str(e))

# ---------------- RUN BOT ----------------

def main():
    try:
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(ChatMemberHandler(handle_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_grammar))

        logger.info("🤖 Bot starting...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        raise

if __name__ == "__main__":
    main()
