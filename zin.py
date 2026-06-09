from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, QuickReply, QuickReplyItem,
    MessageAction, PostbackAction, PushMessageRequest
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, PostbackEvent
)
from datetime import datetime, timezone, timedelta
import json
import os
import calendar
import requests

# ========== 台灣時區 ==========
def get_taiwan_now():
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)

def get_taiwan_today():
    return get_taiwan_now().replace(hour=0, minute=0, second=0, microsecond=0)

def get_weekday_name(date_str):
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return weekdays[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
# =============================

# ========== LINE 設定 ==========
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
ADMIN_USER_IDS = [uid.strip() for uid in os.environ.get('ADMIN_USER_IDS', '').split(',') if uid.strip()]

# Google Sheets Apps Script 網址
GOOGLE_SHEETS_URL = os.environ.get('GOOGLE_SHEETS_URL', '')

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

SERVICES = {
    "1": {"name": "健康調理", "price": 1500, "duration": 90, "emoji": "💆"},
    "2": {"name": "局部紓壓", "price": 800, "duration": 90, "emoji": "💪"}
}
ITEMS_PER_PAGE = 10

# 記憶體暫存（加速用）
appointments_cache = []
cache_time = 0

def get_all_appointments():
    global appointments_cache, cache_time
    now = get_taiwan_now().timestamp()
    if now - cache_time < 5:
        return appointments_cache
    
    if not GOOGLE_SHEETS_URL:
        return []
    
    try:
        response = requests.get(GOOGLE_SHEETS_URL, timeout=10)
        result = response.json()
        if result.get('success'):
            appointments_cache = result.get('data', [])
            cache_time = now
            return appointments_cache
    except Exception as e:
        print(f"⚠️ 讀取失敗: {e}")
    return []

def add_appointment(data):
    if not GOOGLE_SHEETS_URL:
        return False
    try:
        response = requests.post(GOOGLE_SHEETS_URL, json=data, timeout=10)
        return response.status_code == 200
    except:
        return False

def is_business_day(date_obj):
    return date_obj.weekday() != 4

def get_available_dates(year, month):
    dates = []
    today = get_taiwan_today()
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        date_obj = datetime(year, month, day)
        if is_business_day(date_obj) and date_obj >= today:
            dates.append(date_obj.strftime("%Y-%m-%d"))
    return dates

def get_available_slots(date_str):
    slots = ["14:00", "15:30", "17:00", "18:30", "20:00"]
    now = get_taiwan_now()
    if date_str == now.strftime("%Y-%m-%d"):
        slots = [s for s in slots if s > now.strftime("%H:%M")]
    
    appointments = get_all_appointments()
    booked = [a['time'] for a in appointments if a['date'] == date_str]
    return [s for s in slots if s not in booked]

def check_duplicate(date, time, phone, user_id):
    appointments = get_all_appointments()
    for a in appointments:
        if a['date'] == date and a['time'] == time:
            return True, f"❌ {date} {time} 這個時段已經被預約了！"
        if a['phone'] == phone and a['date'] == date and a['time'] == time:
            return True, "❌ 這個手機號碼已經在這個時段有預約了！"
        if a.get('user_id') == user_id and a['date'] == date and a['time'] == time:
            return True, "❌ 您已經在這個時段有預約了！"
    return False, None

def create_appointment(user_id, name, phone, service_id, date_str, time_str):
    is_dup, err = check_duplicate(date_str, time_str, phone, user_id)
    if is_dup:
        return None, err
    
    service = SERVICES[service_id]
    weekday = get_weekday_name(date_str)
    appointments = get_all_appointments()
    next_id = max([int(a['id']) for a in appointments], default=0) + 1
    
    success = add_appointment({
        'id': next_id,
        'user_id': user_id,
        'date': date_str,
        'weekday': weekday,
        'time': time_str,
        'service': service['name'],
        'price': service['price'],
        'name': name,
        'phone': phone
    })
    
    if not success:
        return None, "❌ 寫入失敗，請稍後再試"
    
    # 清除快取
    global appointments_cache, cache_time
    appointments_cache = []
    cache_time = 0
    
    # 發送通知
    msg = f"🔔 新預約通知！\n\n📌 預約編號：{next_id}\n📅 日期：{date_str} {weekday}\n⏰ 時間：{time_str}\n💆 服務：{service['name']}\n💰 金額：${service['price']}\n👤 客戶：{name}\n📞 電話：{phone}"
    for admin_id in ADMIN_USER_IDS:
        if admin_id:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).push_message(PushMessageRequest(to=admin_id, messages=[TextMessage(text=msg)]))
    
    return next_id, None

