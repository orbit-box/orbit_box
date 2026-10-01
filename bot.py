import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
PRIVACY_OPERATOR = os.getenv("PRIVACY_OPERATOR", "운영팀").strip()
PRIVACY_CONTACT = os.getenv("PRIVACY_CONTACT", "관리자 문의").strip()
DB_PATH = os.getenv("DB_PATH", "applications.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN이 설정되지 않았습니다.")
if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID가 설정되지 않았습니다.")

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
KST = ZoneInfo("Asia/Seoul")

(CONSENT, NAME, PHONE, EXPERIENCE, AMOUNT, INTERESTS, HELP_TYPE, CONFIRM) = range(8)

EXPERIENCE_OPTIONS = [
    "투자 경험 없음",
    "6개월 미만",
    "6개월~1년",
    "1~3년",
    "3~5년",
    "5년 이상",
]

AMOUNT_OPTIONS = [
    "500만원 미만",
    "500만~1,000만원",
    "1,000만~3,000만원",
    "3,000만~5,000만원",
    "5,000만~1억원",
    "1억원 이상",
]

HELP_OPTIONS = [
    "보유 종목 대응",
    "시장 흐름 분석",
    "차트 공부",
    "투자 방향 설정",
    "처음부터 배우기",
    "기타",
]

STATUS_LABELS = {
    "new": "신규",
    "accepted": "접수",
    "consulting": "상담중",
    "done": "완료",
}

OPERATING_HOURS_NOTICE = (
    "상담 가능 시간은 평일 오전 9시부터 오후 9시까지이며, 주말 및 공휴일은 운영하지 않습니다.\n\n"
    "문의량에 따라 답변이 다소 지연될 수 있으며, 운영시간 외 접수된 요청은 다음 영업일에 순차적으로 안내드립니다."
)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                telegram_name TEXT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                experience TEXT NOT NULL,
                amount_range TEXT NOT NULL,
                interests TEXT,
                help_type TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                admin_message_id INTEGER
            )
            """
        )
        conn.commit()

def privacy_text() -> str:
    return f"""
<b>개인정보 수집·이용 동의</b>

참가 신청 및 상담 진행을 위해 아래와 같이 개인정보를 수집·이용합니다.

<b>1. 수집 항목</b>
• 이름
• 전화번호
• 투자경력
• 현재 운용 중인 투자금액 구간
• 텔레그램 계정 정보(사용자명, 사용자 ID 등)

<b>2. 이용 목적</b>
• 참가 신청자 확인
• 신청 내용 확인 및 상담 진행
• 필요한 안내 및 연락
• 투자경력과 투자규모를 참고한 상담 방향 설정
• 중복 신청 및 상담 진행 상태 관리

투자경력 및 투자금액은 상황에 맞는 상담과 안내를 위한 참고 자료이며, 투자 자격 판단이나 수익 보장을 위한 목적으로 수집하지 않습니다.

<b>3. 보유 기간</b>
상담 종료 또는 참가 신청 처리 완료일로부터 <b>6개월</b>간 보관 후 파기합니다.
다만 관계 법령에 따라 보관이 필요한 경우 해당 기간 동안 보관할 수 있습니다.

<b>4. 제3자 제공</b>
수집된 개인정보는 원칙적으로 제3자에게 제공하지 않습니다.
이용자의 별도 동의가 있거나 법령상 의무가 있는 경우에만 필요한 범위에서 제공될 수 있습니다.

<b>5. 동의 거부 권리</b>
개인정보 수집·이용에 동의하지 않을 권리가 있습니다.
다만 참가 신청 및 상담에 필요한 정보의 수집에 동의하지 않는 경우 신청 진행이 제한될 수 있습니다.

<b>6. 문의</b>
운영자: {PRIVACY_OPERATOR}
문의처: {PRIVACY_CONTACT}

<b>운영시간 안내</b>
{OPERATING_HOURS_NOTICE}

