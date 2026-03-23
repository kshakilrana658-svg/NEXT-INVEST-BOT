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
from bson.objectid import ObjectId

# ======================= LOGGING =======================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======================= CONFIGURATION (Environment) =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
FORCE_CHANNEL = os.environ.get("FORCE_CHANNEL", "")
FORCE_GROUP = os.environ.get("FORCE_GROUP", "")
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = os.environ.get("DB_NAME", "nextinvest")
DEPOSIT_NUMBER = os.environ.get("DEPOSIT_NUMBER", "01309924182")

# Rates
DEPOSIT_RATE_USD_TO_BDT = 130   # 1 USD = 130 BDT
WITHDRAW_RATE_USD_TO_BDT = 110  # 1 USD = 110 BDT (for display)
WITHDRAW_SERVICE_CHARGE_BDT = 10

if not BOT_TOKEN or not OWNER_ID or not FORCE_CHANNEL or not FORCE_GROUP or not MONGO_URI:
    raise ValueError("Missing required environment variables")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ======================= MONGODB SETUP =======================
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
users_col = db["users"]
deposits_col = db["deposits"]
withdraws_col = db["withdraws"]
investments_col = db["investments"]
admins_col = db["admins"]
settings_col = db["settings"]

# Ensure indexes
users_col.create_index("user_id", unique=True)
deposits_col.create_index("request_id", unique=True)
withdraws_col.create_index("request_id", unique=True)

# Default settings if not present
settings = settings_col.find_one({"_id": "global"})
if not settings:
    settings_col.insert_one({
        "_id": "global",
        "referral_bonus": 0.01,
        "deposit_enabled": True,
        "withdraw_enabled": True,
        "maintenance_mode": False
    })

# Default plans if not present
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

def get_user(user_id):
    return users_col.find_one({"user_id": user_id})

def create_user(user_id, username, first_name, ref_by=None):
    user_data = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "joined": datetime.utcnow(),
        "balance": 0.05,  # signup bonus
        "referred_by": ref_by,
        "referrals": [],
        "transactions": [],
        "banned": False
    }
    try:
        users_col.insert_one(user_data)
        # Add referral if applicable
        if ref_by and ref_by != user_id:
            ref_user = users_col.find_one({"user_id": ref_by})
            if ref_user:
                bonus = get_settings()["referral_bonus"]
                # Add bonus to referrer
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
    """operation: 'add' or 'subtract'"""
    user = users_col.find_one({"user_id": user_id})
    if not user:
        return False
    current = user.get("balance", 0.0)
    new_balance = current + amount if operation == "add" else current - amount
    users_col.update_one({"user_id": user_id}, {"$set": {"balance": new_balance}})
    # Add transaction record
    txn = {
        "type": "admin_add" if operation == "add" else "admin_remove",
        "amount": amount,
        "status": "completed",
        "details": f"Balance {'added' if operation == 'add' else 'removed'} by admin",
        "timestamp": datetime.utcnow()
    }
    users_col.update_one({"user_id": user_id}, {"$push": {"transactions": txn}})
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
    # Update balance: convert BDT to USD
    usd_amount = deposit["amount_bdt"] / DEPOSIT_RATE_USD_TO_BDT
    users_col.update_one({"user_id": deposit["user_id"]}, {"$inc": {"balance": usd_amount}})
    add_transaction(deposit["user_id"], "deposit", usd_amount, "completed", f"Deposit of {deposit['amount_bdt']} BDT approved")
    deposits_col.update_one({"request_id": request_id}, {"$set": {"status": "approved"}})
    return True, deposit

def reject_deposit(request_id):
    deposit = deposits_col.find_one({"request_id": request_id, "status": "pending"})
    if not deposit:
        return False, None
    deposits_col.update_one({"request_id": request_id}, {"$set": {"status": "rejected"}})
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
    # Deduct balance
    user = users_col.find_one({"user_id": withdraw["user_id"]})
    if user["balance"] < withdraw["amount_usd"]:
        return False, None
    users_col.update_one({"user_id": withdraw["user_id"]}, {"$inc": {"balance": -withdraw["amount_usd"]}})
    add_transaction(withdraw["user_id"], "withdraw", withdraw["amount_usd"], "completed", "Withdraw approved")
    withdraws_col.update_one({"request_id": request_id}, {"$set": {"status": "approved"}})
    return True, withdraw

