"""Administrator-only leading log and XLSX export. Does not message public groups."""
import io
import os
import sqlite3
import asyncio
import json
import urllib.request
import time
import logging
from urllib.parse import urlencode
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
        fields = {x["name"] for x in conn.execute("PRAGMA table_info(leading_records)").fetchall()}
        if "target_mode" not in fields:
            conn.execute("ALTER TABLE leading_records ADD COLUMN target_mode TEXT NOT NULL DEFAULT 'pct'")
        conn.execute("""CREATE TABLE IF NOT EXISTS leading_alerts (
            record_id INTEGER NOT NULL, milestone TEXT NOT NULL,
            notified_at TEXT NOT NULL, observed_price REAL NOT NULL,
            PRIMARY KEY(record_id,milestone)
        )""")
        cols={x["name"] for x in conn.execute("PRAGMA table_info(leading_records)")}
        if "monitor_active" not in cols:
            conn.execute("ALTER TABLE leading_records ADD COLUMN monitor_active INTEGER NOT NULL DEFAULT 1")
            conn.execute("UPDATE leading_records SET monitor_active=0 WHERE result_type!='open'")
        alert_cols={x["name"] for x in conn.execute("PRAGMA table_info(leading_alerts)")}
        if "delivery_status" not in alert_cols:
            conn.execute("ALTER TABLE leading_alerts ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'sent'")
        conn.execute("""CREATE TABLE IF NOT EXISTS leading_monitor_health (id INTEGER PRIMARY KEY CHECK(id=1), failure_count INTEGER DEFAULT 0, notice_at TEXT)""")
        conn.execute("INSERT OR IGNORE INTO leading_monitor_health(id,failure_count) VALUES(1,0)")
        conn.commit()


def pct(entry, price):
    return (price / entry - 1) * 100


def target_price(r, n):
    v = r[f"target{n}"]
    return v if r["target_mode"] == "price" else r["entry_price"] * (1+v/100)


def price_number(v):
    return f"{v:,.8f}".rstrip("0").rstrip(".") if v is not None else "미설정"


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
        target = target_price(r, i)
        lines.append(f"🎯 {i}차: <b>{fmt(target)}원</b>  ({pct(p,target):+.2f}%)")
    stop = r["stop_price"]
    lines.append(f"🛑 손절가: {fmt(stop)}원" + (f"  <b>({(stop/p-1)*100:+.2f}%)</b>" if stop else ""))
    lines.append("⚠️ 과거 수익률 기준 기록" if r["target_mode"] != "price" else "📡 업비트 KRW 시세 감시 중")
    lines.append("📍 상태: 진행 중" if r["result_type"] == "open" else ("📍 상태: 감시 종료" if r["result_type"] == "stopped" else f"📍 최종 수익률: {r['result_pct']:+.2f}%"))
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
    await update.message.reply_text("🎯 1차 목표가(원)를 입력하세요. 예: 2100\n진입가보다 높은 가격을 입력하세요.")
    return TRADE_T1


async def goal_step(update, context, index):
    d = context.user_data["leading_form"]
    try:
        value = parse_positive(update.message.text)
        prev = d["entry_price"] if index == 1 else d[f"target{index-1}"]
        if value <= prev:
            raise ValueError()
    except (ValueError, OverflowError):
        await update.message.reply_text("이전 목표가보다 높은 원화 목표 가격을 입력해 주세요.")
        return [TRADE_T1, TRADE_T2, TRADE_T3][index-1]
    d[f"target{index}"] = value
    ratio = pct(d["entry_price"], value)
    if index == 3:
        await update.message.reply_text(f"✅ 3차 목표: {fmt(value)}원 ({ratio:+.2f}%)\n\n🛑 손절가(원)를 입력하세요. 진입가보다 낮아야 합니다.")
        return TRADE_STOP
    await update.message.reply_text(f"✅ {index}차 목표: {fmt(value)}원 ({ratio:+.2f}%)\n\n🎯 {index+1}차 목표가(원)를 입력하세요.")
    return [TRADE_T2, TRADE_T3][index-1]


async def goal1(update, context):
    return await goal_step(update, context, 1)


async def goal2(update, context):
    return await goal_step(update, context, 2)


