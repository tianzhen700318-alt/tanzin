import os
import json
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    TextSendMessage, FlexSendMessage, QuickReply, QuickReplyButton, MessageAction,
    PostbackEvent, MessageEvent, TextMessage, FollowEvent
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

channel_access_token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.environ.get('LINE_CHANNEL_SECRET')
if not channel_access_token or not channel_secret:
    raise ValueError("請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(channel_access_token)
handler = WebhookHandler(channel_secret)

# ========== Google Sheets 設定 ==========
def get_google_sheet():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds_json_str = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not creds_json_str:
        raise ValueError("請設定 GOOGLE_SERVICE_ACCOUNT_JSON")
    creds_json = json.loads(creds_json_str)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    sheet_id = os.environ.get('GOOGLE_SHEET_ID')
    if not sheet_id:
        raise ValueError("請設定 GOOGLE_SHEET_ID")
    return client.open_by_key(sheet_id)

SERVICES = [
    {"id": 1, "name": "頭薦骨調理", "price": 1500},
    {"id": 2, "name": "局部按摩", "price": 800}
]

TIME_SLOTS = [
    {"id": 1, "start": "14:00", "end": "15:30"},
    {"id": 2, "start": "15:30", "end": "17:00"},
    {"id": 3, "start": "17:00", "end": "18:30"},
    {"id": 4, "start": "18:30", "end": "20:00"},
    {"id": 5, "start": "20:00", "end": "21:30"}
]

def get_taiwan_now():
    return datetime.now(pytz.timezone('Asia/Taipei'))

def is_admin(user_id):
    try:
        sheet = get_google_sheet()
        admin_ws = sheet.worksheet("店家後台")
        admins_raw = admin_ws.col_values(1)
        admins = [str(a).strip() for a in admins_raw if str(a).strip()]
        return user_id in admins
    except Exception as e:
        print(f"檢查店家權限錯誤: {e}")
        return False

def get_main_menu_quick_reply(user_id):
    items = [QuickReplyButton(action=MessageAction(label="📅 立即預約", text="我要預約"))]
    if is_admin(user_id):
        items.append(QuickReplyButton(action=MessageAction(label="🔧 店家後台", text="店家後台")))
    return QuickReply(items=items)

def send_welcome_with_menu(event):
    welcome_text = (
        "🧠 頭薦骨調理預約系統\n\n"
        "📅 營業時間：14:00 - 21:00\n"
        "⏰ 每1小時一個時段\n"
        "📴 公休日：每週五\n\n"
        "✅ 點擊下方按鈕開始預約。"
    )
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=welcome_text, quick_reply=get_main_menu_quick_reply(event.source.user_id))
    )

def get_available_dates(days=60):
    now = get_taiwan_now()
    today = now.date()
    try:
        sheet = get_google_sheet()
        ws = sheet.worksheet("預約紀錄")
        records = ws.get_all_records()
    except Exception as e:
        print(f"[錯誤] 讀取預約紀錄失敗: {e}")
        records = []
    available = []
    for i in range(days):
        d = today + timedelta(days=i)
        if d.weekday() == 4:
            continue
        date_str = d.strftime("%Y-%m-%d")
        booked = sum(1 for r in records if r.get('日期') == date_str and r.get('狀態') == 'confirmed')
        if booked < len(TIME_SLOTS):
            available.append(d)
    return available

def get_available_slots(date_str):
    now = get_taiwan_now()
    try:
        sheet = get_google_sheet()
        ws = sheet.worksheet("預約紀錄")
        records = ws.get_all_records()
    except Exception as e:
        print(f"[錯誤] 讀取時段錯誤: {e}")
        records = []
    booked = [r.get('開始時間') for r in records if r.get('日期') == date_str and r.get('狀態') == 'confirmed']
    all_slots = TIME_SLOTS[:]
    if date_str == now.strftime("%Y-%m-%d"):
        current_time = now.strftime("%H:%M")
        all_slots = [s for s in all_slots if s['start'] > current_time]
    available = [s for s in all_slots if s['start'] not in booked]
    return available

def create_booking(order_id, date, start, end, service_name, price, name, phone):
    try:
        sheet = get_google_sheet()
        ws = sheet.worksheet("預約紀錄")
        now = get_taiwan_now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([order_id, date, start, end, service_name, price, name, phone, 'confirmed', now])
        print(f"[除錯] 預約寫入成功: {order_id}")
        return True
    except Exception as e:
        print(f"[錯誤] 建立預約失敗: {e}")
        return False

def delete_booking_by_order_id(order_id):
    try:
        sheet = get_google_sheet()
        ws = sheet.worksheet("預約紀錄")
        records = ws.get_all_records()
        for idx, r in enumerate(records, start=2):
            if r.get('訂單編號') == order_id:
                ws.delete_rows(idx)
                print(f"[除錯] 已刪除訂單 {order_id}")
                return True
        return False
    except Exception as e:
        print(f"[錯誤] 刪除訂單失敗: {e}")
        return False

