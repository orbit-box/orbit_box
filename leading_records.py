"""Administrator-only leading log and XLSX export. Does not message public groups."""
import io
import os
import sqlite3
import asyncio
import json
import urllib.request
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters

KST = ZoneInfo("Asia/Seoul")
TRADE_SYMBOL, TRADE_ENTRY, TRADE_T1, TRADE_T2, TRADE_T3, TRADE_RESULT, TRADE_STOP = range(30, 37)
_MARKET_CACHE = {"at": 0, "data": []}


def connection():
    path = os.getenv("DB_PATH", "applications.db").strip()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def setup():
    with connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leading_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                entry_price REAL NOT NULL CHECK(entry_price > 0),
                target1 REAL NOT NULL,
                target2 REAL NOT NULL,
                target3 REAL NOT NULL,
                result_pct REAL,
                result_type TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                closed_at TEXT,
                creator_id INTEGER NOT NULL
            )
        """)
        fields = {x["name"] for x in conn.execute("PRAGMA table_info(leading_records)").fetchall()}
        if "coin_name" not in fields:
            conn.execute("ALTER TABLE leading_records ADD COLUMN coin_name TEXT")
        if "stop_price" not in fields:
            conn.execute("ALTER TABLE leading_records ADD COLUMN stop_price REAL")
        conn.commit()


def fmt(v):
    return f"{v:,.8f}".rstrip("0").rstrip(".") if v is not None else "미설정"


def coin_label(r):
    name = r["coin_name"] if r["coin_name"] else r["symbol"]
    return f"{name} ({r['symbol']})" if name != r["symbol"] else name


def detail_text(r):
    p = r["entry_price"]
    lines = [f"📌 <b>#{r['id']} {escape(coin_label(r))}</b>",
             f"💰 진입가: <b>{fmt(p)}원</b>"]
    for i in range(1, 4):
        pct = r[f"target{i}"]
        lines.append(f"🎯 {i}차 목표: {fmt(p*(1+pct/100))}원  <b>(+{fmt(pct)}%)</b>")
    stop = r["stop_price"]
    lines.append(f"🛑 손절가: {fmt(stop)}원" + (f"  <b>({(stop/p-1)*100:+.2f}%)</b>" if stop else ""))
    lines.append("📍 상태: 진행 중" if r["result_type"] == "open" else f"📍 최종 수익률: {r['result_pct']:+.2f}%")
    return "\n".join(lines)


def allowed(update, admin_id):
    return update.effective_chat is not None and update.effective_chat.id == admin_id


def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 리딩 등록", callback_data="lead:add")],
        [InlineKeyboardButton("📝 수익·손절 기록", callback_data="lead:close")],
        [InlineKeyboardButton("📥 리딩 기록 불러오기", callback_data="lead:exports")],
        [InlineKeyboardButton("📊 진행 중인 리딩", callback_data="lead:open")],
    ])


async def start_admin(update, context):
    await update.message.reply_text("📊 투자자 베이스캠프 | 관리자 리딩 관리\n\n리딩 등록, 결과 기록, 엑셀 내보내기를 선택하세요.", reply_markup=menu())



def fetch_upbit_markets():
    if _MARKET_CACHE["data"] and time.monotonic() - _MARKET_CACHE["at"] < 3600:
        return _MARKET_CACHE["data"]
    req = urllib.request.Request("https://api.upbit.com/v1/market/all?is_details=false", headers={"User-Agent": "BasecampBot/1.0"})
    with urllib.request.urlopen(req, timeout=6) as res:
        data = json.load(res)
    markets = [v for v in data if v.get("market", "").startswith("KRW-")]
    _MARKET_CACHE.update({"at": time.monotonic(), "data": markets})
    return markets


async def search_markets(term):
    try:
        markets = await asyncio.to_thread(fetch_upbit_markets)
    except Exception:
        return None
    t = term.casefold().strip()
    return [v for v in markets if t in v.get("market","").casefold() or t in v.get("korean_name","").casefold() or t in v.get("english_name","").casefold()][:10]


async def pick_symbol(update, context):
    q = update.callback_query
    await q.answer()
    form = context.user_data.get("leading_form")
    if not form:
        await q.message.reply_text("입력 시간이 종료되었습니다. 리딩 등록부터 다시 시작해 주세요.")
        return ConversationHandler.END
    try:
        choice = form.get("search_results", [])[int(q.data.rsplit(":", 1)[-1])]
    except (ValueError, IndexError, TypeError):
        await q.message.reply_text("검색이 만료되었습니다. 다시 검색해 주세요.")
        return TRADE_SYMBOL
    await q.edit_message_reply_markup(reply_markup=None)
    symbol = choice["market"].split("-")[1]
    form["symbol"] = symbol
    form["coin_name"] = choice.get("korean_name") or symbol
    await q.message.reply_text(f"✅ {form['coin_name']} ({symbol}) 선택 완료\n진입가(원)를 숫자로 입력하세요. 예: 2000")
    return TRADE_ENTRY

async def begin_add(update, context):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    context.user_data["leading_form"] = {}
    await q.message.reply_text("🔎 종목 검색 (업비트 KRW)\n\n한글 종목명 또는 티커를 입력하세요.\n예: 리플 / XRP / 비트코인\n검색 결과에서 종목을 선택할 수 있습니다.\n취소: /cancel")
    return TRADE_SYMBOL


async def receive_symbol(update, context):
    term = update.message.text.strip()
    if not term or len(term) > 40:
        await update.message.reply_text("종목명 또는 티커를 40자 이내로 입력해 주세요.")
        return TRADE_SYMBOL
    results = await search_markets(term)
    if results is None:
        await update.message.reply_text("⚠️ 업비트 검색 서버 연결에 실패했습니다. 잠시 후 다시 검색해 주세요.")
        return TRADE_SYMBOL
    if not results:
        await update.message.reply_text("검색 결과가 없습니다. 업비트 원화마켓의 한글명 또는 티커로 다시 검색해 주세요.")
        return TRADE_SYMBOL
    context.user_data["leading_form"]["search_results"] = results
    buttons = [[InlineKeyboardButton(f"{x['korean_name']} ({x['market'].split('-')[1]})", callback_data=f"lead:pick:{i}")] for i,x in enumerate(results)]
    await update.message.reply_text("🔎 검색 결과 (업비트 KRW)\n종목을 선택하세요.", reply_markup=InlineKeyboardMarkup(buttons))
    return TRADE_SYMBOL


def parse_positive(t):
    v = float(t.replace(",", "").replace("%", "").strip())
    if not 0 < v < 1e15:
        raise ValueError()
    return v


async def receive_entry(update, context):
    try:
        p = parse_positive(update.message.text)
    except (ValueError, OverflowError):
        await update.message.reply_text("0보다 큰 진입가를 숫자로 입력해 주세요.")
        return TRADE_ENTRY
    context.user_data["leading_form"]["entry_price"] = p
    await update.message.reply_text("1차 목표 수익률(%)을 입력하세요. 예: 5")
    return TRADE_T1


async def goal_step(update, context, key, current, next_state, next_message):
    try:
        v = parse_positive(update.message.text)
        if v > 100000:
            raise ValueError()
        prev = context.user_data["leading_form"].get("target" + str(int(key[-1])-1))
        if prev is not None and v <= prev:
            raise ValueError()
    except (ValueError, OverflowError):
        await update.message.reply_text("이전 목표보다 높은 양수 수익률(%)을 입력해 주세요.")
        return current
    context.user_data["leading_form"][key] = v
    await update.message.reply_text(next_message)
    return next_state


async def goal1(update, context):
    return await goal_step(update, context, "target1", TRADE_T1, TRADE_T2, "2차 목표 수익률(%)을 입력하세요. 예: 10")


async def goal2(update, context):
    return await goal_step(update, context, "target2", TRADE_T2, TRADE_T3, "3차 목표 수익률(%)을 입력하세요. 예: 15")


async def goal3(update, context):
    try:
        v = parse_positive(update.message.text)
        if v <= context.user_data["leading_form"]["target2"] or v > 100000:
            raise ValueError()
    except (ValueError, OverflowError):
        await update.message.reply_text("2차 목표보다 높은 수익률(%)을 입력해 주세요.")
        return TRADE_T3
    context.user_data["leading_form"]["target3"] = v
    await update.message.reply_text("🛑 손절가(원)를 입력하세요.\n예: 1850\n진입가보다 낮은 가격을 입력해 주세요.")
    return TRADE_STOP


async def receive_stop(update, context):
    d = context.user_data["leading_form"]
    try:
        stop = parse_positive(update.message.text)
        if stop >= d["entry_price"]:
            raise ValueError()
    except (ValueError, OverflowError):
        await update.message.reply_text("손절가는 진입가보다 낮은 양수 가격으로 입력해 주세요.")
        return TRADE_STOP
    d["stop_price"] = stop
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with connection() as conn:
        cursor = conn.execute(
            """INSERT INTO leading_records
            (symbol,coin_name,entry_price,target1,target2,target3,stop_price,created_at,creator_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d["symbol"],d["coin_name"],d["entry_price"],d["target1"],d["target2"],d["target3"],stop,now,update.effective_user.id))
        record_id = cursor.lastrowid
        conn.commit()
        record = conn.execute("SELECT * FROM leading_records WHERE id=?", (record_id,)).fetchone()
    context.user_data.pop("leading_form", None)
    await update.message.reply_text("✅ 리딩 등록 완료\n\n" + detail_text(record) + "\n\n※ 목표별 예상 수익률은 진입가 대비 가격 상승률이며 수수료는 제외합니다.", parse_mode="HTML", reply_markup=menu())
    return ConversationHandler.END


