import os
import json
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    MessagingApi, Configuration, ReplyMessageRequest,
    PushMessageRequest, TextMessage, FlexMessage,
    FlexContainer, QuickReply, QuickReplyItem, MessageAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz

app = Flask(__name__)

# ========== LINE Bot 設定 ==========
channel_access_token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.environ.get('LINE_CHANNEL_SECRET')

configuration = Configuration(access_token=channel_access_token)
handler = WebhookHandler(channel_secret)
line_bot_api = MessagingApi(configuration)

# ========== Google Sheets 設定 ==========
def get_google_sheet():
    scope = ['https://spreadsheets.google.com/feeds',
             'https://www.googleapis.com/auth/drive']
    creds_json = json.loads(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(os.environ.get('GOOGLE_SHEET_ID'))
    return sheet

# ========== 固定時段（間隔30分鐘緩衝已內建） ==========
TIME_SLOTS = [
    {"id": 1, "start": "14:00", "end": "15:30"},
    {"id": 2, "start": "15:30", "end": "17:00"},
    {"id": 3, "start": "17:00", "end": "18:30"},
    {"id": 4, "start": "18:30", "end": "20:00"},
    {"id": 5, "start": "20:00", "end": "21:30"}
]

# ========== 輔助函數 ==========
def get_taiwan_now():
    tz = pytz.timezone('Asia/Taipei')
    return datetime.now(tz)

def get_available_dates(year, month):
    """取得該月份可預約的日期（今天之後 + 已滿日過濾）"""
    tz = pytz.timezone('Asia/Taipei')
    today = get_taiwan_now().date()
    
    sheet = get_google_sheet()
    bookings_sheet = sheet.worksheet("預約紀錄")
    all_bookings = bookings_sheet.get_all_records()
    
    # 找出每個日期已被預約的時段數量
    month_first = datetime(year, month, 1).date()
    if month == 12:
        next_month = datetime(year + 1, 1, 1).date()
    else:
        next_month = datetime(year, month + 1, 1).date()
    
    available_dates = []
    current_date = month_first
    while current_date < next_month:
        if current_date >= today:
            # 檢查該日期是否所有時段都被預約
            date_str = current_date.strftime("%Y-%m-%d")
            booked_count = sum(1 for b in all_bookings 
                              if b['日期'] == date_str and b['狀態'] == 'confirmed')
            if booked_count < len(TIME_SLOTS):
                available_dates.append(current_date)
        current_date += timedelta(days=1)
    
    return available_dates

def get_available_slots(date_str):
    """取得某日期可預約的時段（排除已被預約的）"""
    sheet = get_google_sheet()
    bookings_sheet = sheet.worksheet("預約紀錄")
    all_bookings = bookings_sheet.get_all_records()
    
    booked_slots = [b['開始時間'] for b in all_bookings 
                    if b['日期'] == date_str and b['狀態'] == 'confirmed']
    
    available = [slot for slot in TIME_SLOTS if slot['start'] not in booked_slots]
    return available

def create_booking(order_id, date, start_time, end_time, name, phone):
    """建立預約記錄"""
    sheet = get_google_sheet()
    bookings_sheet = sheet.worksheet("預約紀錄")
    now = get_taiwan_now().strftime("%Y-%m-%d %H:%M:%S")
    
    bookings_sheet.append_row([
        order_id, date, start_time, end_time, name, phone, 'confirmed', now
    ])
    return True

def cancel_booking(order_id):
    """取消預約（將狀態改為 cancelled）"""
    sheet = get_google_sheet()
    bookings_sheet = sheet.worksheet("預約紀錄")
    records = bookings_sheet.get_all_records()
    
    for idx, record in enumerate(records, start=2):  # 第1行是標題
        if record['訂單編號'] == order_id and record['狀態'] == 'confirmed':
            bookings_sheet.update(f'G{idx}', 'cancelled')
            return True
    return False

def get_user_bookings(user_phone):
    """查詢使用者的預約紀錄"""
    sheet = get_google_sheet()
    bookings_sheet = sheet.worksheet("預約紀錄")
    records = bookings_sheet.get_all_records()
    
    user_bookings = [r for r in records if r['客戶電話'] == user_phone and r['狀態'] == 'confirmed']
    return user_bookings

def get_all_bookings():
    """取得所有有效預約（店家後台用）"""
    sheet = get_google_sheet()
    bookings_sheet = sheet.worksheet("預約紀錄")
    records = bookings_sheet.get_all_records()
    return [r for r in records if r['狀態'] == 'confirmed']

def get_statistics():
    """取得營業統計"""
    sheet = get_google_sheet()
    bookings_sheet = sheet.worksheet("預約紀錄")
    records = bookings_sheet.get_all_records()
    
    confirmed = [r for r in records if r['狀態'] == 'confirmed']
    cancelled = [r for r in records if r['狀態'] == 'cancelled']
    
    # 本月統計
    now = get_taiwan_now()
    this_month = now.strftime("%Y-%m")
    this_month_bookings = [r for r in confirmed if r['日期'].startswith(this_month)]
    
    return {
        "total": len(confirmed),
        "cancelled": len(cancelled),
        "this_month": len(this_month_bookings)
    }

# ========== Flex 訊息模板 ==========
def create_calendar_flex(year, month, available_dates):
    """產生月份日曆 Flex 卡片"""
    # 找出當月第一天是星期幾（0=星期一, 6=星期日）
    first_day = datetime(year, month, 1)
    start_weekday = first_day.weekday()  # 0=Monday
    
    # 計算該月天數
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    days_in_month = (next_month - datetime(year, month, 1)).days
    
    # 建立日期按鈕
    buttons = []
    # 星期標題
    weekdays = ['一', '二', '三', '四', '五', '六', '日']
    for wd in weekdays:
        buttons.append({
            "type": "text",
            "text": wd,
            "size": "sm",
            "color": "#aaaaaa",
            "align": "center"
        })
    
    # 補空白
    for _ in range(start_weekday):
        buttons.append({
            "type": "text",
            "text": " ",
            "size": "sm",
            "align": "center"
        })
    
    # 填入日期
    available_set = {d.day for d in available_dates}
    for day in range(1, days_in_month + 1):
        is_available = day in available_set
        buttons.append({
            "type": "button",
            "action": {
                "type": "postback",
                "label": str(day),
                "data": f"select_date={year}-{month:02d}-{day:02d}",
                "displayText": f"選擇 {year}/{month}/{day}"
            },
            "color": "#4CAF50" if is_available else "#cccccc",
            "style": "primary" if is_available else "secondary"
        })
    
    return FlexMessage(
        alt_text=f"{year}年{month}月 預約日曆",
        contents={
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": f"🗓️ {year}年{month}月",
                        "weight": "bold",
                        "size": "xl",
                        "align": "center"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "box",
                        "layout": "grid",
                        "contents": buttons,
                        "justifyContent": "flex-start",
                        "width": "100%"
                    }
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "⬅️ 上月",
                            "data": f"change_month={year},{month-1}"
                        },
                        "color": "#FF9800"
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "➡️ 下月",
                            "data": f"change_month={year},{month+1}"
                        },
                        "color": "#FF9800",
                        "margin": "sm"
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "🏠 主選單",
                            "data": "main_menu"
                        },
                        "color": "#666666",
                        "margin": "sm"
                    }
                ]
            }
        }
    )

