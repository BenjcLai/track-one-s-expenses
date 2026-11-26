import base64
import csv
import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException, Request

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CSV_FILE = "expenses.csv"
TZ_TW = timezone(timedelta(hours=8))

app = FastAPI()


def ensure_csv_exists() -> None:
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["消費日期", "品項", "金額", "分類", "原始訊息", "記帳時間"])


def verify_signature(body_bytes: bytes, signature: str | None) -> bool:
    if not CHANNEL_SECRET:
        return True
    if not signature:
        return False
    mac = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def guess_category(item: str) -> str:
    if re.search(r"早|午|晚|餐|飯|麵|咖啡|茶|飲|早餐|午餐|晚餐|宵夜|點心|零食|超商|飲料", item):
        return "飲食"
    if re.search(r"捷運|公車|高鐵|台鐵|Uber|計程車|加油|停車|票|悠遊卡|交通", item):
        return "交通"
    if re.search(r"藥|醫|診所|牙醫|健檢|醫療", item):
        return "醫療"
    if re.search(r"衣|褲|鞋|包|百貨|網購|蝦皮|購物", item):
        return "購物"
    return "其他"


def parse_date_header(line: str, now_tw: datetime) -> str | None:
    """解析像 11/22、11-22、2026/11/22 這類日期行，回傳 YYYY-MM-DD"""
    s = line.strip()
    m = re.fullmatch(r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})", s)
    if not m:
        return None

    year = int(m.group(1)) if m.group(1) else now_tw.year
    month = int(m.group(2))
    day = int(m.group(3))

    # 若未指定年份且輸入的月份比現在晚很多（例如 12 月在 1 月輸入），視為去年
    if not m.group(1) and month > now_tw.month + 1:
        year -= 1

    try:
        dt = datetime(year, month, day, tzinfo=TZ_TW)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_single_line_item(line: str) -> tuple[str, int | float, str | None] | None:
    """
    解析單行花費，支援：
    - 晚：120
    - 晚: 120 飲食
    - 咖啡 85
    - 85 咖啡 飲食
    回傳 (品項, 金額, 自訂分類或None)
    """
    cleaned_line = line.strip()
    if not cleaned_line:
        return None

    # 優先處理「品項：金額 [分類]」或「品項:金額」格式
    m_colon = re.match(r"^([^:：]+)[:：]\s*([\d,]+(?:\.\d+)?)(?:\s+(.+))?$", cleaned_line)
    if m_colon:
        item = m_colon.group(1).strip()
        amount_val = float(m_colon.group(2).replace(",", ""))
        category = m_colon.group(3).strip() if m_colon.group(3) else None
        amount_disp = int(amount_val) if amount_val.is_integer() else amount_val
        return item or "未命名花費", amount_disp, category

    # 其次處理空白分隔格式：如「午餐 120」或「120 咖啡 飲食」
    parts = cleaned_line.split()
    if len(parts) < 2:
        return None

    amount_val: float | None = None
    amount_idx = -1
    for idx, token in enumerate(parts):
        num_str = token.replace(",", "")
        if re.fullmatch(r"\d+(?:\.\d+)?", num_str):
            amount_val = float(num_str)
            amount_idx = idx
            break

    if amount_val is None:
        return None

    others = [p for i, p in enumerate(parts) if i != amount_idx]
    item = others[0] if len(others) >= 1 else "未命名花費"
    category = others[1] if len(others) >= 2 else None
    amount_disp = int(amount_val) if amount_val.is_integer() else amount_val
    return item, amount_disp, category


