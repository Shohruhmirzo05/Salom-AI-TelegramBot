import asyncio
import contextlib
import html
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

import json
import time
import requests
import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.error import RetryAfter

load_dotenv()

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
STATE_FILE = os.getenv("STATE_FILE", "bot_state.pickle")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set. Add it to your environment or .env file.")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("salom_ai_bot")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

# Constants for Menu
BTN_NEW_CHAT = "💬 Yangi chat"
BTN_HISTORY = "📚 Tarix"
BTN_IMAGE = "🖼️ Rasm yaratish"
BTN_MODEL = "🤖 Modelni o'zgartirish"
BTN_SETTINGS = "⚙️ Sozlamalar"
BTN_USAGE = "📊 Statistika"
BTN_SUBSCRIBE = "💎 Premium Obuna"
BTN_FEEDBACK = "📩 Fikr-mulohaza"
BTN_HELP = "❓ Yordam"

USER_DEFAULTS = {
    "access_token": None,
    "refresh_token": None,
    "conversation_id": None,
    "model": DEFAULT_MODEL,
    "input_mode": "chat",  # chat | image | set_prompt | card_number | card_expiry | sms_code | feedback
    "attachments": [],
    "pending_plan_code": None,
    "pending_card_number": None,
    "pending_request_id": None,
    "pending_phone_hint": None,
}


# --- State helpers --------------------------------------------------------- #
def get_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """Return user state with defaults applied."""
    state = context.user_data.setdefault("state", {})
    for key, value in USER_DEFAULTS.items():
        if key not in state:
            state[key] = value.copy() if isinstance(value, list) else value
    return state