def get_today_bookings():
    today = get_taiwan_now().date()
    try:
        sheet = get_google_sheet()
        ws = sheet.worksheet("預約紀錄")
        records = ws.get_all_records()
        confirmed = [r for r in records if r.get('狀態') == 'confirmed']
        result = []
        for r in confirmed:
            booking_date = datetime.strptime(r.get('日期'), '%Y-%m-%d').date()
            if booking_date >= today:
                result.append(r)
        return result
    except Exception as e:
        print(f"[錯誤] 取得今日訂單失敗: {e}")
        return []

def notify_admins_new_booking(order_id, date, start, end, service_name, price, name, phone):
    """向所有店家發送新訂單通知"""
    print(f"[通知] 開始發送新訂單通知: {order_id}")
    try:
        sheet = get_google_sheet()
        admin_ws = sheet.worksheet("店家後台")
        admins_raw = admin_ws.col_values(1)
        print(f"[通知] 原始店家列表: {admins_raw}")
        admin_ids = [str(a).strip() for a in admins_raw if str(a).strip()]
        print(f"[通知] 清理後店家 ID: {admin_ids}")
        if not admin_ids:
            print("[通知] 沒有設定店家 ID，跳過通知")
            return
        flex = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "📢 新訂單通知", "weight": "bold", "size": "xl", "color": "#FF5722", "align": "center"}]},
            "body": {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": f"📅 日期：{date}"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"⏰ 時間：{start} ~ {end}", "margin": "md"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"💆 服務：{service_name}", "margin": "md"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"💰 金額：{price}元", "margin": "md", "weight": "bold", "color": "#FF5722"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"👤 姓名：{name}", "margin": "md"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"📞 電話：{phone}", "margin": "md"},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": f"🆔 訂單編號：{order_id}", "margin": "md", "size": "sm", "color": "#999999"}
            ]},
            "footer": {"type": "box", "layout": "vertical", "contents": [
                {"type": "button", "action": {"type": "postback", "label": "📋 今日訂單", "data": "admin_today_orders"}, "color": "#2196F3"}
            ]}
        }
        for admin_id in admin_ids:
            try:
                line_bot_api.push_message(admin_id, FlexSendMessage(alt_text="新訂單通知", contents=flex))
                print(f"[通知] 已發送訂單 {order_id} 給店家 {admin_id}")
            except Exception as e:
                print(f"[通知] 發送給 {admin_id} 失敗: {e}")
    except Exception as e:
        print(f"[通知] 發送新訂單通知失敗: {e}")

# ========== Flex 訊息（客戶預約流程） ==========
def create_date_quick_reply(available_dates, offset=0):
    weekday_names = ['一', '二', '三', '四', '五', '六', '日']
    items = []
    for i, d in enumerate(available_dates[offset:offset+10]):
        label = d.strftime(f"%m/%d (週{weekday_names[d.weekday()]})")
        items.append(QuickReplyButton(action=MessageAction(label=label, text=f"日期_{d.strftime('%Y-%m-%d')}")))
    if len(available_dates) > offset+10:
        items.append(QuickReplyButton(action=MessageAction(label="下一頁 ➡️", text="更多日期")))
    if offset > 0:
        items.append(QuickReplyButton(action=MessageAction(label="⬅️ 上一頁", text="上一頁日期")))
    return QuickReply(items=items)

def create_slots_flex(date_str, slots):
    buttons = []
    for s in slots:
        buttons.append({
            "type": "button",
            "action": {"type": "postback", "label": f"{s['start']} ~ {s['end']}", "data": f"select_slot={date_str}|{s['start']}|{s['end']}"},
            "color": "#2196F3",
            "margin": "md"
        })
    buttons.append({
        "type": "button",
        "action": {"type": "postback", "label": "🔙 重新選擇日期", "data": "back_to_date"},
        "color": "#9E9E9E",
        "margin": "lg"
    })
    flex = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"📅 {date_str}", "weight": "bold", "size": "xl"}, {"type": "text", "text": "請選擇時段", "color": "#666666"}]},
        "body": {"type": "box", "layout": "vertical", "contents": buttons}
    }
    return FlexSendMessage(alt_text=f"{date_str} 可預約時段", contents=flex)

def create_service_flex():
    buttons = []
    for s in SERVICES:
        buttons.append({
            "type": "button",
            "action": {"type": "postback", "label": f"{s['name']} - {s['price']}元", "data": f"select_service={s['id']}|{s['name']}|{s['price']}"},
            "color": "#9C27B0",
            "margin": "md"
        })
    flex = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "💆 請選擇服務項目", "weight": "bold", "size": "xl", "align": "center"}]},
        "body": {"type": "box", "layout": "vertical", "contents": buttons}
    }
    return FlexSendMessage(alt_text="服務項目選擇", contents=flex)