async def goal3(update, context):
    return await goal_step(update, context, 3)


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
            (symbol,coin_name,entry_price,target1,target2,target3,stop_price,created_at,creator_id,target_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d["symbol"],d["coin_name"],d["entry_price"],d["target1"],d["target2"],d["target3"],stop,now,update.effective_user.id,"price"))
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
        rows = conn.execute("SELECT id,symbol,entry_price FROM leading_records WHERE result_type IN ('open','stopped') ORDER BY id DESC LIMIT 30").fetchall()
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
        record = conn.execute("SELECT id,symbol FROM leading_records WHERE id=? AND result_type IN ('open','stopped')", (rid,)).fetchone()
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
            """UPDATE leading_records SET result_pct=?, result_type=?, closed_at=?, monitor_active=0
            WHERE id=? AND result_type IN ('open','stopped')""", (pct, kind, now, rid)
        ).rowcount
        conn.commit()
        verified = conn.execute("SELECT result_pct,result_type FROM leading_records WHERE id=?", (rid,)).fetchone()
    if not changed or not verified or verified["result_type"] != kind or verified["result_pct"] != pct:
        await update.message.reply_text("⚠️ 저장 확인에 실패했습니다. 다시 시도해 주세요.")
        return TRADE_RESULT
    context.user_data.pop("leading_close_id", None)
    await update.message.reply_text(
        f"{'✅' if changed else '⚠️'} #{rid} {'최종 결과 저장' if changed else '이미 종료된 리딩'}\n최종 수익률: {pct:+.2f}%",
        reply_markup=menu(),
    )
    await update.message.reply_text("📥 최종 결과가 저장되었습니다. /start → 리딩 기록 불러오기에서 수익만·손실만·전체로 확인할 수 있습니다.")
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
            [InlineKeyboardButton("🎯 목표 도달 기록", callback_data="lead:xlsx:milestones")],
        ]))
    elif action == "lead:open":
        with connection() as conn:
            rows = conn.execute("SELECT * FROM leading_records WHERE result_type='open' AND monitor_active=1 ORDER BY id DESC LIMIT 20").fetchall()
            count = conn.execute("SELECT COUNT(*) FROM leading_records WHERE result_type='open' AND monitor_active=1").fetchone()[0]
        if not rows:
            await q.message.reply_text("📊 진행 중인 리딩이 없습니다.")
            return
        await q.message.reply_text(f"📊 <b>진행 중인 리딩</b> · 총 {count}건\n종목별 목표가와 예상 수익률입니다.", parse_mode="HTML")
        for record in rows:
            await q.message.reply_text(detail_text(record), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹ 리딩 감시 종료", callback_data=f"lead:stopask:{record['id']}")]]))
        if count > len(rows):
            await q.message.reply_text(f"최근 {len(rows)}건 표시 중입니다. 전체 기록은 엑셀로 확인할 수 있습니다.")
    elif action.startswith("lead:xlsx:"):
        kind = action.rsplit(":",1)[-1]
        if kind not in {"profit", "loss", "all", "milestones"}:
            return
        if kind == "milestones":
            with connection() as conn:
                hits=conn.execute("""SELECT a.record_id,r.coin_name,r.symbol,r.entry_price,
                    a.milestone,a.observed_price,a.notified_at,r.result_pct
                    FROM leading_alerts a JOIN leading_records r ON r.id=a.record_id
                    WHERE a.milestone LIKE 'target%' AND a.delivery_status='sent'
                    ORDER BY a.notified_at DESC""").fetchall()
            wb=Workbook()
            ws=wb.active
            ws.title="목표 도달 내역"
            ws.append(["종목명","진입가(원)","도달 목표","도달 시세(원)","도달 기준 상승률(%)","도달일시(KST)","최종 확정 수익률(%)"])
            for h in hits:
                ws.append([f"{h['coin_name'] or h['symbol']} ({h['symbol']})",h["entry_price"],h["milestone"].replace("target","")+"차",h["observed_price"],pct(h["entry_price"],h["observed_price"]),h["notified_at"],h["result_pct"]])
            for cell in ws[1]:
                cell.font=Font(bold=True,color="FFFFFF")
                cell.fill=PatternFill("solid",fgColor="193553")
            for col,width in {"A":27,"B":18,"C":15,"D":20,"E":25,"F":25,"G":24}.items():
                ws.column_dimensions[col].width=width
            ws.freeze_panes="A2"
            ws.auto_filter.ref=ws.dimensions
            data=io.BytesIO()
            wb.save(data)
            data.seek(0)
            filename=f"leading_milestones_{datetime.now(KST).strftime('%Y%m%d_%H%M')}.xlsx"
            await q.message.reply_document(document=data,filename=filename,caption=f"🎯 목표 도달 기록: {len(hits)}건\n※ 목표 도달은 실제 실현 수익이 아닙니다.")
            return
        sql = "SELECT * FROM leading_records"
        if kind == "profit":
            sql += " WHERE result_type='profit'"
        elif kind == "loss":
            sql += " WHERE result_type='loss'"
        sql += " ORDER BY id DESC"
        with connection() as conn:
            rows = conn.execute(sql).fetchall()
        await q.message.reply_text(f"📊 조회 결과: {len(rows)}건")
        wb = Workbook()
        ws = wb.active
        ws.title = "📊 공유용 요약"
        ws.sheet_view.showGridLines = False
        ws.merge_cells("A1:D1")
        ws["A1"] = "리딩 성과 기록"
        ws["A1"].font = Font(size=18, bold=True, color="FFFFFF")
        ws["A1"].fill = PatternFill("solid", fgColor="132C4A")
        ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 40
        ws.merge_cells("A2:D2")
        ws["A2"] = "기준: 업비트 KRW | 최종 기록 수익률은 입력된 값이며 실제 개인 수익률과 다를 수 있습니다."
        ws["A2"].font = Font(size=9, color="64748B")
        ws["A2"].alignment = Alignment(horizontal="center")
        ws.row_dimensions[2].height = 25
        ws.append(["종목명", "진입가 (원)", "최종 수익률", "결과"])
        header = 3
        labels = {"profit":"수익", "loss":"손실", "breakeven":"본전", "open":"진행중", "stopped":"감시종료"}
        for cell in ws[header]:
            cell.fill = PatternFill("solid", fgColor="193553")
            cell.font = Font(bold=True, color="FFFFFF", size=11)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[header].height = 30
        for idx, r in enumerate(rows, 4):
            ws.append([f"{r['coin_name'] or r['symbol']} ({r['symbol']})", r["entry_price"], r["result_pct"], labels[r["result_type"]]])
            ws.row_dimensions[idx].height = 29
            shade = "FFFFFF" if idx % 2 == 0 else "EFF4F9"
            for cell in ws[idx]:
                cell.fill = PatternFill("solid", fgColor=shade)
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.font = Font(size=11, color="243447")
            ws.cell(idx,2).number_format = '#,##0.########'
            ws.cell(idx,3).number_format = '+0.00"%;";-0.00"%";0.00"%"'
            if r["result_type"] == "profit":
                ws.cell(idx,3).font = Font(bold=True,color="00835A")
            elif r["result_type"] == "loss":
                ws.cell(idx,3).font = Font(bold=True,color="C83C48")
        for letter, width in {"A":27,"B":21,"C":22,"D":17}.items():
            ws.column_dimensions[letter].width = width
        ws.freeze_panes = "A4"
        ws.print_options.horizontalCentered = True
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.print_area = f"A1:D{max(3,ws.max_row)}"

        detail = wb.create_sheet("상세 기록")
        detail.append(["번호","종목명","티커","진입가","최종수익률(%)","결과","1차목표가","1차 예상(%)","2차목표가","2차 예상(%)","3차목표가","3차 예상(%)","손절가","손절 예상(%)","등록(KST)","종료(KST)"])
        for r in rows:
            detail.append([r["id"],r["coin_name"] or r["symbol"],r["symbol"],r["entry_price"],r["result_pct"],labels[r["result_type"]],*[v for i in range(1,4) for v in (target_price(r,i),pct(r["entry_price"],target_price(r,i)))],r["stop_price"],((r["stop_price"]/r["entry_price"]-1)*100 if r["stop_price"] else None),r["created_at"],r["closed_at"]])
        for cell in detail[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="193553")
            cell.alignment = Alignment(horizontal="center")
        detail.freeze_panes = "D2"
        detail.auto_filter.ref = detail.dimensions
        for col, width in {"A":10,"B":19,"C":14,"D":18,"E":20,"F":13,"G":17,"H":17,"I":17,"J":17,"K":17,"L":17,"M":17,"N":17,"O":23,"P":23}.items():
            detail.column_dimensions[col].width = width
        for row in detail.iter_rows(min_row=2):
            for index in [4,7,9,11,13]:
                row[index].number_format = '0.00"%"'
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        day = datetime.now(KST).strftime("%Y%m%d_%H%M")
        output.name = f"leading_{kind}_{day}.xlsx"
        await q.message.reply_document(document=output, filename=output.name, caption=f"📥 리딩 기록 {len(rows)}건\n첫 번째 시트는 공유용 요약, 두 번째 시트는 관리자 상세 기록입니다.")



def get_prices(markets):
    values = {}
    items = sorted(markets)
    for offset in range(0, len(items), 70):
        query = urlencode({"markets": ",".join(items[offset:offset+70])})
        req = urllib.request.Request("https://api.upbit.com/v1/ticker?" + query, headers={"User-Agent": "BasecampBot/1.0", "Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.load(resp)
        for item in rows:
            if item.get("market") and item.get("trade_price") is not None:
                values[item["market"]] = float(item["trade_price"])
    return values


async def monitor_prices(app, admin_id):
    # Sequential cycle; successful messages are idempotently recorded in SQLite.
    while True:
        try:
            with connection() as conn:
                records = conn.execute("SELECT * FROM leading_records WHERE result_type='open' AND monitor_active=1 AND target_mode='price'").fetchall()
            if records:
                prices = await asyncio.to_thread(get_prices, {"KRW-" + r["symbol"] for r in records})
                missing = sorted({r["symbol"] for r in records if "KRW-" + r["symbol"] not in prices})
                if missing:
                    raise RuntimeError("업비트 조회 누락: " + ", ".join(missing[:10]))
                for r in records:
                    observed = prices.get("KRW-" + r["symbol"])
                    if observed is None:
                        continue
                    levels = [(f"target{i}", target_price(r,i), observed >= target_price(r,i)) for i in range(1,4)]
                    if r["stop_price"] is not None:
                        levels.append(("stop", r["stop_price"], observed <= r["stop_price"]))
                    for key, level, hit in levels:
                        if not hit:
                            continue
                        with connection() as conn:
                            already = conn.execute("SELECT 1 FROM leading_alerts WHERE record_id=? AND milestone=?", (r["id"],key)).fetchone()
                        if already:
                            continue
                        desc = f"🎯 {key[-1]}차 목표 도달" if key != "stop" else "🛑 손절가 도달"
                        message = (f"🔔 <b>리딩 가격 도달 알림</b>\n\n"
                                   f"📌 <b>#{r['id']} {escape(coin_label(r))}</b>\n"
                                   f"📍 {desc}\n"
                                   f"진입가: {fmt(r['entry_price'])}원\n"
                                   f"설정 가격: {fmt(level)}원 ({pct(r['entry_price'],level):+.2f}%)\n"
                                   f"조회 현재가: {fmt(observed)}원 ({pct(r['entry_price'],observed):+.2f}%)\n\n"
                                   "📢 관리자 확인 및 안내가 필요합니다.\n"
                                   "※ 업비트 KRW 조회 시세 기준, 체결 수익률이 아닙니다.")
                        with connection() as conn:
                            still_open = conn.execute("SELECT 1 FROM leading_records WHERE id=? AND result_type='open' AND monitor_active=1", (r["id"],)).fetchone()
                        if not still_open:
                            break
                        # Reserve alert milestone before network I/O; UNIQUE(record_id,milestone) prevents duplicate sends.
                        with connection() as conn:
                            inserted=conn.execute("""INSERT OR IGNORE INTO leading_alerts
                                (record_id,milestone,notified_at,observed_price,delivery_status)
                                VALUES(?,?,?,?, 'pending')""",
                                (r["id"],key,datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),observed)).rowcount
                            conn.commit()
                        if not inserted:
                            continue
                        try:
                            await app.bot.send_message(chat_id=admin_id,text=message,parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔔 알림 유지", callback_data=f"lead:keep:{r['id']}"),
                            InlineKeyboardButton("⏹ 알림 종료", callback_data=f"lead:stopask:{r['id']}")
                        ]]))
                        except Exception:
                            # If Telegram times out after accepting the message, retry may duplicate.
                            # Keep pending for manual inspection instead of blind resend.
                            logging.getLogger(__name__).exception("Leading alert delivery uncertain %s/%s",r["id"],key)
                            raise
                        with connection() as conn:
                            conn.execute("UPDATE leading_alerts SET delivery_status='sent' WHERE record_id=? AND milestone=?",(r["id"],key))
                            conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.getLogger(__name__).exception("Leading price monitor failed: %s",error)
            now=datetime.now(KST)
            with connection() as conn:
                conn.execute("UPDATE leading_monitor_health SET failure_count=failure_count+1 WHERE id=1")
                health=conn.execute("SELECT failure_count,notice_at FROM leading_monitor_health WHERE id=1").fetchone()
                conn.commit()
            previous=datetime.strptime(health["notice_at"],"%Y-%m-%d %H:%M:%S").replace(tzinfo=KST) if health["notice_at"] else None
            if health["failure_count"]>=3 and (previous is None or (now-previous).total_seconds()>1800):
                try:
                    await app.bot.send_message(admin_id,"⚠️ 업비트 시세 감시 오류: 3회 이상 연속 조회/알림에 실패했습니다. 운영자가 확인해 주세요.")
                    with connection() as conn:
                        conn.execute("UPDATE leading_monitor_health SET notice_at=? WHERE id=1",(now.strftime("%Y-%m-%d %H:%M:%S"),))
                        conn.commit()
                except Exception:
                    logging.getLogger(__name__).exception("Monitor health notification failed")
        else:
            with connection() as conn:
                conn.execute("UPDATE leading_monitor_health SET failure_count=0 WHERE id=1")
                conn.commit()
        await asyncio.sleep(3)