def reject_withdraw(request_id):
    withdraw = withdraws_col.find_one({"request_id": request_id, "status": "pending"})
    if not withdraw:
        return False, None
    withdraws_col.update_one({"request_id": request_id}, {"$set": {"status": "rejected"}})
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
    # Deduct balance
    users_col.update_one({"user_id": user_id}, {"$inc": {"balance": -amount}})
    add_transaction(user_id, "investment", amount, "completed", f"Invested in {plan['name']}")
    # Save investment
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
        time.sleep(86400)  # 24 hours
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

# ======================= WELCOME MESSAGE =======================
def welcome_message(first_name):
    return (
        f"🌟 <b>স্বাগতম {first_name}!</b> 🌟\n\n"
        f"🎉 <b>NextInvest Bot</b> এ আপনাকে দেখে আমরা আনন্দিত!\n\n"
        f"🔹 <b>আপনি যা করতে পারবেন:</b>\n"
        f"✅ ডিপোজিট করে ব্যালান্স বাড়ান\n"
        f"✅ ইনভেস্ট করে লাভ করুন\n"
        f"✅ রেফারেল লিংক শেয়ার করে আয় করুন\n"
        f"✅ সহজেই উইথড্র করুন\n\n"
        f"💡 <b>প্রথম পদক্ষেপ:</b>\n"
        f"1️⃣ নিচের মেনু থেকে <b>💳 Deposit Money</b> বাটনে ক্লিক করুন\n"
        f"2️⃣ TXID ও পরিমাণ দিন (শুধু টাকা পাঠানোর রেফারেন্স)\n"
        f"3️⃣ এডমিন অনুমোদন দিলে ব্যালান্স অ্যাড হবে\n"
        f"4️⃣ তারপর <b>🚀 Invest Now</b> করে ইনভেস্ট করুন\n\n"
        f"🎁 <b>বোনাস:</b> সাইনআপে $0.05, প্রতি রেফারে $0.01\n\n"
        f"🔽 <b>নিচের বাটন ব্যবহার করে শুরু করুন</b> 🔽"
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
        user = create_user(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
            ref_by
        )
        bot.send_message(message.chat.id, welcome_message(message.from_user.first_name), parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, f"👋 Welcome back {user['first_name']}!")

    bot.send_message(message.chat.id, "🔹 Main Menu:", reply_markup=main_menu())

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
    text = "📈 <b>Investment Plans:</b>\n\n"
    for pid, p in plans.items():
        text += f"🔹 <b>{p['name']}</b>\n   Profit: {p['profit_percent']}%\n   Duration: {p['duration_days']} days\n   Min: ${p['min_amount']}\n\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🚀 Invest Now")
def invest_btn(m):
    settings = get_settings()
    if not settings.get("deposit_enabled", True):
        bot.send_message(m.chat.id, "❌ Investment is currently disabled by admin.")
        return
    plans = get_plans()
    plan_list = "\n".join([f"{pid}: {p['name']} (min ${p['min_amount']}, {p['profit_percent']}%)" for pid, p in plans.items()])
    msg = bot.send_message(m.chat.id, f"🚀 <b>Send investment in format:</b>\n<code>&lt;plan_id&gt; &lt;amount&gt;</code>\n\nAvailable plans:\n{plan_list}\n\nExample: <code>basic 50</code>", parse_mode="HTML")
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
            bot.send_message(m.chat.id, f"✅ Investment of ${amount} in {plan['name']} successful!")
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
    text = f"💰 <b>Balance:</b> ${bal:.2f}\n\n<b>📜 Last 5 Transactions:</b>\n"
    for t in transactions[::-1]:
        text += f"{t['type']}: ${t['amount']} ({t['status']})\n"
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
        confirm = f"✅ You sent {amount_bdt} BDT → will receive ${usd_amount:.2f} USD.\n\nConfirm? (yes/no)"
        msg = bot.send_message(m.chat.id, confirm)
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
        bot.send_message(m.chat.id, f"✅ <b>Deposit request submitted!</b>\nAmount: {amount_bdt} BDT\nTXID: <code>{txid}</code>\nRequest ID: <code>{req_id}</code>\n\nAdmin will review it.", parse_mode="HTML")
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
        confirm_text = f"💸 You will receive approximately {estimated_bdt:.2f} BDT after charge.\n\nProceed? (yes/no)"
        msg = bot.send_message(m.chat.id, confirm_text)
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
    bot.send_message(m.chat.id, f"✅ <b>Withdrawal request submitted!</b>\nAmount: ${amount}\nRequest ID: <code>{req_id}</code>\n\nAdmin will process it.", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📈 My Investments")
def my_investments_btn(m):
    invs = list(investments_col.find({"user_id": m.from_user.id}))
    if not invs:
        bot.send_message(m.chat.id, "📭 You have no investments.")
        return
    text = "📈 <b>Your Investments:</b>\n"
    for inv in invs:
        plans = get_plans()
        plan = plans.get(inv["plan_id"], {"name": inv["plan_id"]})
        text += f"Plan: {plan['name']} | Amount: ${inv['amount']} | Status: {inv['status']}\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💸 Profit History")
