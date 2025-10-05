# -*- coding: utf-8 -*-
import os
import json
import sqlite3
from datetime import datetime, timedelta
import logging
from google import genai
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
    CallbackQueryHandler,
)

# --- Basic Setup ---
# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEVELOPER_CHAT_ID = os.getenv("DEVELOPER_CHAT_ID")
DB_FILE = "bot_database.db"  # Single SQLite database file
RESPONSES_FILE = "responses.json"
GEM_Model = 'gemini-2.0-flash-001'

# --- Conversation Handler States ---
PARSE_PILL, CONFIRM_PILL, AWAIT_CORRECTION = range(3)
LOG_PILL_CHOICE = range(1) # State for the new logpill conversation


# --- Database Functions ---
def init_db():
    """Initializes the SQLite database and creates tables if they don't exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Table for storing pill schedules
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            schedule_json TEXT NOT NULL
        )
    ''')
    # Table for tracking taken pills
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            taken_date TEXT NOT NULL,
            taken_time TEXT NOT NULL,
            logged_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def load_responses():
    """Loads bot responses from the JSON file."""
    try:
        with open(RESPONSES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"FATAL: {RESPONSES_FILE} not found.")
        return None

responses = load_responses()
if responses is None:
    exit()

# --- Reminder & Tracking Logic ---

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Checks for reminders and sends them with an inline button."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name, start_date, schedule_json FROM schedules")
    all_schedules = cursor.fetchall()
    conn.close()
    
    now = datetime.now()
    current_time_str = now.strftime("%H:%M")

    for user_id, name, start_date_str, schedule_json in all_schedules:
        try:
            schedule = json.loads(schedule_json)
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            if now < start_date:
                continue
            days_since_start = (now - start_date).days
            cumulative_days = 0
            for period in schedule:
                duration = period['duration_days']
                if cumulative_days <= days_since_start < cumulative_days + duration:
                    if period['time'] == current_time_str:
                        message = f"🔔 Reminder: It's time for your '{name}'!\n\n" \
                                  f"Dosage/Task: {period['dosage']}"
                        
                        callback_data = f"take|{name}|{period['time']}"
                        keyboard = [[InlineKeyboardButton("✅ Mark as Taken", callback_data=callback_data)]]
                        reply_markup = InlineKeyboardMarkup(keyboard)

                        await context.bot.send_message(
                            chat_id=user_id, 
                            text=message,
                            reply_markup=reply_markup
                        )
                    break
                cumulative_days += duration
        except Exception as e:
            logger.error(f"Error processing reminder for user {user_id}: {e}")

# --- Helper Function ---
def clean_user_context(context: ContextTypes.DEFAULT_TYPE):
    keys_to_delete = ['parsed_pills', 'pill_conversation_history']
    for key in keys_to_delete:
        if key in context.user_data:
            del context.user_data[key]


# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    keyboard = [
        ["/addpill", "/showpills"],
        ["/logpill", "/todaypills"],
        ["/deletepill"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_html(
        responses["start_command"].format(user_mention=user.mention_html()),
        reply_markup=reply_markup
    )

# --- Add Pill Flow ---

async def addpill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['pill_conversation_history'] = []
    await update.message.reply_text(responses["addpill_initial"])
    return PARSE_PILL


async def parse_with_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_text = update.message.text
    context.user_data['pill_conversation_history'].append(user_text)
    await update.message.reply_text(responses["analyzing_text"], reply_markup=ReplyKeyboardRemove())

    today_date = datetime.now().strftime("%Y-%m-%d")
    full_conversation = "\n---\n".join(context.user_data['pill_conversation_history'])

    prompt = f"""
    You are an expert at parsing medical prescriptions from a conversation.
    Use the ENTIRE conversation history to produce a SINGLE, final JSON array.
    Today's date is {today_date}. Use this for relative dates.
    The JSON structure for each object must be: {{"name": "string", "start_date": "YYYY-MM-DD", "schedule": [{{"duration_days": integer, "dosage": "string", "time": "HH:MM"}}]}}
    - For "ongoing" durations, use 9999 for duration_days.
    - If parsing is impossible, return an empty JSON array [].
    - Return ONLY the JSON array.
    Conversation History: --- {full_conversation} ---
    """
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(model=GEM_Model, contents=prompt)
        json_response_text = response.candidates[0].content.parts[0].text.strip().replace("```json", "").replace("```", "")
        parsed_pills = json.loads(json_response_text)

        if not parsed_pills:
            await update.message.reply_text(responses["parse_error"])
            clean_user_context(context)
            await start_command(update, context)
            return ConversationHandler.END

        context.user_data['parsed_pills'] = parsed_pills
        schedule_text = ""
        for pill in parsed_pills:
            schedule_text += f"*{pill['name']}* (Starts on {pill['start_date']})\n"
            for period in pill['schedule']:
                schedule_text += f"  - For {period['duration_days']} days: `{period['dosage']}` at `{period['time']}`\n"
            schedule_text += "\n"
        
        confirmation_message = responses["confirmation_prompt"].format(schedule_text=schedule_text)
        keyboard = [["Yes"], ["No"], ["Cancel"]]
        await update.message.reply_text(
            confirmation_message,
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
            parse_mode='Markdown'
        )
        return CONFIRM_PILL

    except Exception as e:
        user_id = update.effective_user.id
        logger.error(f"Gemini parsing error for user {user_id}: {e}\nFull conversation:\n{full_conversation}")
        if DEVELOPER_CHAT_ID:
            log_message = (
                f"🔥 **Gemini Parsing Error** 🔥\n\n"
                f"**User ID:** `{user_id}`\n"
                f"**Conversation:**\n```\n{full_conversation}\n```\n\n"
                f"**Error:**\n```\n{e}\n```"
            )
            try:
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID, 
                    text=log_message, 
                    parse_mode='Markdown'
                )
            except Exception as log_e:
                logger.error(f"Failed to send log message to developer: {log_e}")
        await update.message.reply_text(responses["generic_error"])
        clean_user_context(context)
        await start_command(update, context)
        return ConversationHandler.END

async def handle_rejection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(responses["correction_prompt"])
    return AWAIT_CORRECTION

async def save_confirmed_pills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    parsed_pills = context.user_data.get('parsed_pills', [])
    
    if parsed_pills:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        for pill in parsed_pills:
            cursor.execute(
                "INSERT INTO schedules (user_id, name, start_date, schedule_json) VALUES (?, ?, ?, ?)",
                (user_id, pill['name'], pill['start_date'], json.dumps(pill['schedule']))
            )
        conn.commit()
        conn.close()
        
        pill_names = ', '.join([pill['name'] for pill in parsed_pills])
        await update.message.reply_text(responses["save_success"].format(pill_names=pill_names))
    else:
        await update.message.reply_text(responses["save_error"])

    clean_user_context(context)
    await start_command(update, context)
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(responses["action_cancelled"])
    clean_user_context(context)
    await start_command(update, context)
    return ConversationHandler.END


# --- Log Pill Flow ---
async def get_pending_pills(user_id: str) -> list:
    """Helper to get a list of pills scheduled for today that have not been taken."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT name, start_date, schedule_json FROM schedules WHERE user_id = ?", (user_id,))
    user_items = cursor.fetchall()
    
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    
    cursor.execute("SELECT name, taken_time FROM tracking WHERE user_id = ? AND taken_date = ?", (user_id, today_str))
    taken_pills_today = cursor.fetchall()
    conn.close()
    
    taken_pills = {(name, time) for name, time in taken_pills_today}
    
    todays_pills = []
    for name, start_date_str, schedule_json in user_items:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            if now < start_date: continue
            
            days_since_start = (now - start_date).days
            cumulative_days = 0
            schedule = json.loads(schedule_json)
            for period in schedule:
                duration = period['duration_days']
                if cumulative_days <= days_since_start < cumulative_days + duration:
                    todays_pills.append({"name": name, "time": period['time']})
                    break
                cumulative_days += duration
        except Exception:
            pass
    
    return [pill for pill in todays_pills if (pill['name'], pill['time']) not in taken_pills]

