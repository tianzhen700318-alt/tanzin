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
from datetime import datetime
import json
import os
import calendar

# ========== 從環境變數讀取 LINE 金鑰 ==========
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')

# 店家 LINE ID（多個用逗號分隔）
admin_ids_str = os.environ.get('ADMIN_USER_IDS', '')
ADMIN_USER_IDS = [uid.strip() for uid in admin_ids_str.split(',') if uid.strip()]
# ==========================================

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 服務項目
SERVICES = {
    "1": {"name": "健康調理", "price": 1500, "duration": 60, "emoji": "💆"},
    "2": {"name": "局部紓壓", "price": 800, "duration": 30, "emoji": "💪"}
}

# 使用 /tmp 目錄儲存（Render 可寫入）
DATA_FILE = "/tmp/data.json"
ITEMS_PER_PAGE = 10

def init_db():
    if not os.path.exists(DATA_FILE):
        save_data({"appointments": [], "next_id": 1})

def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_business_day(date_obj):
    return date_obj.weekday() != 4

def get_available_dates(year, month):
    dates = []
    days_in_month = calendar.monthrange(year, month)[1]
    for day in range(1, days_in_month + 1):
        date_obj = datetime(year, month, day)
        if is_business_day(date_obj):
            dates.append(date_obj.strftime("%Y-%m-%d"))
    return dates

def get_available_slots(date_str):
    """產生時段 - 14:00 到 20:00（最後時段20:00，服務到21:00）"""
    slots = []
    for hour in range(14, 21):
        slots.append(f"{hour:02d}:00")
    
    data = load_data()
    booked = [a["time"] for a in data["appointments"] 
              if a["date"] == date_str and a["status"] == "confirmed"]
    
    return [s for s in slots if s not in booked]

def check_duplicate_appointment(user_id, name, phone, date_str, time_str):
    data = load_data()
    
    for a in data["appointments"]:
        if a["status"] != "confirmed":
            continue
        
        if (a["user_id"] == user_id and 
            a["date"] == date_str and 
            a["time"] == time_str):
            return True, "❌ 您已經在這個時段有預約了！"
        
        if (a["customer_phone"] == phone and 
            a["date"] == date_str and 
            a["time"] == time_str):
            return True, "❌ 這個手機號碼已經在相同時段有預約了！"
    
    same_day_count = sum(1 for x in data["appointments"] 
                        if x["user_id"] == user_id and 
                        x["date"] == date_str and 
                        x["status"] == "confirmed")
    if same_day_count >= 3:
        return True, "❌ 您同一天最多只能預約3個時段！"
    
    return False, None

def send_admin_notification(apt_id, name, phone, service_name, service_price, date_str, time_str):
    weekday = get_weekday_name(date_str)
    notification_msg = (
        f"🔔 新預約通知！\n\n"
        f"📌 預約編號：{apt_id}\n"
        f"📅 日期：{date_str} {weekday}\n"
        f"⏰ 時間：{time_str}\n"
        f"💆 服務：{service_name}\n"
        f"💰 金額：${service_price}\n"
        f"👤 客戶：{name}\n"
        f"📞 電話：{phone}"
    )
    
    try:
        for admin_id in ADMIN_USER_IDS:
            if admin_id:
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=admin_id,
                            messages=[TextMessage(text=notification_msg)]
                        )
                    )
        print(f"✅ 已發送預約通知")
    except Exception as e:
        print(f"⚠️ 發送通知失敗: {e}")