def profit_btn(m):
    user = get_user(m.from_user.id)
    profits = [t for t in user.get("transactions", []) if t["type"] == "profit"]
    if not profits:
        bot.send_message(m.chat.id, "📭 No profit history found.")
        return
    text = "💸 <b>Last 5 Profits:</b>\n"
    for p in profits[-5:]:
        text += f"${p['amount']} on {p['timestamp'].strftime('%Y-%m-%d')}\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🤝 Referral Program")
def referral_btn(m):
    bot_username = bot.get_me().username
    ref_link = f"https://t.me/{bot_username}?start={m.from_user.id}"
    user = get_user(m.from_user.id)
    referrals = user.get("referrals", [])
    settings = get_settings()
    bonus = settings.get("referral_bonus", 0.01)
    text = f"🔗 <b>Your referral link:</b>\n<code>{ref_link}</code>\n\n👥 <b>Total referrals:</b> {len(referrals)}\n💰 <b>Earn ${bonus} per referral!</b>"
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
    text = f"👤 <b>Name:</b> {user.get('first_name', 'N/A')}\n🆔 <b>ID:</b> {m.from_user.id}\n💰 <b>Balance:</b> ${bal:.2f}\n👥 <b>Referrals:</b> {len(referrals)}\n📅 <b>Joined:</b> {user.get('joined', datetime.utcnow()).strftime('%Y-%m-%d')}"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📩 Support & Help")
def support_btn(m):
    bot.send_message(m.chat.id, "📩 <b>For support contact:</b> @dark_princes12", parse_mode="HTML")

# ======================= ADMIN PANEL =======================
def admin_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "👥 Users", "💰 Balance",
        "📥 Deposit", "📤 Withdraw",
        "📊 Stats", "📢 Broadcast",
        "📦 Plans", "🛑 Ban",
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
    bot.send_message(message.chat.id, "🔧 <b>Admin Panel:</b>", reply_markup=admin_menu(), parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🔙 User Menu" and is_admin(m.from_user.id))
def back_to_user_menu(m):
    bot.send_message(m.chat.id, "🔹 Main Menu:", reply_markup=main_menu())

# ---------- Admin Handlers ----------
@bot.message_handler(func=lambda m: m.text == "👥 Users" and is_admin(m.from_user.id))
def admin_users(m):
    users = list(users_col.find().limit(10))
    text = f"👥 <b>Total Users:</b> {users_col.count_documents({})}\n"
    for u in users:
        text += f"{u['user_id']} - {u.get('first_name', 'N/A')} (${u.get('balance',0)})\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💰 Balance" and is_admin(m.from_user.id))
def admin_balance(m):
    msg = bot.send_message(m.chat.id, "💸 <b>Send:</b> <code>user_id amount</code> to add, or <code>user_id -amount</code> to remove.\nExample: <code>123456 10</code> or <code>123456 -10</code>", parse_mode="HTML")
    bot.register_next_step_handler(msg, balance_admin)