async def logpill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the process of logging a pill by showing pending pills as buttons."""
    user_id = str(update.effective_user.id)
    pending_pills = await get_pending_pills(user_id)
    
    if not pending_pills:
        await update.message.reply_text("Looks like everything for today is already logged!")
        return ConversationHandler.END

    keyboard = []
    for pill in sorted(pending_pills, key=lambda x: x['time']):
        button_text = f"{pill['name']} ({pill['time']})"
        keyboard.append([button_text])
    
    keyboard.append(["Cancel"]) # Add a cancel button
    
    await update.message.reply_text(
        "Which pending pill would you like to log as taken?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return LOG_PILL_CHOICE

async def log_selected_pill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Logs the pill the user selected from the keyboard."""
    choice = update.message.text
    user_id = str(update.effective_user.id)
    
    if choice.lower() == 'cancel':
        await start_command(update, context)
        return ConversationHandler.END

    try:
        pill_name, pill_time = choice.rsplit(' (', 1)
        pill_time = pill_time[:-1]

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        today_str = datetime.now().strftime("%Y-%m-%d")
        logged_at_timestamp = datetime.now().isoformat()
        
        cursor.execute("INSERT INTO tracking (user_id, name, taken_date, taken_time, logged_at) VALUES (?, ?, ?, ?, ?)",
                       (user_id, pill_name, today_str, pill_time, logged_at_timestamp))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"✅ Logged '{pill_name}' as taken!")

    except sqlite3.IntegrityError: # This would be better with a UNIQUE constraint
         await update.message.reply_text(f"'{pill_name}' was already logged for that time.")
    except Exception as e:
        logger.error(f"Error logging selected pill: {e}")
        await update.message.reply_text("Sorry, an error occurred. Please try again.")
    
    await start_command(update, context)
    return ConversationHandler.END