def create_booking_detail_flex(order_id, date, start, end, service_name, price, name, phone):
    flex = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "✅ 預約完成", "weight": "bold", "size": "xl", "color": "#4CAF50", "align": "center"}]},
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": f"📅 日期：{date}"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": f"⏰ 時間：{start} ~ {end}", "margin": "md"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": f"💆 服務：{service_name}", "margin": "md"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": f"💰 金額：{price}元", "margin": "md", "weight": "bold", "color": "#FF5722"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": f"👤 姓名：{name}", "margin": "md"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": f"📞 電話：{phone}", "margin": "md"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": f"🆔 訂單編號：{order_id}", "margin": "md", "size": "sm", "color": "#999999"},
            {"type": "separator", "margin": "md"},
            {"type": "text", "text": "📌 提醒您：\n• 請準時到達\n• 取消預約請提早告知\n• 調理當天請穿著寬鬆褲裝", "margin": "md", "size": "sm", "color": "#666666", "wrap": True}
        ]},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "action": {"type": "postback", "label": "🏠 主選單", "data": "main_menu"}, "color": "#666666"}
        ]}
    }
    return FlexSendMessage(alt_text="預約完成", contents=flex)

def create_order_list_flex(orders, title="訂單列表"):
    if not orders:
        return TextSendMessage(text="目前無符合的訂單")
    contents = []
    for i, b in enumerate(orders[:10]):
        order_id = b.get('訂單編號', '無')
        date = b.get('日期', '未知')
        start = b.get('開始時間', '未知')
        service = b.get('服務項目', '無')
        price = b.get('金額', '0')
        name = b.get('客戶姓名', '未知')
        phone = b.get('客戶電話', '未知')
        order_box = {
            "type": "box",
            "layout": "vertical",
            "margin": "md",
            "contents": [
                {"type": "text", "text": f"📅 {date} {start}", "weight": "bold", "size": "sm"},
                {"type": "text", "text": f"💆 {service} ({price}元)", "size": "xs", "color": "#FF5722"},
                {"type": "text", "text": f"👤 {name} ({phone})", "size": "xs", "color": "#666666"},
                {"type": "text", "text": f"🆔 {order_id}", "size": "xs", "color": "#999999", "wrap": True},
                {"type": "separator", "margin": "sm"},
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "🗑️ 刪除此訂單",
                        "data": f"admin_delete_order={order_id}",
                        "displayText": f"刪除訂單 {order_id}"
                    },
                    "color": "#F44336",
                    "style": "primary",
                    "margin": "sm"
                }
            ]
        }
        contents.append(order_box)
    if len(orders) > 10:
        contents.append({"type": "text", "text": f"... 共 {len(orders)} 筆，僅顯示最近 10 筆", "size": "xs", "color": "#aaaaaa", "margin": "md"})
    flex = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "xl", "align": "center"}]},
        "body": {"type": "box", "layout": "vertical", "contents": contents, "spacing": "sm"},
        "footer": {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "action": {"type": "postback", "label": "🔙 返回店家後台", "data": "admin_menu"}, "color": "#666666"}
        ]}
    }
    return FlexSendMessage(alt_text=title, contents=flex)

def create_admin_menu_flex():
    flex = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "🔧 店家後台", "weight": "bold", "size": "xl", "align": "center"}]},
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "請選擇功能：", "weight": "bold", "margin": "md"},
            {"type": "button", "action": {"type": "postback", "label": "📅 今日訂單", "data": "admin_today_orders"}, "color": "#2196F3", "margin": "md"},
            {"type": "button", "action": {"type": "postback", "label": "🏠 主選單", "data": "main_menu"}, "color": "#666666", "margin": "md"}
        ]}
    }
    return FlexSendMessage(alt_text="店家後台", contents=flex)

# ========== Webhook 處理 ==========
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/", methods=['GET'])
def health():
    return "OK"

user_session = {}
date_offset = {}