def parse_multiline_expenses(text: str, now_tw: datetime) -> tuple[str, list[dict[str, Any]], list[str]]:
    """
    解析多行輸入：
    回傳 (消費日期 YYYY-MM-DD, 有效記帳列表, 無法辨識的行列表)
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    expense_date = now_tw.strftime("%Y-%m-%d")
    records: list[dict[str, Any]] = []
    unparsed: list[str] = []

    for idx, line in enumerate(lines):
        if idx == 0:
            parsed_date = parse_date_header(line, now_tw)
            if parsed_date:
                expense_date = parsed_date
                continue

        parsed = parse_single_line_item(line)
        if not parsed:
            unparsed.append(line)
            continue

        item, amount, custom_cat = parsed
        if amount == 0:
            # 金額為 0 視為未花費，略過不入帳
            continue

        category = custom_cat or guess_category(item)
        records.append({
            "date": expense_date,
            "item": item,
            "amount": amount,
            "category": category,
            "raw_line": line,
        })

    return expense_date, records, unparsed


def append_expenses(records: list[dict[str, Any]], raw_text: str) -> None:
    ensure_csv_exists()
    logged_at = datetime.now(TZ_TW).strftime("%Y-%m-%d %H:%M:%S")
    with open(CSV_FILE, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        for r in records:
            writer.writerow([
                r["date"],
                r["item"],
                r["amount"],
                r["category"],
                raw_text,
                logged_at,
            ])


def build_summary(period: str) -> str:
    ensure_csv_exists()
    now = datetime.now(TZ_TW)
    today_prefix = now.strftime("%Y-%m-%d")
    month_prefix = now.strftime("%Y-%m")

    total = 0.0
    count = 0

    with open(CSV_FILE, mode="r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 優先以「消費日期」統計，若舊資料沒有該欄位則退回「時間」
            date_str = (row.get("消費日期") or row.get("時間") or "").strip()
            amt_str = (row.get("金額") or "0").strip()
            try:
                amt = float(amt_str)
            except ValueError:
                continue

            if period == "今天" and date_str.startswith(today_prefix):
                total += amt
                count += 1
            elif period == "本月" and date_str.startswith(month_prefix):
                total += amt
                count += 1

    total_display = int(total) if total.is_integer() else round(total, 2)
    return f"{period}花費統計\n筆數：{count} 筆\n總金額：{total_display} 元"


def reply_line_message(reply_token: str, text: str) -> None:
    if not CHANNEL_ACCESS_TOKEN:
        print("尚未設定 LINE_CHANNEL_ACCESS_TOKEN，無法回覆：", text)
        return

    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    resp.raise_for_status()


def handle_event(event: dict[str, Any]) -> None:
    if event.get("type") != "message":
        return
    message = event.get("message") or {}
    if message.get("type") != "text":
        return

    user_text = (message.get("text") or "").strip()
    reply_token = event.get("replyToken")
    if not reply_token:
        return

    if user_text in ("今天", "本月"):
        reply_line_message(reply_token, build_summary(user_text))
        return

    now_tw = datetime.now(TZ_TW)
    expense_date, records, unparsed = parse_multiline_expenses(user_text, now_tw)

    if records:
        append_expenses(records, user_text)
        lines_out = [f"已記帳（{expense_date}）✅"]
        subtotal = 0.0
        for r in records:
            subtotal += float(r["amount"])
            lines_out.append(f"• {r['item']}：{r['amount']} 元（{r['category']}）")
        sub_disp = int(subtotal) if subtotal.is_integer() else round(subtotal, 2)
        lines_out.append(f"本批小計：{sub_disp} 元")
        if unparsed:
            lines_out.append(f"未辨識列：{', '.join(unparsed)}")
        reply_line_message(reply_token, "\n".join(lines_out))
        return

    reply_line_message(
        reply_token,
        "沒有找到可入帳的金額喔！\n可傳送：\n11/22\n早：0\n午：0\n晚：120\n或單行「咖啡 85」，輸入「今天」或「本月」可查總額。",
    )


@app.post("/webhook")
async def webhook(
    request: Request,
    x_line_signature: str | None = Header(default=None, alias="X-Line-Signature"),
) -> dict[str, str]:
    body_bytes = await request.body()
    if not verify_signature(body_bytes, x_line_signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = await request.json()
    for event in data.get("events", []):
        handle_event(event)

    return {"status": "ok"}