# --- Today's Pills & Tracking ---

async def todaypills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates and shows the interactive list of today's pills."""
    user_id = str(update.effective_user.id)
    message, reply_markup = await get_todaypills_message(user_id)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def get_todaypills_message(user_id: str):
    """Helper function to build the message and keyboard for /todaypills."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT name, start_date, schedule_json FROM schedules WHERE user_id = ?", (user_id,))
    user_items = cursor.fetchall()
    
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    
    cursor.execute("SELECT name, taken_time FROM tracking WHERE user_id = ? AND taken_date = ?", (user_id, today_str))
    taken_pills_today = cursor.fetchall()
    conn.close()

    taken_pills = {(name, time) for name, time in taken_pills_today}
    
    todays_pills = []
    for name, start_date_str, schedule_json in user_items:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            if now < start_date: continue
            
            days_since_start = (now - start_date).days
            cumulative_days = 0
            schedule = json.loads(schedule_json)
            for period in schedule:
                duration = period['duration_days']
                if cumulative_days <= days_since_start < cumulative_days + duration:
                    todays_pills.append({"name": name, "dosage": period['dosage'], "time": period['time']})
                    break
                cumulative_days += duration
        except Exception as e:
            logger.error(f"Could not process item '{name}' for /todaypills: {e}")

    if not todays_pills:
        return responses["no_reminders_today"], None

    message_lines = [responses["todaypills_header"]]
    keyboard = []
    
    for pill in sorted(todays_pills, key=lambda x: x['time']):
        pill_id = f"{pill['name']}|{pill['time']}"
        if (pill['name'], pill['time']) in taken_pills:
            status_icon = "✅"
            message_lines.append(f"{status_icon} *{pill['name']}* - `{pill['dosage']}` at `{pill['time']}` (Taken)")
        else:
            status_icon = "⚪️"
            message_lines.append(f"{status_icon} *{pill['name']}* - `{pill['dosage']}` at `{pill['time']}` (Pending)")
            callback_data = f"take|{pill_id}"
            keyboard.append([InlineKeyboardButton(f"✅ Mark '{pill['name']}' as Taken", callback_data=callback_data)])
    
    return "\n".join(message_lines), InlineKeyboardMarkup(keyboard) if keyboard else None


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button presses for marking pills as taken."""
    query = update.callback_query
    await query.answer()

    try:
        action, pill_name, pill_time = query.data.split('|', 2)
        user_id = str(query.from_user.id)
        today_str = datetime.now().strftime("%Y-%m-%d")
        logged_at_timestamp = datetime.now().isoformat()

        if action == 'take':
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO tracking (user_id, name, taken_date, taken_time, logged_at) VALUES (?, ?, ?, ?, ?)",
                           (user_id, pill_name, today_str, pill_time, logged_at_timestamp))
            conn.commit()
            conn.close()
            
            message, reply_markup = await get_todaypills_message(user_id)
            await query.edit_message_text(text=message, reply_markup=reply_markup, parse_mode='Markdown')
    
    except Exception as e:
        logger.error(f"Error in button handler: {e}")
        await query.edit_message_text(text="Sorry, something went wrong while updating.")