def balance_admin(m):
    try:
        parts = m.text.split()
        uid = int(parts[0])
        amt = float(parts[1])
        if amt > 0:
            update_balance(uid, amt, "add")
            msg = f"✅ Added ${amt} to user {uid}"
        else:
            update_balance(uid, abs(amt), "subtract")
            msg = f"✅ Removed ${abs(amt)} from user {uid}"
        bot.send_message(m.chat.id, msg)
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
                         f"📥 <b>Deposit Request</b>\nUser: <code>{dep['user_id']}</code>\nAmount: {dep['amount_bdt']} BDT\nTXID: <code>{dep['txid']}</code>",
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
                         f"📤 <b>Withdraw Request</b>\nUser: <code>{wd['user_id']}</code>\nAmount: ${wd['amount_usd']}\nMethod: {wd['method']}\nAccount: <code>{wd['account']}</code>",
                         reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📊 Stats" and is_admin(m.from_user.id))
def admin_stats(m):
    total_users = users_col.count_documents({})
    total_balance = sum(u.get("balance", 0) for u in users_col.find())
    total_invested = sum(inv["amount"] for inv in investments_col.find({"status": "active"}))
    text = f"📊 <b>Stats:</b>\n👥 Users: {total_users}\n💰 Total Balance: ${total_balance:.2f}\n💸 Total Invested: ${total_invested:.2f}"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_admin(m.from_user.id))
def admin_broadcast(m):
    msg = bot.send_message(m.chat.id, "📢 <b>Send broadcast message:</b>", parse_mode="HTML")
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
    bot.send_message(m.chat.id, f"✅ Broadcast sent to {count} users.")

