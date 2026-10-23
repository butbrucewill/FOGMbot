"""
FOGM Support Bot (v2)

Flow:
1. New user: bot asks phone -> email -> name, saves ONE row to the Google Form.
2. Menu: Withdraw / Deposit / Account opening / MT5 issue -> bot replies with a video link.
3. Every topic the user opens is also logged to the Google Sheet.
4. Support team: /resolve TICKET_ID tells the user the issue is resolved,
   /reply TICKET_ID message sends them any message.
5. "Other issue / Raise a ticket" -> user types the issue -> saved to the Google Form
   -> bot confirms the ticket (team replies within 4 hours).
"""
import asyncio
import logging
import os
import threading
import random
import re
import string
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ---------------------------------------------------------------- CONFIG ----
# Token comes from an environment variable. Never paste it into the code.
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Paste the values printed by create_form.gs (Google Apps Script)
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSdoB7yB4152uO6ViCmPrvX3QlvCgwIdj5DNRz87j8IAZlKCnw/formResponse"
ENTRY = {
    "type": "entry.1696266383",
    "ticket_id": "entry.793359954",
    "tg_id": "entry.1082210238",
    "tg_username": "entry.937108528",
    "name": "entry.582593757",
    "phone": "entry.403637552",
    "email": "entry.1084306208",
    "issue": "entry.1856751537",
}

# Support contact shown after a ticket (leave a value as "" to hide it)
SUPPORT_WHATSAPP = "91XXXXXXXXXX"      # digits only, with country code
SUPPORT_USERNAME = "PASTE_SUPPORT_USERNAME"  # Telegram username without @

# After a ticket the bot says the team will contact the user within 4 hours.
# Set to True to also show the WhatsApp number / Telegram username below.
SHOW_SUPPORT_CONTACT = False

# Numeric Telegram IDs of your support team (comma separated), set as the
# environment variable SUPPORT_TEAM_IDS, for example: 123456789,987654321
# Each member must press Start on the bot once. They get new-ticket alerts and
# can use /resolve and /reply. (The old SUPPORT_CHAT_ID variable still works.)
_team_raw = os.environ.get("SUPPORT_TEAM_IDS") or os.environ.get("SUPPORT_CHAT_ID", "")
SUPPORT_TEAM_IDS = {int(x) for x in re.split(r"[,\s]+", _team_raw) if x.isdigit()}

# Issue topics: key -> (button label, message, video link)
TOPICS = {
    "withdraw": (
        "Withdraw issue",
        "Having trouble withdrawing? Watch this short video, it covers the common causes and fixes.",
        "https://youtu.be/REPLACE_WITHDRAW",
    ),
    "deposit": (
        "Deposit issue",
        "Deposit not showing or failing? This video explains what to check.",
        "https://youtu.be/REPLACE_DEPOSIT",
    ),
    "account": (
        "Account opening issue",
        "Stuck while opening your account? Follow this step-by-step video.",
        "https://youtu.be/REPLACE_ACCOUNT",
    ),
    "mt5": (
        "MT5 issue",
        "Problem with MT5 login or connection? This video shows how to fix it.",
        "https://youtu.be/REPLACE_MT5",
    ),
}
# --------------------------------------------------------------------------

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("fogm_support_bot")

PHONE, EMAIL, NAME = range(3)
ISSUE = 3

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ------------------------------------------------------------- helpers -----
def topics_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(v[0], callback_data=f"topic_{k}")] for k, v in TOPICS.items()]
    rows.append([InlineKeyboardButton("Other issue / Raise a ticket", callback_data="ticket")])
    return InlineKeyboardMarkup(rows)