# --- Other Commands ---
async def showpills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT name, start_date, schedule_json FROM schedules WHERE user_id = ?", (user_id,))
    user_items = cursor.fetchall()
    conn.close()

    if not user_items:
        await update.message.reply_text(responses["no_reminders_show"])
        return
        
    message = responses["showpills_header"]
    for i, (name, start_date, schedule_json) in enumerate(user_items, 1):
        message += f"*{i}. {name}* (Starts on {start_date})\n"
        schedule = json.loads(schedule_json)
        for period in schedule:
            message += f"  - For {period['duration_days']} days: `{period['dosage']}` at `{period['time']}`\n"
        message += "\n"
    await update.message.reply_text(message, parse_mode='Markdown')

async def deletepill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM schedules WHERE user_id = ?", (user_id,))
    user_items = cursor.fetchall()
    conn.close()

    if not user_items:
        await update.message.reply_text(responses["no_reminders_delete"])
        return ConversationHandler.END
        
    keyboard = [[item[0]] for item in user_items]
    await update.message.reply_text(
        responses["deletepill_prompt"],
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, input_field_placeholder="Select a pill to remove"),
    )
    return 0

async def delete_selected_pill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    user_id = str(update.effective_user.id)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM schedules WHERE user_id = ? AND name = ?", (user_id, choice))
    conn.commit()
    conn.close()

    if cursor.rowcount > 0:
        await update.message.reply_text(responses["delete_success"].format(choice=choice))
    else:
        await update.message.reply_text(responses["delete_error"])
        
    await start_command(update, context)
    return ConversationHandler.END

def main() -> None:
    """Start the bot."""
    init_db() # Initialize the database on startup
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        logger.error("FATAL: Missing API keys in .env file.")
        return
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders, interval=60, first=10)
    
    addpill_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addpill", addpill_command)],
        states={
            PARSE_PILL: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_with_gemini)],
            CONFIRM_PILL: [
                MessageHandler(filters.Regex('^(Yes|yes)$'), save_confirmed_pills),
                MessageHandler(filters.Regex('^(No|no)$'), handle_rejection),
                MessageHandler(filters.Regex('^(Cancel|cancel)$'), cancel_command)
            ],
            AWAIT_CORRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_with_gemini)]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    deletepill_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("deletepill", deletepill_command)],
        states={0: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_selected_pill)]},
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    logpill_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("logpill", logpill_command)],
        states={
            LOG_PILL_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_selected_pill)]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )


    application.add_handler(addpill_conv_handler)
    application.add_handler(deletepill_conv_handler)
    application.add_handler(logpill_conv_handler) # Add the new handler
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("showpills", showpills_command))
    application.add_handler(CommandHandler("todaypills", todaypills_command))
    application.add_handler(CallbackQueryHandler(button_handler)) 
    
    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()