async def record_result(update, context):
    q = update.callback_query
    await q.answer()
    with connection() as conn:
        rows = conn.execute("SELECT id,symbol,entry_price FROM leading_records WHERE result_type='open' ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        await q.message.reply_text("현재 결과를 입력할 진행 중인 리딩이 없습니다.", reply_markup=menu())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(f"#{r['id']} {r['symbol']} · {r['entry_price']:,.8g}", callback_data=f"lead:choose:{r['id']}")] for r in rows]
    await q.message.reply_text("결과를 기록할 리딩을 선택하세요.", reply_markup=InlineKeyboardMarkup(buttons))
    return TRADE_RESULT


async def result_choose(update, context):
    q = update.callback_query
    await q.answer()
    rid = int(q.data.split(":")[-1])
    with connection() as conn:
        record = conn.execute("SELECT id,symbol FROM leading_records WHERE id=? AND result_type='open'", (rid,)).fetchone()
    if not record:
        await q.message.reply_text("이미 종료됐거나 찾을 수 없는 리딩입니다.")
        return ConversationHandler.END
    context.user_data["leading_close_id"] = rid
    await q.message.reply_text(f"#{rid} {record['symbol']} 최종 실현 수익률(%) 입력\n수익 예: 7.5\n손절 예: -3\n본전: 0\n취소: /cancel")
    return TRADE_RESULT


async def result_input(update, context):
    rid = context.user_data.get("leading_close_id")
    if not rid:
        await update.message.reply_text("종목을 먼저 선택해 주세요.", reply_markup=menu())
        return ConversationHandler.END
    try:
        pct = float(update.message.text.strip().replace("%", "").replace(",", ""))
        if not -100 <= pct <= 100000:
            raise ValueError()
    except (ValueError, OverflowError):
        await update.message.reply_text("수익률을 숫자로 입력하세요. 수익은 양수, 손절은 음수입니다.")
        return TRADE_RESULT
    kind = "profit" if pct > 0 else "loss" if pct < 0 else "breakeven"
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with connection() as conn:
        changed = conn.execute(
            """UPDATE leading_records SET result_pct=?, result_type=?, closed_at=?
            WHERE id=? AND result_type='open'""", (pct, kind, now, rid)
        ).rowcount
        conn.commit()
    context.user_data.pop("leading_close_id", None)
    await update.message.reply_text(
        f"{'✅' if changed else '⚠️'} #{rid} {'최종 결과 저장' if changed else '이미 종료된 리딩'}\n최종 수익률: {pct:+.2f}%",
        reply_markup=menu(),
    )
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.pop("leading_form", None)
    context.user_data.pop("leading_close_id", None)
    await update.message.reply_text("리딩 입력을 취소했습니다.", reply_markup=menu())
    return ConversationHandler.END


async def callbacks(update, context):
    q = update.callback_query
    await q.answer()
    action = q.data
    if action == "lead:exports":
        await q.message.reply_text("📥 리딩 기록 불러오기\n엑셀 다운로드 범위를 선택하세요.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 수익만", callback_data="lead:xlsx:profit"),
             InlineKeyboardButton("📉 손실만", callback_data="lead:xlsx:loss")],
            [InlineKeyboardButton("📋 전체", callback_data="lead:xlsx:all")],
        ]))
    elif action == "lead:open":
        with connection() as conn:
            rows = conn.execute("SELECT * FROM leading_records WHERE result_type='open' ORDER BY id DESC LIMIT 20").fetchall()
            count = conn.execute("SELECT COUNT(*) FROM leading_records WHERE result_type='open'").fetchone()[0]
        if not rows:
            await q.message.reply_text("📊 진행 중인 리딩이 없습니다.")
            return
        await q.message.reply_text(f"📊 <b>진행 중인 리딩</b> · 총 {count}건\n종목별 목표가와 예상 수익률입니다.", parse_mode="HTML")
        for record in rows:
            await q.message.reply_text(detail_text(record), parse_mode="HTML")
        if count > len(rows):
            await q.message.reply_text(f"최근 {len(rows)}건 표시 중입니다. 전체 기록은 엑셀로 확인할 수 있습니다.")
    elif action.startswith("lead:xlsx:"):
        kind = action.rsplit(":",1)[-1]
        if kind not in {"profit", "loss", "all"}:
            return
        sql = "SELECT * FROM leading_records"
        if kind == "profit":
            sql += " WHERE result_type='profit'"
        elif kind == "loss":
            sql += " WHERE result_type='loss'"
        sql += " ORDER BY id DESC"
        with connection() as conn:
            rows = conn.execute(sql).fetchall()
        wb = Workbook()
        ws = wb.active
        ws.title = {"profit":"수익 기록","loss":"손실 기록","all":"전체 리딩"}[kind]
        ws.append(["기록번호", "종목명", "티커", "진입가", "최종수익률(%)", "결과", "1차 목표가", "1차 예상수익률(%)", "2차 목표가", "2차 예상수익률(%)", "3차 목표가", "3차 예상수익률(%)", "손절가", "손절 예상수익률(%)", "등록일시(KST)", "종료일시(KST)"])
        labels = {"profit":"수익", "loss":"손실", "breakeven":"본전", "open":"진행중"}
        for r in rows:
            ws.append([r["id"],r["coin_name"] or r["symbol"],r["symbol"],r["entry_price"],r["result_pct"],labels[r["result_type"]],*[v for i in range(1,4) for v in (r["entry_price"]*(1+r[f"target{i}"]/100),r[f"target{i}"])],r["stop_price"],((r["stop_price"]/r["entry_price"]-1)*100 if r["stop_price"] else None),r["created_at"],r["closed_at"]])
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="183153")
            cell.alignment = Alignment(horizontal="center")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col, width in {"A":12,"B":19,"C":15,"D":19,"E":19,"F":13,"G":18,"H":21,"I":18,"J":21,"K":18,"L":21,"M":18,"N":22,"O":24,"P":24}.items():
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows(min_row=2):
            for idx in [4,7,9,11,13]:
                row[idx].number_format = '0.00"%"'
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        day = datetime.now(KST).strftime("%Y%m%d_%H%M")
        output.name = f"leading_{kind}_{day}.xlsx"
        await q.message.reply_document(document=output, filename=output.name, caption=f"📥 {ws.title}: {len(rows)}건\n※ 미종료 리딩은 전체 파일에만 포함됩니다.")