async def start_monitor(app, admin_id):
    app.create_task(monitor_prices(app,admin_id))


async def management_buttons(update, context):
    q = update.callback_query
    await q.answer()
    kind, record_id = q.data.split(":")[1:3]
    record_id = int(record_id)
    with connection() as conn:
        row = conn.execute("SELECT * FROM leading_records WHERE id=?", (record_id,)).fetchone()
    if not row:
        await q.answer("기록을 찾을 수 없습니다.", show_alert=True)
        return
    if kind == "keep":
        if row["result_type"] == "open" and row["monitor_active"] == 1:
            await q.message.reply_text(f"🔔 #{record_id} {coin_label(row)}\n알림을 유지합니다. 남은 목표가 및 손절가를 계속 감시합니다.")
        else:
            await q.message.reply_text("이미 종료된 리딩입니다. 기존 기록은 유지됩니다.")
    elif kind == "stopask":
        if row["result_type"] != "open" or row["monitor_active"] != 1:
            await q.message.reply_text("이미 종료된 리딩입니다.")
            return
        await q.message.reply_text(
            f"⏹ #{record_id} {coin_label(row)}\n정말 리딩 감시를 종료하시겠습니까?\n\n종료하면 이후 목표가·손절가 알림은 발송되지 않습니다. 엑셀 기록은 보존됩니다.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 감시 종료 확정", callback_data=f"lead:stopconfirm:{record_id}"),
                InlineKeyboardButton("↩️ 취소", callback_data=f"lead:stopcancel:{record_id}")
            ]])
        )
    elif kind == "stopcancel":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"↩️ #{record_id} 감시 종료를 취소했습니다.")
    elif kind == "stopconfirm":
        now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        with connection() as conn:
            updated = conn.execute(
                "UPDATE leading_records SET result_type='stopped', monitor_active=0, closed_at=? WHERE id=? AND result_type='open' AND monitor_active=1",
                (now, record_id)
            ).rowcount
            conn.commit()
        await q.edit_message_reply_markup(reply_markup=None)
        if updated:
            await q.message.reply_text(f"⏹ #{record_id} {coin_label(row)} 리딩 감시를 종료했습니다.\n이후 알림은 중단되며 과거 기록은 엑셀에 남습니다.")
        else:
            await q.message.reply_text("이미 종료된 리딩입니다.")


def install(app, admin_id):
    setup()
    # Runs after PTB initialization during polling startup, without extra scheduler dependencies.
    old_post_init = app.post_init
    async def on_init(application):
        if old_post_init:
            await old_post_init(application)
        await start_monitor(application,admin_id)
    app.post_init = on_init
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
    app.add_handler(CallbackQueryHandler(callbacks, pattern=r"^lead:(exports|open|xlsx:(profit|loss|all|milestones))$"))
    app.add_handler(CallbackQueryHandler(management_buttons, pattern=r"^lead:(keep|stopask|stopconfirm|stopcancel):\d+$"))
