import os
import json
import requests
import time
import threading
import logging
from datetime import datetime, timedelta
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from flask import Flask

# ======================= LOGGING =======================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======================= CONFIGURATION =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
FORCE_CHANNEL = os.environ.get("FORCE_CHANNEL", "")
FORCE_GROUP = os.environ.get("FORCE_GROUP", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
DEPOSIT_NUMBER = os.environ.get("DEPOSIT_NUMBER", "01309924182")

# Rates
DEPOSIT_RATE_USD_TO_BDT = 130   # 1 USD = 130 BDT
WITHDRAW_RATE_USD_TO_BDT = 110  # 1 USD = 110 BDT (for display)
WITHDRAW_SERVICE_CHARGE_BDT = 10

if not BOT_TOKEN or not OWNER_ID or not FORCE_CHANNEL or not FORCE_GROUP or not GITHUB_TOKEN or not GITHUB_REPO:
    raise ValueError("Missing required environment variables")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ======================= GITHUB HELPER =======================
def github_read(file_name):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_name}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            import base64
            content = base64.b64decode(resp.json()["content"]).decode("utf-8")
            return json.loads(content)
        else:
            logger.warning(f"GitHub read {file_name}: status {resp.status_code}")
            return {}
    except Exception as e:
        logger.error(f"GitHub read error: {e}")
        return {}