@handler.add(FollowEvent)
def handle_follow(event):
    send_welcome_with_menu(event)

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    print(f"[除錯] 使用者 LINE ID: {user_id} | 訊息: {text}")

    if text.lower() in ["嗨", "hello", "你好", "開始", "start", "主選單"]:
        send_welcome_with_menu(event)
        return

    if text.startswith("日期_"):
        date_str = text.replace("日期_", "")
        slots = get_available_slots(date_str)
        if slots:
            flex = create_slots_flex(date_str, slots)
            line_bot_api.reply_message(event.reply_token, flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="該日期已無可預約時段"))
        return

    if text == "更多日期":
        offset = date_offset.get(user_id, 0) + 10
        available_dates = get_available_dates()
        if offset < len(available_dates):
            date_offset[user_id] = offset
            quick = create_date_quick_reply(available_dates, offset)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇日期：", quick_reply=quick))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="沒有更多可預約日期了"))
        return
    if text == "上一頁日期":
        offset = date_offset.get(user_id, 0) - 10
        if offset >= 0:
            date_offset[user_id] = offset
            available_dates = get_available_dates()
            quick = create_date_quick_reply(available_dates, offset)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇日期：", quick_reply=quick))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已經是第一頁了"))
        return

    if user_id in user_session and user_session[user_id].get("step") == "waiting_name_phone":
        parts = text.split()
        if len(parts) >= 2:
            name = parts[0]
            phone = parts[1]
            date = user_session[user_id]["date"]
            start = user_session[user_id]["start"]
            end = user_session[user_id]["end"]
            service_name = user_session[user_id]["service_name"]
            price = user_session[user_id]["price"]
            order_id = str(uuid.uuid4())[:8]
            if create_booking(order_id, date, start, end, service_name, price, name, phone):
                # 發送新訂單通知給店家
                notify_admins_new_booking(order_id, date, start, end, service_name, price, name, phone)
                del user_session[user_id]
                flex = create_booking_detail_flex(order_id, date, start, end, service_name, price, name, phone)
                line_bot_api.reply_message(event.reply_token, flex)
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="預約失敗，請稍後再試"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="格式錯誤，請輸入「姓名 電話」"))
        return

    if text == "我要預約":
        available_dates = get_available_dates()
        if available_dates:
            date_offset[user_id] = 0
            quick = create_date_quick_reply(available_dates, 0)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇預約日期：", quick_reply=quick))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前尚無可預約日期"))

    elif text == "店家後台":
        if is_admin(user_id):
            admin_flex = create_admin_menu_flex()
            line_bot_api.reply_message(event.reply_token, admin_flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您無權限使用店家後台"))
    elif text == "今日" and is_admin(user_id):
        bookings = get_today_bookings()
        if bookings:
            flex = create_order_list_flex(bookings, "📅 今日以後訂單")
            line_bot_api.reply_message(event.reply_token, flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前沒有今日以後的訂單"))
    else:
        if is_admin(user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請使用「今日」查看訂單，或點選後台按鈕"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請點選下方按鈕預約：", quick_reply=get_main_menu_quick_reply(user_id)))

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data

    if data.startswith("select_slot="):
        _, info = data.split("=")
        date_str, start, end = info.split("|")
        user_session[user_id] = {"step": "waiting_service", "date": date_str, "start": start, "end": end}
        service_flex = create_service_flex()
        line_bot_api.reply_message(event.reply_token, service_flex)
    elif data.startswith("select_service="):
        _, info = data.split("=")
        service_id, service_name, price = info.split("|")
        price = int(price)
        session_data = user_session.get(user_id, {})
        session_data.update({"service_id": service_id, "service_name": service_name, "price": price, "step": "waiting_name_phone"})
        if "date" not in session_data or "start" not in session_data:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請重新預約"))
            return
        user_session[user_id] = session_data
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"您選擇：{service_name} {price}元\n請輸入您的姓名和電話（例如：王小明 0912345678）："))
    elif data == "back_to_date":
        available_dates = get_available_dates()
        if available_dates:
            date_offset[user_id] = 0
            quick = create_date_quick_reply(available_dates, 0)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請重新選擇日期：", quick_reply=quick))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前尚無可預約日期"))
    elif data == "main_menu":
        send_welcome_with_menu(event)
    elif data == "admin_menu":
        if is_admin(user_id):
            admin_flex = create_admin_menu_flex()
            line_bot_api.reply_message(event.reply_token, admin_flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您無權限使用店家後台"))
    elif data == "admin_today_orders":
        if not is_admin(user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您無權限執行此操作"))
            return
        bookings = get_today_bookings()
        if bookings:
            flex = create_order_list_flex(bookings, "📅 今日以後訂單")
            line_bot_api.reply_message(event.reply_token, flex)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前沒有今日以後的訂單"))
    elif data.startswith("admin_delete_order="):
        if not is_admin(user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您無權限執行此操作"))
            return
        order_id = data.split("=")[1]
        if delete_booking_by_order_id(order_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已刪除訂單 {order_id}"))
            new_bookings = get_today_bookings()
            if new_bookings:
                flex = create_order_list_flex(new_bookings, "📅 更新後的訂單")
                line_bot_api.push_message(user_id, flex)
            else:
                line_bot_api.push_message(user_id, TextSendMessage(text="目前沒有今日以後的訂單"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 刪除失敗，訂單 {order_id} 不存在"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
