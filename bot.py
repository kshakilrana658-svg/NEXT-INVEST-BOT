import os
import logging
import threading
import time
import json
import requests
from datetime import datetime, timedelta
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ======================= LOGGING =======================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======================= CONFIGURATION =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
FORCE_CHANNEL = os.environ.get("FORCE_CHANNEL", "")
FORCE_GROUP = os.environ.get("FORCE_GROUP", "")
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = os.environ.get("DB_NAME", "nextinvest")
DEFAULT_DEPOSIT_RATE = 130
DEFAULT_WITHDRAW_RATE = 128
SERVICE_CHARGE_BDT = 10
TRADING_APP_URL = os.environ.get("TRADING_APP_URL", "https://next-invest-six.vercel.app")

if not BOT_TOKEN or not OWNER_ID or not FORCE_CHANNEL or not FORCE_GROUP or not MONGO_URI:
    raise ValueError("Missing required environment variables")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ======================= MONGODB SETUP =======================
client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=False)
db = client[DB_NAME]

users_col = db["users"]
deposits_col = db["deposits"]
withdraws_col = db["withdraws"]
investments_col = db["investments"]
admins_col = db["admins"]
settings_col = db["settings"]
user_activity_col = db["user_activity"]

# Indexes
users_col.create_index("user_id", unique=True)
deposits_col.create_index("request_id", unique=True)
withdraws_col.create_index("request_id", unique=True)
user_activity_col.create_index("user_id", unique=True)

# Default settings
settings = settings_col.find_one({"_id": "global"})
if not settings:
    settings_col.insert_one({
        "_id": "global",
        "referral_bonus": 0.01,
        "deposit_enabled": True,
        "withdraw_enabled": True,
        "maintenance_mode": False,
        "deposit_rate": DEFAULT_DEPOSIT_RATE,
        "withdraw_rate": DEFAULT_WITHDRAW_RATE,
        "deposit_numbers": {
            "bkash": "01309924182",
            "nagad": "01309924182",
            "rocket": "01309924182",
            "trc20": "Your TRC20 Address",
            "erc20": "Your ERC20 Address",
            "bep20": "Your BEP20 Address",
            "btc": "Your BTC Address"
        },
        "min_withdraw_usd": 5,
        "max_withdraw_usd": 500,
        "daily_withdraw_limit": 1000,
        "min_deposit_usd": 5,
        "max_deposit_usd": 5000,
        "min_deposit_bdt": 100,
        "max_deposit_bdt": 50000,
        "support_contact": "dark_princes12",
        "trading_enabled": True,
        "min_trade_usd": 1,
        "max_trade_usd": 100,
        "trade_payout_multiplier": 1.5
    })
    logger.info("Default settings initialized.")

# Default plans
if investments_col.count_documents({"_id": "plans"}) == 0:
    investments_col.insert_one({
        "_id": "plans",
        "plans": {
            "basic": {"name": "Basic", "profit_percent": 20, "duration_days": 7, "min_amount": 10},
            "premium": {"name": "Premium", "profit_percent": 30, "duration_days": 14, "min_amount": 50},
            "gold": {"name": "Gold", "profit_percent": 40, "duration_days": 30, "min_amount": 100}
        }
    })
    logger.info("Default plans initialized.")

# ======================= HELPER FUNCTIONS =======================
def get_settings():
    return settings_col.find_one({"_id": "global"})

def update_settings(updates):
    settings_col.update_one({"_id": "global"}, {"$set": updates})
    logger.info(f"Settings updated: {updates}")

def get_plans():
    doc = investments_col.find_one({"_id": "plans"})
    return doc["plans"] if doc else {}

def update_plans(new_plans):
    investments_col.update_one({"_id": "plans"}, {"$set": {"plans": new_plans}})

def remove_plan(plan_id):
    plans = get_plans()
    if plan_id in plans:
        del plans[plan_id]
        update_plans(plans)
        return True
    return False

def get_user(user_id):
    return users_col.find_one({"user_id": user_id})

def create_user(user_id, username, first_name, ref_by=None):
    user_data = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "joined": datetime.utcnow(),
        "balance": 0.05,
        "referred_by": ref_by,
        "referrals": [],
        "transactions": [],
        "banned": False,
        "total_invested": 0.0,
        "total_profit": 0.0,
        "total_deposit": 0.0,
        "total_withdraw": 0.0,
        "trading": {
            "open_trades": [],
            "history": []
        }
    }
    try:
        users_col.insert_one(user_data)
        if ref_by and ref_by != user_id:
            ref_user = users_col.find_one({"user_id": ref_by})
            if ref_user:
                bonus = get_settings()["referral_bonus"]
                users_col.update_one(
                    {"user_id": ref_by},
                    {"$inc": {"balance": bonus},
                     "$push": {"referrals": user_id},
                     "$push": {"transactions": {
                         "type": "referral_bonus",
                         "amount": bonus,
                         "status": "completed",
                         "details": f"New user {user_id}",
                         "timestamp": datetime.utcnow()
                     }}}
                )
                logger.info(f"Referral bonus ${bonus} given to user {ref_by} for new user {user_id}")
        logger.info(f"New user created: {user_id}")
        return user_data
    except DuplicateKeyError:
        logger.warning(f"User {user_id} already exists.")
        return None

def update_balance(user_id, amount, operation="add"):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        return False
    current = user.get("balance", 0.0)
    new_balance = current + amount if operation == "add" else current - amount
    users_col.update_one({"user_id": user_id}, {"$set": {"balance": new_balance}})
    txn_type = "admin_add" if operation == "add" else "admin_remove"
    users_col.update_one(
        {"user_id": user_id},
        {"$push": {"transactions": {
            "type": txn_type,
            "amount": amount,
            "status": "completed",
            "details": f"Balance {'added' if operation == 'add' else 'removed'} by admin",
            "timestamp": datetime.utcnow()
        }}}
    )
    logger.info(f"Balance updated for {user_id}: {operation} ${amount} -> ${new_balance}")
    return new_balance

def add_transaction(user_id, txn_type, amount, status, details=""):
    users_col.update_one(
        {"user_id": user_id},
        {"$push": {"transactions": {
            "type": txn_type,
            "amount": amount,
            "status": status,
            "details": details,
            "timestamp": datetime.utcnow()
        }}}
    )
    if txn_type == "deposit" and status == "completed":
        users_col.update_one({"user_id": user_id}, {"$inc": {"total_deposit": amount}})
    elif txn_type == "withdraw" and status == "completed":
        users_col.update_one({"user_id": user_id}, {"$inc": {"total_withdraw": amount}})
    elif txn_type == "profit" and status == "completed":
        users_col.update_one({"user_id": user_id}, {"$inc": {"total_profit": amount}})
    elif txn_type == "investment" and status == "completed":
        users_col.update_one({"user_id": user_id}, {"$inc": {"total_invested": amount}})

def update_user_activity(user_id, action):
    now = datetime.utcnow()
    user_activity_col.update_one(
        {"user_id": user_id},
        {"$set": {"last_active": now}, "$inc": {f"counts.{action}": 1}, "$setOnInsert": {"first_seen": now}},
        upsert=True
    )

def is_admin(user_id):
    if user_id == OWNER_ID:
        return True
    return admins_col.find_one({"user_id": user_id}) is not None

def is_banned(user_id):
    user = users_col.find_one({"user_id": user_id})
    return user.get("banned", False) if user else False

def ban_user(user_id):
    users_col.update_one({"user_id": user_id}, {"$set": {"banned": True}})

def unban_user(user_id):
    users_col.update_one({"user_id": user_id}, {"$set": {"banned": False}})

def get_user_name(user_id):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        return f"User {user_id}"
    name = user.get("first_name", "")
    username = user.get("username", "")
    if username:
        return f"{name} (@{username})"
    return name

def mask_string(s, visible=4):
    if not s:
        return "****"
    if len(s) <= visible:
        return s
    return "*" * (len(s) - visible) + s[-visible:]

def mask_number(number):
    if not number or len(number) < 7:
        return "*******"
    return number[:3] + "*****" + number[-3:]

