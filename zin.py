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
import gspread
from google.oauth2.service_account import Credentials

# ========== 從環境變數讀取 LINE 金鑰 ==========
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')

# 店家 LINE ID
admin_ids_str = os.environ.get('ADMIN_USER_IDS', '')
ADMIN_USER_IDS = [uid.strip() for uid in admin_ids_str.split(',') if uid.strip()]

# Google Sheets 設定
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get('GOOGLE_SHEETS_CREDENTIALS_JSON', '')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
SHEET_NAME = os.environ.get('SHEET_NAME', '預約記錄')
# ==========================================

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 服務項目
SERVICES = {
    "1": {"name": "健康調理", "price": 1500, "duration": 60, "emoji": "💆"},
    "2": {"name": "局部紓壓", "price": 800, "duration": 30, "emoji": "💪"}
}

ITEMS_PER_PAGE = 10
user_state = {}

# 記憶體備用儲存
memory_appointments = []
memory_next_id = 1

# ========== Google Sheets 操作函數 ==========
def get_google_sheets_client():
    if not GOOGLE_SHEETS_CREDENTIALS_JSON or not SPREADSHEET_ID:
        return None
    try:
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/spreadsheets', 
                    'https://www.googleapis.com/auth/drive']
        )
        return gspread.authorize(creds)
    except Exception as e:
        print(f"Google Sheets 認證失敗: {e}")
        return None

def init_sheet():
    client = get_google_sheets_client()
    if not client:
        return None
    try:
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        try:
            worksheet = spreadsheet.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1, cols=12)
            headers = ['id', 'user_id', 'customer_name', 'customer_phone', 'service_id', 
                      'service_name', 'service_price', 'duration', 'date', 'time', 
                      'status', 'created_at']
            worksheet.append_row(headers)
        return worksheet
    except Exception as e:
        print(f"初始化 Google Sheets 失敗: {e}")
        return None

def load_appointments_from_sheet():
    global memory_appointments, memory_next_id
    worksheet = init_sheet()
    if not worksheet:
        return memory_appointments, memory_next_id
    try:
        records = worksheet.get_all_records()
        appointments = []
        for idx, record in enumerate(records, start=2):
            if record.get('id'):
                try:
                    appointment = {
                        'id': int(record['id']),
                        'user_id': record['user_id'],
                        'customer_name': record['customer_name'],
                        'customer_phone': str(record['customer_phone']),
                        'service_id': record['service_id'],
                        'service_name': record['service_name'],
                        'service_price': int(record['service_price']),
                        'duration': int(record['duration']),
                        'date': record['date'],
                        'time': record['time'],
                        'status': record['status'],
                        'created_at': record['created_at'],
                    }
                    appointments.append(appointment)
                except (ValueError, KeyError) as e:
                    continue
        max_id = max([a['id'] for a in appointments]) if appointments else 0
        return appointments, max_id + 1
    except Exception as e:
        return memory_appointments, memory_next_id

def save_appointment_to_sheet(appointment):
    global memory_appointments, memory_next_id
    worksheet = init_sheet()
    if not worksheet:
        memory_appointments.append(appointment)
        memory_next_id = appointment['id'] + 1
        return True
    try:
        row = [
            appointment['id'], appointment['user_id'], appointment['customer_name'],
            appointment['customer_phone'], appointment['service_id'], appointment['service_name'],
            appointment['service_price'], appointment['duration'], appointment['date'],
            appointment['time'], appointment['status'], appointment['created_at']
        ]
        worksheet.append_row(row)
        return True
    except Exception as e:
        memory_appointments.append(appointment)
        memory_next_id = appointment['id'] + 1
        return True

def update_appointment_status_in_sheet(appointment_id, new_status):
    global memory_appointments
    worksheet = init_sheet()
    if worksheet:
        try:
            records = worksheet.get_all_records()
            for idx, record in enumerate(records, start=2):
                if record.get('id') == appointment_id:
                    worksheet.update_cell(idx, 11, new_status)
                    return True
        except Exception as e:
            pass
    for a in memory_appointments:
        if a['id'] == appointment_id:
            a['status'] = new_status
            return True
    return False

def get_all_appointments():
    appointments, _ = load_appointments_from_sheet()
    return appointments

# ========== 業務邏輯函數 ==========
def is_business_day(date_obj):
    return date_obj.weekday() != 4  # 週五公休

def get_available_dates(year, month):
    dates = []
    days_in_month = calendar.monthrange(year, month)[1]
    for day in range(1, days_in_month + 1):
        date_obj = datetime(year, month, day)
        if is_business_day(date_obj):
            dates.append(date_obj.strftime("%Y-%m-%d"))
    return dates

def get_available_slots(date_str):
    appointments = get_all_appointments()
    slots = [f"{hour:02d}:00" for hour in range(14, 21)]
    booked = [a["time"] for a in appointments 
              if a["date"] == date_str and a["status"] == "confirmed"]
    return [s for s in slots if s not in booked]

def get_weekday_name(date_str):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return weekdays[date_obj.weekday()]