def github_write(file_name, data):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_name}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    get_resp = requests.get(url, headers=headers)
    sha = None
    if get_resp.status_code == 200:
        sha = get_resp.json()["sha"]
    import base64
    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    payload = {
        "message": f"Update {file_name}",
        "content": content,
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha
    put_resp = requests.put(url, headers=headers, json=payload)
    if put_resp.status_code not in [200, 201]:
        logger.error(f"GitHub write {file_name}: {put_resp.status_code} - {put_resp.text}")
        return False
    return True

# ======================= DATA LAYER =======================
# Files: users.json, deposit.json, withdraw.json, invest.json, admins.json, settings.json

def get_user(user_id):
    users = github_read("users.json")
    return users.get(str(user_id))

def save_user(user_id, user_data):
    users = github_read("users.json")
    users[str(user_id)] = user_data
    github_write("users.json", users)

def update_balance(user_id, amount, operation="add"):
    users = github_read("users.json")
    user = users.get(str(user_id), {})
    current = user.get("balance", 0.0)
    if operation == "add":
        current += amount
    elif operation == "subtract":
        current -= amount
    user["balance"] = current
    users[str(user_id)] = user
    github_write("users.json", users)
    return current

def add_transaction(user_id, txn_type, amount, status, details=""):
    users = github_read("users.json")
    user = users.get(str(user_id), {})
    txn_list = user.get("transactions", [])
    txn_list.append({
        "type": txn_type,
        "amount": amount,
        "status": status,
        "details": details,
        "timestamp": datetime.now().isoformat()
    })
    if len(txn_list) > 50:
        txn_list = txn_list[-50:]
    user["transactions"] = txn_list
    users[str(user_id)] = user
    github_write("users.json", users)

def add_referral(new_user_id, ref_by):
    if ref_by == new_user_id:
        return False
    settings = github_read("settings.json")
    referral_bonus = settings.get("referral_bonus", 0.01)
    users = github_read("users.json")
    ref_user = users.get(str(ref_by), {})
    referrals = ref_user.get("referrals", [])
    if new_user_id not in referrals:
        referrals.append(new_user_id)
        ref_user["referrals"] = referrals
        users[str(ref_by)] = ref_user
        update_balance(ref_by, referral_bonus, "add")
        add_transaction(ref_by, "referral_bonus", referral_bonus, "completed", f"New user {new_user_id}")
    new_user = users.get(str(new_user_id), {})
    new_user["referred_by"] = ref_by
    users[str(new_user_id)] = new_user
    github_write("users.json", users)
    return True

def get_user_balance(user_id):
    user = get_user(user_id)
    return user.get("balance", 0.0) if user else 0.0

def is_user_banned(user_id):
    user = get_user(user_id)
    return user.get("banned", False) if user else False

def is_admin(user_id):
    if user_id == OWNER_ID:
        return True
    admins = github_read("admins.json")
    return str(user_id) in admins.get("admins", [])

# ---------- Deposit ----------
def create_deposit_request(user_id, amount_bdt, txid):
    deposits = github_read("deposit.json")
    req_id = f"{user_id}_{int(time.time())}"
    deposits[req_id] = {
        "user_id": user_id,
        "amount_bdt": amount_bdt,
        "txid": txid,
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    github_write("deposit.json", deposits)
    return req_id

def get_pending_deposits():
    deposits = github_read("deposit.json")
    return {k: v for k, v in deposits.items() if v["status"] == "pending"}

def approve_deposit(req_id):
    deposits = github_read("deposit.json")
    if req_id in deposits and deposits[req_id]["status"] == "pending":
        req = deposits[req_id]
        usd_amount = req["amount_bdt"] / DEPOSIT_RATE_USD_TO_BDT
        update_balance(req["user_id"], usd_amount, "add")
        add_transaction(req["user_id"], "deposit", usd_amount, "completed", f"Deposit of {req['amount_bdt']} BDT approved")
        deposits[req_id]["status"] = "approved"
        github_write("deposit.json", deposits)
        return True, req
    return False, None

def reject_deposit(req_id):
    deposits = github_read("deposit.json")
    if req_id in deposits and deposits[req_id]["status"] == "pending":
        req = deposits[req_id]
        deposits[req_id]["status"] = "rejected"
        github_write("deposit.json", deposits)
        return True, req
    return False, None

# ---------- Withdraw ----------
def create_withdraw_request(user_id, amount_usd, method, account):
    withdraws = github_read("withdraw.json")
    req_id = f"{user_id}_{int(time.time())}"
    withdraws[req_id] = {
        "user_id": user_id,
        "amount_usd": amount_usd,
        "method": method,
        "account": account,
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    github_write("withdraw.json", withdraws)
    return req_id

def get_pending_withdraws():
    withdraws = github_read("withdraw.json")
    return {k: v for k, v in withdraws.items() if v["status"] == "pending"}

def approve_withdraw(req_id):
    withdraws = github_read("withdraw.json")
    if req_id in withdraws and withdraws[req_id]["status"] == "pending":
        req = withdraws[req_id]
        new_bal = update_balance(req["user_id"], req["amount_usd"], "subtract")
        if new_bal < 0:
            update_balance(req["user_id"], req["amount_usd"], "add")
            return False, None
        add_transaction(req["user_id"], "withdraw", req["amount_usd"], "completed", "Withdraw approved")
        withdraws[req_id]["status"] = "approved"
        github_write("withdraw.json", withdraws)
        return True, req
    return False, None

def reject_withdraw(req_id):
    withdraws = github_read("withdraw.json")
    if req_id in withdraws and withdraws[req_id]["status"] == "pending":
        req = withdraws[req_id]
        withdraws[req_id]["status"] = "rejected"
        github_write("withdraw.json", withdraws)
        return True, req
    return False, None

# ---------- Investment ----------
def get_plans():
    data = github_read("invest.json")
    if "plans" not in data:
        data["plans"] = {
            "basic": {"name": "Basic", "profit_percent": 20, "duration_days": 7, "min_amount": 10},
            "premium": {"name": "Premium", "profit_percent": 30, "duration_days": 14, "min_amount": 50},
            "gold": {"name": "Gold", "profit_percent": 40, "duration_days": 30, "min_amount": 100}
        }
        github_write("invest.json", data)
    return data["plans"]

def get_user_investments(user_id):
    data = github_read("invest.json")
    investments = data.get("investments", {})
    return investments.get(str(user_id), [])

def add_investment(user_id, plan_id, amount):
    plans = get_plans()
    if plan_id not in plans:
        return False
    plan = plans[plan_id]
    if amount < plan["min_amount"]:
        return False
    user_balance = get_user_balance(user_id)
    if user_balance < amount:
        return False
    update_balance(user_id, amount, "subtract")
    data = github_read("invest.json")
    if "investments" not in data:
        data["investments"] = {}
    user_invs = data["investments"].get(str(user_id), [])
    end_date = datetime.now() + timedelta(days=plan["duration_days"])
    user_invs.append({
        "plan_id": plan_id,
        "amount": amount,
        "start_date": datetime.now().isoformat(),
        "end_date": end_date.isoformat(),
        "status": "active",
        "profit_added": False
    })
    data["investments"][str(user_id)] = user_invs
    github_write("invest.json", data)
    add_transaction(user_id, "investment", amount, "completed", f"Invested in {plan['name']}")
    return True

def process_auto_profit():
    while True:
        time.sleep(86400)  # 24 hours
        logger.info("Checking investments for profit...")
        data = github_read("invest.json")
        if "investments" not in data:
            continue
        changed = False
        for uid_str, inv_list in data["investments"].items():
            uid = int(uid_str)
            for inv in inv_list:
                if inv["status"] == "active" and not inv.get("profit_added", False):
                    end_date = datetime.fromisoformat(inv["end_date"])
                    if datetime.now() >= end_date:
                        profit = inv["amount"] * (get_plans()[inv["plan_id"]]["profit_percent"] / 100)
                        update_balance(uid, profit, "add")
                        add_transaction(uid, "profit", profit, "completed", f"Profit from {inv['plan_id']} investment")
                        inv["status"] = "completed"
                        inv["profit_added"] = True
                        changed = True
        if changed:
            github_write("invest.json", data)

threading.Thread(target=process_auto_profit, daemon=True).start()

# ---------- Settings ----------
def get_settings():
    settings = github_read("settings.json")
    if not settings:
        settings = {
            "referral_bonus": 0.01,
            "deposit_enabled": True,
            "withdraw_enabled": True,
            "maintenance_mode": False
        }
        github_write("settings.json", settings)
    return settings

def update_settings(key, value):
    settings = get_settings()
    settings[key] = value
    github_write("settings.json", settings)

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
    if is_user_banned(user_id):
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
        user_data = {
            "id": user_id,
            "username": message.from_user.username,
            "first_name": message.from_user.first_name,
            "joined": datetime.now().isoformat(),
            "balance": 0.05,
            "referred_by": None,
            "referrals": [],
            "transactions": [],
            "banned": False
        }
        save_user(user_id, user_data)
        add_transaction(user_id, "signup_bonus", 0.05, "completed")
        if ref_by and ref_by != user_id:
            add_referral(user_id, ref_by)
        bot.send_message(message.chat.id, welcome_message(message.from_user.first_name), parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, f"👋 Welcome back {message.from_user.first_name}!")

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
    bal = get_user_balance(m.from_user.id)
    user = get_user(m.from_user.id)
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
    # Show rate info
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
        # Show conversion
        confirm = f"✅ You sent {amount_bdt} BDT → will receive ${usd_amount:.2f} USD.\n\nConfirm? (yes/no)"
        msg = bot.send_message(m.chat.id, confirm)
        # Store temp data
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
        # Clean up temp
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
        bal = get_user_balance(m.from_user.id)
        if bal < amount:
            bot.send_message(m.chat.id, "❌ Insufficient balance.")
            return
        # Show estimated BDT after charge
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
        # Ask for method
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
    invs = get_user_investments(m.from_user.id)
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
        text += f"${p['amount']} on {p['timestamp'][:10]}\n"
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
    bal = user.get("balance", 0.0)
    text = f"👤 <b>Name:</b> {user.get('first_name', 'N/A')}\n🆔 <b>ID:</b> {m.from_user.id}\n💰 <b>Balance:</b> ${bal:.2f}\n📅 <b>Joined:</b> {user.get('joined', 'N/A')}"
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
    users = github_read("users.json")
    text = f"👥 <b>Total Users:</b> {len(users)}\n"
    for uid, u in list(users.items())[:10]:
        text += f"{uid} - {u.get('first_name')} (${u.get('balance',0)})\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "💰 Balance" and is_admin(m.from_user.id))
def admin_balance(m):
    msg = bot.send_message(m.chat.id, "💸 <b>Send:</b> <code>user_id amount</code> (e.g., 123456 10)", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_balance_admin)

def add_balance_admin(m):
    try:
        parts = m.text.split()
        uid = int(parts[0])
        amt = float(parts[1])
        update_balance(uid, amt, "add")
        add_transaction(uid, "admin_add", amt, "completed", "Added by admin")
        bot.send_message(m.chat.id, f"✅ Added ${amt} to user {uid}")
    except:
        bot.send_message(m.chat.id, "❌ Invalid format. Use: user_id amount")

@bot.message_handler(func=lambda m: m.text == "📥 Deposit" and is_admin(m.from_user.id))
def admin_deposits(m):
    pending = get_pending_deposits()
    if not pending:
        bot.send_message(m.chat.id, "📭 No pending deposits.")
        return
    for req_id, req in pending.items():
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_dep|{req_id}"),
                   InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_dep|{req_id}"))
        bot.send_message(m.chat.id,
                         f"📥 <b>Deposit Request</b>\nUser: <code>{req['user_id']}</code>\nAmount: {req['amount_bdt']} BDT\nTXID: <code>{req['txid']}</code>",
                         reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📤 Withdraw" and is_admin(m.from_user.id))
def admin_withdraws(m):
    pending = get_pending_withdraws()
    if not pending:
        bot.send_message(m.chat.id, "📭 No pending withdrawals.")
        return
    for req_id, req in pending.items():
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_wd|{req_id}"),
                   InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_wd|{req_id}"))
        bot.send_message(m.chat.id,
                         f"📤 <b>Withdraw Request</b>\nUser: <code>{req['user_id']}</code>\nAmount: ${req['amount_usd']}\nMethod: {req['method']}\nAccount: <code>{req['account']}</code>",
                         reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📊 Stats" and is_admin(m.from_user.id))
def admin_stats(m):
    users = github_read("users.json")
    total_balance = sum(u.get("balance", 0) for u in users.values())
    inv_data = github_read("invest.json")
    total_invested = 0
    for invs in inv_data.get("investments", {}).values():
        total_invested += sum(i["amount"] for i in invs)
    text = f"📊 <b>Stats:</b>\n👥 Users: {len(users)}\n💰 Total Balance: ${total_balance:.2f}\n💸 Total Invested: ${total_invested:.2f}"
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast" and is_admin(m.from_user.id))
def admin_broadcast(m):
    msg = bot.send_message(m.chat.id, "📢 <b>Send broadcast message:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, broadcast_msg)

def broadcast_msg(m):
    text = m.text
    users = github_read("users.json")
    count = 0
    for uid in users.keys():
        try:
            bot.send_message(int(uid), text)
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
    bot.send_message(m.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🛑 Ban" and is_admin(m.from_user.id))
def admin_ban(m):
    msg = bot.send_message(m.chat.id, "🚫 <b>Enter user ID to ban:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, ban_user)

def ban_user(m):
    try:
        uid = int(m.text)
        users = github_read("users.json")
        if str(uid) in users:
            users[str(uid)]["banned"] = True
            github_write("users.json", users)
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
        admins = github_read("admins.json")
        if "admins" not in admins:
            admins["admins"] = []
        if str(uid) not in admins["admins"]:
            admins["admins"].append(str(uid))
            github_write("admins.json", admins)
            bot.send_message(m.chat.id, f"✅ User {uid} is now an admin.")
        else:
            bot.send_message(m.chat.id, f"❌ User {uid} is already an admin.")
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
        admins = github_read("admins.json")
        if "admins" not in admins:
            admins["admins"] = []
        if str(uid) in admins["admins"]:
            admins["admins"].remove(str(uid))
            github_write("admins.json", admins)
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
        update_settings("referral_bonus", new_bonus)
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
        update_settings("deposit_enabled", settings["deposit_enabled"])
        msg = "Deposit is now " + ("✅ enabled" if settings["deposit_enabled"] else "❌ disabled")
    elif action == "withdraw":
        settings["withdraw_enabled"] = not settings.get("withdraw_enabled", True)
        update_settings("withdraw_enabled", settings["withdraw_enabled"])
        msg = "Withdraw is now " + ("✅ enabled" if settings["withdraw_enabled"] else "❌ disabled")
    elif action == "maintenance":
        settings["maintenance_mode"] = not settings.get("maintenance_mode", False)
        update_settings("maintenance_mode", settings["maintenance_mode"])
        msg = "Maintenance mode is now " + ("🔧 ON" if settings["maintenance_mode"] else "✅ OFF")
    else:
        msg = "Unknown action"
    bot.answer_callback_query(call.id, msg)
    bot.edit_message_text("✅ Settings updated. Use /admin again to see changes.", call.message.chat.id, call.message.message_id)

# ------------------- Admin Approval Callbacks -------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_approve_dep|"))
def approve_dep_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Not admin")
        return
    req_id = call.data.split("|")[1]
    success, req = approve_deposit(req_id)
    if success:
        bot.answer_callback_query(call.id, "✅ Deposit approved!")
        bot.edit_message_text("✅ Deposit approved.", call.message.chat.id, call.message.message_id)
        user_id = req["user_id"]
        bot.send_message(user_id, "✅ Your deposit has been approved! Balance updated.")
        msg_text = f"📢 Deposit approved!\nUser: {user_id}\nAmount: {req['amount_bdt']} BDT\nTXID: {req['txid']}"
        try:
            bot.send_message(FORCE_CHANNEL, msg_text)
            bot.send_message(FORCE_GROUP, msg_text)
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
    success, req = reject_deposit(req_id)
    if success:
        bot.answer_callback_query(call.id, "❌ Deposit rejected.")
        bot.edit_message_text("❌ Deposit rejected.", call.message.chat.id, call.message.message_id)
        user_id = req["user_id"]
        bot.send_message(user_id, "❌ Your deposit request was rejected.")
        msg_text = f"❌ Deposit rejected!\nUser: {user_id}\nAmount: {req['amount_bdt']} BDT\nTXID: {req['txid']}"
        try:
            bot.send_message(FORCE_CHANNEL, msg_text)
            bot.send_message(FORCE_GROUP, msg_text)
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
    success, req = approve_withdraw(req_id)
    if success:
        bot.answer_callback_query(call.id, "✅ Withdraw approved!")
        bot.edit_message_text("✅ Withdraw approved.", call.message.chat.id, call.message.message_id)
        user_id = req["user_id"]
        amount = req["amount_usd"]
        bot.send_message(user_id, f"✅ Your withdrawal of ${amount} has been approved and sent.")
        msg_text = f"📢 Withdrawal approved!\nUser: {user_id}\nAmount: ${amount}\nMethod: {req['method']}\nAccount: {req['account']}"
        try:
            bot.send_message(FORCE_CHANNEL, msg_text)
            bot.send_message(FORCE_GROUP, msg_text)
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
    success, req = reject_withdraw(req_id)
    if success:
        bot.answer_callback_query(call.id, "❌ Withdraw rejected.")
        bot.edit_message_text("❌ Withdraw rejected.", call.message.chat.id, call.message.message_id)
        user_id = req["user_id"]
        bot.send_message(user_id, "❌ Your withdrawal request was rejected.")
        msg_text = f"❌ Withdrawal rejected!\nUser: {user_id}\nAmount: ${req['amount_usd']}\nMethod: {req['method']}"
        try:
            bot.send_message(FORCE_CHANNEL, msg_text)
            bot.send_message(FORCE_GROUP, msg_text)
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