def create_slots_flex(date_str, available_slots):
    """產生時段選擇 Flex 卡片"""
    buttons = []
    for slot in available_slots:
        buttons.append({
            "type": "button",
            "action": {
                "type": "postback",
                "label": f"{slot['start']} ~ {slot['end']}",
                "data": f"select_slot={date_str}|{slot['start']}|{slot['end']}"
            },
            "color": "#2196F3",
            "margin": "md"
        })
    
    buttons.append({
        "type": "button",
        "action": {
            "type": "postback",
            "label": "🔙 返回日曆",
            "data": "back_to_calendar"
        },
        "color": "#9E9E9E",
        "margin": "lg"
    })
    
    return FlexMessage(
        alt_text=f"{date_str} 可預約時段",
        contents={
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": f"📅 {date_str}",
                        "weight": "bold",
                        "size": "xl"
                    },
                    {
                        "type": "text",
                        "text": "請選擇時段",
                        "color": "#666666"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": buttons
            }
        }
    )

def create_booking_detail_flex(order_id, date, start_time, end_time, name, phone):
    """產生預約完成詳情 Flex 卡片"""
    return FlexMessage(
        alt_text="預約完成",
        contents={
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": "✅ 預約完成",
                        "weight": "bold",
                        "size": "xl",
                        "color": "#4CAF50",
                        "align": "center"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": f"📅 日期：{date}"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": f"⏰ 時間：{start_time} ~ {end_time}", "margin": "md"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": f"👤 姓名：{name}", "margin": "md"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": f"📞 電話：{phone}", "margin": "md"},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": f"🆔 訂單編號：{order_id}", "margin": "md", "size": "sm", "color": "#999999"}
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "📋 查詢我的預約",
                            "data": "my_bookings"
                        },
                        "color": "#2196F3"
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "🏠 主選單",
                            "data": "main_menu"
                        },
                        "color": "#666666",
                        "margin": "sm"
                    }
                ]
            }
        }
    )