def install(app, admin_id):
    setup()
    admin_chat = filters.Chat(admin_id)
    app.add_handler(CommandHandler("start", start_admin, filters=admin_chat))
    add = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_add, pattern=r"^lead:add$")],
        states={
            TRADE_SYMBOL:[CallbackQueryHandler(pick_symbol, pattern=r"^lead:pick:\d+$"), MessageHandler(admin_chat & filters.TEXT & ~filters.COMMAND, receive_symbol)],
            TRADE_ENTRY:[MessageHandler(admin_chat & filters.TEXT & ~filters.COMMAND, receive_entry)],
            TRADE_T1:[MessageHandler(admin_chat & filters.TEXT & ~filters.COMMAND, goal1)],
            TRADE_T2:[MessageHandler(admin_chat & filters.TEXT & ~filters.COMMAND, goal2)],
            TRADE_T3:[MessageHandler(admin_chat & filters.TEXT & ~filters.COMMAND, goal3)],
            TRADE_STOP:[MessageHandler(admin_chat & filters.TEXT & ~filters.COMMAND, receive_stop)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True, per_user=True,
    )
    close = ConversationHandler(
        entry_points=[CallbackQueryHandler(record_result, pattern=r"^lead:close$")],
        states={TRADE_RESULT:[
            CallbackQueryHandler(result_choose, pattern=r"^lead:choose:\d+$"),
            MessageHandler(admin_chat & filters.TEXT & ~filters.COMMAND, result_input),
        ]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True, per_user=True,
    )
    app.add_handler(add)
    app.add_handler(close)
    app.add_handler(CallbackQueryHandler(callbacks, pattern=r"^lead:(exports|open|xlsx:(profit|loss|all))$"))