def check_daily_withdraw_limit(user_id, amount):
    settings = get_settings()
    daily_limit = settings.get("daily_withdraw_limit", 1000)
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    pipeline = [
        {"$match": {"user_id": user_id, "status": "approved", "timestamp": {"$gte": today_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount_usd"}}}
    ]
    result = list(withdraws_col.aggregate(pipeline))
    total_today = result[0]["total"] if result else 0
    if total_today + amount > daily_limit:
        return False, total_today
    return True, total_today

# ---------- Deposit ----------
def create_deposit_request(user_id, amount_usd, original_amount, original_unit, method, txid):
    request_id = f"{user_id}_{int(time.time())}"
    deposit = {
        "request_id": request_id,
        "user_id": user_id,
        "amount_usd": amount_usd,
        "original_amount": original_amount,
        "original_unit": original_unit,
        "txid": txid,
        "method": method,
        "status": "pending",
        "timestamp": datetime.utcnow()
    }
    deposits_col.insert_one(deposit)
    return request_id

def get_pending_deposits():
    return list(deposits_col.find({"status": "pending"}))

def approve_deposit(request_id):
    deposit = deposits_col.find_one({"request_id": request_id, "status": "pending"})
    if not deposit:
        return False, None
    users_col.update_one({"user_id": deposit["user_id"]}, {"$inc": {"balance": deposit["amount_usd"]}})
    add_transaction(deposit["user_id"], "deposit", deposit["amount_usd"], "completed", f"Deposit of ${deposit['amount_usd']} USD approved")
    deposits_col.update_one({"request_id": request_id}, {"$set": {"status": "approved"}})
    return True, deposit

def reject_deposit(request_id, reason):
    deposit = deposits_col.find_one({"request_id": request_id, "status": "pending"})
    if not deposit:
        return False, None
    deposits_col.update_one({"request_id": request_id}, {"$set": {"status": "rejected", "reason": reason}})
    return True, deposit

# ---------- Withdraw ----------
def create_withdraw_request(user_id, amount_usd, bdt_to_send, method, address):
    request_id = f"{user_id}_{int(time.time())}"
    withdraw = {
        "request_id": request_id,
        "user_id": user_id,
        "amount_usd": amount_usd,
        "bdt_to_send": bdt_to_send,
        "method": method,
        "address": address,
        "status": "pending",
        "timestamp": datetime.utcnow()
    }
    withdraws_col.insert_one(withdraw)
    return request_id

def get_pending_withdraws():
    return list(withdraws_col.find({"status": "pending"}))

def approve_withdraw(request_id):
    withdraw = withdraws_col.find_one({"request_id": request_id, "status": "pending"})
    if not withdraw:
        return False, None
    user = users_col.find_one({"user_id": withdraw["user_id"]})
    if user["balance"] < withdraw["amount_usd"]:
        return False, None
    users_col.update_one({"user_id": withdraw["user_id"]}, {"$inc": {"balance": -withdraw["amount_usd"]}})
    add_transaction(withdraw["user_id"], "withdraw", withdraw["amount_usd"], "completed", "Withdraw approved")
    withdraws_col.update_one({"request_id": request_id}, {"$set": {"status": "approved"}})
    return True, withdraw

def reject_withdraw(request_id, reason):
    withdraw = withdraws_col.find_one({"request_id": request_id, "status": "pending"})
    if not withdraw:
        return False, None
    withdraws_col.update_one({"request_id": request_id}, {"$set": {"status": "rejected", "reason": reason}})
    return True, withdraw

# ---------- Investment ----------
def add_investment(user_id, plan_id, amount):
    plans = get_plans()
    if plan_id not in plans:
        return False
    plan = plans[plan_id]
    if amount < plan["min_amount"]:
        return False
    user = users_col.find_one({"user_id": user_id})
    if user["balance"] < amount:
        return False
    users_col.update_one({"user_id": user_id}, {"$inc": {"balance": -amount}})
    add_transaction(user_id, "investment", amount, "completed", f"Invested in {plan['name']}")
    end_date = datetime.utcnow() + timedelta(days=plan["duration_days"])
    inv_doc = {
        "user_id": user_id,
        "plan_id": plan_id,
        "plan_name": plan["name"],
        "amount": amount,
        "profit_percent": plan["profit_percent"],
        "start_date": datetime.utcnow(),
        "end_date": end_date,
        "status": "active",
        "profit_added": False
    }
    investments_col.insert_one(inv_doc)
    return True

def process_auto_profit():
    while True:
        time.sleep(86400)
        logger.info("Running auto-profit check...")
        now = datetime.utcnow()
        active_invs = investments_col.find({"status": "active", "profit_added": False, "end_date": {"$lte": now}})
        for inv in active_invs:
            profit = inv["amount"] * (inv["profit_percent"] / 100)
            users_col.update_one({"user_id": inv["user_id"]}, {"$inc": {"balance": profit}})
            add_transaction(inv["user_id"], "profit", profit, "completed", f"Profit from {inv['plan_name']} investment")
            investments_col.update_one({"_id": inv["_id"]}, {"$set": {"status": "completed", "profit_added": True}})
            logger.info(f"Profit ${profit} added to user {inv['user_id']}")

threading.Thread(target=process_auto_profit, daemon=True).start()

# ---------- Trading ----------
def get_current_price(symbol):
    try:
        resp = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=5)
        if resp.status_code != 200:
            logger.warning(f"Binance price API returned {resp.status_code} for {symbol}")
            return None
        data = resp.json()
        if isinstance(data, dict) and 'price' in data:
            return float(data['price'])
        else:
            logger.warning(f"Unexpected response from Binance for {symbol}: {data}")
            return None
    except Exception as e:
        logger.error(f"Failed to get price for {symbol}: {e}")
        return None

def settle_expired_trades():
    while True:
        time.sleep(1)
        now = datetime.utcnow().timestamp() * 1000
        users = users_col.find({"trading.open_trades": {"$exists": True, "$ne": []}})
        for user in users:
            open_trades = user.get("trading", {}).get("open_trades", [])
            if not open_trades:
                continue
            for trade in open_trades:
                if trade["expiry_timestamp"] <= now:
                    symbol = trade["symbol"]
                    entry_price = trade["entry_price"]
                    direction = trade["direction"]
                    amount = trade["amount"]
                    current_price = get_current_price(symbol)
                    if current_price is None:
                        continue
                    win = False
                    if direction == "up" and current_price > entry_price:
                        win = True
                    elif direction == "down" and current_price < entry_price:
                        win = True
                    payout = 0
                    settings = get_settings()
                    multiplier = settings.get("trade_payout_multiplier", 1.5)
                    if win:
                        payout = amount * multiplier
                        users_col.update_one({"user_id": user["user_id"]}, {"$inc": {"balance": payout - amount}})
                        add_transaction(user["user_id"], "trade_win", payout - amount, "completed", f"Won trade on {symbol}")
                    else:
                        add_transaction(user["user_id"], "trade_loss", amount, "completed", f"Lost trade on {symbol}")
                    history_entry = {
                        "id": trade["id"],
                        "symbol": symbol,
                        "direction": "UP" if direction == "up" else "DOWN",
                        "amount": amount,
                        "result": "WIN" if win else "LOSS",
                        "payout": payout if win else 0,
                        "timestamp": datetime.utcnow()
                    }
                    users_col.update_one(
                        {"user_id": user["user_id"]},
                        {
                            "$pull": {"trading.open_trades": {"id": trade["id"]}},
                            "$push": {"trading.history": history_entry}
                        }
                    )
                    logger.info(f"Settled trade {trade['id']} for user {user['user_id']}: {'win' if win else 'loss'}")

threading.Thread(target=settle_expired_trades, daemon=True).start()

# ======================= FORCE JOIN CHECK =======================
def is_joined(user_id):
    try:
        member1 = bot.get_chat_member(FORCE_CHANNEL, user_id)
        member2 = bot.get_chat_member(FORCE_GROUP, user_id)
        return member1.status in ["member", "administrator", "creator"] and member2.status in ["member", "administrator", "creator"]
    except:
        return False

def ensure_joined(user_id, chat_id):
    if not is_joined(user_id):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_CHANNEL[1:]}"))
        markup.add(InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{FORCE_GROUP[1:]}"))
        markup.add(InlineKeyboardButton("✅ Verify", callback_data="verify"))
        bot.send_message(chat_id, "❌ You are not a member of our channel or group. Please join and click Verify to continue:", reply_markup=markup)
        return False
    return True

# ======================= MAIN MENU =======================
def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "📊 Trade Now",
        "📈 Investment Plans", "💰 Invest Now",
        "💳 My Wallet", "💸 Deposit Money",
        "💵 Withdraw Money", "📊 My Investments",
        "🏆 Profit History", "🤝 Referral Program",
        "📊 My Stats", "🏆 Leaderboard",
        "👤 My Profile", "📞 Support & Help"
    ]
    markup.add(*[KeyboardButton(b) for b in buttons])
    return markup

def welcome_message(first_name):
    return (
        f"🌟 <b>Welcome to NextInvest Bot, {first_name}!</b> 🌟\n\n"
        f"🎉 <b>Your Premium Investment Partner</b> 🎉\n\n"
        f"🔹 <b>What you can do:</b>\n"
        f"   ✅ Deposit BDT / Crypto → Get USD balance\n"
        f"   ✅ Invest in high‑profit plans\n"
        f"   ✅ Earn daily profits automatically\n"
        f"   ✅ Refer friends and earn bonuses\n"
        f"   ✅ Withdraw your earnings anytime\n"
        f"   ✅ Trade on the live market with binary options\n\n"
        f"📘 <b>How to get started:</b>\n"
        f"   1️⃣ Click <b>💸 Deposit Money</b> below\n"
        f"   2️⃣ Choose your preferred payment method\n"
        f"   3️⃣ Send the exact amount to the provided address/number\n"
        f"   4️⃣ Enter the transaction ID (TXID)\n"
        f"   5️⃣ Enter the amount you sent (BDT for fiat, USD for crypto)\n"
        f"   6️⃣ Confirm your deposit\n"
        f"   7️⃣ Admin will verify and credit your balance\n"
        f"   8️⃣ Once credited, click <b>💰 Invest Now</b> to start earning\n"
        f"   9️⃣ Or click <b>📊 Trade Now</b> to try binary trading\n\n"
        f"🎁 <b>Welcome Bonus:</b> $0.05 instantly!\n\n"
        f"👇 <b>Use the buttons below to begin</b> 👇"
    )

# ======================= COMMAND HANDLERS =======================
@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    update_user_activity(user_id, "start")
    settings = get_settings()
    if settings.get("maintenance_mode", False) and not is_admin(user_id):
        bot.reply_to(message, "🔧 Bot is under maintenance. Please try again later.")
        return
    if is_banned(user_id):
        bot.reply_to(message, "⛔ You are banned from using this bot.")
        return
    if not is_joined(user_id):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_CHANNEL[1:]}"))
        markup.add(InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{FORCE_GROUP[1:]}"))
        markup.add(InlineKeyboardButton("✅ Verify", callback_data="verify"))
        bot.reply_to(message, "❌ Please join our channel and group first:", reply_markup=markup)
        return

    user = get_user(user_id)
    if not user:
        ref_param = message.text.split()
        ref_by = None
        if len(ref_param) > 1 and ref_param[1].isdigit():
            ref_by = int(ref_param[1])
        user = create_user(user_id, message.from_user.username, message.from_user.first_name, ref_by)
        bot.reply_to(message, welcome_message(message.from_user.first_name), parse_mode="HTML")
    else:
        bot.reply_to(message, f"👋 <b>Welcome back, {user['first_name']}!</b>", parse_mode="HTML")

    bot.send_message(message.chat.id, "🔹 <b>Main Menu</b>", reply_markup=main_menu(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "verify")
def verify_cb(call):
    if is_joined(call.from_user.id):
        bot.edit_message_text("✅ Verified! Use /start again.", call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "Press /start", reply_markup=main_menu())
    else:
        bot.answer_callback_query(call.id, "Still not joined. Please join both.")

# ------------------- MAIN BUTTON HANDLERS -------------------
@bot.message_handler(func=lambda m: m.text == "📈 Investment Plans")
def plans_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    plans = get_plans()
    if not plans:
        bot.reply_to(m, "📭 No investment plans available at the moment.")
        return
    text = "📈 <b>📊 Investment Plans</b>\n\n"
    for pid, p in plans.items():
        text += f"🔹 <b>{p['name']}</b>\n"
        text += f"   💰 <b>Profit:</b> {p['profit_percent']}%\n"
        text += f"   ⏳ <b>Duration:</b> {p['duration_days']} days\n"
        text += f"   💵 <b>Minimum:</b> ${p['min_amount']}\n\n"
    bot.reply_to(m, text, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_plans")

@bot.message_handler(func=lambda m: m.text == "💰 Invest Now")
def invest_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    if not settings.get("deposit_enabled", True):
        bot.reply_to(m, "❌ Investment is currently disabled by admin.")
        return
    plans = get_plans()
    if not plans:
        bot.reply_to(m, "📭 No investment plans available. Please contact admin.")
        return
    markup = InlineKeyboardMarkup()
    for pid, p in plans.items():
        markup.add(InlineKeyboardButton(f"{p['name']} (${p['min_amount']} min)", callback_data=f"select_plan|{pid}"))
    bot.reply_to(m, "📊 <b>Select an investment plan:</b>", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_plan|"))
def select_plan_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    plan_id = call.data.split("|")[1]
    plans = get_plans()
    plan = plans.get(plan_id)
    if not plan:
        bot.answer_callback_query(call.id, "Plan not found.")
        return
    if not hasattr(bot, 'temp_invest'):
        bot.temp_invest = {}
    bot.temp_invest[call.from_user.id] = {"plan_id": plan_id, "plan_name": plan["name"], "min_amount": plan["min_amount"]}
    msg = bot.send_message(call.message.chat.id, f"🚀 You selected <b>{plan['name']}</b>.\n\nMinimum investment: <b>${plan['min_amount']}</b>\n\n💰 <b>Enter the amount you want to invest (in USD):</b>\n\n<i>Example: 100</i>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_invest_amount)
    bot.answer_callback_query(call.id)

def process_invest_amount(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        amount = float(m.text)
        user_id = m.from_user.id
        temp = bot.temp_invest.get(user_id)
        if not temp:
            bot.reply_to(m, "❌ Session expired. Please start investment again.")
            return
        plan_id = temp["plan_id"]
        min_amount = temp["min_amount"]
        if amount < min_amount:
            bot.reply_to(m, f"❌ Minimum investment for {temp['plan_name']} is ${min_amount}.")
            return
        user = get_user(user_id)
        if user["balance"] < amount:
            bot.reply_to(m, f"❌ Insufficient balance. Your current balance: ${user['balance']:.2f}.")
            return
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_invest|{plan_id}|{amount}"),
                   InlineKeyboardButton("❌ Cancel", callback_data="cancel_invest"))
        bot.reply_to(m, f"✅ You are about to invest <b>${amount}</b> in <b>{temp['plan_name']}</b>.\n\n📝 Please confirm:", reply_markup=markup, parse_mode="HTML")
    except:
        bot.reply_to(m, "❌ Invalid amount. Please enter a number.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_invest|"))
def confirm_invest_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    parts = call.data.split("|")
    plan_id = parts[1]
    amount = float(parts[2])
    user_id = call.from_user.id
    if add_investment(user_id, plan_id, amount):
        bot.answer_callback_query(call.id, "✅ Investment successful!")
        bot.edit_message_text("✅ Investment successful! Your balance has been updated.", call.message.chat.id, call.message.message_id)
        update_user_activity(user_id, "invest")
        if user_id in bot.temp_invest:
            del bot.temp_invest[user_id]
    else:
        bot.answer_callback_query(call.id, "❌ Investment failed.")
        bot.edit_message_text("❌ Investment failed. Check balance or try again.", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "cancel_invest")
def cancel_invest_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    bot.answer_callback_query(call.id, "Cancelled.")
    bot.edit_message_text("❌ Investment cancelled.", call.message.chat.id, call.message.message_id)
    if call.from_user.id in bot.temp_invest:
        del bot.temp_invest[call.from_user.id]

@bot.message_handler(func=lambda m: m.text == "💳 My Wallet")
def wallet_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    user = get_user(m.from_user.id)
    if not user:
        bot.reply_to(m, "❌ User not found. Use /start.")
        return
    bal = user.get("balance", 0.0)
    transactions = user.get("transactions", [])[-5:]
    text = f"💰 <b>My Wallet</b>\n\n<b>💰 Balance:</b> ${bal:.2f}\n\n<b>📜 Last 5 Transactions:</b>\n"
    for t in transactions[::-1]:
        text += f"   • {t['type']}: ${t['amount']} ({t['status']})\n"
    bot.reply_to(m, text, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_wallet")

# ------------------- DEPOSIT -------------------
@bot.message_handler(func=lambda m: m.text == "💸 Deposit Money")
def deposit_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    if not settings.get("deposit_enabled", True):
        bot.reply_to(m, "❌ Deposit is currently disabled by admin.")
        return
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("💳 Bkash", callback_data="deposit_method|bkash"),
               InlineKeyboardButton("💳 Nagad", callback_data="deposit_method|nagad"),
               InlineKeyboardButton("💳 Rocket", callback_data="deposit_method|rocket"))
    markup.add(InlineKeyboardButton("🪙 TRC20", callback_data="deposit_method|trc20"),
               InlineKeyboardButton("🪙 ERC20", callback_data="deposit_method|erc20"),
               InlineKeyboardButton("🪙 BEP20", callback_data="deposit_method|bep20"),
               InlineKeyboardButton("🪙 BTC", callback_data="deposit_method|btc"))
    bot.reply_to(m, "📱 <b>Select payment method:</b>", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("deposit_method|"))
def deposit_method_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    method = call.data.split("|")[1]
    settings = get_settings()
    numbers = settings.get("deposit_numbers", {})
    real_number = numbers.get(method, "Not set")
    if not hasattr(bot, 'temp_deposit'):
        bot.temp_deposit = {}
    bot.temp_deposit[call.from_user.id] = {"method": method, "real_number": real_number}

    if method in ["bkash", "nagad", "rocket"]:
        rate = settings.get("deposit_rate", DEFAULT_DEPOSIT_RATE)
        msg_text = (
            f"📱 <b>{method.capitalize()} Deposit</b>\n\n"
            f"💸 Send money to this number:\n<code>{real_number}</code>\n\n"
            f"💱 <b>Exchange Rate:</b> 1 USD = {rate} BDT\n\n"
            f"📝 <b>Steps:</b>\n"
            f"   1️⃣ Send the exact amount (in BDT) to the number above.\n"
            f"   2️⃣ After sending, tap <b>✅ Confirm</b>.\n"
            f"   3️⃣ You will be asked for the <b>TXID</b> and then the <b>amount in BDT</b> you sent.\n\n"
            f"🔁 <i>Need to choose another method?</i>"
        )
    else:
        msg_text = (
            f"🪙 <b>{method.upper()} Deposit</b>\n\n"
            f"📬 Send funds to this address:\n<code>{real_number}</code>\n\n"
            f"📝 <b>Steps:</b>\n"
            f"   1️⃣ Send the exact amount (in USD equivalent) to the address above.\n"
            f"   2️⃣ After sending, tap <b>✅ Confirm</b>.\n"
            f"   3️⃣ You will be asked for the <b>TXID</b> and then the <b>amount in USD</b> you sent.\n\n"
            f"🔁 <i>Need to choose another method?</i>"
        )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Confirm", callback_data="confirm_deposit_details"),
               InlineKeyboardButton("🔁 Back to methods", callback_data="back_to_deposit_methods"))
    bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "confirm_deposit_details")
def confirm_deposit_details_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    msg = bot.send_message(call.message.chat.id, "🔑 <b>Enter the transaction ID (TXID) of your payment:</b>\n\n<i>Example: 8A1B2C3D4E5F</i>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_deposit_txid)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_deposit_methods")
def back_to_deposit_methods_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("💳 Bkash", callback_data="deposit_method|bkash"),
               InlineKeyboardButton("💳 Nagad", callback_data="deposit_method|nagad"),
               InlineKeyboardButton("💳 Rocket", callback_data="deposit_method|rocket"))
    markup.add(InlineKeyboardButton("🪙 TRC20", callback_data="deposit_method|trc20"),
               InlineKeyboardButton("🪙 ERC20", callback_data="deposit_method|erc20"),
               InlineKeyboardButton("🪙 BEP20", callback_data="deposit_method|bep20"),
               InlineKeyboardButton("🪙 BTC", callback_data="deposit_method|btc"))
    bot.edit_message_text("📱 <b>Select payment method:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

def process_deposit_txid(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    txid = m.text.strip()
    if not txid:
        bot.reply_to(m, "❌ TXID cannot be empty. Please start deposit again.")
        return
    user_id = m.from_user.id
    if not hasattr(bot, 'temp_deposit') or user_id not in bot.temp_deposit:
        bot.reply_to(m, "❌ Session expired. Please start deposit again.")
        return
    bot.temp_deposit[user_id]["txid"] = txid
    method = bot.temp_deposit[user_id]["method"]
    if method in ["bkash", "nagad", "rocket"]:
        bot.reply_to(m, "💸 <b>Enter the amount in BDT you sent:</b>\n\n<i>Example: 5000</i>\n(You'll receive USD based on current rate)", parse_mode="HTML")
    else:
        bot.reply_to(m, "💸 <b>Enter the amount in USD you sent:</b>\n\n<i>Example: 100</i>\n(You'll receive the same amount in USD balance)", parse_mode="HTML")
    bot.register_next_step_handler(m, process_deposit_amount)

def process_deposit_amount(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        amount_input = float(m.text)
        if amount_input <= 0:
            raise ValueError
        user_id = m.from_user.id
        if user_id not in bot.temp_deposit:
            bot.reply_to(m, "❌ Session expired. Please start deposit again.")
            return
        method = bot.temp_deposit[user_id]["method"]
        settings = get_settings()
        if method in ["bkash", "nagad", "rocket"]:
            # amount_input is in BDT
            original_amount = amount_input
            original_unit = "BDT"
            rate = settings.get("deposit_rate", DEFAULT_DEPOSIT_RATE)
            usd_amount = amount_input / rate
            min_deposit_bdt = settings.get("min_deposit_bdt", 100)
            max_deposit_bdt = settings.get("max_deposit_bdt", 50000)
            if amount_input < min_deposit_bdt:
                bot.reply_to(m, f"❌ Minimum deposit amount is {min_deposit_bdt} BDT.")
                return
            if amount_input > max_deposit_bdt:
                bot.reply_to(m, f"❌ Maximum deposit amount is {max_deposit_bdt} BDT.")
                return
            confirm_msg = f"✅ You sent <b>{amount_input} BDT</b> → will receive <b>${usd_amount:.2f} USD</b> (1 USD = {rate} BDT).\n\nConfirm?"
        else:
            # amount_input is in USD
            original_amount = amount_input
            original_unit = "USD"
            usd_amount = amount_input
            min_deposit = settings.get("min_deposit_usd", 5)
            max_deposit = settings.get("max_deposit_usd", 5000)
            if usd_amount < min_deposit:
                bot.reply_to(m, f"❌ Minimum deposit amount is ${min_deposit}.")
                return
            if usd_amount > max_deposit:
                bot.reply_to(m, f"❌ Maximum deposit amount is ${max_deposit}.")
                return
            confirm_msg = f"✅ You are sending <b>${usd_amount}</b> via {method.upper()}.\n\nConfirm?"
        bot.temp_deposit[user_id]["original_amount"] = original_amount
        bot.temp_deposit[user_id]["original_unit"] = original_unit
        bot.temp_deposit[user_id]["amount_usd"] = usd_amount
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Confirm", callback_data="confirm_deposit"),
                   InlineKeyboardButton("❌ Cancel", callback_data="cancel_deposit"))
        bot.reply_to(m, confirm_msg, reply_markup=markup, parse_mode="HTML")
    except:
        bot.reply_to(m, "❌ Invalid amount. Please start deposit again.")
        if user_id in bot.temp_deposit:
            del bot.temp_deposit[user_id]

@bot.callback_query_handler(func=lambda call: call.data == "confirm_deposit")
def confirm_deposit_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    user_id = call.from_user.id
    data = bot.temp_deposit.get(user_id)
    if not data:
        bot.answer_callback_query(call.id, "Session expired. Please start again.")
        return
    method = data.get("method")
    txid = data.get("txid")
    amount_usd = data.get("amount_usd")
    original_amount = data.get("original_amount")
    original_unit = data.get("original_unit")
    if not method or not txid or not amount_usd or not original_amount or not original_unit:
        bot.answer_callback_query(call.id, "Missing data.")
        return
    req_id = create_deposit_request(user_id, amount_usd, original_amount, original_unit, method, txid)
    bot.answer_callback_query(call.id, "✅ Deposit request submitted!")
    bot.edit_message_text(f"✅ <b>Deposit request submitted!</b>\n\n💰 Amount: <b>{original_amount} {original_unit}</b> → ${amount_usd:.2f}\n🔑 TXID: <code>{txid}</code>\n💳 Method: {method.upper()}\n🆔 Request ID: <code>{req_id}</code>\n\n⏳ <b>Admin will review it shortly.</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML")
    update_user_activity(user_id, "deposit_request")
    del bot.temp_deposit[user_id]

@bot.callback_query_handler(func=lambda call: call.data == "cancel_deposit")
def cancel_deposit_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    bot.answer_callback_query(call.id, "Cancelled.")
    bot.edit_message_text("❌ Deposit cancelled.", call.message.chat.id, call.message.message_id)
    if call.from_user.id in bot.temp_deposit:
        del bot.temp_deposit[call.from_user.id]

# ------------------- WITHDRAW -------------------
@bot.message_handler(func=lambda m: m.text == "💵 Withdraw Money")
def withdraw_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    if not settings.get("withdraw_enabled", True):
        bot.reply_to(m, "❌ Withdrawal is currently disabled by admin.")
        return
    rate = settings.get("withdraw_rate", DEFAULT_WITHDRAW_RATE)
    min_withdraw = settings.get("min_withdraw_usd", 5)
    max_withdraw = settings.get("max_withdraw_usd", 500)
    info = (f"💱 <b>Withdraw Rate:</b> 1 USD = {rate} BDT (for fiat)\n"
            f"💰 <b>Service Charge:</b> {SERVICE_CHARGE_BDT} BDT per withdrawal (only for fiat)\n"
            f"📏 <b>Limits:</b> ${min_withdraw} - ${max_withdraw} USD per request\n")
    msg = bot.reply_to(m, info + "💸 <b>Enter amount in USD:</b>\n\n<i>Example: 50</i>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_withdraw_amount)

def process_withdraw_amount(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        amount = float(m.text)
        settings = get_settings()
        min_withdraw = settings.get("min_withdraw_usd", 5)
        max_withdraw = settings.get("max_withdraw_usd", 500)
        if amount < min_withdraw:
            bot.reply_to(m, f"❌ Minimum withdraw amount is ${min_withdraw}.")
            return
        if amount > max_withdraw:
            bot.reply_to(m, f"❌ Maximum withdraw amount is ${max_withdraw}.")
            return
        user = get_user(m.from_user.id)
        if user["balance"] < amount:
            bot.reply_to(m, f"❌ Insufficient balance. Your current balance: ${user['balance']:.2f}.")
            return
        within_limit, total_today = check_daily_withdraw_limit(m.from_user.id, amount)
        if not within_limit:
            bot.reply_to(m, f"❌ Daily withdraw limit reached. You have already withdrawn ${total_today:.2f} USD today. Limit is ${settings.get('daily_withdraw_limit', 1000)}.")
            return
        rate = settings.get("withdraw_rate", DEFAULT_WITHDRAW_RATE)
        bdt_to_send = amount * rate - SERVICE_CHARGE_BDT
        if bdt_to_send < 0:
            bdt_to_send = 0
        confirm_text = f"💸 You will receive approximately <b>{bdt_to_send:.2f} BDT</b> after charge.\n\nProceed?"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Proceed", callback_data=f"confirm_withdraw|{amount}|{bdt_to_send}"),
                   InlineKeyboardButton("❌ Cancel", callback_data="cancel_withdraw"))
        bot.reply_to(m, confirm_text, reply_markup=markup, parse_mode="HTML")
    except:
        bot.reply_to(m, "❌ Invalid amount. Use /start.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_withdraw|"))
def confirm_withdraw_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    parts = call.data.split("|")
    amount = float(parts[1])
    bdt_to_send = float(parts[2])
    if not hasattr(bot, 'temp_withdraw'):
        bot.temp_withdraw = {}
    bot.temp_withdraw[call.from_user.id] = {"amount": amount, "bdt_to_send": bdt_to_send}
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("💳 Bkash", callback_data="wd_method|bkash"),
               InlineKeyboardButton("💳 Nagad", callback_data="wd_method|nagad"),
               InlineKeyboardButton("💳 Rocket", callback_data="wd_method|rocket"))
    markup.add(InlineKeyboardButton("🪙 TRC20", callback_data="wd_method|trc20"),
               InlineKeyboardButton("🪙 ERC20", callback_data="wd_method|erc20"),
               InlineKeyboardButton("🪙 BEP20", callback_data="wd_method|bep20"),
               InlineKeyboardButton("🪙 BTC", callback_data="wd_method|btc"))
    bot.edit_message_text("📲 <b>Select withdrawal method:</b>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("wd_method|"))
def withdraw_method_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    method = call.data.split("|")[1]
    bot.temp_withdraw[call.from_user.id]["method"] = method
    if method in ["bkash", "nagad", "rocket"]:
        msg_text = f"📞 <b>Enter your {method.capitalize()} account number:</b>\n\n<i>Example: 01XXXXXXXXX</i>"
    else:
        msg_text = f"🪙 <b>Enter your {method.upper()} wallet address:</b>\n\n<i>Example: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa</i>"
    msg = bot.send_message(call.message.chat.id, msg_text, parse_mode="HTML")
    bot.register_next_step_handler(msg, process_withdraw_account)
    bot.answer_callback_query(call.id)

def process_withdraw_account(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    account = m.text.strip()
    if not account:
        bot.reply_to(m, "❌ Account/address cannot be empty.")
        return
    user_id = m.from_user.id
    temp = bot.temp_withdraw.get(user_id)
    if not temp:
        bot.reply_to(m, "❌ Session expired. Please start withdrawal again.")
        return
    amount = temp["amount"]
    bdt_to_send = temp["bdt_to_send"]
    method = temp["method"]
    req_id = create_withdraw_request(user_id, amount, bdt_to_send, method, account)
    bot.reply_to(m, f"✅ <b>Withdrawal request submitted!</b>\n\n💰 Amount: ${amount}\n🆔 Request ID: <code>{req_id}</code>\n\n⏳ <b>Admin will process it.</b>", parse_mode="HTML")
    update_user_activity(user_id, "withdraw_request")
    del bot.temp_withdraw[user_id]

@bot.callback_query_handler(func=lambda call: call.data == "cancel_withdraw")
def cancel_withdraw_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    bot.answer_callback_query(call.id, "Cancelled.")
    bot.edit_message_text("❌ Withdrawal cancelled.", call.message.chat.id, call.message.message_id)
    if call.from_user.id in bot.temp_withdraw:
        del bot.temp_withdraw[call.from_user.id]

@bot.message_handler(func=lambda m: m.text == "📊 My Investments")
def my_investments_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    invs = list(investments_col.find({"user_id": m.from_user.id}))
    if not invs:
        bot.reply_to(m, "📭 You have no investments.")
        return
    text = "📈 <b>My Investments</b>\n\n"
    for inv in invs:
        text += f"🔹 <b>{inv.get('plan_name', inv['plan_id'])}</b>\n"
        text += f"   💰 Amount: ${inv['amount']}\n"
        text += f"   📊 Status: {inv['status']}\n"
        if inv["status"] == "active":
            end = inv["end_date"].strftime("%Y-%m-%d")
            text += f"   ⏳ Ends: {end}\n"
        text += "\n"
    bot.reply_to(m, text, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_investments")

@bot.message_handler(func=lambda m: m.text == "🏆 Profit History")
def profit_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    user = get_user(m.from_user.id)
    profits = [t for t in user.get("transactions", []) if t["type"] == "profit"]
    if not profits:
        bot.reply_to(m, "📭 No profit history found.")
        return
    text = "🏆 <b>Profit History</b>\n\n"
    for p in profits[-5:]:
        text += f"   • ${p['amount']} on {p['timestamp'].strftime('%Y-%m-%d')}\n"
    bot.reply_to(m, text, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_profit")

@bot.message_handler(func=lambda m: m.text == "🤝 Referral Program")
def referral_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    bot_username = bot.get_me().username
    ref_link = f"https://t.me/{bot_username}?start={m.from_user.id}"
    user = get_user(m.from_user.id)
    referrals = user.get("referrals", [])
    settings = get_settings()
    bonus = settings.get("referral_bonus", 0.01)
    text = f"🔗 <b>Your Referral Link</b>\n\n<code>{ref_link}</code>\n\n👥 <b>Total referrals:</b> {len(referrals)}\n💰 <b>Earn ${bonus} per referral!</b>\n\n📤 <b>Share this link with your friends and earn rewards!</b>"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📤 Share Link", switch_inline_query=ref_link))
    bot.reply_to(m, text, reply_markup=markup, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_referral")

# ------------------- MY STATS & LEADERBOARD -------------------
@bot.message_handler(func=lambda m: m.text == "📊 My Stats")
def my_stats_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    user = get_user(m.from_user.id)
    if not user:
        bot.reply_to(m, "❌ User not found. Use /start.")
        return
    referrals = user.get("referrals", [])
    referral_count = len(referrals)
    bonus = get_settings().get("referral_bonus", 0.01)
    total_earned = referral_count * bonus
    text = (
        f"📊 <b>Your Personal Statistics</b>\n\n"
        f"👥 <b>Referrals:</b> {referral_count}\n"
        f"💰 <b>Referral Earnings:</b> ${total_earned:.2f}\n"
        f"💸 <b>Total Invested:</b> ${user.get('total_invested', 0):.2f}\n"
        f"🏆 <b>Total Profit:</b> ${user.get('total_profit', 0):.2f}\n"
        f"📥 <b>Total Deposit:</b> ${user.get('total_deposit', 0):.2f}\n"
        f"📤 <b>Total Withdraw:</b> ${user.get('total_withdraw', 0):.2f}\n\n"
        f"💡 <i>Invite more friends to increase your earnings!</i>"
    )
    bot.reply_to(m, text, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_stats")

@bot.message_handler(func=lambda m: m.text == "🏆 Leaderboard")
def leaderboard_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    pipeline = [
        {"$project": {"user_id": 1, "first_name": 1, "username": 1, "referral_count": {"$size": {"$ifNull": ["$referrals", []]}}}},
        {"$sort": {"referral_count": -1}},
        {"$limit": 10}
    ]
    top_users = list(users_col.aggregate(pipeline))
    if not top_users:
        bot.reply_to(m, "🏆 <b>Leaderboard</b>\n\nNo referrals yet. Be the first to invite friends!")
        return
    text = "🏆 <b>Top Referrers</b>\n\n"
    for idx, u in enumerate(top_users, 1):
        name = u.get("first_name", f"User {u['user_id']}")
        if u.get("username"):
            name += f" (@{u['username']})"
        count = u.get("referral_count", 0)
        text += f"{idx}. {name} – <b>{count}</b> referrals\n"
    bot.reply_to(m, text, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_leaderboard")

# ------------------- TRADE NOW -------------------
@bot.message_handler(func=lambda m: m.text == "📊 Trade Now")
def trade_now_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    if not settings.get("trading_enabled", True):
        bot.reply_to(m, "❌ Trading is currently disabled by admin.")
        return
    web_app_url = f"{TRADING_APP_URL}?user_id={m.from_user.id}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🚀 Open Trading Platform", web_app=WebAppInfo(web_app_url)))
    bot.reply_to(m, "📊 <b>Trade Now</b>\n\nClick the button below to open the trading platform.", reply_markup=markup, parse_mode="HTML")

# ------------------- PROFILE & SUPPORT -------------------
@bot.message_handler(func=lambda m: m.text == "👤 My Profile")
def profile_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    user = get_user(m.from_user.id)
    if not user:
        bot.reply_to(m, "❌ User not found. Use /start.")
        return
    bal = user.get("balance", 0.0)
    referrals = user.get("referrals", [])
    total_deposit = user.get("total_deposit", 0.0)
    total_withdraw = user.get("total_withdraw", 0.0)
    total_invested = user.get("total_invested", 0.0)
    total_profit = user.get("total_profit", 0.0)
    text = f"👤 <b>My Profile</b>\n\n"
    text += f"📛 <b>Name:</b> {user.get('first_name', 'N/A')}\n"
    if user.get("username"):
        text += f"🔖 <b>Username:</b> @{user['username']}\n"
    text += f"🆔 <b>ID:</b> <code>{m.from_user.id}</code>\n"
    text += f"💰 <b>Balance:</b> ${bal:.2f}\n"
    text += f"📥 <b>Total Deposit:</b> ${total_deposit:.2f}\n"
    text += f"📤 <b>Total Withdraw:</b> ${total_withdraw:.2f}\n"
    text += f"💸 <b>Total Invested:</b> ${total_invested:.2f}\n"
    text += f"🏆 <b>Total Profit:</b> ${total_profit:.2f}\n"
    text += f"👥 <b>Referrals:</b> {len(referrals)}\n"
    text += f"📅 <b>Joined:</b> {user.get('joined', datetime.utcnow()).strftime('%Y-%m-%d')}"
    bot.reply_to(m, text, parse_mode="HTML")
    update_user_activity(m.from_user.id, "view_profile")

@bot.message_handler(func=lambda m: m.text == "📞 Support & Help")
def support_btn(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    contact = settings.get("support_contact", "dark_princes12")
    text = f"📞 <b>Support & Help</b>\n\nFor any assistance, please contact:\n👑 <b>Support</b>: @{contact}\n📢 Channel: {FORCE_CHANNEL}\n👥 Group: {FORCE_GROUP}\n\nWe're here to help! 💙"
    bot.reply_to(m, text, parse_mode="HTML")

# ======================= ADMIN PANEL =======================
def admin_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "👥 Users", "💰 Balance",
        "📥 Deposit", "📤 Withdraw",
        "📊 Stats", "📢 Broadcast",
        "📦 Plans", "🛑 Ban",
        "🔓 Unban User", "📝 Update Plans",
        "🗑 Remove Plan", "📊 Analytics",
        "👑 Add Admin", "🗑 Remove Admin",
        "💸 Referral Control", "⚙ System Settings",
        "💱 Set Deposit Rate", "💱 Set Withdraw Rate",
        "📞 Set Payment Details", "📞 Set Support Contact",
        "⚙ Trade Control", "🔙 User Menu"
    ]
    markup.add(*[KeyboardButton(b) for b in buttons])
    return markup

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized.")
        return
    bot.reply_to(message, "🔧 <b>Admin Panel</b>", reply_markup=admin_menu(), parse_mode="HTML")
    update_user_activity(message.from_user.id, "admin_panel")

@bot.message_handler(func=lambda m: m.text == "🔙 User Menu" and is_admin(m.from_user.id))
def back_to_user_menu(m):
    bot.send_message(m.chat.id, "🔹 <b>Main Menu</b>", reply_markup=main_menu(), parse_mode="HTML")

# ---------- Admin Handlers ----------
@bot.message_handler(func=lambda m: m.text == "👥 Users" and is_admin(m.from_user.id))
def admin_users(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    users = list(users_col.find().limit(10))
    total = users_col.count_documents({})
    text = f"👥 <b>Total Users:</b> {total}\n\n<b>First 10 Users:</b>\n"
    for u in users:
        text += f"• <code>{u['user_id']}</code> – {u.get('first_name', 'N/A')} (${u.get('balance',0)})\n"
    bot.reply_to(m, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💰 Balance" and is_admin(m.from_user.id))
def admin_balance(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    msg = bot.reply_to(m, "💸 <b>Balance Control</b>\n\nSend: <code>user_id amount</code> to add, or <code>user_id -amount</code> to remove.\n\nExample: <code>123456 10</code> or <code>123456 -10</code>", parse_mode="HTML")
    bot.register_next_step_handler(msg, balance_admin)

def balance_admin(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        parts = m.text.split()
        uid = int(parts[0])
        amt = float(parts[1])
        if amt > 0:
            update_balance(uid, amt, "add")
            msg = f"✅ Added ${amt} to user <code>{uid}</code>"
        else:
            update_balance(uid, abs(amt), "subtract")
            msg = f"✅ Removed ${abs(amt)} from user <code>{uid}</code>"
        bot.reply_to(m, msg, parse_mode="HTML")
    except:
        bot.reply_to(m, "❌ Invalid format. Use: user_id amount")

@bot.message_handler(func=lambda m: m.text == "📥 Deposit" and is_admin(m.from_user.id))
def admin_deposits(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    pending = get_pending_deposits()
    if not pending:
        bot.reply_to(m, "📭 No pending deposits.")
        return
    by_method = {}
    for dep in pending:
        method = dep.get("method", "unknown")
        by_method.setdefault(method, []).append(dep)
    for method, deps in by_method.items():
        bot.send_message(m.chat.id, f"📥 <b>Deposits - {method.upper()}</b>", parse_mode="HTML")
        for dep in deps:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_dep|{dep['request_id']}"),
                       InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_dep|{dep['request_id']}"))
            if dep["original_unit"] == "BDT":
                amount_display = f"{dep['original_amount']} BDT (≈ ${dep['amount_usd']:.2f} USD)"
            else:
                amount_display = f"${dep['original_amount']} USD"
            bot.send_message(m.chat.id,
                             f"📥 <b>Deposit Request</b>\n👤 User: <code>{dep['user_id']}</code>\n💰 Amount: <b>{amount_display}</b>\n🔑 TXID: <code>{dep['txid']}</code>\n💳 Method: {dep.get('method', 'N/A').upper()}",
                             reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📤 Withdraw" and is_admin(m.from_user.id))
def admin_withdraws(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    pending = get_pending_withdraws()
    if not pending:
        bot.reply_to(m, "📭 No pending withdrawals.")
        return
    by_method = {}
    for wd in pending:
        method = wd.get("method", "unknown")
        by_method.setdefault(method, []).append(wd)
    for method, wds in by_method.items():
        bot.send_message(m.chat.id, f"📤 <b>Withdrawals - {method.upper()}</b>", parse_mode="HTML")
        for wd in wds:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_wd|{wd['request_id']}"),
                       InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_wd|{wd['request_id']}"))
            bot.send_message(m.chat.id,
                             f"📤 <b>Withdraw Request</b>\n👤 User: <code>{wd['user_id']}</code>\n💰 Amount: <b>${wd['amount_usd']} USD → BDT to send: {wd['bdt_to_send']:.2f} BDT</b>\n💳 Method: {wd['method'].upper()}\n📞 Address: <code>{wd['address']}</code>",
                             reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📊 Stats" and is_admin(m.from_user.id))
def admin_stats(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    total_users = users_col.count_documents({})
    total_balance = sum(u.get("balance", 0) for u in users_col.find())
    total_invested = sum(inv["amount"] for inv in investments_col.find({"status": "active"}))
    text = f"📊 <b>Statistics</b>\n\n👥 Users: <b>{total_users}</b>\n💰 Total Balance: <b>${total_balance:.2f}</b>\n💸 Total Invested: <b>${total_invested:.2f}</b>"
    bot.reply_to(m, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_admin(m.from_user.id))
def admin_broadcast(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    msg = bot.reply_to(m, "📢 <b>Broadcast Message</b>\n\nSend the message you want to broadcast to all users:", parse_mode="HTML")
    bot.register_next_step_handler(msg, broadcast_msg)

def broadcast_msg(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    text = m.text
    count = 0
    for user in users_col.find():
        try:
            bot.send_message(user["user_id"], text)
            count += 1
        except:
            pass
    bot.reply_to(m, f"✅ Broadcast sent to <b>{count}</b> users.", parse_mode="HTML")
    logger.info(f"Broadcast sent to {count} users by admin {m.from_user.id}")

@bot.message_handler(func=lambda m: m.text == "📦 Plans" and is_admin(m.from_user.id))
def admin_plans(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    plans = get_plans()
    text = "📦 <b>Current Investment Plans</b>\n\n"
    for pid, p in plans.items():
        text += f"🔹 <b>{p['name']}</b> (<code>{pid}</code>)\n"
        text += f"   Profit: {p['profit_percent']}%\n"
        text += f"   Duration: {p['duration_days']} days\n"
        text += f"   Minimum: ${p['min_amount']}\n\n"
    bot.reply_to(m, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🛑 Ban" and is_admin(m.from_user.id))
def admin_ban(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    msg = bot.reply_to(m, "🚫 <b>Ban User</b>\n\nEnter user ID to ban:", parse_mode="HTML")
    bot.register_next_step_handler(msg, ban_user_cmd)

def ban_user_cmd(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        uid = int(m.text)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Yes, ban", callback_data=f"confirm_ban|{uid}"),
                   InlineKeyboardButton("❌ No", callback_data="cancel_ban"))
        bot.reply_to(m, f"⚠️ Are you sure you want to ban user <code>{uid}</code>?", reply_markup=markup, parse_mode="HTML")
    except:
        bot.reply_to(m, "❌ Invalid user ID.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_ban|"))
def confirm_ban_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    uid = int(call.data.split("|")[1])
    ban_user(uid)
    bot.answer_callback_query(call.id, "✅ User banned.")
    bot.edit_message_text(f"✅ User <code>{uid}</code> has been banned.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
    logger.info(f"User {uid} banned by admin {call.from_user.id}")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_ban")
def cancel_ban_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    bot.answer_callback_query(call.id, "Cancelled.")
    bot.edit_message_text("❌ Ban cancelled.", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda m: m.text == "🔓 Unban User" and is_admin(m.from_user.id))
def admin_unban(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    msg = bot.reply_to(m, "🔓 <b>Unban User</b>\n\nEnter user ID to unban:", parse_mode="HTML")
    bot.register_next_step_handler(msg, unban_user_cmd)

def unban_user_cmd(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        uid = int(m.text)
        unban_user(uid)
        bot.reply_to(m, f"✅ User <code>{uid}</code> has been unbanned.", parse_mode="HTML")
    except:
        bot.reply_to(m, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "📝 Update Plans" and is_admin(m.from_user.id))
def admin_update_plans(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    plans = get_plans()
    text = "📝 <b>Update Investment Plans</b>\n\n"
    for pid, p in plans.items():
        text += f"<b>{pid}</b>: {p['name']} | {p['profit_percent']}% | {p['duration_days']}d | ${p['min_amount']}\n"
    text += "\nEnter new plan details in format:\n<code>plan_id name profit% duration_days min_amount</code>\n\nExample: <code>basic Basic 20 7 10</code>"
    msg = bot.reply_to(m, text, parse_mode="HTML")
    bot.register_next_step_handler(msg, process_plan_update)

def process_plan_update(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        parts = m.text.split()
        if len(parts) != 5:
            raise ValueError
        pid = parts[0].lower()
        name = parts[1]
        profit = float(parts[2])
        days = int(parts[3])
        min_amt = float(parts[4])
        plans = get_plans()
        plans[pid] = {
            "name": name,
            "profit_percent": profit,
            "duration_days": days,
            "min_amount": min_amt
        }
        update_plans(plans)
        bot.reply_to(m, f"✅ Plan <code>{pid}</code> updated successfully!", parse_mode="HTML")
        logger.info(f"Plan {pid} updated by admin {m.from_user.id}")
    except Exception as e:
        logger.error(f"Plan update error: {e}")
        bot.reply_to(m, "❌ Invalid format. Use: plan_id name profit% duration_days min_amount")

@bot.message_handler(func=lambda m: m.text == "🗑 Remove Plan" and is_admin(m.from_user.id))
def admin_remove_plan(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    plans = get_plans()
    if not plans:
        bot.reply_to(m, "📭 No plans to remove.")
        return
    plan_list = "\n".join([f"<code>{pid}</code>: {p['name']}" for pid, p in plans.items()])
    msg = bot.reply_to(m, f"🗑 <b>Remove a Plan</b>\n\nCurrent plans:\n{plan_list}\n\nEnter the <b>plan ID</b> to remove:", parse_mode="HTML")
    bot.register_next_step_handler(msg, confirm_plan_removal)

def confirm_plan_removal(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    plan_id = m.text.strip().lower()
    plans = get_plans()
    if plan_id not in plans:
        bot.reply_to(m, "❌ Plan ID not found.")
        return
    plan_name = plans[plan_id]["name"]
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, remove", callback_data=f"confirm_remove_plan|{plan_id}"),
               InlineKeyboardButton("❌ No", callback_data="cancel_remove_plan"))
    bot.reply_to(m, f"⚠️ Are you sure you want to remove plan <b>{plan_name}</b> (<code>{plan_id}</code>)?\n\nExisting investments will keep the plan name but new investments cannot use it.", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_remove_plan|"))
def confirm_remove_plan_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    plan_id = call.data.split("|")[1]
    if remove_plan(plan_id):
        bot.answer_callback_query(call.id, "✅ Plan removed.")
        bot.edit_message_text(f"✅ Plan <code>{plan_id}</code> has been removed.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
        logger.info(f"Plan {plan_id} removed by admin {call.from_user.id}")
    else:
        bot.answer_callback_query(call.id, "❌ Failed to remove plan.")

@bot.callback_query_handler(func=lambda call: call.data == "cancel_remove_plan")
def cancel_remove_plan_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    bot.answer_callback_query(call.id, "Removal cancelled.")
    bot.edit_message_text("✅ Removal cancelled.", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda m: m.text == "📊 Analytics" and is_admin(m.from_user.id))
def admin_analytics(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        total_users = users_col.count_documents({})

        deposit_pipeline = [{"$match": {"status": "approved"}}, {"$group": {"_id": None, "total": {"$sum": "$amount_usd"}}}]
        deposit_result = list(deposits_col.aggregate(deposit_pipeline))
        total_deposits = deposit_result[0]["total"] if deposit_result else 0.0

        withdraw_pipeline = [{"$match": {"status": "approved"}}, {"$group": {"_id": None, "total": {"$sum": "$amount_usd"}}}]
        withdraw_result = list(withdraws_col.aggregate(withdraw_pipeline))
        total_withdraws = withdraw_result[0]["total"] if withdraw_result else 0.0

        active_investments = investments_col.count_documents({"status": "active"})

        text = (
            "📊 <b>Analytics</b>\n\n"
            f"👥 Total Users: <b>{total_users}</b>\n"
            f"💰 Total Deposits (USD): <b>${total_deposits:.2f}</b>\n"
            f"💸 Total Withdraws (USD): <b>${total_withdraws:.2f}</b>\n"
            f"📈 Active Investments: <b>{active_investments}</b>"
        )
        bot.reply_to(m, text, parse_mode="HTML")
        logger.info(f"Admin {m.from_user.id} viewed analytics.")
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        bot.reply_to(m, "❌ An error occurred while fetching analytics. Please check the logs.")

@bot.message_handler(func=lambda m: m.text == "👑 Add Admin" and m.from_user.id == OWNER_ID)
def admin_add_admin(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    msg = bot.reply_to(m, "👑 <b>Add Admin</b>\n\nEnter user ID to add as admin:", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_admin)

def add_admin(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        uid = int(m.text)
        if admins_col.find_one({"user_id": uid}):
            bot.reply_to(m, f"❌ User <code>{uid}</code> is already an admin.", parse_mode="HTML")
            return
        admins_col.insert_one({"user_id": uid})
        bot.reply_to(m, f"✅ User <code>{uid}</code> is now an admin.", parse_mode="HTML")
        logger.info(f"Admin {uid} added by owner {m.from_user.id}")
    except:
        bot.reply_to(m, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "🗑 Remove Admin" and m.from_user.id == OWNER_ID)
def admin_remove_admin(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    msg = bot.reply_to(m, "🗑 <b>Remove Admin</b>\n\nEnter user ID to remove from admin:", parse_mode="HTML")
    bot.register_next_step_handler(msg, remove_admin)

def remove_admin(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        uid = int(m.text)
        if uid == OWNER_ID:
            bot.reply_to(m, "❌ Cannot remove the owner.")
            return
        result = admins_col.delete_one({"user_id": uid})
        if result.deleted_count:
            bot.reply_to(m, f"✅ User <code>{uid}</code> is no longer an admin.", parse_mode="HTML")
            logger.info(f"Admin {uid} removed by owner {m.from_user.id}")
        else:
            bot.reply_to(m, f"❌ User <code>{uid}</code> is not an admin.", parse_mode="HTML")
    except:
        bot.reply_to(m, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "💸 Referral Control" and is_admin(m.from_user.id))
def admin_referral_control(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    current = settings.get("referral_bonus", 0.01)
    msg = bot.reply_to(m, f"💸 <b>Referral Bonus Control</b>\n\nCurrent bonus: <b>${current}</b>\n\nSend new bonus amount (e.g., 0.02):", parse_mode="HTML")
    bot.register_next_step_handler(msg, set_referral_bonus)

def set_referral_bonus(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        new_bonus = float(m.text)
        if new_bonus <= 0:
            raise ValueError
        update_settings({"referral_bonus": new_bonus})
        bot.reply_to(m, f"✅ Referral bonus updated to <b>${new_bonus}</b>.", parse_mode="HTML")
        logger.info(f"Referral bonus set to {new_bonus} by admin {m.from_user.id}")
    except:
        bot.reply_to(m, "❌ Invalid amount. Please send a number > 0.")

@bot.message_handler(func=lambda m: m.text == "⚙ System Settings" and is_admin(m.from_user.id))
def admin_system_settings(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    text = (
        "⚙ <b>System Settings</b>\n\n"
        f"💳 Deposit: {'✅ Enabled' if settings.get('deposit_enabled', True) else '❌ Disabled'}\n"
        f"💸 Withdraw: {'✅ Enabled' if settings.get('withdraw_enabled', True) else '❌ Disabled'}\n"
        f"🔧 Maintenance: {'🔧 ON' if settings.get('maintenance_mode', False) else '✅ OFF'}\n\n"
        "Use buttons below to toggle:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Toggle Deposit", callback_data="sys_toggle_deposit"),
        InlineKeyboardButton("Toggle Withdraw", callback_data="sys_toggle_withdraw"),
        InlineKeyboardButton("Toggle Maintenance", callback_data="sys_toggle_maintenance")
    )
    bot.reply_to(m, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sys_toggle_"))
def sys_toggle_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    action = call.data.split("_")[2]
    settings = get_settings()
    if action == "deposit":
        settings["deposit_enabled"] = not settings.get("deposit_enabled", True)
        update_settings({"deposit_enabled": settings["deposit_enabled"]})
        msg = "Deposit is now " + ("✅ enabled" if settings["deposit_enabled"] else "❌ disabled")
    elif action == "withdraw":
        settings["withdraw_enabled"] = not settings.get("withdraw_enabled", True)
        update_settings({"withdraw_enabled": settings["withdraw_enabled"]})
        msg = "Withdraw is now " + ("✅ enabled" if settings["withdraw_enabled"] else "❌ disabled")
    elif action == "maintenance":
        settings["maintenance_mode"] = not settings.get("maintenance_mode", False)
        update_settings({"maintenance_mode": settings["maintenance_mode"]})
        msg = "Maintenance mode is now " + ("🔧 ON" if settings["maintenance_mode"] else "✅ OFF")
    else:
        msg = "Unknown action"
    bot.answer_callback_query(call.id, msg)
    bot.edit_message_text("✅ Settings updated. Use /admin again to see changes.", call.message.chat.id, call.message.message_id)
    logger.info(f"System setting toggled by admin {call.from_user.id}: {msg}")

@bot.message_handler(func=lambda m: m.text == "💱 Set Deposit Rate" and is_admin(m.from_user.id))
def admin_set_deposit_rate(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    current = get_settings().get("deposit_rate", DEFAULT_DEPOSIT_RATE)
    msg = bot.reply_to(m, f"💱 <b>Set Deposit Rate</b>\n\nCurrent: 1 USD = {current} BDT\n\nEnter new rate (e.g., 130):", parse_mode="HTML")
    bot.register_next_step_handler(msg, set_deposit_rate)

def set_deposit_rate(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        new_rate = int(m.text)
        if new_rate <= 0:
            raise ValueError
        update_settings({"deposit_rate": new_rate})
        bot.reply_to(m, f"✅ Deposit rate updated: 1 USD = {new_rate} BDT", parse_mode="HTML")
        logger.info(f"Deposit rate set to {new_rate} by admin {m.from_user.id}")
    except:
        bot.reply_to(m, "❌ Invalid rate. Please enter a positive integer.")

@bot.message_handler(func=lambda m: m.text == "💱 Set Withdraw Rate" and is_admin(m.from_user.id))
def admin_set_withdraw_rate(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    current = get_settings().get("withdraw_rate", DEFAULT_WITHDRAW_RATE)
    msg = bot.reply_to(m, f"💱 <b>Set Withdraw Rate</b>\n\nCurrent: 1 USD = {current} BDT\n\nEnter new rate (e.g., 128):", parse_mode="HTML")
    bot.register_next_step_handler(msg, set_withdraw_rate)

def set_withdraw_rate(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        new_rate = int(m.text)
        if new_rate <= 0:
            raise ValueError
        update_settings({"withdraw_rate": new_rate})
        bot.reply_to(m, f"✅ Withdraw rate updated: 1 USD = {new_rate} BDT", parse_mode="HTML")
        logger.info(f"Withdraw rate set to {new_rate} by admin {m.from_user.id}")
    except:
        bot.reply_to(m, "❌ Invalid rate. Please enter a positive integer.")

@bot.message_handler(func=lambda m: m.text == "📞 Set Payment Details" and is_admin(m.from_user.id))
def admin_set_deposit_numbers(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    current = get_settings().get("deposit_numbers", {})
    text = "📞 <b>Set Payment Details</b>\n\n"
    text += f"💳 Bkash: {current.get('bkash', 'Not set')}\n"
    text += f"💳 Nagad: {current.get('nagad', 'Not set')}\n"
    text += f"💳 Rocket: {current.get('rocket', 'Not set')}\n"
    text += f"🪙 TRC20: {current.get('trc20', 'Not set')}\n"
    text += f"🪙 ERC20: {current.get('erc20', 'Not set')}\n"
    text += f"🪙 BEP20: {current.get('bep20', 'Not set')}\n"
    text += f"🪙 BTC: {current.get('btc', 'Not set')}\n\n"
    text += "Send new address/number in format:\n<code>method:value</code>\n\nExample: <code>bkash:01309924182</code> or <code>trc20:TXxx...xxx</code>"
    msg = bot.reply_to(m, text, parse_mode="HTML")
    bot.register_next_step_handler(msg, process_deposit_numbers)

def process_deposit_numbers(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        parts = m.text.split(":")
        if len(parts) != 2:
            raise ValueError
        method = parts[0].lower()
        value = parts[1].strip()
        allowed_methods = ["bkash", "nagad", "rocket", "trc20", "erc20", "bep20", "btc"]
        if method not in allowed_methods:
            bot.reply_to(m, f"❌ Invalid method. Use: {', '.join(allowed_methods)}")
            return
        settings = get_settings()
        numbers = settings.get("deposit_numbers", {})
        numbers[method] = value
        update_settings({"deposit_numbers": numbers})
        bot.reply_to(m, f"✅ {method.upper()} updated to <code>{value}</code>", parse_mode="HTML")
        logger.info(f"Deposit {method} updated to {value} by admin {m.from_user.id}")
    except:
        bot.reply_to(m, "❌ Invalid format. Use: method:value")

@bot.message_handler(func=lambda m: m.text == "📞 Set Support Contact" and is_admin(m.from_user.id))
def admin_set_support_contact(m):
    current = get_settings().get("support_contact", "dark_princes12")
    msg = bot.reply_to(m, f"📞 <b>Set Support Contact</b>\n\nCurrent support username: @{current}\n\nEnter new username (without @):", parse_mode="HTML")
    bot.register_next_step_handler(msg, set_support_contact)

def set_support_contact(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    new_contact = m.text.strip().replace("@", "")
    if not new_contact:
        bot.reply_to(m, "❌ Username cannot be empty.")
        return
    update_settings({"support_contact": new_contact})
    bot.reply_to(m, f"✅ Support contact updated to @{new_contact}.", parse_mode="HTML")
    logger.info(f"Support contact changed to {new_contact} by admin {m.from_user.id}")

@bot.message_handler(func=lambda m: m.text == "⚙ Trade Control" and is_admin(m.from_user.id))
def admin_trade_control(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    settings = get_settings()
    current_enabled = settings.get("trading_enabled", True)
    current_min = settings.get("min_trade_usd", 1)
    current_max = settings.get("max_trade_usd", 100)
    current_multiplier = settings.get("trade_payout_multiplier", 1.5)
    text = (
        "⚙ <b>Trading Control</b>\n\n"
        f"Status: {'✅ Enabled' if current_enabled else '❌ Disabled'}\n"
        f"Min Trade: ${current_min}\n"
        f"Max Trade: ${current_max}\n"
        f"Payout Multiplier: {current_multiplier}x (profit {int((current_multiplier-1)*100)}%)\n\n"
        "Use buttons below to toggle or change settings:"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Toggle Enable", callback_data="trade_toggle"),
        InlineKeyboardButton("Set Min Trade", callback_data="trade_set_min"),
        InlineKeyboardButton("Set Max Trade", callback_data="trade_set_max"),
        InlineKeyboardButton("Set Multiplier", callback_data="trade_set_multiplier")
    )
    bot.reply_to(m, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "trade_toggle")
def trade_toggle_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    settings = get_settings()
    new_state = not settings.get("trading_enabled", True)
    update_settings({"trading_enabled": new_state})
    bot.answer_callback_query(call.id, f"Trading {'enabled' if new_state else 'disabled'}")
    bot.edit_message_text(f"✅ Trading is now {'enabled' if new_state else 'disabled'}.", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "trade_set_min")
def trade_set_min_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    msg = bot.send_message(call.message.chat.id, "📝 Enter the minimum trade amount in USD (e.g., 1):")
    bot.register_next_step_handler(msg, set_trade_min)

def set_trade_min(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        min_val = float(m.text)
        if min_val <= 0:
            raise ValueError
        update_settings({"min_trade_usd": min_val})
        bot.reply_to(m, f"✅ Minimum trade amount set to ${min_val}.")
    except:
        bot.reply_to(m, "❌ Invalid amount. Please enter a positive number.")

@bot.callback_query_handler(func=lambda call: call.data == "trade_set_max")
def trade_set_max_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    msg = bot.send_message(call.message.chat.id, "📝 Enter the maximum trade amount in USD (e.g., 100):")
    bot.register_next_step_handler(msg, set_trade_max)

def set_trade_max(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        max_val = float(m.text)
        if max_val <= 0:
            raise ValueError
        update_settings({"max_trade_usd": max_val})
        bot.reply_to(m, f"✅ Maximum trade amount set to ${max_val}.")
    except:
        bot.reply_to(m, "❌ Invalid amount. Please enter a positive number.")

@bot.callback_query_handler(func=lambda call: call.data == "trade_set_multiplier")
def trade_set_multiplier_cb(call):
    if not ensure_joined(call.from_user.id, call.message.chat.id):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    msg = bot.send_message(call.message.chat.id, "📝 Enter the payout multiplier (e.g., 1.5 for 50% profit):")
    bot.register_next_step_handler(msg, set_trade_multiplier)

def set_trade_multiplier(m):
    if not ensure_joined(m.from_user.id, m.chat.id):
        return
    try:
        mult = float(m.text)
        if mult <= 1:
            raise ValueError
        update_settings({"trade_payout_multiplier": mult})
        bot.reply_to(m, f"✅ Payout multiplier set to {mult}x (profit {int((mult-1)*100)}%).")
    except:
        bot.reply_to(m, "❌ Invalid multiplier. Must be > 1.")

# ======================= FLASK API (CORS enabled) =======================
flask_app = Flask(__name__)
CORS(flask_app)  # Allow all origins

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return "Bot is running!", 200

@flask_app.route('/trading/api/settings')
def api_settings():
    settings = get_settings()
    return jsonify({
        "min_trade": settings.get("min_trade_usd", 1),
        "max_trade": settings.get("max_trade_usd", 100),
        "payout_multiplier": settings.get("trade_payout_multiplier", 1.5)
    })

@flask_app.route('/trading/api/balance')
def api_balance():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    user = get_user(int(user_id))
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"balance": user.get("balance", 0.0)})

@flask_app.route('/trading/api/open_trades')
def api_open_trades():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    user = get_user(int(user_id))
    if not user:
        return jsonify({"error": "User not found"}), 404
    open_trades = user.get("trading", {}).get("open_trades", [])
    return jsonify(open_trades)

@flask_app.route('/trading/api/history')
def api_history():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    user = get_user(int(user_id))
    if not user:
        return jsonify({"error": "User not found"}), 404
    history = user.get("trading", {}).get("history", [])
    formatted = []
    for h in history:
        formatted.append({
            "symbol": h["symbol"],
            "direction": h["direction"],
            "amount": h["amount"],
            "result": h["result"],
            "time": h["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(h["timestamp"], datetime) else h["timestamp"]
        })
    return jsonify(formatted)

@flask_app.route('/trading/api/place_trade', methods=['POST'])
def api_place_trade():
    data = request.get_json()
    user_id = data.get('user_id')
    symbol = data.get('symbol')
    direction = data.get('direction')
    amount = float(data.get('amount'))
    entry_price = float(data.get('entry_price'))
    expiry_seconds = int(data.get('expiry_seconds'))

    if not user_id or not symbol or not direction or not amount or not entry_price or not expiry_seconds:
        return jsonify({"error": "Missing parameters"}), 400

    user = get_user(int(user_id))
    if not user:
        return jsonify({"error": "User not found"}), 404

    settings = get_settings()
    if not settings.get("trading_enabled", True):
        return jsonify({"error": "Trading is disabled by admin"}), 403

    min_trade = settings.get("min_trade_usd", 1)
    max_trade = settings.get("max_trade_usd", 100)
    if amount < min_trade or amount > max_trade:
        return jsonify({"error": f"Trade amount must be between ${min_trade} and ${max_trade}"}), 400

    if user["balance"] < amount:
        return jsonify({"error": "Insufficient balance"}), 400

    new_balance = user["balance"] - amount
    users_col.update_one({"user_id": user["user_id"]}, {"$set": {"balance": new_balance}})

    trade_id = int(time.time() * 1000)
    trade = {
        "id": trade_id,
        "symbol": symbol,
        "direction": direction,
        "amount": amount,
        "entry_price": entry_price,
        "expiry_timestamp": int((datetime.utcnow().timestamp() + expiry_seconds) * 1000),
        "created_at": datetime.utcnow()
    }

    users_col.update_one(
        {"user_id": user["user_id"]},
        {"$push": {"trading.open_trades": trade}}
    )

    return jsonify({"success": True, "new_balance": new_balance, "trade_id": trade_id})

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ======================= START BOT =======================
if __name__ == "__main__":
    logger.info("Bot started...")
    time.sleep(5)
    bot.infinity_polling()