def create_admin_menu_flex():
    """產生店家後台選單 Flex 卡片"""
    stats = get_statistics()
    
    return FlexMessage(
        alt_text="店家後台",
        contents={
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": "🔧 店家後台",
                        "weight": "bold",
                        "size": "xl",
                        "align": "center"
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": f"📊 營業統計",
                        "weight": "bold",
                        "size": "md"
                    },
                    {"type": "text", "text": f"總預約數：{stats['total']}"},
                    {"type": "text", "text": f"本月預約：{stats['this_month']}"},
                    {"type": "text", "text": f"已取消：{stats['cancelled']}"},
                    {"type": "separator", "margin": "md"},
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "📋 查看所有訂單",
                            "data": "admin_view_orders"
                        },
                        "color": "#FF9800",
                        "margin": "md"
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "❌ 取消訂單",
                            "data": "admin_cancel_order"
                        },
                        "color": "#F44336",
                        "margin": "md"
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "🏠 主選單",
                            "data": "main_menu"
                        },
                        "color": "#666666",
                        "margin": "md"
                    }
                ]
            }
        }
    )

# ========== LINE Webhook 處理 ==========
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return 'OK'

# 暫存使用者輸入（姓名/電話）
user_session = {}

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    
    # 主選單快速回覆
    if text == "主選單" or text == "開始" or text == "選單":
        quick_reply = QuickReply(
            items=[
                QuickReplyItem(action=MessageAction(label="📅 立即預約", text="我要預約")),
                QuickReplyItem(action=MessageAction(label="📋 查詢預約", text="查詢預約")),
                QuickReplyItem(action=MessageAction(label="❌ 取消預約", text="取消預約"))
            ]
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="歡迎使用頭薦骨調理預約系統，請選擇功能：", quick_reply=quick_reply)]
            )
        )
        return
    
    # 處理預約流程的姓名/電話輸入
    if user_id in user_session and user_session[user_id].get("step"):
        step = user_session[user_id]["step"]
        
        if step == "waiting_name":
            # 儲存姓名，詢問電話
            user_session[user_id]["name"] = text
            user_session[user_id]["step"] = "waiting_phone"
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請輸入您的電話號碼：")]
                )
            )
            return
        
        elif step == "waiting_phone":
            # 儲存電話，完成預約
            name = user_session[user_id]["name"]
            phone = text
            date = user_session[user_id]["date"]
            start_time = user_session[user_id]["start_time"]
            end_time = user_session[user_id]["end_time"]
            order_id = str(uuid.uuid4())[:8]
            
            create_booking(order_id, date, start_time, end_time, name, phone)
            
            # 清除 session
            del user_session[user_id]
            
            # 發送預約完成 Flex
            reply_flex = create_booking_detail_flex(order_id, date, start_time, end_time, name, phone)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[reply_flex]
                )
            )
            return
    
    # 一般功能選單
    if text == "我要預約":
        # 顯示當前月份日曆
        now = get_taiwan_now()
        year, month = now.year, now.month
        available_dates = get_available_dates(year, month)
        calendar_flex = create_calendar_flex(year, month, available_dates)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[calendar_flex]
            )
        )
    
    elif text == "查詢預約":
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請輸入您預約時留的電話號碼：")]
            )
        )
        user_session[user_id] = {"step": "query_phone"}
    
    elif text == "取消預約":
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請輸入您預約時留的電話號碼：")]
            )
        )
        user_session[user_id] = {"step": "cancel_phone"}
    
    elif user_id in user_session and user_session[user_id].get("step") == "query_phone":
        phone = text
        bookings = get_user_bookings(phone)
        if bookings:
            msg = "📋 您的預約紀錄：\n\n"
            for b in bookings:
                msg += f"📅 {b['日期']} {b['開始時間']}~{b['結束時間']}\n"
                msg += f"訂單編號：{b['訂單編號']}\n\n"
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=msg)]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="查無預約紀錄")]
                )
            )
        del user_session[user_id]
    
    elif user_id in user_session and user_session[user_id].get("step") == "cancel_phone":
        phone = text
        bookings = get_user_bookings(phone)
        if bookings:
            # 顯示可取消的訂單
            cancel_options = []
            for b in bookings:
                cancel_options.append({
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": f"{b['日期']} {b['開始時間']}",
                        "data": f"user_cancel={b['訂單編號']}"
                    }
                })
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請選擇要取消的訂單：")]
                )
            )
            # 實際應用應發送 Flex 讓使用者選擇
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="查無預約紀錄")]
                )
            )
        del user_session[user_id]
    
    # 店家後台（需檢查權限）
    elif text == "店家後台":
        sheet = get_google_sheet()
        admin_sheet = sheet.worksheet("店家後台")
        admins = admin_sheet.col_values(1)
        if user_id in admins:
            admin_flex = create_admin_menu_flex()
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[admin_flex]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="您無權限使用店家後台")]
                )
            )
    
    else:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請輸入「主選單」開始使用")]
            )
        )

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = event.postback.data
    
    # 月份切換
    if data.startswith("change_month="):
        _, ym = data.split("=")
        year, month = map(int, ym.split(","))
        if month < 1:
            month = 12
            year -= 1
        elif month > 12:
            month = 1
            year += 1
        available_dates = get_available_dates(year, month)
        calendar_flex = create_calendar_flex(year, month, available_dates)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[calendar_flex]
            )
        )
    
    # 選擇日期
    elif data.startswith("select_date="):
        date_str = data.split("=")[1]
        available_slots = get_available_slots(date_str)
        if available_slots:
            slots_flex = create_slots_flex(date_str, available_slots)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[slots_flex]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="該日期已無可預約時段，請選擇其他日期")]
                )
            )
    
    # 選擇時段
    elif data.startswith("select_slot="):
        _, info = data.split("=")
        date_str, start_time, end_time = info.split("|")
        user_session[user_id] = {
            "step": "waiting_name",
            "date": date_str,
            "start_time": start_time,
            "end_time": end_time
        }
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請輸入您的姓名：")]
            )
        )
    
    # 返回日曆
    elif data == "back_to_calendar":
        now = get_taiwan_now()
        year, month = now.year, now.month
        available_dates = get_available_dates(year, month)
        calendar_flex = create_calendar_flex(year, month, available_dates)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[calendar_flex]
            )
        )
    
    # 主選單
    elif data == "main_menu":
        quick_reply = QuickReply(
            items=[
                QuickReplyItem(action=MessageAction(label="📅 立即預約", text="我要預約")),
                QuickReplyItem(action=MessageAction(label="📋 查詢預約", text="查詢預約")),
                QuickReplyItem(action=MessageAction(label="❌ 取消預約", text="取消預約"))
            ]
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="歡迎使用頭薦骨調理預約系統，請選擇功能：", quick_reply=quick_reply)]
            )
        )
    
    # 店家後台 - 查看訂單
    elif data == "admin_view_orders":
        bookings = get_all_bookings()
        if bookings:
            msg = "📋 所有有效訂單：\n\n"
            for b in bookings:
                msg += f"📅 {b['日期']} {b['開始時間']}~{b['結束時間']}\n"
                msg += f"👤 {b['客戶姓名']} ({b['客戶電話']})\n"
                msg += f"🆔 {b['訂單編號']}\n\n"
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=msg)]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="目前無有效訂單")]
                )
            )
    
    # 店家後台 - 取消訂單
    elif data == "admin_cancel_order":
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請輸入要取消的訂單編號：")]
            )
        )
        user_session[user_id] = {"step": "admin_cancel"}
    
    elif user_id in user_session and user_session[user_id].get("step") == "admin_cancel":
        order_id = data  # 實際應從文字訊息取得
        if cancel_booking(order_id):
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"訂單 {order_id} 已取消")]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="訂單編號不存在或已取消")]
                )
            )
        del user_session[user_id]
    
    # 使用者自行取消
    elif data.startswith("user_cancel="):
        order_id = data.split("=")[1]
        if cancel_booking(order_id):
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"已取消預約 {order_id}")]
                )
            )
        else:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="取消失敗，請聯繫店家")]
                )
            )
    
    elif data == "my_bookings":
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請輸入您預約時留的電話號碼：")]
            )
        )
        user_session[user_id] = {"step": "query_phone"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