def get_user_appointments(user_id):
    appointments = get_all_appointments()
    return [a for a in appointments if a.get('user_id') == user_id]

def admin_view_all():
    appointments = get_all_appointments()
    if not appointments:
        return "📋 目前沒有任何預約"
    appointments.sort(key=lambda x: (x['date'], x['time']))
    total = sum(int(a['price']) for a in appointments)
    msg = "📋 所有預約清單\n\n"
    for a in appointments:
        msg += f"🔹 #{a['id']}\n   📅 {a['date']}\n   ⏰ {a['time']}\n   💆 {a['service']}\n   💰 ${a['price']}\n   👤 {a['name']}\n   📞 {a['phone']}\n\n"
    msg += f"總計: {len(appointments)} 筆預約\n總營收: ${total}"
    return msg

def admin_view_month(year, month):
    appointments = get_all_appointments()
    filtered = [a for a in appointments if a['date'].startswith(f"{year}-{month:02d}")]
    if not filtered:
        return f"📋 {year}年{month}月 沒有任何預約"
    total = sum(int(a['price']) for a in filtered)
    msg = f"📋 {year}年{month}月 預約清單\n\n"
    for a in filtered:
        msg += f"🔹 #{a['id']}\n   📅 {a['date']}\n   ⏰ {a['time']}\n   💆 {a['service']}\n   💰 ${a['price']}\n   👤 {a['name']}\n   📞 {a['phone']}\n\n"
    msg += f"總計: {len(filtered)} 筆預約\n總營收: ${total}"
    return msg

def admin_cancel_by_id(apt_id):
    return "⚠️ 取消預約請直接在 Google 試算表中刪除該列"

user_state = {}

def send_reply(reply_token, messages):
    with ApiClient(configuration) as api_client:
        if not isinstance(messages, list):
            messages = [messages]
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))

def get_service_buttons():
    return [QuickReplyItem(action=PostbackAction(label=f"{s['emoji']} {s['name']} ${s['price']}", data=f"service_{sid}")) for sid, s in SERVICES.items()]

@app.route("/callback", methods=['POST'])
def callback():
    sig = request.headers.get('X-Line-Signature', '')
    if not sig:
        return 'Missing signature', 400
    try:
        handler.handle(request.get_data(as_text=True), sig)
    except InvalidSignatureError:
        return 'Invalid signature', 400
    return 'OK', 200

