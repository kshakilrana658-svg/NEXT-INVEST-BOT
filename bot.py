import os
import logging
import threading
import time
from datetime import datetime, timedelta
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from flask import Flask
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
DEPOSIT_NUMBER = os.environ.get("DEPOSIT_NUMBER", "01309924182")

# Rates
DEPOSIT_RATE_USD_TO_BDT = 130   # 1 USD = 130 BDT
WITHDRAW_RATE_USD_TO_BDT = 128  # 1 USD = 128 BDT (for display)
WITHDRAW_SERVICE_CHARGE_BDT = 10

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

# Indexes
users_col.create_index("user_id", unique=True)
deposits_col.create_index("request_id", unique=True)
withdraws_col.create_index("request_id", unique=True)

# Default settings
settings = settings_col.find_one({"_id": "global"})
if not settings:
    settings_col.insert_one({
        "_id": "global",
        "referral_bonus": 0.01,
        "deposit_enabled": True,
        "withdraw_enabled": True,
        "maintenance_mode": False
    })

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

# ======================= HELPER FUNCTIONS =======================
def get_settings():
    return settings_col.find_one({"_id": "global"})

def update_settings(updates):
    settings_col.update_one({"_id": "global"}, {"$set": updates})

def get_plans():
    doc = investments_col.find_one({"_id": "plans"})
    return doc["plans"] if doc else {}

def update_plans(new_plans):
    investments_col.update_one({"_id": "plans"}, {"$set": {"plans": new_plans}})

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
        "banned": False
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
        return user_data
    except DuplicateKeyError:
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