def check_duplicate_appointment(user_id, name, phone, date_str, time_str):
    appointments = get_all_appointments()
    
    # 檢查手機號碼是否已經有預約
    phone_records = [a for a in appointments if a['customer_phone'] == phone]
    if phone_records:
        return True, f"❌ 手機號碼 {phone} 已經有預約記錄，無法再次預約！"
    
    # 檢查同使用者同時間
    for a in appointments:
        if a['status'] == 'confirmed' and a['user_id'] == user_id and a['date'] == date_str and a['time'] == time_str:
            return True, "❌ 您已經在這個時段有預約了！"
    
    # 檢查同一天是否超過3個預約
    same_day_count = sum(1 for a in appointments 
                        if a['user_id'] == user_id and 
                        a['date'] == date_str and 
                        a['status'] == 'confirmed')
    if same_day_count >= 3:
        return True, "❌ 您同一天最多只能預約3個時段！"
    
    return False, None

def create_appointment(user_id, name, phone, service_id, date_str, time_str):
    is_duplicate, error_msg = check_duplicate_appointment(user_id, name, phone, date_str, time_str)
    if is_duplicate:
        return None, error_msg
    
    service = SERVICES[service_id]
    appointments, next_id = load_appointments_from_sheet()
    
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
    
    if save_appointment_to_sheet(new_appointment):
        send_admin_notification(new_appointment['id'], name, phone, service["name"], 
                               service["price"], date_str, time_str)
        return new_appointment['id'], None
    return None, "儲存失敗"

def cancel_appointment(user_id, apt_id):
    appointments = get_all_appointments()
    for a in appointments:
        if a['id'] == apt_id and a['user_id'] == user_id and a['status'] == 'confirmed':
            return update_appointment_status_in_sheet(apt_id, 'cancelled')
    return False

def get_user_appointments(user_id):
    appointments = get_all_appointments()
    return [a for a in appointments if a["user_id"] == user_id and a["status"] == "confirmed"]

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
    except Exception as e:
        print(f"發送通知失敗: {e}")

def admin_view_all():
    appointments = get_all_appointments()
    confirmed = [a for a in appointments if a["status"] == "confirmed"]
    if not confirmed:
        return "📋 目前沒有任何預約"
    confirmed.sort(key=lambda x: (x["date"], x["time"]))
    total_revenue = sum(a["service_price"] for a in confirmed)
    msg = "📋 所有預約清單\n\n"
    for apt in confirmed:
        weekday = get_weekday_name(apt["date"])
        msg += f"🔹 #{apt['id']}\n   📅 {apt['date']} {weekday}\n   ⏰ {apt['time']}\n   💆 {apt['service_name']}\n   👤 {apt['customer_name']}\n   📞 {apt['customer_phone']}\n\n"
    msg += f"總計: {len(confirmed)} 筆預約\n總營收: ${total_revenue}"
    return msg

def admin_view_month(year, month):
    appointments = get_all_appointments()
    month_str = f"{year}-{month:02d}"
    confirmed = [a for a in appointments if a["status"] == "confirmed" and a["date"].startswith(month_str)]
    if not confirmed:
        return f"📋 {year}年{month}月 沒有任何預約"
    confirmed.sort(key=lambda x: (x["date"], x["time"]))
    total_revenue = sum(a["service_price"] for a in confirmed)
    msg = f"📋 {year}年{month}月 預約清單\n\n"
    for apt in confirmed:
        weekday = get_weekday_name(apt["date"])
        msg += f"🔹 {apt['date']} {weekday} {apt['time']}\n   💆 {apt['service_name']}\n   👤 {apt['customer_name']}\n   📞 {apt['customer_phone']}\n\n"
    msg += f"總計: {len(confirmed)} 筆預約\n總營收: ${total_revenue}"
    return msg

def admin_cancel_by_id(apt_id):
    appointments = get_all_appointments()
    for a in appointments:
        if a["id"] == apt_id and a["status"] == "confirmed":
            if update_appointment_status_in_sheet(apt_id, 'cancelled'):
                return f"✅ 已取消預約 #{apt_id}\n客戶: {a['customer_name']}\n日期: {a['date']} {a['time']}"
    return f"❌ 找不到預約 #{apt_id}"

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

def show_date_page(user_id, reply_token):
    state = user_state.get(user_id, {})
    all_dates = state.get("all_dates", [])
    year = state.get("year", datetime.now().year)
    month = state.get("month", datetime.now().month)
    current_page = state.get("date_page", 0)
    
    # 如果沒有日期，顯示錯誤並清除狀態
    if not all_dates:
        send_reply(reply_token, TextMessage(text="該月份無營業日（週五公休）"))
        if user_id in user_state:
            del user_state[user_id]
        return
    
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
    
    # 添加分頁按鈕
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
    
    # 確保 items 不是空的
    if not items:
        send_reply(reply_token, TextMessage(text="暫無可預約日期"))
        if user_id in user_state:
            del user_state[user_id]
        return
    
    send_reply(reply_token, TextMessage(
        text=f"📅 {year}年{month}月\n\n共 {len(all_dates)} 天（第{current_page + 1}/{total_pages}頁）\n\n請選擇日期：",
        quick_reply=QuickReply(items=items)
    ))