@app.route("/", methods=['GET'])
def health():
    return "OK", 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    text = event.message.text.strip()
    print(f"使用者 {user_id} 說: {text}")

    if user_id in user_state:
        state = user_state[user_id]
        if state.get("step") == "waiting_name":
            parts = text.strip().split()
            if len(parts) >= 2:
                state["name"] = parts[0]
                state["phone"] = parts[1]
                apt_id, err = create_appointment(
                    user_id, state["name"], state["phone"],
                    state["service_id"], state["selected_date"], state["selected_time"]
                )
                if err:
                    send_reply(reply_token, TextMessage(text=f"❌ {err}"))
                else:
                    service = SERVICES[state["service_id"]]
                    weekday = get_weekday_name(state["selected_date"])
                    send_reply(reply_token, TextMessage(
                        text=f"✅ 預約成功！\n\n📌 預約編號：{apt_id}\n📅 日期：{state['selected_date']} {weekday}\n⏰ 時間：{state['selected_time']}\n💆 服務：{service['name']}\n💰 費用：${service['price']}\n👤 姓名：{state['name']}\n📞 手機：{state['phone']}\n\n⚠️ 請準時抵達，取消請提前告知"
                    ))
                del user_state[user_id]
            else:
                send_reply(reply_token, TextMessage(text="❌ 請輸入：姓名 手機（中間空格）\n\n範例：王小明 0912345678"))
            return
        elif state.get("step") == "admin_waiting_year":
            try:
                state["admin_year"] = int(text)
                state["step"] = "admin_waiting_month"
                user_state[user_id] = state
                send_reply(reply_token, TextMessage(text="請輸入月份 (1-12)："))
            except:
                send_reply(reply_token, TextMessage(text="❌ 請輸入正確的年份"))
            return
        elif state.get("step") == "admin_waiting_month":
            try:
                year = state.get("admin_year")
                month = int(text)
                if 1 <= month <= 12:
                    send_reply(reply_token, TextMessage(text=admin_view_month(year, month)))
                else:
                    send_reply(reply_token, TextMessage(text="❌ 月份請輸入 1-12"))
                del user_state[user_id]
            except:
                send_reply(reply_token, TextMessage(text="❌ 請輸入正確的月份"))
                del user_state[user_id]
            return
        elif state.get("step") == "admin_waiting_cancel_id":
            try:
                apt_id = int(text)
                send_reply(reply_token, TextMessage(text=admin_cancel_by_id(apt_id)))
                del user_state[user_id]
            except:
                send_reply(reply_token, TextMessage(text="❌ 請輸入正確的編號"))
                del user_state[user_id]
            return

    if text == "我要預約":
        send_reply(reply_token, TextMessage(
            text="🧠 頭薦骨調理預約系統\n\n📅 營業時間：14:00-21:30\n⏰ 每1.5小時一個時段\n📴 公休日：每週五\n\n請選擇服務項目：",
            quick_reply=QuickReply(items=get_service_buttons())
        ))
    elif text == "我的預約":
        apps = get_user_appointments(user_id)
        if not apps:
            send_reply(reply_token, TextMessage(text="📋 您目前沒有有效預約"))
        else:
            msg = "📋 您的預約：\n\n"
            for a in apps:
                msg += f"🔹 編號 {a['id']}\n   📅 {a['date']}\n   ⏰ {a['time']}\n   💆 {a['service']}\n   💰 ${a['price']}\n\n"
            send_reply(reply_token, TextMessage(text=msg))
    elif text == "取消查詢":
        apps = get_user_appointments(user_id)
        if not apps:
            send_reply(reply_token, TextMessage(text="您目前沒有有效預約"))
        else:
            items = [QuickReplyItem(action=PostbackAction(label=f"取消 {a['date'][5:]} {a['time']}", data=f"cancel_{a['id']}")) for a in apps]
            send_reply(reply_token, TextMessage(text="請選擇要取消的預約：", quick_reply=QuickReply(items=items)))
    elif text == "店家後台":
        if user_id in ADMIN_USER_IDS:
            items = [
                QuickReplyItem(action=PostbackAction(label="📋 所有預約", data="admin_all")),
                QuickReplyItem(action=PostbackAction(label="📅 按月查詢", data="admin_month")),
                QuickReplyItem(action=PostbackAction(label="❌ 取消預約", data="admin_cancel")),
                QuickReplyItem(action=MessageAction(label="🔙 返回", text="我要預約"))
            ]
            send_reply(reply_token, TextMessage(text="🔐 店家後台\n\n請選擇功能：", quick_reply=QuickReply(items=items)))
        else:
            send_reply(reply_token, TextMessage(text=f"⛔ 無權限\n\n您的 LINE ID：{user_id}"))
    else:
        send_reply(reply_token, TextMessage(
            text="🧠 頭薦骨調理預約系統\n\n📅 營業時間：14:00-21:30\n📴 公休日：每週五\n\n✅ 輸入「我要預約」開始\n✅ 輸入「我的預約」查詢\n✅ 輸入「取消查詢」取消",
            quick_reply=QuickReply(items=[
                QuickReplyItem(action=MessageAction(label="📅 我要預約", text="我要預約")),
                QuickReplyItem(action=MessageAction(label="📋 我的預約", text="我的預約")),
                QuickReplyItem(action=MessageAction(label="❌ 取消查詢", text="取消查詢")),
                QuickReplyItem(action=MessageAction(label="🔐 店家後台", text="店家後台"))
            ])
        ))

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    data = event.postback.data

    if data.startswith("service_"):
        service_id = data.split("_")[1]
        user_state[user_id] = {"step": "waiting_year", "service_id": service_id, "date_page": 0}
        items = [QuickReplyItem(action=PostbackAction(label=f"{i}年", data=f"year_{i}")) for i in range(get_taiwan_now().year, get_taiwan_now().year + 2)]
        send_reply(reply_token, TextMessage(text="請選擇年份：", quick_reply=QuickReply(items=items)))
    elif data.startswith("year_"):
        year = int(data.split("_")[1])
        state = user_state.get(user_id, {})
        state["year"] = year
        state["step"] = "waiting_month"
        user_state[user_id] = state
        now = get_taiwan_now()
        items = [QuickReplyItem(action=PostbackAction(label=f"{i}月", data=f"month_{i}")) for i in range(1, 13) if not (year == now.year and i < now.month)]
        send_reply(reply_token, TextMessage(text=f"請選擇 {year} 年月份：", quick_reply=QuickReply(items=items)))
    elif data.startswith("month_"):
        month = int(data.split("_")[1])
        state = user_state.get(user_id, {})
        year = state.get("year", get_taiwan_now().year)
        all_dates = get_available_dates(year, month)
        if not all_dates:
            send_reply(reply_token, TextMessage(text="該月份無營業日（週五公休）或無可預約日期"))
            return
        state["all_dates"] = all_dates
        state["step"] = "waiting_date"
        user_state[user_id] = state
        show_date_page(user_id, reply_token)
    elif data.startswith("date_page_"):
        page = int(data.split("_")[2])
        state = user_state.get(user_id, {})
        if state.get("step") == "waiting_date":
            state["date_page"] = page
            user_state[user_id] = state
            show_date_page(user_id, reply_token)
    elif data.startswith("date_"):
        date_str = data.split("_")[1]
        slots = get_available_slots(date_str)
        if not slots:
            send_reply(reply_token, TextMessage(text="當日已滿，請重新選擇"))
            return
        state = user_state.get(user_id, {})
        state["selected_date"] = date_str
        state["step"] = "waiting_time"
        user_state[user_id] = state
        weekday = get_weekday_name(date_str)
        items = [QuickReplyItem(action=PostbackAction(label=slot, data=f"time_{slot}")) for slot in slots]
        items.append(QuickReplyItem(action=PostbackAction(label="⬅️ 返回選日期", data="back_to_date")))
        send_reply(reply_token, TextMessage(text=f"📅 {date_str} {weekday}\n\n⏰ 營業時間：14:00-21:30\n\n請選擇時段：", quick_reply=QuickReply(items=items)))
    elif data == "back_to_date":
        state = user_state.get(user_id, {})
        state["step"] = "waiting_date"
        user_state[user_id] = state
        show_date_page(user_id, reply_token)
    elif data.startswith("time_"):
        time_str = data.split("_")[1]
        state = user_state.get(user_id, {})
        state["selected_time"] = time_str
        state["step"] = "waiting_name"
        user_state[user_id] = state
        send_reply(reply_token, TextMessage(text=f"⏰ 時段：{time_str}\n\n請輸入您的姓名和手機號碼（中間用空格隔開）\n\n範例：王小明 0912345678"))
    elif data.startswith("cancel_"):
        apt_id = int(data.split("_")[1])
        if cancel_appointment(user_id, apt_id):
            send_reply(reply_token, TextMessage(text="✅ 已取消預約"))
        else:
            send_reply(reply_token, TextMessage(text="❌ 取消失敗"))
    elif data == "admin_all":
        if user_id not in ADMIN_USER_IDS:
            send_reply(reply_token, TextMessage(text="⛔ 無權限"))
            return
        send_reply(reply_token, TextMessage(text=admin_view_all()))
    elif data == "admin_month":
        if user_id not in ADMIN_USER_IDS:
            send_reply(reply_token, TextMessage(text="⛔ 無權限"))
            return
        user_state[user_id] = {"step": "admin_waiting_year"}
        send_reply(reply_token, TextMessage(text="請輸入年份 (例如 2026)："))
    elif data == "admin_cancel":
        if user_id not in ADMIN_USER_IDS:
            send_reply(reply_token, TextMessage(text="⛔ 無權限"))
            return
        user_state[user_id] = {"step": "admin_waiting_cancel_id"}
        send_reply(reply_token, TextMessage(text="請輸入要取消的預約編號："))

def show_date_page(user_id, reply_token):
    state = user_state.get(user_id, {})
    all_dates = state.get("all_dates", [])
    year = state.get("year", get_taiwan_now().year)
    month = state.get("month", get_taiwan_now().month)
    current_page = state.get("date_page", 0)
    total_pages = (len(all_dates) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = current_page * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, len(all_dates))
    page_dates = all_dates[start_idx:end_idx]
    items = [QuickReplyItem(action=PostbackAction(label=f"{d.split('-')[2]}日 {get_weekday_name(d)}", data=f"date_{d}")) for d in page_dates]
    if current_page > 0:
        items.insert(0, QuickReplyItem(action=PostbackAction(label="⬅️ 上一頁", data=f"date_page_{current_page - 1}")))
    if current_page < total_pages - 1:
        items.append(QuickReplyItem(action=PostbackAction(label="➡️ 下一頁", data=f"date_page_{current_page + 1}")))
    send_reply(reply_token, TextMessage(text=f"📅 {year}年{month}月\n\n共 {len(all_dates)} 天（第{current_page + 1}/{total_pages}頁）\n\n請選擇日期：", quick_reply=QuickReply(items=items)))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