# ---------- Deposit ----------
def create_deposit_request(user_id, amount_bdt, txid):
    request_id = f"{user_id}_{int(time.time())}"
    deposit = {
        "request_id": request_id,
        "user_id": user_id,
        "amount_bdt": amount_bdt,
        "txid": txid,
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
    usd_amount = deposit["amount_bdt"] / DEPOSIT_RATE_USD_TO_BDT
    users_col.update_one({"user_id": deposit["user_id"]}, {"$inc": {"balance": usd_amount}})
    add_transaction(deposit["user_id"], "deposit", usd_amount, "completed", f"Deposit of {deposit['amount_bdt']} BDT approved")
    deposits_col.update_one({"request_id": request_id}, {"$set": {"status": "approved"}})
    return True, deposit

def reject_deposit(request_id, reason):
    deposit = deposits_col.find_one({"request_id": request_id, "status": "pending"})
    if not deposit:
        return False, None
    deposits_col.update_one({"request_id": request_id}, {"$set": {"status": "rejected", "reason": reason}})
    return True, deposit

# ---------- Withdraw ----------
def create_withdraw_request(user_id, amount_usd, method, account):
    request_id = f"{user_id}_{int(time.time())}"
    withdraw = {
        "request_id": request_id,
        "user_id": user_id,
        "amount_usd": amount_usd,
        "method": method,
        "account": account,
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
        "amount": amount,
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
        logger.info("Checking investments for profit...")
        now = datetime.utcnow()
        active_invs = investments_col.find({"status": "active", "profit_added": False})
        for inv in active_invs:
            if now >= inv["end_date"]:
                plans = get_plans()
                plan = plans.get(inv["plan_id"])
                if plan:
                    profit = inv["amount"] * (plan["profit_percent"] / 100)
                    users_col.update_one({"user_id": inv["user_id"]}, {"$inc": {"balance": profit}})
                    add_transaction(inv["user_id"], "profit", profit, "completed", f"Profit from {plan['name']} investment")
                    investments_col.update_one({"_id": inv["_id"]}, {"$set": {"status": "completed", "profit_added": True}})

threading.Thread(target=process_auto_profit, daemon=True).start()

# ======================= FORCE JOIN CHECK =======================
def is_joined(user_id):
    try:
        member1 = bot.get_chat_member(FORCE_CHANNEL, user_id)
        member2 = bot.get_chat_member(FORCE_GROUP, user_id)
        return member1.status in ["member", "administrator", "creator"] and member2.status in ["member", "administrator", "creator"]
    except:
        return False

# ======================= MAIN MENU =======================
def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "📊 Investment Plans", "🚀 Invest Now",
        "💰 My Wallet", "💳 Deposit Money",
        "💵 Withdraw Money", "📈 My Investments",
        "💸 Profit History", "🤝 Referral Program",
        "👤 My Profile", "📩 Support & Help"
    ]
    markup.add(*[KeyboardButton(b) for b in buttons])
    return markup

def welcome_message(first_name):
    return (
        f"🌟 <b>Welcome to NextInvest Bot, {first_name}!</b> 🌟\n\n"
        f"🎉 <b>Your Premium Investment Partner</b> 🎉\n\n"
        f"🔹 <b>Features:</b>\n"
        f"   ✅ Deposit BDT → Get USD Balance\n"
        f"   ✅ Invest & Earn Daily Profit\n"
        f"   ✅ Refer Friends & Earn Bonus\n"
        f"   ✅ Fast Withdrawals\n\n"
        f"💡 <b>How to Start:</b>\n"
        f"   1️⃣ Click <b>💳 Deposit Money</b> below\n"
        f"   2️⃣ Enter TXID and amount (BDT)\n"
        f"   3️⃣ Admin approves → Balance added\n"
        f"   4️⃣ Click <b>🚀 Invest Now</b> to grow your capital\n\n"
        f"🎁 <b>Welcome Bonus:</b> $0.05 instantly!\n\n"
        f"👇 <b>Use the buttons below to begin</b> 👇"
    )

# ======================= COMMAND HANDLERS =======================
@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    settings = get_settings()
    if settings.get("maintenance_mode", False) and not is_admin(user_id):
        bot.send_message(message.chat.id, "🔧 Bot is under maintenance. Please try again later.")
        return
    if is_banned(user_id):
        bot.send_message(message.chat.id, "⛔ You are banned from using this bot.")
        return
    if not is_joined(user_id):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{FORCE_CHANNEL[1:]}"))
        markup.add(InlineKeyboardButton("👥 Join Group", url=f"https://t.me/{FORCE_GROUP[1:]}"))
        markup.add(InlineKeyboardButton("✅ Verify", callback_data="verify"))
        bot.send_message(message.chat.id, "❌ Please join our channel and group first:", reply_markup=markup)
        return

    user = get_user(user_id)
    if not user:
        ref_param = message.text.split()
        ref_by = None
        if len(ref_param) > 1 and ref_param[1].isdigit():
            ref_by = int(ref_param[1])
        user = create_user(user_id, message.from_user.username, message.from_user.first_name, ref_by)
        bot.send_message(message.chat.id, welcome_message(message.from_user.first_name), parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, f"👋 <b>Welcome back, {user['first_name']}!</b>", parse_mode="HTML")

    bot.send_message(message.chat.id, "🔹 <b>Main Menu</b>", reply_markup=main_menu(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "verify")
def verify_cb(call):
    if is_joined(call.from_user.id):
        bot.edit_message_text("✅ Verified! Use /start again.", call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "Press /start", reply_markup=main_menu())
    else:
        bot.answer_callback_query(call.id, "Still not joined. Please join both.")

# ------------------- MAIN BUTTON HANDLERS -------------------
@bot.message_handler(func=lambda m: m.text == "📊 Investment Plans")
def plans_btn(m):
    plans = get_plans()
    text = "📈 <b>📊 Investment Plans</b>\n\n"
    for pid, p in plans.items():
        text += f"🔹 <b>{p['name']}</b>\n"
        text += f"   💰 <b>Profit:</b> {p['profit_percent']}%\n"
        text += f"   ⏳ <b>Duration:</b> {p['duration_days']} days\n"
        text += f"   💵 <b>Minimum:</b> ${p['min_amount']}\n\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🚀 Invest Now")
def invest_btn(m):
    settings = get_settings()
    if not settings.get("deposit_enabled", True):
        bot.send_message(m.chat.id, "❌ Investment is currently disabled by admin.")
        return
    plans = get_plans()
    plan_list = "\n".join([f"{pid}: {p['name']} (min ${p['min_amount']}, {p['profit_percent']}%)" for pid, p in plans.items()])
    msg = bot.send_message(m.chat.id, f"🚀 <b>Send investment in format:</b>\n<code>&lt;plan_id&gt; &lt;amount&gt;</code>\n\n📋 <b>Available plans:</b>\n{plan_list}\n\n📝 <b>Example:</b> <code>basic 50</code>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_invest)

def process_invest(m):
    try:
        parts = m.text.split()
        if len(parts) != 2:
            raise ValueError
        plan_id = parts[0].lower()
        amount = float(parts[1])
        plans = get_plans()
        if plan_id not in plans:
            bot.send_message(m.chat.id, "❌ Invalid plan ID. Use basic, premium or gold.")
            return
        plan = plans[plan_id]
        if amount < plan["min_amount"]:
            bot.send_message(m.chat.id, f"❌ Minimum investment for {plan['name']} is ${plan['min_amount']}.")
            return
        if add_investment(m.from_user.id, plan_id, amount):
            bot.send_message(m.chat.id, f"✅ <b>Investment of ${amount} in {plan['name']} successful!</b>")
        else:
            bot.send_message(m.chat.id, "❌ Investment failed. Check balance or try again.")
    except Exception as e:
        logger.error(f"Invest error: {e}")
        bot.send_message(m.chat.id, "❌ Invalid format. Use: plan_id amount")

@bot.message_handler(func=lambda m: m.text == "💰 My Wallet")
def wallet_btn(m):
    user = get_user(m.from_user.id)
    if not user:
        bot.send_message(m.chat.id, "❌ User not found. Use /start.")
        return
    bal = user.get("balance", 0.0)
    transactions = user.get("transactions", [])[-5:]
    text = f"💰 <b>My Wallet</b>\n\n<b>Balance:</b> ${bal:.2f}\n\n<b>📜 Last 5 Transactions:</b>\n"
    for t in transactions[::-1]:
        text += f"   • {t['type']}: ${t['amount']} ({t['status']})\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💳 Deposit Money")
def deposit_btn(m):
    settings = get_settings()
    if not settings.get("deposit_enabled", True):
        bot.send_message(m.chat.id, "❌ Deposit is currently disabled by admin.")
        return
    info = f"💱 <b>Deposit Rate:</b> 1 USD = {DEPOSIT_RATE_USD_TO_BDT} BDT\n"
    msg = bot.send_message(m.chat.id, info + f"📱 <b>Send Money / Cash In</b>\nNumber: <code>{DEPOSIT_NUMBER}</code>\n\nAfter sending, <b>enter the TXID</b>:", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_deposit_txid)

def process_deposit_txid(m):
    txid = m.text.strip()
    if not txid:
        bot.send_message(m.chat.id, "❌ TXID cannot be empty. Please start deposit again.")
        return
    if not hasattr(bot, 'temp_deposit'):
        bot.temp_deposit = {}
    bot.temp_deposit[m.from_user.id] = {"txid": txid}
    bot.send_message(m.chat.id, "💸 <b>Enter the amount in BDT you sent:</b>\n(You'll receive USD = amount / 130)", parse_mode="HTML")
    bot.register_next_step_handler(m, process_deposit_amount)

def process_deposit_amount(m):
    try:
        amount_bdt = float(m.text)
        if amount_bdt <= 0:
            raise ValueError
        usd_amount = amount_bdt / DEPOSIT_RATE_USD_TO_BDT
        confirm = f"✅ You sent <b>{amount_bdt} BDT</b> → will receive <b>${usd_amount:.2f} USD</b>.\n\nConfirm? (yes/no)"
        msg = bot.send_message(m.chat.id, confirm, parse_mode="HTML")
        bot.temp_deposit[m.from_user.id]["amount_bdt"] = amount_bdt
        bot.register_next_step_handler(msg, lambda m2: confirm_deposit(m2, m.from_user.id))
    except:
        bot.send_message(m.chat.id, "❌ Invalid amount. Please start deposit again.")
        if hasattr(bot, 'temp_deposit') and m.from_user.id in bot.temp_deposit:
            del bot.temp_deposit[m.from_user.id]

def confirm_deposit(m, user_id):
    if m.text.lower() in ["yes", "y", "হ্যাঁ"]:
        txid = bot.temp_deposit.get(user_id, {}).get("txid")
        amount_bdt = bot.temp_deposit.get(user_id, {}).get("amount_bdt")
        if not txid or not amount_bdt:
            bot.send_message(m.chat.id, "❌ Missing data. Please start deposit again.")
            return
        req_id = create_deposit_request(user_id, amount_bdt, txid)
        bot.send_message(m.chat.id, f"✅ <b>Deposit request submitted!</b>\n\n💰 Amount: <b>{amount_bdt} BDT</b>\n🔑 TXID: <code>{txid}</code>\n🆔 Request ID: <code>{req_id}</code>\n\n⏳ <b>Admin will review it shortly.</b>", parse_mode="HTML")
        del bot.temp_deposit[user_id]
    else:
        bot.send_message(m.chat.id, "❌ Deposit cancelled.")
        if hasattr(bot, 'temp_deposit') and user_id in bot.temp_deposit:
            del bot.temp_deposit[user_id]

@bot.message_handler(func=lambda m: m.text == "💵 Withdraw Money")
def withdraw_btn(m):
    settings = get_settings()
    if not settings.get("withdraw_enabled", True):
        bot.send_message(m.chat.id, "❌ Withdrawal is currently disabled by admin.")
        return
    info = f"💱 <b>Withdraw Rate:</b> 1 USD = {WITHDRAW_RATE_USD_TO_BDT} BDT\n💰 <b>Service Charge:</b> {WITHDRAW_SERVICE_CHARGE_BDT} BDT per withdrawal\n"
    msg = bot.send_message(m.chat.id, info + "💸 <b>Enter amount in USD (min $5):</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_withdraw_amount)

def process_withdraw_amount(m):
    try:
        amount = float(m.text)
        if amount < 5:
            bot.send_message(m.chat.id, "❌ Minimum withdraw amount is $5.")
            return
        user = get_user(m.from_user.id)
        if user["balance"] < amount:
            bot.send_message(m.chat.id, "❌ Insufficient balance.")
            return
        estimated_bdt = amount * WITHDRAW_RATE_USD_TO_BDT - WITHDRAW_SERVICE_CHARGE_BDT
        if estimated_bdt < 0:
            estimated_bdt = 0
        confirm_text = f"💸 You will receive approximately <b>{estimated_bdt:.2f} BDT</b> after charge.\n\nProceed? (yes/no)"
        msg = bot.send_message(m.chat.id, confirm_text, parse_mode="HTML")
        bot.register_next_step_handler(msg, lambda m2: confirm_withdraw(m2, amount))
    except:
        bot.send_message(m.chat.id, "❌ Invalid amount. Use /start.")

def confirm_withdraw(m, amount):
    if m.text.lower() in ["yes", "y", "হ্যাঁ"]:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 Bkash", callback_data=f"wd_method|bkash|{amount}"))
        markup.add(InlineKeyboardButton("💳 Nagad", callback_data=f"wd_method|nagad|{amount}"))
        markup.add(InlineKeyboardButton("💳 Rocket", callback_data=f"wd_method|rocket|{amount}"))
        bot.send_message(m.chat.id, "📲 <b>Select withdrawal method:</b>", reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(m.chat.id, "❌ Withdrawal cancelled.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("wd_method|"))
def withdraw_method_cb(call):
    parts = call.data.split("|")
    method = parts[1]
    amount = float(parts[2])
    msg = bot.send_message(call.message.chat.id, f"📞 <b>Enter your {method.capitalize()} account number:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, lambda m: process_withdraw_account(m, amount, method, call.message.chat.id))

def process_withdraw_account(m, amount, method, original_chat_id):
    account = m.text.strip()
    req_id = create_withdraw_request(m.from_user.id, amount, method, account)
    bot.send_message(m.chat.id, f"✅ <b>Withdrawal request submitted!</b>\n\n💰 Amount: ${amount}\n🆔 Request ID: <code>{req_id}</code>\n\n⏳ <b>Admin will process it.</b>", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📈 My Investments")
def my_investments_btn(m):
    invs = list(investments_col.find({"user_id": m.from_user.id}))
    if not invs:
        bot.send_message(m.chat.id, "📭 You have no investments.")
        return
    text = "📈 <b>My Investments</b>\n\n"
    for inv in invs:
        plans = get_plans()
        plan = plans.get(inv["plan_id"], {"name": inv["plan_id"]})
        text += f"🔹 <b>{plan['name']}</b>\n"
        text += f"   💰 Amount: ${inv['amount']}\n"
        text += f"   📊 Status: {inv['status']}\n"
        if inv["status"] == "active":
            end = inv["end_date"].strftime("%Y-%m-%d")
            text += f"   ⏳ Ends: {end}\n"
        text += "\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💸 Profit History")
def profit_btn(m):
    user = get_user(m.from_user.id)
    profits = [t for t in user.get("transactions", []) if t["type"] == "profit"]
    if not profits:
        bot.send_message(m.chat.id, "📭 No profit history found.")
        return
    text = "💸 <b>Profit History</b>\n\n"
    for p in profits[-5:]:
        text += f"   • ${p['amount']} on {p['timestamp'].strftime('%Y-%m-%d')}\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🤝 Referral Program")
def referral_btn(m):
    bot_username = bot.get_me().username
    ref_link = f"https://t.me/{bot_username}?start={m.from_user.id}"
    user = get_user(m.from_user.id)
    referrals = user.get("referrals", [])
    settings = get_settings()
    bonus = settings.get("referral_bonus", 0.01)
    text = f"🔗 <b>Your Referral Link</b>\n\n<code>{ref_link}</code>\n\n👥 <b>Total referrals:</b> {len(referrals)}\n💰 <b>Earn ${bonus} per referral!</b>"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📤 Share Link", switch_inline_query=ref_link))
    bot.send_message(m.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "👤 My Profile")
def profile_btn(m):
    user = get_user(m.from_user.id)
    if not user:
        bot.send_message(m.chat.id, "❌ User not found. Use /start.")
        return
    bal = user.get("balance", 0.0)
    referrals = user.get("referrals", [])
    text = f"👤 <b>My Profile</b>\n\n"
    text += f"📛 <b>Name:</b> {user.get('first_name', 'N/A')}\n"
    if user.get("username"):
        text += f"🔖 <b>Username:</b> @{user['username']}\n"
    text += f"🆔 <b>ID:</b> <code>{m.from_user.id}</code>\n"
    text += f"💰 <b>Balance:</b> ${bal:.2f}\n"
    text += f"👥 <b>Referrals:</b> {len(referrals)}\n"
    text += f"📅 <b>Joined:</b> {user.get('joined', datetime.utcnow()).strftime('%Y-%m-%d')}"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📩 Support & Help")
def support_btn(m):
    bot.send_message(m.chat.id, "📩 <b>Support & Help</b>\n\nFor any assistance, please contact:\n👑 Owner: @dark_princes12\n📢 Channel: " + FORCE_CHANNEL + "\n👥 Group: " + FORCE_GROUP, parse_mode="HTML")

# ======================= ADMIN PANEL =======================
def admin_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "👥 Users", "💰 Balance",
        "📥 Deposit", "📤 Withdraw",
        "📊 Stats", "📢 Broadcast",
        "📦 Plans", "🛑 Ban",
        "🔓 Unban User", "📝 Update Plans",
        "👑 Add Admin", "🗑 Remove Admin",
        "💸 Referral Control", "⚙ System Settings",
        "🔙 User Menu"
    ]
    markup.add(*[KeyboardButton(b) for b in buttons])
    return markup

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Unauthorized.")
        return
    bot.send_message(message.chat.id, "🔧 <b>Admin Panel</b>", reply_markup=admin_menu(), parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🔙 User Menu" and is_admin(m.from_user.id))
def back_to_user_menu(m):
    bot.send_message(m.chat.id, "🔹 <b>Main Menu</b>", reply_markup=main_menu(), parse_mode="HTML")

# ---------- Admin Handlers ----------
@bot.message_handler(func=lambda m: m.text == "👥 Users" and is_admin(m.from_user.id))
def admin_users(m):
    users = list(users_col.find().limit(10))
    total = users_col.count_documents({})
    text = f"👥 <b>Total Users:</b> {total}\n\n<b>First 10 Users:</b>\n"
    for u in users:
        text += f"• <code>{u['user_id']}</code> – {u.get('first_name', 'N/A')} (${u.get('balance',0)})\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💰 Balance" and is_admin(m.from_user.id))
def admin_balance(m):
    msg = bot.send_message(m.chat.id, "💸 <b>Balance Control</b>\n\nSend: <code>user_id amount</code> to add, or <code>user_id -amount</code> to remove.\n\nExample: <code>123456 10</code> or <code>123456 -10</code>", parse_mode="HTML")
    bot.register_next_step_handler(msg, balance_admin)

def balance_admin(m):
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
        bot.send_message(m.chat.id, msg, parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Invalid format. Use: user_id amount")

@bot.message_handler(func=lambda m: m.text == "📥 Deposit" and is_admin(m.from_user.id))
def admin_deposits(m):
    pending = get_pending_deposits()
    if not pending:
        bot.send_message(m.chat.id, "📭 No pending deposits.")
        return
    for dep in pending:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_dep|{dep['request_id']}"),
                   InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_dep|{dep['request_id']}"))
        bot.send_message(m.chat.id,
                         f"📥 <b>Deposit Request</b>\n👤 User: <code>{dep['user_id']}</code>\n💰 Amount: <b>{dep['amount_bdt']} BDT</b>\n🔑 TXID: <code>{dep['txid']}</code>",
                         reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📤 Withdraw" and is_admin(m.from_user.id))
def admin_withdraws(m):
    pending = get_pending_withdraws()
    if not pending:
        bot.send_message(m.chat.id, "📭 No pending withdrawals.")
        return
    for wd in pending:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_wd|{wd['request_id']}"),
                   InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_wd|{wd['request_id']}"))
        bot.send_message(m.chat.id,
                         f"📤 <b>Withdraw Request</b>\n👤 User: <code>{wd['user_id']}</code>\n💰 Amount: <b>${wd['amount_usd']}</b>\n💳 Method: {wd['method']}\n📞 Account: <code>{wd['account']}</code>",
                         reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📊 Stats" and is_admin(m.from_user.id))
def admin_stats(m):
    total_users = users_col.count_documents({})
    total_balance = sum(u.get("balance", 0) for u in users_col.find())
    total_invested = sum(inv["amount"] for inv in investments_col.find({"status": "active"}))
    text = f"📊 <b>Statistics</b>\n\n👥 Users: <b>{total_users}</b>\n💰 Total Balance: <b>${total_balance:.2f}</b>\n💸 Total Invested: <b>${total_invested:.2f}</b>"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_admin(m.from_user.id))
def admin_broadcast(m):
    msg = bot.send_message(m.chat.id, "📢 <b>Broadcast Message</b>\n\nSend the message you want to broadcast to all users:", parse_mode="HTML")
    bot.register_next_step_handler(msg, broadcast_msg)

def broadcast_msg(m):
    text = m.text
    count = 0
    for user in users_col.find():
        try:
            bot.send_message(user["user_id"], text)
            count += 1
        except:
            pass
    bot.send_message(m.chat.id, f"✅ Broadcast sent to <b>{count}</b> users.", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📦 Plans" and is_admin(m.from_user.id))
def admin_plans(m):
    plans = get_plans()
    text = "📦 <b>Current Investment Plans</b>\n\n"
    for pid, p in plans.items():
        text += f"🔹 <b>{p['name']}</b> (<code>{pid}</code>)\n"
        text += f"   Profit: {p['profit_percent']}%\n"
        text += f"   Duration: {p['duration_days']} days\n"
        text += f"   Minimum: ${p['min_amount']}\n\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🛑 Ban" and is_admin(m.from_user.id))
def admin_ban(m):
    msg = bot.send_message(m.chat.id, "🚫 <b>Ban User</b>\n\nEnter user ID to ban:", parse_mode="HTML")
    bot.register_next_step_handler(msg, ban_user_cmd)

def ban_user_cmd(m):
    try:
        uid = int(m.text)
        ban_user(uid)
        bot.send_message(m.chat.id, f"✅ User <code>{uid}</code> has been banned.", parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "🔓 Unban User" and is_admin(m.from_user.id))
def admin_unban(m):
    msg = bot.send_message(m.chat.id, "🔓 <b>Unban User</b>\n\nEnter user ID to unban:", parse_mode="HTML")
    bot.register_next_step_handler(msg, unban_user_cmd)

def unban_user_cmd(m):
    try:
        uid = int(m.text)
        unban_user(uid)
        bot.send_message(m.chat.id, f"✅ User <code>{uid}</code> has been unbanned.", parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "📝 Update Plans" and is_admin(m.from_user.id))
def admin_update_plans(m):
    plans = get_plans()
    text = "📝 <b>Update Investment Plans</b>\n\n"
    for pid, p in plans.items():
        text += f"<b>{pid}</b>: {p['name']} | {p['profit_percent']}% | {p['duration_days']}d | ${p['min_amount']}\n"
    text += "\nEnter new plan details in format:\n<code>plan_id name profit% duration_days min_amount</code>\n\nExample: <code>basic Basic 20 7 10</code>"
    bot.send_message(m.chat.id, text, parse_mode="HTML")
    bot.register_next_step_handler(m, process_plan_update)

def process_plan_update(m):
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
        bot.send_message(m.chat.id, f"✅ Plan <code>{pid}</code> updated successfully!", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Plan update error: {e}")
        bot.send_message(m.chat.id, "❌ Invalid format. Use: plan_id name profit% duration_days min_amount")

@bot.message_handler(func=lambda m: m.text == "👑 Add Admin" and m.from_user.id == OWNER_ID)
def admin_add_admin(m):
    msg = bot.send_message(m.chat.id, "👑 <b>Add Admin</b>\n\nEnter user ID to add as admin:", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_admin)

def add_admin(m):
    try:
        uid = int(m.text)
        if admins_col.find_one({"user_id": uid}):
            bot.send_message(m.chat.id, f"❌ User <code>{uid}</code> is already an admin.", parse_mode="HTML")
            return
        admins_col.insert_one({"user_id": uid})
        bot.send_message(m.chat.id, f"✅ User <code>{uid}</code> is now an admin.", parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "🗑 Remove Admin" and m.from_user.id == OWNER_ID)
def admin_remove_admin(m):
    msg = bot.send_message(m.chat.id, "🗑 <b>Remove Admin</b>\n\nEnter user ID to remove from admin:", parse_mode="HTML")
    bot.register_next_step_handler(msg, remove_admin)

def remove_admin(m):
    try:
        uid = int(m.text)
        if uid == OWNER_ID:
            bot.send_message(m.chat.id, "❌ Cannot remove the owner.")
            return
        result = admins_col.delete_one({"user_id": uid})
        if result.deleted_count:
            bot.send_message(m.chat.id, f"✅ User <code>{uid}</code> is no longer an admin.", parse_mode="HTML")
        else:
            bot.send_message(m.chat.id, f"❌ User <code>{uid}</code> is not an admin.", parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "💸 Referral Control" and is_admin(m.from_user.id))
def admin_referral_control(m):
    settings = get_settings()
    current = settings.get("referral_bonus", 0.01)
    msg = bot.send_message(m.chat.id, f"💸 <b>Referral Bonus Control</b>\n\nCurrent bonus: <b>${current}</b>\n\nSend new bonus amount (e.g., 0.02):", parse_mode="HTML")
    bot.register_next_step_handler(msg, set_referral_bonus)

def set_referral_bonus(m):
    try:
        new_bonus = float(m.text)
        if new_bonus <= 0:
            raise ValueError
        update_settings({"referral_bonus": new_bonus})
        bot.send_message(m.chat.id, f"✅ Referral bonus updated to <b>${new_bonus}</b>.", parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Invalid amount. Please send a number > 0.")

@bot.message_handler(func=lambda m: m.text == "⚙ System Settings" and is_admin(m.from_user.id))
def admin_system_settings(m):
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
    bot.send_message(m.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sys_toggle_"))
def sys_toggle_cb(call):
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

# ------------------- Admin Approval Callbacks (with reason) -------------------
def format_auto_post(action, type_, user_id, amount, reason=None, txid=None, method=None, account=None):
    user_name = get_user_name(user_id)
    if type_ == "deposit":
        if action == "approve":
            emoji = "✅"
            title = "Deposit Approved"
            details = f"💵 Amount: <b>{amount} BDT</b>\n🔑 TXID: <code>{txid}</code>"
        else:  # reject
            emoji = "❌"
            title = "Deposit Rejected"
            details = f"💵 Amount: <b>{amount} BDT</b>\n🔑 TXID: <code>{txid}</code>"
            if reason:
                details += f"\n💬 <b>Reason:</b> {reason}"
    else:  # withdraw
        if action == "approve":
            emoji = "✅"
            title = "Withdrawal Approved"
            details = f"💰 Amount: <b>${amount}</b>\n💳 Method: {method}\n📞 Account: <code>{account}</code>"
        else:  # reject
            emoji = "❌"
            title = "Withdrawal Rejected"
            details = f"💰 Amount: <b>${amount}</b>\n💳 Method: {method}\n📞 Account: <code>{account}</code>"
            if reason:
                details += f"\n💬 <b>Reason:</b> {reason}"
    return (
        f"{emoji} <b>{title}</b> {emoji}\n\n"
        f"👤 <b>User:</b> {user_name} (<code>{user_id}</code>)\n"
        f"{details}\n\n"
        f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

# Deposit approve (no reason needed)
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_approve_dep|"))
def approve_dep_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    req_id = call.data.split("|")[1]
    success, dep = approve_deposit(req_id)
    if success:
        bot.answer_callback_query(call.id, "✅ Deposit approved!")
        bot.edit_message_text("✅ Deposit approved.", call.message.chat.id, call.message.message_id)
        bot.send_message(dep["user_id"], "✅ Your deposit has been approved! Balance updated.")
        msg_text = format_auto_post("approve", "deposit", dep["user_id"], dep["amount_bdt"], txid=dep["txid"])
        try:
            bot.send_message(FORCE_CHANNEL, msg_text, parse_mode="HTML")
            bot.send_message(FORCE_GROUP, msg_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Auto-post error: {e}")
    else:
        bot.answer_callback_query(call.id, "❌ Failed or already processed.")

# Deposit reject (ask for reason)
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reject_dep|"))
def reject_dep_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    req_id = call.data.split("|")[1]
    # Ask for reason
    msg = bot.send_message(call.message.chat.id, "💬 Please enter the reason for rejecting this deposit:")
    bot.register_next_step_handler(msg, lambda m: process_deposit_reject(m, req_id, call.message.chat.id, call.message.message_id))

def process_deposit_reject(m, req_id, chat_id, message_id):
    reason = m.text.strip()
    if not reason:
        reason = "No reason provided"
    success, dep = reject_deposit(req_id, reason)
    if success:
        bot.answer_callback_query(m.message_id, "❌ Deposit rejected.")
        bot.edit_message_text("❌ Deposit rejected.", chat_id, message_id)
        bot.send_message(dep["user_id"], f"❌ Your deposit request was rejected.\n\n<b>Reason:</b> {reason}", parse_mode="HTML")
        msg_text = format_auto_post("reject", "deposit", dep["user_id"], dep["amount_bdt"], reason=reason, txid=dep["txid"])
        try:
            bot.send_message(FORCE_CHANNEL, msg_text, parse_mode="HTML")
            bot.send_message(FORCE_GROUP, msg_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Auto-post error: {e}")
        bot.send_message(chat_id, f"✅ Deposit request {req_id} has been rejected with reason: {reason}")
    else:
        bot.send_message(chat_id, "❌ Failed or already processed.")

# Withdraw approve (no reason needed)
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_approve_wd|"))
def approve_wd_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    req_id = call.data.split("|")[1]
    success, wd = approve_withdraw(req_id)
    if success:
        bot.answer_callback_query(call.id, "✅ Withdraw approved!")
        bot.edit_message_text("✅ Withdraw approved.", call.message.chat.id, call.message.message_id)
        bot.send_message(wd["user_id"], f"✅ Your withdrawal of ${wd['amount_usd']} has been approved and sent.")
        msg_text = format_auto_post("approve", "withdraw", wd["user_id"], wd["amount_usd"], method=wd["method"], account=wd["account"])
        try:
            bot.send_message(FORCE_CHANNEL, msg_text, parse_mode="HTML")
            bot.send_message(FORCE_GROUP, msg_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Auto-post error: {e}")
    else:
        bot.answer_callback_query(call.id, "❌ Failed or already processed.")

# Withdraw reject (ask for reason)
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reject_wd|"))
def reject_wd_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    req_id = call.data.split("|")[1]
    msg = bot.send_message(call.message.chat.id, "💬 Please enter the reason for rejecting this withdrawal:")
    bot.register_next_step_handler(msg, lambda m: process_withdraw_reject(m, req_id, call.message.chat.id, call.message.message_id))

def process_withdraw_reject(m, req_id, chat_id, message_id):
    reason = m.text.strip()
    if not reason:
        reason = "No reason provided"
    success, wd = reject_withdraw(req_id, reason)
    if success:
        bot.answer_callback_query(m.message_id, "❌ Withdraw rejected.")
        bot.edit_message_text("❌ Withdraw rejected.", chat_id, message_id)
        bot.send_message(wd["user_id"], f"❌ Your withdrawal request was rejected.\n\n<b>Reason:</b> {reason}", parse_mode="HTML")
        msg_text = format_auto_post("reject", "withdraw", wd["user_id"], wd["amount_usd"], reason=reason, method=wd["method"], account=wd["account"])
        try:
            bot.send_message(FORCE_CHANNEL, msg_text, parse_mode="HTML")
            bot.send_message(FORCE_GROUP, msg_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Auto-post error: {e}")
        bot.send_message(chat_id, f"✅ Withdrawal request {req_id} has been rejected with reason: {reason}")
    else:
        bot.send_message(chat_id, "❌ Failed or already processed.")

# ======================= FLASK HEALTH CHECK =======================
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ======================= START BOT =======================
if __name__ == "__main__":
    logger.info("Bot started...")
    bot.infinity_polling()