async def save_to_form(payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.post(FORM_URL, data=payload)
        return r.status_code == 200
    except httpx.HTTPError:
        log.exception("Google Form submit failed")
        return False


def new_ticket_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"FOGM-{datetime.now():%y%m%d}-{suffix}"


def user_fields(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict:
    u = update.effective_user
    d = context.user_data
    return {
        ENTRY["tg_id"]: str(u.id),
        ENTRY["tg_username"]: f"@{u.username}" if u.username else "no username",
        ENTRY["name"]: d.get("name", ""),
        ENTRY["phone"]: d.get("phone", ""),
        ENTRY["email"]: d.get("email", ""),
    }


# ---------------------------------------------------------- registration ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("registered"):
        await update.message.reply_text(
            f"Welcome back, {context.user_data.get('name', '')}! Choose your issue:",
            reply_markup=topics_keyboard(),
        )
        return ConversationHandler.END

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("Share my phone number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "Welcome to FOGM Support.\n\n"
        "First, please share your phone number. Tap the button below or type it "
        "with country code (example: +919876543210).",
        reply_markup=keyboard,
    )
    return PHONE


async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        raw = update.message.contact.phone_number
    else:
        raw = update.message.text or ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:  # assume India when no country code is given
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        await update.message.reply_text(
            "That phone number doesn't look right. Please send it again with country code, "
            "for example +919876543210."
        )
        return PHONE
    context.user_data["phone"] = "+" + digits
    await update.message.reply_text("Thanks. Now send your email address:", reply_markup=ReplyKeyboardRemove())
    return EMAIL


async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if not EMAIL_RE.match(email):
        await update.message.reply_text("That email doesn't look right. Please send a valid email address.")
        return EMAIL
    context.user_data["email"] = email
    await update.message.reply_text("Great. Finally, what is your full name?")
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    payload = user_fields(update, context)
    payload[ENTRY["type"]] = "Registration"
    if not await save_to_form(payload):
        log.error("Registration not saved: %s", payload)
    context.user_data["registered"] = True
    await update.message.reply_text(
        f"Thank you, {context.user_data['name']}! What do you need help with?",
        reply_markup=topics_keyboard(),
    )
    return ConversationHandler.END


# ------------------------------------------------------------ menu & FAQ ---
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("registered"):
        await update.message.reply_text("Please send /start first.")
        return
    await update.message.reply_text("Choose your issue:", reply_markup=topics_keyboard())


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Choose your issue:", reply_markup=topics_keyboard())


async def topic_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split("_", 1)[1]
    label, text, link = TOPICS[key]
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Back to menu", callback_data="menu")],
            [InlineKeyboardButton("Still not solved? Raise a ticket", callback_data="ticket")],
        ]
    )
    await q.edit_message_text(f"{label}\n\n{text}\n\nVideo: {link}", reply_markup=buttons)

    # Record the selection (with the user's details) in the Google Sheet
    payload = user_fields(update, context)
    payload[ENTRY["type"]] = f"Topic: {label}"
    payload[ENTRY["issue"]] = label
    await save_to_form(payload)


# ----------------------------------------------------------- ticket flow ---
async def ticket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        send = update.callback_query.message.reply_text
    else:
        send = update.message.reply_text

    if not context.user_data.get("registered"):
        await send("Please send /start first so we can save your details.")
        return ConversationHandler.END

    await send("Please describe your issue in detail. (Send /cancel to stop.)")

    # Record that the user chose "Other issue / Raise a ticket"
    choice = user_fields(update, context)
    choice[ENTRY["type"]] = "Topic: Other issue / Raise a ticket"
    choice[ENTRY["issue"]] = "Other issue / Raise a ticket"
    await save_to_form(choice)
    return ISSUE


async def get_issue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticket_id = new_ticket_id()
    payload = user_fields(update, context)
    payload[ENTRY["type"]] = "Ticket"
    payload[ENTRY["ticket_id"]] = ticket_id
    payload[ENTRY["issue"]] = update.message.text.strip()

    if not await save_to_form(payload):
        await update.message.reply_text(
            "Sorry, we could not save your ticket right now. Please try again in a few minutes with /ticket."
        )
        return ConversationHandler.END

    put_ticket(
        context,
        ticket_id,
        {
            "chat_id": update.effective_chat.id,
            "name": context.user_data.get("name", ""),
            "phone": context.user_data.get("phone", ""),
            "email": context.user_data.get("email", ""),
            "tg_username": payload[ENTRY["tg_username"]],
            "status": "open",
        },
    )

    lines = [
        f"Your ticket is raised.\n\nTicket ID: {ticket_id}\n",
        "Our support team will contact you within 4 hours.",
    ]
    if SHOW_SUPPORT_CONTACT:
        if SUPPORT_WHATSAPP:
            lines.append(f"\nWhatsApp: +{SUPPORT_WHATSAPP}\nhttps://wa.me/{SUPPORT_WHATSAPP}")
        if SUPPORT_USERNAME:
            lines.append(f"Telegram: @{SUPPORT_USERNAME}\nhttps://t.me/{SUPPORT_USERNAME}")
    await update.message.reply_text("\n".join(lines))

    alert = (
        f"New ticket {ticket_id}\n"
        f"Name: {payload[ENTRY['name']]}\n"
        f"Phone: {payload[ENTRY['phone']]}\n"
        f"Email: {payload[ENTRY['email']]}\n"
        f"Telegram: {payload[ENTRY['tg_username']]} ({payload[ENTRY['tg_id']]})\n\n"
        f"{payload[ENTRY['issue']]}\n\n"
        f"When solved, send: /resolve {ticket_id}"
    )
    for member_id in SUPPORT_TEAM_IDS:
        try:
            await context.bot.send_message(chat_id=member_id, text=alert)
        except Exception:
            log.exception("Could not notify support member %s", member_id)

    return ConversationHandler.END