def create_appointment(user_id, name, phone, service_id, date_str, time_str):
    is_duplicate, error_msg = check_duplicate_appointment(user_id, name, phone, date_str, time_str)
    if is_duplicate:
        return None, error_msg
    
    data = load_data()
    service = SERVICES[service_id]
    
    next_id = data.get("next_id", 1)
    
    new_appointment = {
        "id": next_id,
        "user_id": user_id,
        "customer_name": name,
        "customer_phone": phone,
        "service_id": service_id,
        "service_name": service["name"],
        "service_price": service["price"],
        "duration": service["duration"],
        "date": date_str,
        "time": time_str,
        "status": "confirmed",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    data["appointments"].append(new_appointment)
    data["next_id"] = next_id + 1
    save_data(data)
    
    send_admin_notification(next_id, name, phone, service["name"], service["price"], date_str, time_str)
    
    return next_id, None

def get_user_appointments(user_id):
    data = load_data()
    return [a for a in data["appointments"] if a["user_id"] == user_id and a["status"] == "confirmed"]

def cancel_appointment(user_id, apt_id):
    data = load_data()
    for a in data["appointments"]:
        if a["id"] == apt_id and a["user_id"] == user_id and a["status"] == "confirmed":
            a["status"] = "cancelled"
            save_data(data)
            return True
    return False

def get_weekday_name(date_str):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return weekdays[date_obj.weekday()]

def admin_view_all():
    data = load_data()
    confirmed = [a for a in data["appointments"] if a["status"] == "confirmed"]
    
    if not confirmed:
        return "📋 目前沒有任何預約"
    
    confirmed.sort(key=lambda x: (x["date"], x["time"]))
    total_revenue = sum(a["service_price"] for a in confirmed)
    
    msg = "📋 所有預約清單\n\n"
    for apt in confirmed:
        weekday = get_weekday_name(apt["date"])
        msg += f"🔹 #{apt['id']}\n"
        msg += f"   📅 {apt['date']} {weekday}\n"
        msg += f"   ⏰ {apt['time']}\n"
        msg += f"   💆 {apt['service_name']}\n"
        msg += f"   👤 {apt['customer_name']}\n"
        msg += f"   📞 {apt['customer_phone']}\n\n"
    
    msg += f"總計: {len(confirmed)} 筆預約\n總營收: ${total_revenue}"
    return msg

def admin_view_month(year, month):
    data = load_data()
    month_str = f"{year}-{month:02d}"
    confirmed = [a for a in data["appointments"] 
                 if a["status"] == "confirmed" and a["date"].startswith(month_str)]
    
    if not confirmed:
        return f"📋 {year}年{month}月 沒有任何預約"
    
    confirmed.sort(key=lambda x: (x["date"], x["time"]))
    total_revenue = sum(a["service_price"] for a in confirmed)
    
    msg = f"📋 {year}年{month}月 預約清單\n\n"
    for apt in confirmed:
        weekday = get_weekday_name(apt["date"])
        msg += f"🔹 {apt['date']} {weekday} {apt['time']}\n"
        msg += f"   💆 {apt['service_name']}\n"
        msg += f"   👤 {apt['customer_name']}\n"
        msg += f"   📞 {apt['customer_phone']}\n\n"
    
    msg += f"總計: {len(confirmed)} 筆預約\n總營收: ${total_revenue}"
    return msg

def admin_cancel_by_id(apt_id):
    data = load_data()
    for a in data["appointments"]:
        if a["id"] == apt_id and a["status"] == "confirmed":
            a["status"] = "cancelled"
            save_data(data)
            return f"✅ 已取消預約 #{apt_id}\n客戶: {a['customer_name']}\n日期: {a['date']} {a['time']}"
    return f"❌ 找不到預約 #{apt_id}"

user_state = {}

def send_reply(reply_token, messages):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        if not isinstance(messages, list):
            messages = [messages]
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )

def get_service_buttons():
    items = []
    for sid, service in SERVICES.items():
        items.append(QuickReplyItem(action=PostbackAction(
            label=f"{service['emoji']} {service['name']} ${service['price']}",
            data=f"service_{sid}"
        )))
    return items

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    if not signature:
        return 'Missing signature', 400
    
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return 'Invalid signature', 400
    except Exception as e:
        print(f"錯誤: {e}")
        return 'OK', 200
    
    return 'OK', 200