위 내용을 확인하였으며 개인정보 수집·이용에 동의하시겠습니까?
""".strip()

def inline_options(prefix: str, options: list[str], columns: int = 2):
    rows = []
    for i in range(0, len(options), columns):
        row = [
            InlineKeyboardButton(text=o, callback_data=f"{prefix}:{idx+i}")
            for idx, o in enumerate(options[i:i+columns])
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def phone_is_valid(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return len(digits) in (10, 11)

def normalize_phone(text: str) -> str:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return text.strip()

def summary_text(data: dict) -> str:
    return (
        "<b>입력하신 참가신청 내용을 확인해 주세요.</b>\n\n"
        f"이름: {data.get('name','')}\n"
        f"전화번호: {data.get('phone','')}\n"
        f"투자경력: {data.get('experience','')}\n"
        f"투자금액: {data.get('amount','')}\n"
        f"보유·관심 종목: {data.get('interests','없음')}\n"
        f"도움 요청: {data.get('help_type','')}\n\n"
        "내용이 맞으시면 <b>참가신청 완료</b>를 눌러주세요."
    )

def admin_application_text(row: dict | sqlite3.Row, status: str = "new") -> str:
    username = row["telegram_username"] if row["telegram_username"] else "없음"
    return (
        "🔔 <b>새로운 참가신청이 도착했습니다.</b>\n\n"
        f"<b>신청번호</b>: #{row['id']}\n"
        f"<b>이름</b>: {row['name']}\n"
        f"<b>전화번호</b>: {row['phone']}\n"
        f"<b>투자경력</b>: {row['experience']}\n"
        f"<b>투자금액</b>: {row['amount_range']}\n"
        f"<b>보유·관심 종목</b>: {row['interests'] or '없음'}\n"
        f"<b>도움 요청</b>: {row['help_type'] or '없음'}\n"
        f"<b>Telegram</b>: @{username if username != '없음' else '없음'}\n"
        f"<b>User ID</b>: <code>{row['telegram_user_id']}</code>\n"
        f"<b>신청시간</b>: {row['created_at']}\n\n"
        f"상태: <b>{STATUS_LABELS.get(status, status)}</b>"
    )

def admin_status_keyboard(app_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("접수", callback_data=f"status:{app_id}:accepted"),
        InlineKeyboardButton("상담중", callback_data=f"status:{app_id}:consulting"),
        InlineKeyboardButton("완료", callback_data=f"status:{app_id}:done"),
    ]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("동의하고 참가신청 시작", callback_data="consent:yes")],
        [InlineKeyboardButton("상담사 연결", callback_data="consult:request")],
        [InlineKeyboardButton("동의하지 않습니다", callback_data="consent:no")],
    ])
    await update.message.reply_text(
        privacy_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    return CONSENT

async def consultation_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    username = f"@{user.username}" if user.username else "없음"
    requested_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    admin_text = (
        "📞 <b>상담사 연결 요청이 도착했습니다.</b>\n\n"
        f"<b>Telegram 이름</b>: {user.full_name}\n"
        f"<b>Telegram</b>: {username}\n"
        f"<b>User ID</b>: <code>{user.id}</code>\n"
        f"<b>요청시간</b>: {requested_at}\n\n"
        "개인정보 안내 화면에서 상담사 연결을 요청했습니다."
    )

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_text,
        parse_mode=ParseMode.HTML,
    )

    await query.message.reply_text(
        "<b>상담 요청이 접수되었습니다.</b>\n\n"
        f"{OPERATING_HOURS_NOTICE}\n\n"
        "관리자가 확인한 뒤 순차적으로 안내드리겠습니다.",
        parse_mode=ParseMode.HTML,
    )
    return CONSENT


async def consent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "consent:no":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "개인정보 수집·이용에 동의하지 않으신 경우 참가신청 및 상담 진행이 어렵습니다.\n"
            "원하실 때 언제든 /start 명령으로 다시 신청하실 수 있습니다."
        )
        return ConversationHandler.END
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("신청자님의 <b>성함</b>을 입력해 주세요.", parse_mode=ParseMode.HTML)
    return NAME

async def name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("성함을 다시 입력해 주세요.")
        return NAME
    context.user_data["name"] = name
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("내 전화번호 공유하기", request_contact=True)],
         [KeyboardButton("직접 입력하기")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "연락 가능한 <b>전화번호</b>를 확인하겠습니다.\n\n"
        "아래 버튼으로 본인 전화번호를 공유하거나 직접 입력하실 수 있습니다.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return PHONE

async def phone_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    if not contact or not contact.phone_number:
        await update.message.reply_text("전화번호를 다시 확인해 주세요.")
        return PHONE
    context.user_data["phone"] = normalize_phone(contact.phone_number)
    await ask_experience(update, context)
    return EXPERIENCE

async def phone_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "직접 입력하기":
        await update.message.reply_text(
            "전화번호를 입력해 주세요.\n예: 010-1234-5678",
            reply_markup=ReplyKeyboardRemove(),
        )
        return PHONE
    if not phone_is_valid(text):
        await update.message.reply_text(
            "전화번호 형식을 확인해 주세요.\n예: 010-1234-5678",
            reply_markup=ReplyKeyboardRemove(),
        )
        return PHONE
    context.user_data["phone"] = normalize_phone(text)
    await ask_experience(update, context)
    return EXPERIENCE

async def ask_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>투자경력은 어느 정도인가요?</b>\n\n"
        "투자경력은 현재 상황에 맞는 설명과 대응 방향을 함께 살펴보기 위한 참고 정보입니다.\n"
        "더 정확한 안내를 드릴 수 있도록 실제 경험과 가장 가까운 항목을 선택해 주세요."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=inline_options("exp", EXPERIENCE_OPTIONS, 2),
    )

async def experience_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    context.user_data["experience"] = EXPERIENCE_OPTIONS[idx]
    await query.edit_message_reply_markup(reply_markup=None)
    text = (
        "<b>현재 운용 중인 투자금액은 어느 정도인가요?</b>\n\n"
        "정확한 금액을 공개하는 부담을 줄이기 위해 금액 구간으로 확인하고 있습니다.\n"
        "투자 규모에 따라 리스크 관리와 대응 방식이 달라질 수 있으니 실제 상황과 가장 가까운 항목을 선택해 주세요."
    )
    await query.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=inline_options("amt", AMOUNT_OPTIONS, 2),
    )
    return AMOUNT

async def amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    context.user_data["amount"] = AMOUNT_OPTIONS[idx]
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "현재 <b>보유하고 있거나 관심 있게 보고 있는 종목</b>이 있다면 작성해 주세요.\n\n"
        "예: 비트코인, 이더리움, XRP\n"
        "없다면 '없음'이라고 입력해 주세요.",
        parse_mode=ParseMode.HTML,
    )
    return INTERESTS

async def interests_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["interests"] = update.message.text.strip()
    await update.message.reply_text(
        "<b>현재 가장 도움이 필요한 부분</b>을 선택해 주세요.",
        parse_mode=ParseMode.HTML,
        reply_markup=inline_options("help", HELP_OPTIONS, 2),
    )
    return HELP_TYPE

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    context.user_data["help_type"] = HELP_OPTIONS[idx]
    await query.edit_message_reply_markup(reply_markup=None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("참가신청 완료", callback_data="confirm:submit")],
        [InlineKeyboardButton("처음부터 다시 작성", callback_data="confirm:restart")],
    ])
    await query.message.reply_text(
        summary_text(context.user_data),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return CONFIRM

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm:restart":
        context.user_data.clear()
        await query.edit_message_reply_markup(reply_markup=None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("동의하고 참가신청 시작", callback_data="consent:yes")],
            [InlineKeyboardButton("상담사 연결", callback_data="consult:request")],
            [InlineKeyboardButton("동의하지 않습니다", callback_data="consent:no")],
        ])
        await query.message.reply_text(
            privacy_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return CONSENT

    user = query.from_user
    created_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO applications (
                created_at, telegram_user_id, telegram_username, telegram_name,
                name, phone, experience, amount_range, interests, help_type, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
            """,
            (
                created_at,
                user.id,
                user.username,
                user.full_name,
                context.user_data["name"],
                context.user_data["phone"],
                context.user_data["experience"],
                context.user_data["amount"],
                context.user_data.get("interests"),
                context.user_data.get("help_type"),
            ),
        )
        app_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()

    admin_msg = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_application_text(row, "new"),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_status_keyboard(app_id),
    )

    with db() as conn:
        conn.execute(
            "UPDATE applications SET admin_message_id = ? WHERE id = ?",
            (admin_msg.message_id, app_id),
        )
        conn.commit()

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "<b>참가신청이 정상적으로 접수되었습니다.</b>\n\n"
        "작성해주신 내용을 확인한 뒤 순차적으로 안내드리겠습니다.\n\n"
        f"{OPERATING_HOURS_NOTICE}\n\n"
        "신청 후 운영진을 사칭하여 수익·원금 보장, 계정 위임, 과도한 입금을 요구하는 개인 메시지에는 각별히 주의해 주세요.\n\n"
        "감사합니다.",
        parse_mode=ParseMode.HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END