# ------------------------------------------------- support team commands ---
def put_ticket(context: ContextTypes.DEFAULT_TYPE, ticket_id: str, info: dict):
    # Build a new dict each time so the persistence layer notices the change.
    tickets = dict(context.application.bot_data.get("tickets", {}))
    tickets[ticket_id] = info
    context.application.bot_data["tickets"] = tickets


async def _support_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, usage: str):
    """Shared checks for /resolve and /reply. Returns (ticket_id, ticket, rest) or None."""
    if update.effective_user.id not in SUPPORT_TEAM_IDS:
        await update.message.reply_text("You are not authorised to use this command.")
        return None
    if not context.args:
        await update.message.reply_text(usage)
        return None
    ticket_id = context.args[0].upper()
    ticket = context.application.bot_data.get("tickets", {}).get(ticket_id)
    if not ticket:
        await update.message.reply_text(f"Ticket {ticket_id} not found.")
        return None
    return ticket_id, ticket, " ".join(context.args[1:]).strip()


async def resolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resolve TICKET_ID [optional note]  -> tells the user their issue is resolved."""
    found = await _support_lookup(update, context, "Usage: /resolve TICKET_ID [optional note]")
    if not found:
        return
    ticket_id, ticket, note = found

    text = f"Good news! Your ticket {ticket_id} has been marked as resolved."
    if note:
        text += f"\n\nNote from support: {note}"
    text += "\n\nIf you still face the issue, you can raise a new ticket with /ticket."
    try:
        await context.bot.send_message(chat_id=ticket["chat_id"], text=text)
    except Exception:
        log.exception("Could not message user for ticket %s", ticket_id)
        await update.message.reply_text("Could not message the user (they may have blocked the bot).")
        return

    put_ticket(context, ticket_id, {**ticket, "status": "resolved"})
    await save_to_form(
        {
            ENTRY["type"]: "Resolved",
            ENTRY["ticket_id"]: ticket_id,
            ENTRY["tg_id"]: str(ticket["chat_id"]),
            ENTRY["tg_username"]: ticket.get("tg_username", ""),
            ENTRY["name"]: ticket.get("name", ""),
            ENTRY["phone"]: ticket.get("phone", ""),
            ENTRY["email"]: ticket.get("email", ""),
            ENTRY["issue"]: f"Resolved by {update.effective_user.full_name}. {note}".strip(),
        }
    )
    await update.message.reply_text(f"User notified. Ticket {ticket_id} marked as resolved.")


async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reply TICKET_ID your message  -> sends any message to the ticket's user."""
    found = await _support_lookup(update, context, "Usage: /reply TICKET_ID your message")
    if not found:
        return
    ticket_id, ticket, message = found
    if not message:
        await update.message.reply_text("Usage: /reply TICKET_ID your message")
        return
    try:
        await context.bot.send_message(
            chat_id=ticket["chat_id"],
            text=f"Message from support about ticket {ticket_id}:\n\n{message}",
        )
    except Exception:
        log.exception("Could not message user for ticket %s", ticket_id)
        await update.message.reply_text("Could not message the user (they may have blocked the bot).")
        return
    await update.message.reply_text(f"Message sent to the user of ticket {ticket_id}.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    if context.user_data.get("registered"):
        await update.message.reply_text("Choose your issue:", reply_markup=topics_keyboard())
    return ConversationHandler.END


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("registered"):
        await update.message.reply_text("Please choose an option:", reply_markup=topics_keyboard())
    else:
        await update.message.reply_text("Please send /start to begin.")


# Tiny web server so Render's free Web Service sees an open port.
# Not needed on a VPS, but harmless there.
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    text_only = filters.TEXT & ~filters.COMMAND

    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PHONE: [MessageHandler((text_only | filters.CONTACT), get_phone)],
            EMAIL: [MessageHandler(text_only, get_email)],
            NAME: [MessageHandler(text_only, get_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )

    ticket_conv = ConversationHandler(
        entry_points=[
            CommandHandler("ticket", ticket_start),
            CallbackQueryHandler(ticket_start, pattern="^ticket$"),
        ],
        states={ISSUE: [MessageHandler(text_only, get_issue)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(register_conv)
    app.add_handler(ticket_conv)
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("resolve", resolve_cmd))
    app.add_handler(CommandHandler("reply", reply_cmd))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(topic_answer, pattern=r"^topic_\w+$"))
    app.add_handler(MessageHandler(text_only, fallback_text))

    log.info("Bot started")
    threading.Thread(target=keep_alive, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