@bot.message_handler(func=lambda m: m.text == "📦 Plans" and is_admin(m.from_user.id))
def admin_plans(m):
    plans = get_plans()
    text = "📦 <b>Current Plans:</b>\n"
    for pid, p in plans.items():
        text += f"{pid}: {p['name']} - {p['profit_percent']}%, {p['duration_days']} days, min ${p['min_amount']}\n"
    text += "\nTo edit, use MongoDB directly (not implemented in this demo)."
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🛑 Ban" and is_admin(m.from_user.id))
def admin_ban(m):
    msg = bot.send_message(m.chat.id, "🚫 <b>Enter user ID to ban:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, ban_user)

def ban_user(m):
    try:
        uid = int(m.text)
        result = users_col.update_one({"user_id": uid}, {"$set": {"banned": True}})
        if result.modified_count:
            bot.send_message(m.chat.id, f"✅ User {uid} banned.")
        else:
            bot.send_message(m.chat.id, "❌ User not found.")
    except:
        bot.send_message(m.chat.id, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "👑 Add Admin" and m.from_user.id == OWNER_ID)
def admin_add_admin(m):
    msg = bot.send_message(m.chat.id, "👑 <b>Enter user ID to add as admin:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_admin)

def add_admin(m):
    try:
        uid = int(m.text)
        if admins_col.find_one({"user_id": uid}):
            bot.send_message(m.chat.id, f"❌ User {uid} is already an admin.")
            return
        admins_col.insert_one({"user_id": uid})
        bot.send_message(m.chat.id, f"✅ User {uid} is now an admin.")
    except:
        bot.send_message(m.chat.id, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "🗑 Remove Admin" and m.from_user.id == OWNER_ID)
def admin_remove_admin(m):
    msg = bot.send_message(m.chat.id, "🗑 <b>Enter user ID to remove from admin:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, remove_admin)

def remove_admin(m):
    try:
        uid = int(m.text)
        if uid == OWNER_ID:
            bot.send_message(m.chat.id, "❌ Cannot remove the owner.")
            return
        result = admins_col.delete_one({"user_id": uid})
        if result.deleted_count:
            bot.send_message(m.chat.id, f"✅ User {uid} is no longer an admin.")
        else:
            bot.send_message(m.chat.id, f"❌ User {uid} is not an admin.")
    except:
        bot.send_message(m.chat.id, "❌ Invalid user ID.")

@bot.message_handler(func=lambda m: m.text == "💸 Referral Control" and is_admin(m.from_user.id))
def admin_referral_control(m):
    settings = get_settings()
    current = settings.get("referral_bonus", 0.01)
    msg = bot.send_message(m.chat.id, f"💸 <b>Current referral bonus:</b> ${current}\n\nSend new bonus amount (e.g., 0.02):", parse_mode="HTML")
    bot.register_next_step_handler(msg, set_referral_bonus)

def set_referral_bonus(m):
    try:
        new_bonus = float(m.text)
        if new_bonus <= 0:
            raise ValueError
        update_settings({"referral_bonus": new_bonus})
        bot.send_message(m.chat.id, f"✅ Referral bonus updated to ${new_bonus}.")
    except:
        bot.send_message(m.chat.id, "❌ Invalid amount. Please send a number > 0.")

@bot.message_handler(func=lambda m: m.text == "⚙ System Settings" and is_admin(m.from_user.id))
def admin_system_settings(m):
    settings = get_settings()
    text = (
        "⚙ <b>System Settings</b>\n\n"
        f"Deposit: {'✅ Enabled' if settings.get('deposit_enabled', True) else '❌ Disabled'}\n"
        f"Withdraw: {'✅ Enabled' if settings.get('withdraw_enabled', True) else '❌ Disabled'}\n"
        f"Maintenance: {'🔧 Enabled' if settings.get('maintenance_mode', False) else '✅ Disabled'}\n\n"
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

# ------------------- Admin Approval Callbacks (with formatted auto-posts) -------------------
def format_auto_post(action, type_, user_id, amount, txid=None, method=None, account=None):
    if type_ == "deposit":
        if action == "approve":
            emoji = "✅"
            title = "Deposit Approved"
            details = f"💵 Amount: <b>{amount} BDT</b>\n🔑 TXID: <code>{txid}</code>"
        else:
            emoji = "❌"
            title = "Deposit Rejected"
            details = f"💵 Amount: <b>{amount} BDT</b>\n🔑 TXID: <code>{txid}</code>"
    else:
        if action == "approve":
            emoji = "✅"
            title = "Withdrawal Approved"
            details = f"💰 Amount: <b>${amount}</b>\n💳 Method: {method}\n📞 Account: <code>{account}</code>"
        else:
            emoji = "❌"
            title = "Withdrawal Rejected"
            details = f"💰 Amount: <b>${amount}</b>\n💳 Method: {method}\n📞 Account: <code>{account}</code>"
    user_link = f'<a href="tg://user?id={user_id}">User {user_id}</a>'
    return (
        f"{emoji} <b>{title}</b> {emoji}\n\n"
        f"👤 {user_link}\n"
        f"{details}\n\n"
        f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

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

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reject_dep|"))
def reject_dep_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    req_id = call.data.split("|")[1]
    success, dep = reject_deposit(req_id)
    if success:
        bot.answer_callback_query(call.id, "❌ Deposit rejected.")
        bot.edit_message_text("❌ Deposit rejected.", call.message.chat.id, call.message.message_id)
        bot.send_message(dep["user_id"], "❌ Your deposit request was rejected.")
        msg_text = format_auto_post("reject", "deposit", dep["user_id"], dep["amount_bdt"], txid=dep["txid"])
        try:
            bot.send_message(FORCE_CHANNEL, msg_text, parse_mode="HTML")
            bot.send_message(FORCE_GROUP, msg_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Auto-post error: {e}")
    else:
        bot.answer_callback_query(call.id, "❌ Failed or already processed.")

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

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_reject_wd|"))
def reject_wd_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    req_id = call.data.split("|")[1]
    success, wd = reject_withdraw(req_id)
    if success:
        bot.answer_callback_query(call.id, "❌ Withdraw rejected.")
        bot.edit_message_text("❌ Withdraw rejected.", call.message.chat.id, call.message.message_id)
        bot.send_message(wd["user_id"], "❌ Your withdrawal request was rejected.")
        msg_text = format_auto_post("reject", "withdraw", wd["user_id"], wd["amount_usd"], method=wd["method"], account=wd["account"])
        try:
            bot.send_message(FORCE_CHANNEL, msg_text, parse_mode="HTML")
            bot.send_message(FORCE_GROUP, msg_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Auto-post error: {e}")
    else:
        bot.answer_callback_query(call.id, "❌ Failed or already processed.")

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