def build_url(path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{BACKEND_URL}{path}"


# --- API helpers ----------------------------------------------------------- #
async def authenticate_user(
    telegram_id: int, first_name: Optional[str], username: Optional[str], state: Dict[str, Any]
) -> bool:
    """Authenticate via backend and store tokens."""
    payload = {
        "telegram_id": telegram_id,
        "first_name": first_name,
        "username": username,
    }

    def _request():
        resp = requests.post(build_url("/auth/telegram"), json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    try:
        data = await asyncio.to_thread(_request)
        state["access_token"] = data["access_token"]
        state["refresh_token"] = data.get("refresh_token")
        return True
    except Exception as exc:
        logger.exception("Auth failed: %s", exc)
        return False


async def refresh_tokens(state: Dict[str, Any]) -> bool:
    if not state.get("refresh_token"):
        return False

    def _request():
        resp = requests.post(
            build_url("/auth/refresh"),
            json={"refresh_token": state["refresh_token"]},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        data = await asyncio.to_thread(_request)
        state["access_token"] = data["access_token"]
        state["refresh_token"] = data.get("refresh_token", state.get("refresh_token"))
        return True
    except Exception:
        logger.warning("Refresh token failed; user will be re-authenticated.")
        state["access_token"] = None
        return False


async def api_request(
    method: str,
    path: str,
    state: Dict[str, Any],
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    files: Optional[dict] = None,
    expect_json: bool = True,
) -> Any:
    """Perform an API request with automatic refresh on 401."""
    url = build_url(path)

    def _request(token: str):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.request(
            method,
            url,
            json=json_body,
            params=params,
            files=files,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        return resp

    resp = await asyncio.to_thread(_request, state.get("access_token"))
    if resp.status_code == 401 and await refresh_tokens(state):
        resp = await asyncio.to_thread(_request, state.get("access_token"))

    if not resp.ok:
        raise RuntimeError(f"{resp.status_code}: {resp.text}")

    return resp.json() if expect_json else resp


def _extract_api_error(exc: Exception) -> str:
    """Extract human-readable error from API RuntimeError ('status: json_body')."""
    msg = str(exc)
    # RuntimeError format: "400: {"detail":"Card tokenization failed: ..."}"
    if ": " in msg:
        json_part = msg.split(": ", 1)[1]
        try:
            data = json.loads(json_part)
            if isinstance(data, dict) and "detail" in data:
                detail = data["detail"]
                if isinstance(detail, dict):
                    return detail.get("message", str(detail))
                return detail
        except (json.JSONDecodeError, ValueError):
            pass
    return msg


def _is_limit_exceeded(exc: Exception) -> bool:
    """Check if an API error is a LIMIT_EXCEEDED response."""
    msg = str(exc)
    if ": " in msg:
        json_part = msg.split(": ", 1)[1]
        try:
            data = json.loads(json_part)
            if isinstance(data, dict):
                detail = data.get("detail")
                if isinstance(detail, dict) and detail.get("code") == "LIMIT_EXCEEDED":
                    return True
        except (json.JSONDecodeError, ValueError):
            pass
    return False


# --- UI helpers ------------------------------------------------------------ #
def get_main_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_NEW_CHAT), KeyboardButton(BTN_IMAGE)],
        [KeyboardButton(BTN_HISTORY), KeyboardButton(BTN_MODEL)],
        [KeyboardButton(BTN_SETTINGS), KeyboardButton(BTN_SUBSCRIBE)],
        [KeyboardButton(BTN_FEEDBACK), KeyboardButton(BTN_HELP)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


async def answer(
    update: Update,
    text: str,
    *,
    markup: Optional[Any] = None,
    html_mode: bool = False,
) -> None:
    """Reply or edit the message depending on update type."""
    parse_mode = ParseMode.HTML if html_mode else None
    if update.callback_query:
        query = update.callback_query
        with contextlib.suppress(BadRequest):
            # If we are answering a callback query, we usually want to edit the message
            # But if markup is ReplyKeyboardMarkup, we must send a new message
            if isinstance(markup, ReplyKeyboardMarkup):
                await query.message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)
            else:
                await query.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
            await query.answer()
            return
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)


def trim(text: str, limit: int = 3500) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


# --- Core flows ------------------------------------------------------------ #
async def ensure_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """Ensure user is authenticated and state is initialized."""
    state = get_state(context)
    if state.get("access_token"):
        return state

    user = update.effective_user
    if not user:
        raise RuntimeError("No user information available.")

    ok = await authenticate_user(user.id, user.first_name, user.username, state)
    if not ok:
        await answer(update, "⚠️ Autentifikatsiya xatosi. Iltimos /start ni bosing.")
        raise RuntimeError("Auth failed")

    # Enforce phone number check for ALL users (new and existing)
    # We check via API if user has phone.
    try:
        me = await api_request("get", "/auth/me", state)
        has_phone = bool(me.get("phone_e164"))
    except Exception:
        has_phone = False

    if not has_phone:
        # Request phone number
        btn = KeyboardButton("📱 Telefon raqamni yuborish", request_contact=True)
        kb = ReplyKeyboardMarkup([[btn]], resize_keyboard=True, one_time_keyboard=True)
        await answer(update, "Salom! Botdan to'liq foydalanish uchun telefon raqamingizni yuboring.", markup=kb)
        # Stop processing current update
        raise RuntimeError("Phone required")

    # Register for notifications (link Chat ID)
    try:
        # We use the chat ID as the token for Telegram platform
        device_payload = {"token": str(update.effective_chat.id), "platform": "telegram"}
        await api_request("post", "/notifications/device", state, json_body=device_payload)
    except Exception as exc:
        # Log but don't fail the flow - notifications are optional
        logger.warning("Notification registration failed: %s", exc)

    return state


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        state = await ensure_ready(update, context)
    except RuntimeError as e:
        if str(e) == "Phone required":
            return
        raise e
    state["input_mode"] = "chat"
    state["conversation_id"] = None
    state["attachments"] = []
    await ensure_default_model(state)

    # Handle deep links (e.g., /start payment_123)
    if context.args:
        arg = context.args[0]
        if arg.startswith("payment_"):
            try:
                payment_id = int(arg.replace("payment_", ""))
                resp = await api_request("get", f"/subscriptions/payments/{payment_id}", state)
                status = resp.get("status", "unknown")
                if status == "paid":
                    await answer(update, "✅ To'lov muvaffaqiyatli amalga oshdi! Obunangiz faol.", markup=get_main_menu())
                elif status == "failed":
                    await answer(update, "❌ To'lov amalga oshmadi. Qayta urinib ko'ring.", markup=get_main_menu())
                else:
                    await answer(update, f"⏳ To'lov holati: {status}", markup=get_main_menu())
                return
            except Exception as exc:
                logger.warning("Payment deep link failed: %s", exc)

    user = update.effective_user

    name = html.escape(user.first_name or "do'st")
    hello = (
        f"Assalomu alaykum, {name}! 👋\n\n"
        "Men <b>Salom AI</b> telegram yordamchisiman.\n"
        "Matn, ovoz va rasm yaratish uchun quyidagi menyudan foydalaning."
    )
    await answer(update, hello, markup=get_main_menu(), html_mode=True)


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(context)
    contact = update.message.contact
    
    if contact.user_id != update.effective_user.id:
        await answer(update, "⚠️ Iltimos, o'zingizning raqamingizni yuboring.")
        return
        
    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
        
    # Send to backend to link phone
    try:
        # Re-authenticate with phone
        payload = {
            "telegram_id": update.effective_user.id,
            "first_name": update.effective_user.first_name,
            "username": update.effective_user.username,
            "phone": phone
        }
        
        # We use direct request or api_request but MUST capture tokens
        # /auth/telegram is public, simply posts data
        data = await api_request("post", "/auth/telegram", state, json_body=payload)
        
        # CRITICAL: Save the new tokens
        state["access_token"] = data["access_token"]
        state["refresh_token"] = data.get("refresh_token")
        
        # Register for notifications immediately
        try:
            device_payload = {"token": str(update.effective_chat.id), "platform": "telegram"}
            await api_request("post", "/notifications/device", state, json_body=device_payload)
            success_text = "✅ Notifications enabled! You will now receive alerts here."
        except Exception as exc:
            logger.warning("Notification registration failed: %s", exc)
            success_text = "✅ Telefon raqamingiz tasdiqlandi!" # Fallback

        await answer(update, success_text, markup=get_main_menu())
        
    except Exception as exc:
        logger.exception("Phone update failed: %s", exc)
        await answer(update, "⚠️ Raqamni saqlashda xatolik.")
        return

    # Proceed to main menu logic
    await start(update, context)


async def handle_new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    state["conversation_id"] = None
    state["input_mode"] = "chat"
    state["attachments"] = []
    await answer(update, "🆕 Yangi suhbat boshlandi. Savolingizni yozing yoki ovozli xabar yuboring.", markup=get_main_menu())


async def fetch_conversations(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        data = await api_request("get", "/conversations", state, params={"limit": 10})
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Failed to load conversations: %s", exc)
        return []


async def choose_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    conversations = await fetch_conversations(state)
    if not conversations:
        await answer(update, "📭 Hali saqlangan suhbatlar yo'q.", markup=get_main_menu())
        return

    rows = []
    for conv in conversations[:10]:
        title = conv.get("title") or conv.get("preview") or f"Suhbat {conv.get('id')}"
        button_text = trim(title, 40)
        rows.append([InlineKeyboardButton(button_text, callback_data=f"conv:{conv['id']}")])
    
    await answer(update, "Davom ettirish uchun suhbatni tanlang:", markup=InlineKeyboardMarkup(rows))


async def set_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE, conv_id: int) -> None:
    state = await ensure_ready(update, context)
    state["conversation_id"] = conv_id
    state["input_mode"] = "chat"
    await answer(update, f"✅ Suhbat #{conv_id} tanlandi. Davom etishingiz mumkin.", markup=get_main_menu())


async def load_models(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        data = await api_request("get", "/chat/models", state)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Model list failed: %s", exc)
        return []


async def ensure_default_model(state: Dict[str, Any]) -> None:
    """Ensure the currently set model is allowed for this user."""
    models = await load_models(state)
    if not models:
        return

    current = state.get("model") or DEFAULT_MODEL
    allowed_ids = {m.get("id") for m in models}
    if current not in allowed_ids:
        preferred = next((m for m in models if "mini" in m.get("id", "")), None)
        state["model"] = preferred.get("id") if preferred else models[0].get("id")


async def choose_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    await ensure_default_model(state)
    models = await load_models(state)
    if not models:
        await answer(update, "⚠️ Model ro'yxatini olishda xatolik.", markup=get_main_menu())
        return

    rows = []
    current = state.get("model")
    for model in models:
        label = f"{model.get('name', model.get('id'))}"
        if model.get("vision"):
            label += " 👁"
        if model.get("id") == current:
            label += " ✅"
        rows.append([InlineKeyboardButton(label, callback_data=f"model:{model['id']}")])
    
    await answer(update, "🤖 Modelni tanlang:", markup=InlineKeyboardMarkup(rows))


async def prompt_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    state["input_mode"] = "image"
    await answer(update, "🖼️ Rasm tavsifini yuboring (masalan: 'Tog'dagi uy').", markup=get_main_menu())


async def prompt_system_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    state["input_mode"] = "set_prompt"
    await answer(update, "⚙️ Yangi tizim ko'rsatmasini (system prompt) yuboring.", markup=get_main_menu())


async def show_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    try:
        sub_usage = await api_request("get", "/subscriptions/usage", state)
    except Exception as exc:
        logger.exception("Usage fetch failed: %s", exc)
        await answer(update, "⚠️ Statistika yuklashda xatolik.", markup=get_main_menu())
        return

    limits = sub_usage.get("limits", {})
    usage = sub_usage.get("usage", {})
    plan = sub_usage.get("plan_name", sub_usage.get("plan", "Noma'lum"))
    
    text = (
        f"📊 <b>Tarif: {html.escape(plan)}</b>\n\n"
        f"⚡ Fast: {usage.get('fast_messages', 0)}/{limits.get('max_messages_fast', 0)}\n"
        f"🧠 Smart: {usage.get('smart_messages', 0)}/{limits.get('max_messages_smart', 0)}\n"
        f"🚀 Super: {usage.get('super_smart_messages', 0)}/{limits.get('max_messages_super_smart', 0)}\n"
        f"🖼️ Rasmlar: {usage.get('images', 0)}/{limits.get('max_image_generations', 0)}\n"
        f"🎙️ Ovoz: {usage.get('voice_minutes', 0)}/{limits.get('max_voice_minutes', 0)} daqiqa\n"
    )
    await answer(update, text, markup=get_main_menu(), html_mode=True)


async def handle_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    try:
        plans = await api_request("get", "/subscriptions/plans", state)
    except Exception as exc:
        logger.warning("Failed to fetch plans: %s", exc)
        await answer(update, "⚠️ Rejalarni yuklashda xatolik.")
        return

    message_text = "<b>💎 Obuna Rejalari</b>\n\n"
    rows = []

    for plan in plans:
        price = f"{plan['price_uzs']:,} UZS" if plan['price_uzs'] > 0 else "Bepul"
        message_text += f"<b>{plan['name']}</b> - {price}\n"

        if plan.get('benefits'):
            for benefit in plan['benefits']:
                text = benefit.get('uz', benefit.get('en', ''))
                if text:
                    message_text += f"✅ {text}\n"
        else:
            message_text += "✅ Imtiyozlar ko'rsatilmagan\n"

        message_text += "\n"

        if plan['price_uzs'] > 0:
            text = f"{plan['name']} - {price}"
            rows.append([InlineKeyboardButton(text, callback_data=f"plan:{plan['code']}")])

    await answer(update, message_text, markup=InlineKeyboardMarkup(rows), html_mode=True)


async def initiate_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_code: str) -> None:
    """Start card tokenization flow: ask user for card number."""
    state = await ensure_ready(update, context)
    state["pending_plan_code"] = plan_code
    state["input_mode"] = "card_number"

    cancel_row = [[InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_payment")]]
    await answer(
        update,
        "💳 <b>Karta raqamingizni kiriting</b> (16 raqam):\n\n"
        "Masalan: <code>8600 1234 5678 9012</code>\n\n"
        "🔒 Ma'lumotlar xavfsiz Click tizimi orqali qayta ishlanadi.\n"
        "Karta raqami saqlanmaydi.",
        markup=InlineKeyboardMarkup(cancel_row),
        html_mode=True,
    )


async def handle_card_number(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Process card number input, ask for expiry."""
    state = get_state(context)
    digits = text.replace(" ", "").replace("-", "")

    if not digits.isdigit() or len(digits) != 16:
        await answer(update, "⚠️ Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qayta kiriting:")
        return

    state["pending_card_number"] = digits
    state["input_mode"] = "card_expiry"
    cancel_row = [[InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_payment")]]
    await answer(
        update,
        "📅 Amal qilish muddatini kiriting (MMYY):\n\nMasalan: <code>0826</code>",
        markup=InlineKeyboardMarkup(cancel_row),
        html_mode=True,
    )


async def handle_card_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Process expiry input, call tokenize API, ask for SMS code."""
    state = get_state(context)
    digits = text.replace("/", "").replace(" ", "")

    if not digits.isdigit() or len(digits) != 4:
        await answer(update, "⚠️ Muddat MMYY formatida bo'lishi kerak (masalan: 0826). Qayta kiriting:")
        return

    card_number = state.get("pending_card_number")
    if not card_number:
        state["input_mode"] = "chat"
        await answer(update, "⚠️ Karta raqami topilmadi. Qaytadan boshlang.", markup=get_main_menu())
        return

    status_msg = await update.message.reply_text("⏳ Karta tekshirilmoqda...")

    try:
        resp = await api_request(
            "post", "/cards/tokenize/request", state,
            json_body={"card_number": card_number, "expire_date": digits},
        )
        request_id = resp.get("request_id")
        phone_hint = resp.get("phone_hint", "")

        state["pending_request_id"] = request_id
        state["pending_phone_hint"] = phone_hint
        state["input_mode"] = "sms_code"

        hint_text = f" ({phone_hint})" if phone_hint else ""
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"📱 SMS kod yuborildi{hint_text}.\n\nKodni kiriting:",
        )
    except Exception as exc:
        logger.exception("Tokenize request failed: %s", exc)
        error_detail = _extract_api_error(exc)

        rows = [
            [InlineKeyboardButton("🔄 Qayta urinish", callback_data="retry_card")],
            [InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_payment")],
        ]
        state["input_mode"] = "chat"
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"⚠️ Xatolik: {html.escape(error_detail)}\n\nBoshqa karta bilan urinib ko'ring yoki bekor qiling.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def handle_sms_code(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Process SMS code, verify token, charge first payment."""
    state = get_state(context)
    code = text.strip()

    if not code.isdigit():
        await answer(update, "⚠️ Faqat raqam kiriting:")
        return

    request_id = state.get("pending_request_id")
    plan_code = state.get("pending_plan_code")

    if not request_id or not plan_code:
        state["input_mode"] = "chat"
        await answer(update, "⚠️ Sessiya tugadi. Qaytadan boshlang.", markup=get_main_menu())
        return

    status_msg = await update.message.reply_text("⏳ Tasdiqlanmoqda va to'lov amalga oshirilmoqda...")

    try:
        resp = await api_request(
            "post", "/cards/tokenize/verify", state,
            json_body={"request_id": request_id, "sms_code": int(code), "plan_code": plan_code},
        )

        if resp.get("success"):
            sub_info = resp.get("subscription", {})
            plan_name = sub_info.get("plan", plan_code)
            expires = sub_info.get("expires_at", "")

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=(
                    f"✅ <b>Obuna muvaffaqiyatli faollashtirildi!</b>\n\n"
                    f"📋 Reja: <b>{html.escape(plan_name)}</b>\n"
                    f"📅 Amal qilish: {html.escape(expires[:10] if expires else 'N/A')}\n"
                    f"🔄 Avtomatik yangilanish: Yoqilgan\n\n"
                    f"Karta saqlandi va har oy avtomatik to'lov amalga oshiriladi."
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text="⚠️ Tasdiqlash amalga oshmadi. Qayta urinib ko'ring.",
            )
    except Exception as exc:
        logger.exception("SMS verify failed: %s", exc)
        error_detail = _extract_api_error(exc)

        rows = [
            [InlineKeyboardButton("🔄 Qayta urinish", callback_data="retry_sms")],
            [InlineKeyboardButton("💳 Kartani o'zgartirish", callback_data="retry_card")],
            [InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_payment")],
        ]
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"⚠️ Xatolik: {html.escape(error_detail)}\n\nQuyidagi amallardan birini tanlang:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        # Don't clear state yet — user might retry
        return

    # Clear pending state on success or non-retryable outcome
    state["input_mode"] = "chat"
    state["pending_card_number"] = None
    state["pending_request_id"] = None
    state["pending_phone_hint"] = None
    state["pending_plan_code"] = None


async def show_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current subscription status with management options."""
    state = await ensure_ready(update, context)
    try:
        current = await api_request("get", "/subscriptions/current", state)
    except Exception as exc:
        logger.warning("Failed to fetch subscription: %s", exc)
        await answer(update, "⚠️ Obuna ma'lumotlarini yuklashda xatolik.", markup=get_main_menu())
        return

    if not current.get("active"):
        rows = [[InlineKeyboardButton("💎 Obuna sotib olish", callback_data="goto_subscribe")]]
        await answer(update, "Faol obunangiz yo'q.", markup=InlineKeyboardMarkup(rows))
        return

    plan = current.get("plan", "Noma'lum")
    expires = current.get("expires_at", "")[:10] if current.get("expires_at") else "N/A"
    auto_renew = current.get("auto_renew", False)
    card = current.get("saved_card")

    renew_status = "✅ Yoqilgan" if auto_renew else "❌ O'chirilgan"
    text = (
        f"<b>📋 Obuna holati</b>\n\n"
        f"Reja: <b>{html.escape(plan)}</b>\n"
        f"Amal qilish: {html.escape(expires)}\n"
        f"Avtomatik yangilanish: {renew_status}\n"
    )
    if card:
        text += f"Karta: {html.escape(card.get('masked_number', ''))}\n"

    rows = []
    if auto_renew:
        rows.append([InlineKeyboardButton("🔄 Avtomatik yangilanishni o'chirish", callback_data="toggle_renew:off")])
    else:
        rows.append([InlineKeyboardButton("🔄 Avtomatik yangilanishni yoqish", callback_data="toggle_renew:on")])
    rows.append([InlineKeyboardButton("💳 Saqlangan kartalar", callback_data="show_cards")])
    rows.append([InlineKeyboardButton("❌ Obunani bekor qilish", callback_data="cancel_sub")])

    await answer(update, text, markup=InlineKeyboardMarkup(rows), html_mode=True)


async def show_saved_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's saved cards with delete option."""
    state = await ensure_ready(update, context)
    try:
        cards = await api_request("get", "/cards", state)
    except Exception as exc:
        logger.warning("Failed to fetch cards: %s", exc)
        await answer(update, "⚠️ Kartalarni yuklashda xatolik.")
        return

    if not cards:
        await answer(update, "Saqlangan kartalar yo'q.", markup=get_main_menu())
        return

    text = "<b>💳 Saqlangan kartalar</b>\n\n"
    rows = []
    for card in cards:
        text += f"• {html.escape(card.get('masked_number', ''))} ({html.escape(card.get('phone_hint', ''))})\n"
        rows.append([InlineKeyboardButton(
            f"🗑 {card.get('masked_number', '')} ni o'chirish",
            callback_data=f"delete_card:{card['id']}",
        )])

    await answer(update, text, markup=InlineKeyboardMarkup(rows), html_mode=True)


async def prompt_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    state["input_mode"] = "feedback"
    await answer(update, "📩 Fikr va takliflaringizni yozib qoldiring.", markup=get_main_menu())


async def submit_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    state = await ensure_ready(update, context)
    try:
        await api_request("post", "/feedback", state, json_body={"content": text, "platform": "telegram"})
        await answer(update, "✅ Fikr-mulohazangiz qabul qilindi. Rahmat!", markup=get_main_menu())
    except Exception as exc:
        logger.exception("Feedback submission failed: %s", exc)
        await answer(update, "⚠️ Xatolik yuz berdi.")
    finally:
        state["input_mode"] = "chat"


# --- Message processing ---------------------------------------------------- #
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    
    # Handle Menu Commands
    if text == BTN_NEW_CHAT:
        await handle_new_chat(update, context)
        return
    elif text == BTN_HISTORY:
        await choose_conversation(update, context)
        return
    elif text == BTN_IMAGE:
        await prompt_image(update, context)
        return
    elif text == BTN_MODEL:
        await choose_model(update, context)
        return
    elif text == BTN_SETTINGS:
        await prompt_system_prompt(update, context)
        return
    elif text == BTN_SUBSCRIBE:
        await handle_subscribe(update, context)
        return
    elif text == BTN_FEEDBACK:
        await prompt_feedback(update, context)
        return
    elif text == BTN_HELP:
        await start(update, context)
        return

    # Handle Normal Input
    try:
        state = await ensure_ready(update, context)
    except RuntimeError as e:
        if str(e) == "Phone required":
            return
        raise e

    mode = state.get("input_mode", "chat")
    
    if mode == "image":
        await generate_image(update, context, text)
    elif mode == "set_prompt":
        await update_system_prompt(update, context, text)
    elif mode == "feedback":
        await submit_feedback(update, context, text)
    elif mode == "card_number":
        await handle_card_number(update, context, text)
    elif mode == "card_expiry":
        await handle_card_expiry(update, context, text)
    elif mode == "sms_code":
        await handle_sms_code(update, context, text)
    else:
        await handle_chat(update, context, text)


async def stream_chat_response(
    bot: Any,
    chat_id: int,
    message_id: int,
    payload: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Stream chat response to Telegram with throttling and auto-refresh on 401."""
    url = build_url("/chat/stream")
    
    async def _stream(token: str) -> Optional[Dict[str, Any]]:
        headers = {"Authorization": f"Bearer {token}"}
        full_text = ""
        buffer = ""
        last_update_time = time.time()
        result = {"conversation_id": None, "reply": ""}
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    
                    # Check for 401 immediately
                    if response.status_code == 401:
                        return None # Signal need to refresh

                    if response.status_code != 200:
                        error_body = await response.read()
                        error_text = error_body.decode('utf-8')
                        # Try to parse structured error
                        try:
                            err_data = json.loads(error_text)
                            detail = err_data.get("detail")
                            if isinstance(detail, dict) and detail.get("code") == "LIMIT_EXCEEDED":
                                result["error"] = detail.get("message", error_text)
                                result["limit_exceeded"] = True
                                return result
                            elif isinstance(detail, str):
                                error_text = detail
                        except (json.JSONDecodeError, ValueError):
                            pass
                        result["error"] = f"HTTP {response.status_code}: {error_text}"
                        return result

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        
                        try:
                            line_content = line[6:].strip()
                            if not line_content or line_content == "[DONE]":
                                continue
                                
                            data = json.loads(line_content)
                            event_type = data.get("type")
                            
                            if event_type == "chunk":
                                content = data.get("content", "")
                                if content:
                                    full_text += content
                                    buffer += content
                                    
                                    # Throttling
                                    current_time = time.time()
                                    if len(buffer) > 20 or (current_time - last_update_time) > 1.5:
                                        try:
                                            await bot.edit_message_text(
                                                chat_id=chat_id,
                                                message_id=message_id,
                                                text=full_text + " ▌",
                                                parse_mode=None
                                            )
                                            buffer = ""
                                            last_update_time = current_time
                                        except RetryAfter as e:
                                            await asyncio.sleep(e.retry_after)
                                        except Exception:
                                            pass
                                            
                            elif event_type == "done":
                                result["conversation_id"] = data.get("conversation_id")
                                
                            elif event_type == "error":
                                result["error"] = data.get("message")
                                
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"Stream error: {e}")
            full_text += f"\n\n[Xatolik: {str(e)}]"
            result["error"] = str(e) # Capture error

        result["reply"] = full_text
        return result

    # First attempt
    current_token = state.get("access_token")
    data = await _stream(current_token)
    
    # Handle refresh if needed (returns None on 401)
    if data is None:
        logger.info("Got 401 in stream, refreshing token...")
        if await refresh_tokens(state):
            current_token = state.get("access_token")
            data = await _stream(current_token)
            if data is None: # Still 401
                data = {"reply": "", "error": "Autentifikatsiya eskirgan. Iltimos qayta kiring."}
        else:
             data = {"reply": "", "error": "Token yangilanmadi. Iltimos qayta kiring."}

    # Final UI update logic (unchanged)
    full_text = data.get("reply", "")
    try:
        final_text = full_text if full_text else "Javob olinmadi."
        if data.get("error") and not full_text:
             final_text = f"⚠️ {data['error']}"

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=final_text,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                chat_id=chat_id, 
                message_id=message_id, 
                text=final_text, # Use the computed final text
                parse_mode=None
            )
            
    return data


async def handle_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
    *,
    return_reply: bool = False,
) -> Optional[str]:
    state = await ensure_ready(update, context)
    chat_id = update.effective_chat.id
    
    # Send initial status
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    status_msg = await update.message.reply_text("⏳ Salom AI o'ylayapti...")

    payload = {
        "text": user_text,
        "conversation_id": state.get("conversation_id"),
        "model": state.get("model", DEFAULT_MODEL),
    }
    if state.get("attachments"):
        payload["attachments"] = state["attachments"]

    try:
        # Check authentication token validity first
        if not state.get("access_token"):
             await authenticate_user(update.effective_user.id, update.effective_user.first_name, update.effective_user.username, state)

        # Start streaming
        data = await stream_chat_response(
            context.bot, 
            chat_id, 
            status_msg.message_id, 
            payload, 
            state
        )
        
        # If we got a 401 during stream (which is hard to catch mid-stream with httpx), 
        # normally we'd need to retry. For simplicity, we assume auth is valid or handled.
        # Ideally, stream_chat_response could return a specific error code.
        
        if data.get("error"):
            error_text = data['error']
            # Check if the error contains LIMIT_EXCEEDED indicators
            if data.get("limit_exceeded") or "LIMIT_EXCEEDED" in error_text or "limitga yetdingiz" in error_text:
                rows = [[InlineKeyboardButton("💎 Obunani yangilash", callback_data="goto_subscribe")]]
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text=f"⚠️ {error_text}\n\nObunangizni yangilang:",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return None
            await answer(update, f"⚠️ Xatolik: {error_text}")
            return None

    except Exception as exc:
        logger.exception("Chat error: %s", exc)
        if _is_limit_exceeded(exc):
            error_detail = _extract_api_error(exc)
            rows = [[InlineKeyboardButton("💎 Obunani yangilash", callback_data="goto_subscribe")]]
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"⚠️ {error_detail}\n\nObunangizni yangilang:",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return None
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text="⚠️ Kechirasiz, xatolik yuz berdi. Qayta urinib ko'ring."
        )
        return None

    state["conversation_id"] = data.get("conversation_id")
    state["input_mode"] = "chat"
    state["attachments"] = []
    
    reply_text = data.get("reply", "")
    return reply_text if return_reply else None


async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    state = await ensure_ready(update, context)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

    try:
        data = await api_request("post", "/images/generate", state, json_body={"prompt": prompt})
        image_url = data.get("url")
        if image_url:
            await update.message.reply_photo(image_url, caption=f"🖼️ {prompt}")
        else:
            await answer(update, "⚠️ Rasm URL olinmadi.")
    except Exception as exc:
        logger.exception("Image generation error: %s", exc)
        if _is_limit_exceeded(exc):
            error_detail = _extract_api_error(exc)
            rows = [[InlineKeyboardButton("💎 Obunani yangilash", callback_data="goto_subscribe")]]
            await answer(update, f"⚠️ {error_detail}\n\nObunangizni yangilang:", markup=InlineKeyboardMarkup(rows))
        else:
            await answer(update, "⚠️ Rasm yaratishda xatolik.")
        return
    finally:
        state["input_mode"] = "chat"


async def update_system_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    state = await ensure_ready(update, context)
    try:
        await api_request("put", "/settings", state, json_body={"system_prompt": prompt})
        await answer(update, "✅ Tizim ko'rsatmasi yangilandi.", markup=get_main_menu())
    except Exception as exc:
        logger.exception("Settings update failed: %s", exc)
        await answer(update, "⚠️ Sozlamani saqlashda xatolik.")
    finally:
        state["input_mode"] = "chat"


# --- Attachments ----------------------------------------------------------- #
async def upload_file_to_backend(state: Dict[str, Any], file_path: str, file_name: str, mime: str) -> Optional[str]:
    def _request(token: str):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with open(file_path, "rb") as f:
            files = {"file": (file_name, f, mime)}
            resp = requests.post(
                build_url("/files/upload"),
                files=files,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            return resp

    resp = await asyncio.to_thread(_request, state.get("access_token"))
    if resp.status_code == 401 and await refresh_tokens(state):
        resp = await asyncio.to_thread(_request, state.get("access_token"))

    if not resp.ok:
        logger.warning("File upload failed: %s", resp.text)
        return None
    try:
        return resp.json().get("url")
    except Exception:
        return None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(delete=True, suffix=".jpg") as tmp:
        await file.download_to_drive(tmp.name)
        url = await upload_file_to_backend(state, tmp.name, os.path.basename(tmp.name), "image/jpeg")
    if url:
        state.setdefault("attachments", []).append(url)
        await answer(update, "📎 Rasm biriktirildi. Matn yuboring.", markup=get_main_menu())
    else:
        await answer(update, "⚠️ Rasm yuklanmadi.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    suffix = os.path.splitext(doc.file_name or "file")[1] or ".dat"
    with tempfile.NamedTemporaryFile(delete=True, suffix=suffix) as tmp:
        await file.download_to_drive(tmp.name)
        url = await upload_file_to_backend(
            state,
            tmp.name,
            doc.file_name or os.path.basename(tmp.name),
            doc.mime_type or "application/octet-stream",
        )
    if url:
        state.setdefault("attachments", []).append(url)
        await answer(update, "📎 Fayl biriktirildi. Matn yuboring.", markup=get_main_menu())
    else:
        await answer(update, "⚠️ Fayl yuklanmadi.")


# --- Voice ---------------------------------------------------------------- #
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_ready(update, context)
    from io import BytesIO

    voice = update.message.voice
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.RECORD_VOICE)

    # Download voice
    tg_file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(delete=True, suffix=".ogg") as tmp:
        await tg_file.download_to_drive(tmp.name)
        try:
            # 1) STT
            with open(tmp.name, "rb") as f:
                files = {"file": ("voice.ogg", f, "audio/ogg")}
                stt_resp = await api_request("post", "/stt", state, files=files)
            transcript = stt_resp.get("text", "")
            await update.message.reply_text(f"🎤 {transcript}")

            # 2) Chat
            reply_text = await handle_chat(update, context, transcript, return_reply=True)

            # 3) TTS reply
            if reply_text:
                try:
                    tts_resp = await api_request(
                        "post",
                        "/tts",
                        state,
                        json_body={"text": reply_text},
                        expect_json=False,
                    )
                    await update.message.reply_audio(
                        audio=BytesIO(tts_resp.content),
                        filename="salom-ai-reply.mp3",
                        caption="🔊 Javob (audio)",
                    )
                except Exception as tts_exc:
                    logger.warning("TTS failed: %s", tts_exc)
        except Exception as exc:
            logger.exception("Voice handling failed: %s", exc)
            await answer(update, "⚠️ Ovozli xabarni qayta ishlashda xatolik.")


# --- Callback routing ------------------------------------------------------ #
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    if data.startswith("conv:"):
        conv_id = int(data.split(":", 1)[1])
        await set_conversation(update, context, conv_id)
    elif data.startswith("model:"):
        model_id = data.split(":", 1)[1]
        state = await ensure_ready(update, context)
        state["model"] = model_id
        await answer(update, f"✅ Model tanlandi: {html.escape(model_id)}", markup=get_main_menu())
    elif data.startswith("plan:"):
        plan_code = data.split(":", 1)[1]
        await initiate_payment(update, context, plan_code)
    elif data == "goto_subscribe":
        await handle_subscribe(update, context)
    elif data.startswith("toggle_renew:"):
        enabled = data.split(":", 1)[1] == "on"
        state = await ensure_ready(update, context)
        try:
            await api_request("post", "/subscriptions/auto-renew", state, json_body={"enabled": enabled})
            status = "yoqildi ✅" if enabled else "o'chirildi ❌"
            await answer(update, f"🔄 Avtomatik yangilanish {status}", markup=get_main_menu())
        except Exception as exc:
            logger.warning("Toggle auto-renew failed: %s", exc)
            await answer(update, "⚠️ Xatolik yuz berdi.", markup=get_main_menu())
    elif data == "cancel_sub":
        state = await ensure_ready(update, context)
        try:
            resp = await api_request("post", "/subscriptions/cancel", state, json_body={})
            expires = resp.get("expires_at", "")[:10] if resp.get("expires_at") else ""
            await answer(
                update,
                f"✅ Avtomatik yangilanish o'chirildi.\nObuna {html.escape(expires)} gacha faol qoladi.",
                markup=get_main_menu(),
                html_mode=True,
            )
        except Exception as exc:
            logger.warning("Cancel sub failed: %s", exc)
            await answer(update, "⚠️ Xatolik yuz berdi.", markup=get_main_menu())
    elif data == "cancel_payment":
        state = await ensure_ready(update, context)
        state["input_mode"] = "chat"
        state["pending_card_number"] = None
        state["pending_request_id"] = None
        state["pending_phone_hint"] = None
        state["pending_plan_code"] = None
        await answer(update, "❌ To'lov bekor qilindi.", markup=get_main_menu())
    elif data == "retry_sms":
        state = await ensure_ready(update, context)
        if state.get("pending_request_id"):
            state["input_mode"] = "sms_code"
            phone_hint = state.get("pending_phone_hint", "")
            hint_text = f" ({phone_hint})" if phone_hint else ""
            await answer(update, f"📱 SMS kodni qayta kiriting{hint_text}:")
        else:
            await answer(update, "⚠️ Sessiya tugadi. Qaytadan boshlang.", markup=get_main_menu())
    elif data == "retry_card":
        state = await ensure_ready(update, context)
        plan_code = state.get("pending_plan_code")
        if plan_code:
            state["input_mode"] = "chat"
            state["pending_card_number"] = None
            state["pending_request_id"] = None
            await initiate_payment(update, context, plan_code)
        else:
            await answer(update, "⚠️ Sessiya tugadi. Qaytadan boshlang.", markup=get_main_menu())
    elif data == "show_cards":
        await show_saved_cards(update, context)
    elif data.startswith("delete_card:"):
        card_id = int(data.split(":", 1)[1])
        state = await ensure_ready(update, context)
        try:
            await api_request("delete", f"/cards/{card_id}", state)
            await answer(update, "✅ Karta o'chirildi.", markup=get_main_menu())
        except Exception as exc:
            logger.warning("Delete card failed: %s", exc)
            await answer(update, "⚠️ Kartani o'chirishda xatolik.", markup=get_main_menu())
    else:
        await answer(update, "⚠️ Buyruq tanilmadi.", markup=get_main_menu())


# --- Application setup ----------------------------------------------------- #
async def post_init(application: Application) -> None:
    commands = [
        ("start", "Botni ishga tushirish"),
        ("menu", "Asosiy menyu"),
        ("subscription", "Obuna holati"),
        ("cards", "Saqlangan kartalar"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    persistence = PicklePersistence(filepath=STATE_FILE)
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("subscription", show_subscription))
    application.add_handler(CommandHandler("cards", show_saved_cards))

    # Callbacks
    application.add_handler(CallbackQueryHandler(on_callback))

    # Messages
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