async def admin_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, app_id_str, status = query.data.split(":")
        app_id = int(app_id_str)
    except Exception:
        return
    if status not in STATUS_LABELS:
        return
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        if not row:
            await query.answer("신청 정보를 찾을 수 없습니다.", show_alert=True)
            return
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
        conn.commit()
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    await query.edit_message_text(
        admin_application_text(row, status),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_status_keyboard(app_id),
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "참가신청이 취소되었습니다.\n다시 신청하시려면 /start 를 입력해 주세요.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("ERROR:", context.error)

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CONSENT: [
                CallbackQueryHandler(consent_callback, pattern=r"^consent:(yes|no)$"),
                CallbackQueryHandler(consultation_request_callback, pattern=r"^consult:request$"),
            ],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_input)],
            PHONE: [
                MessageHandler(filters.CONTACT, phone_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, phone_text),
            ],
            EXPERIENCE: [CallbackQueryHandler(experience_callback, pattern=r"^exp:\d+$")],
            AMOUNT: [CallbackQueryHandler(amount_callback, pattern=r"^amt:\d+$")],
            INTERESTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, interests_input)],
            HELP_TYPE: [CallbackQueryHandler(help_callback, pattern=r"^help:\d+$")],
            CONFIRM: [CallbackQueryHandler(confirm_callback, pattern=r"^confirm:(submit|restart)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_status_callback, pattern=r"^status:\d+:(accepted|consulting|done)$"))
    app.add_error_handler(error_handler)
    print("봇 실행 중... 종료하려면 Ctrl+C")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