@app.route("/", methods=['GET'])
def health_check():
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
            state["name"] = text
            state["step"] = "waiting_phone"
            user_state[user_id] = state
            send_reply(reply_token, TextMessage(text=f"👤 姓名：{text}\n\n請輸入手機號碼："))
            return
        
        elif state.get("step") == "waiting_phone":
            state["phone"] = text
            apt_id, error = create_appointment(
                user_id, state["name"], text,
                state["service_id"], state["selected_date"], state["selected_time"]
            )
            
            if error:
                send_reply(reply_token, TextMessage(text=f"❌ {error}\n\n請重新選擇"))
                del user_state[user_id]
                return
            
            service = SERVICES[state["service_id"]]
            weekday = get_weekday_name(state["selected_date"])
            
            send_reply(reply_token, TextMessage(
                text=f"✅ 預約成功！\n\n"
                     f"📌 預約編號：{apt_id}\n"
                     f"📅 日期：{state['selected_date']} {weekday}\n"
                     f"⏰ 時間：{state['selected_time']}\n"
                     f"💆 服務：{service['name']}\n"
                     f"💰 費用：${service['price']}\n"
                     f"👤 姓名：{state['name']}\n"
                     f"📞 手機：{text}"
            ))
            del user_state[user_id]
            return
        
        elif state.get("step") == "admin_waiting_year":
            try:
                state["admin_year"] = int(text)
                state["step"] = "admin_waiting_month"
                user_state[user_id] = state
                send_reply(reply_token, TextMessage(text="請輸入月份 (1-12)："))
            except ValueError:
                send_reply(reply_token, TextMessage(text="❌ 請輸入正確的年份"))
            return
        
        elif state.get("step") == "admin_waiting_month":
            try:
                year = state.get("admin_year")
                month = int(text)
                if 1 <= month <= 12:
                    result = admin_view_month(year, month)
                    send_reply(reply_token, TextMessage(text=result))
                else:
                    send_reply(reply_token, TextMessage(text="❌ 月份請輸入 1-12"))
                del user_state[user_id]
            except ValueError:
                send_reply(reply_token, TextMessage(text="❌ 請輸入正確的月份"))
                del user_state[user_id]
            return
        
        elif state.get("step") == "admin_waiting_cancel_id":
            try:
                apt_id = int(text)
                result = admin_cancel_by_id(apt_id)
                send_reply(reply_token, TextMessage(text=result))
                del user_state[user_id]
            except ValueError:
                send_reply(reply_token, TextMessage(text="❌ 請輸入正確的編號"))
                del user_state[user_id]
            return
    
    if text == "我要預約":
        welcome_msg = (
            "🧠 頭薦骨調理預約系統\n\n"
            "📅 營業時間：14:00 - 21:00\n"
            "⏰ 每小時一個時段\n"
            "📴 公休日：每週五\n\n"
            "請選擇服務項目："
        )
        
        send_reply(reply_token, TextMessage(
            text=welcome_msg,
            quick_reply=QuickReply(items=get_service_buttons())
        ))
    
    elif text == "我的預約":
        apps = get_user_appointments(user_id)
        if not apps:
            send_reply(reply_token, TextMessage(text="📋 您目前沒有有效預約"))
        else:
            msg = "📋 您的預約：\n\n"
            for a in apps:
                weekday = get_weekday_name(a["date"])
                msg += f"🔹 編號 {a['id']}\n"
                msg += f"   📅 {a['date']} {weekday}\n"
                msg += f"   ⏰ {a['time']}\n"
                msg += f"   💆 {a['service_name']}\n"
                msg += f"   💰 ${a['service_price']}\n\n"
            send_reply(reply_token, TextMessage(text=msg))
    
    elif text == "取消查詢":
        apps = get_user_appointments(user_id)
        if not apps:
            send_reply(reply_token, TextMessage(text="您目前沒有有效預約"))
        else:
            items = []
            for a in apps:
                items.append(QuickReplyItem(action=PostbackAction(
                    label=f"取消 {a['date'][5:]} {a['time']}",
                    data=f"cancel_{a['id']}"
                )))
            send_reply(reply_token, TextMessage(
                text="請選擇要取消的預約：",
                quick_reply=QuickReply(items=items)
            ))
    
    elif text == "店家後台":
        if user_id in ADMIN_USER_IDS:
            items = [
                QuickReplyItem(action=PostbackAction(label="📋 所有預約", data="admin_all")),
                QuickReplyItem(action=PostbackAction(label="📅 按月查詢", data="admin_month")),
                QuickReplyItem(action=PostbackAction(label="❌ 取消預約", data="admin_cancel")),
                QuickReplyItem(action=MessageAction(label="🔙 返回", text="我要預約"))
            ]
            send_reply(reply_token, TextMessage(
                text="🔐 店家後台\n\n請選擇功能：",
                quick_reply=QuickReply(items=items)
            ))
        else:
            send_reply(reply_token, TextMessage(
                text=f"⛔ 無權限\n\n您的 LINE ID：{user_id}"
            ))
    
    else:
        welcome_msg = (
            "🧠 頭薦骨調理預約系統\n\n"
            "📅 營業時間：14:00 - 21:00\n"
            "📴 公休日：每週五\n\n"
            "✅ 輸入「我要預約」開始\n"
            "✅ 輸入「我的預約」查詢\n"
            "✅ 輸入「取消查詢」取消\n"
            "✅ 店家輸入「店家後台」"
        )
        
        send_reply(reply_token, TextMessage(
            text=welcome_msg,
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
        
        items = []
        current_year = datetime.now().year
        for i in range(current_year, current_year + 2):
            items.append(QuickReplyItem(action=PostbackAction(label=f"{i}年", data=f"year_{i}")))
        send_reply(reply_token, TextMessage(text="請選擇年份：", quick_reply=QuickReply(items=items)))
    
    elif data.startswith("year_"):
        year = int(data.split("_")[1])
        state = user_state.get(user_id, {})
        state["year"] = year
        state["step"] = "waiting_month"
        user_state[user_id] = state
        
        items = []
        for i in range(1, 13):
            items.append(QuickReplyItem(action=PostbackAction(label=f"{i}月", data=f"month_{i}")))
        send_reply(reply_token, TextMessage(text=f"請選擇 {year} 年月份：", quick_reply=QuickReply(items=items)))
    
    elif data.startswith("month_"):
        month = int(data.split("_")[1])
        state = user_state.get(user_id, {})
        year = state.get("year", datetime.now().year)
        all_dates = get_available_dates(year, month)
        
        if not all_dates:
            send_reply(reply_token, TextMessage(text="該月份無營業日（週五公休）"))
            return
        
        state["all_dates"] = all_dates
        state["month"] = month
        state["year"] = year
        state["date_page"] = 0
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
        items = []
        for slot in slots:
            items.append(QuickReplyItem(action=PostbackAction(label=slot, data=f"time_{slot}")))
        
        send_reply(reply_token, TextMessage(
            text=f"📅 {date_str} {weekday}\n\n⏰ 營業時間：14:00-21:00（最後時段20:00）\n\n請選擇時段：",
            quick_reply=QuickReply(items=items)
        ))
    
    elif data.startswith("time_"):
        time_str = data.split("_")[1]
        state = user_state.get(user_id, {})
        state["selected_time"] = time_str
        state["step"] = "waiting_name"
        user_state[user_id] = state
        send_reply(reply_token, TextMessage(text=f"⏰ 時段：{time_str}\n\n請輸入您的姓名："))
    
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
        result = admin_view_all()
        send_reply(reply_token, TextMessage(text=result))
    
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
    year = state.get("year", datetime.now().year)
    month = state.get("month", datetime.now().month)
    current_page = state.get("date_page", 0)
    
    total_pages = (len(all_dates) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = current_page * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, len(all_dates))
    page_dates = all_dates[start_idx:end_idx]
    
    items = []
    for d in page_dates:
        day = d.split("-")[2]
        weekday = get_weekday_name(d)
        items.append(QuickReplyItem(action=PostbackAction(
            label=f"{day}日 {weekday}", 
            data=f"date_{d}"
        )))
    
    if current_page > 0:
        items.append(QuickReplyItem(action=PostbackAction(
            label="⬅️ 上一頁", 
            data=f"date_page_{current_page - 1}"
        )))
    
    if current_page < total_pages - 1:
        items.append(QuickReplyItem(action=PostbackAction(
            label="➡️ 下一頁", 
            data=f"date_page_{current_page + 1}"
        )))
    
    send_reply(reply_token, TextMessage(
        text=f"📅 {year}年{month}月\n\n共 {len(all_dates)} 天（第{current_page + 1}/{total_pages}頁）\n\n請選擇日期：",
        quick_reply=QuickReply(items=items)
    ))

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)