# ========== LINE Bot 路由 ==========
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

@app.route("/", methods=['GET'])
def health_check():
    return "OK", 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    text = event.message.text.strip()
    
    print(f"收到訊息 - 用戶: {user_id}, 內容: {text}")
    
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
                send_reply(reply_token, TextMessage(text=f"{error}\n\n請重新開始預約"))
                del user_state[user_id]
                return
            service = SERVICES[state["service_id"]]
            weekday = get_weekday_name(state["selected_date"])
            send_reply(reply_token, TextMessage(
                text=f"✅ 預約成功！\n\n📌 預約編號：{apt_id}\n📅 日期：{state['selected_date']} {weekday}\n⏰ 時間：{state['selected_time']}\n💆 服務：{service['name']}\n💰 費用：${service['price']}\n👤 姓名：{state['name']}\n📞 手機：{text}"
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
        items = get_service_buttons()
        if not items:
            send_reply(reply_token, TextMessage(text="系統錯誤，請稍後再試"))
            return
        send_reply(reply_token, TextMessage(
            text="🧠 頭薦骨調理預約系統\n\n⚠️ 注意：每個手機號碼僅限預約一次！\n\n請選擇服務項目：",
            quick_reply=QuickReply(items=items)
        ))
    elif text == "我的預約":
        apps = get_user_appointments(user_id)
        if not apps:
            send_reply(reply_token, TextMessage(text="📋 您目前沒有有效預約"))
        else:
            msg = "📋 您的預約：\n\n"
            for a in apps:
                weekday = get_weekday_name(a["date"])
                msg += f"🔹 編號 {a['id']}\n   📅 {a['date']} {weekday}\n   ⏰ {a['time']}\n   💆 {a['service_name']}\n   💰 ${a['service_price']}\n\n"
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
            if not items:
                send_reply(reply_token, TextMessage(text="沒有可取消的預約"))
                return
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
            ]
            send_reply(reply_token, TextMessage(
                text="🔐 店家後台\n\n請選擇功能：",
                quick_reply=QuickReply(items=items)
            ))
        else:
            send_reply(reply_token, TextMessage(text=f"⛔ 無權限"))
    else:
        items = [
            QuickReplyItem(action=MessageAction(label="📅 我要預約", text="我要預約")),
            QuickReplyItem(action=MessageAction(label="📋 我的預約", text="我的預約")),
            QuickReplyItem(action=MessageAction(label="❌ 取消查詢", text="取消查詢")),
            QuickReplyItem(action=MessageAction(label="🔐 店家後台", text="店家後台"))
        ]
        send_reply(reply_token, TextMessage(
            text="🧠 頭薦骨調理預約系統\n\n📅 營業時間：14:00 - 21:00\n📴 公休日：每週五\n⚠️ 每個手機號碼僅限預約一次\n\n✅ 輸入「我要預約」開始\n✅ 輸入「我的預約」查詢\n✅ 輸入「取消查詢」取消",
            quick_reply=QuickReply(items=items)
        ))

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    data = event.postback.data

    print(f"收到 Postback - 用戶: {user_id}, 數據: {data}")

    if data.startswith("service_"):
        service_id = data.split("_")[1]
        user_state[user_id] = {"step": "waiting_year", "service_id": service_id, "date_page": 0}
        items = []
        current_year = datetime.now().year
        for i in range(current_year, current_year + 2):
            items.append(QuickReplyItem(action=PostbackAction(label=f"{i}年", data=f"year_{i}")))
        if not items:
            send_reply(reply_token, TextMessage(text="系統錯誤，請稍後再試"))
            return
        send_reply(reply_token, TextMessage(
            text="請選擇年份：", 
            quick_reply=QuickReply(items=items)
        ))
    elif data.startswith("year_"):
        year = int(data.split("_")[1])
        state = user_state.get(user_id, {})
        state["year"] = year
        state["step"] = "waiting_month"
        user_state[user_id] = state
        items = [QuickReplyItem(action=PostbackAction(label=f"{i}月", data=f"month_{i}")) for i in range(1, 13)]
        if not items:
            send_reply(reply_token, TextMessage(text="系統錯誤，請稍後再試"))
            return
        send_reply(reply_token, TextMessage(
            text=f"請選擇 {year} 年月份：", 
            quick_reply=QuickReply(items=items)
        ))
    elif data.startswith("month_"):
        month = int(data.split("_")[1])
        state = user_state.get(user_id, {})
        year = state.get("year", datetime.now().year)
        all_dates = get_available_dates(year, month)
        if not all_dates:
            send_reply(reply_token, TextMessage(text="該月份無營業日（週五公休）"))
            if user_id in user_state:
                del user_state[user_id]
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
        items = [QuickReplyItem(action=PostbackAction(label=slot, data=f"time_{slot}")) for slot in slots]
        if not items:
            send_reply(reply_token, TextMessage(text="當日已無可預約時段"))
            return
        send_reply(reply_token, TextMessage(
            text=f"📅 {date_str} {weekday}\n\n⏰ 營業時間：14:00-21:00\n\n請選擇時段：",
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

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
