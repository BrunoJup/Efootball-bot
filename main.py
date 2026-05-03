import os
import json
import base64
import telebot
import threading
from datetime import datetime
from openai import OpenAI
from flask import Flask
import re

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not BOT_TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or OPENROUTER_API_KEY")

bot = telebot.TeleBot(BOT_TOKEN)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY
)

DB_FILE = "memory.json"
START_BANKROLL = 1000

# Risk controls
MAX_DRAWDOWN = 0.25   # 25%
MAX_LOSS_STREAK = 5

# =========================
# FLASK
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "V4 Betting AI Running 🚀"

# =========================
# DB
# =========================
def load_db():
    if not os.path.exists(DB_FILE):
        return []
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

def add_record(record):
    db = load_db()
    db.append(record)
    save_db(db)

# =========================
# BANKROLL ENGINE
# =========================
def compute_bankroll():
    db = load_db()
    bankroll = START_BANKROLL
    peak = START_BANKROLL
    loss_streak = 0
    max_dd = 0

    for x in db:
        if x["result"] == "WIN":
            bankroll += x.get("profit", 0)
            loss_streak = 0
        elif x["result"] == "LOSS":
            bankroll -= x.get("stake", 0)
            loss_streak += 1

        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return bankroll, loss_streak, max_dd

# =========================
# PARSER
# =========================
def parse_prediction(text):
    try:
        bet = re.search(r"Best Bet:\s*(.*)", text)
        conf = re.search(r"Confidence Level.*:\s*(\d+)", text)

        return {
            "bet": bet.group(1).strip() if bet else "UNKNOWN",
            "confidence": int(conf.group(1)) if conf else 0
        }
    except:
        return {"bet": "UNKNOWN", "confidence": 0}

# =========================
# STATS
# =========================
def stats():
    db = load_db()
    bankroll, _, _ = compute_bankroll()

    wins = len([x for x in db if x["result"] == "WIN"])
    losses = len([x for x in db if x["result"] == "LOSS"])
    total = wins + losses

    roi = ((bankroll - START_BANKROLL) / START_BANKROLL) * 100 if total > 0 else 0

    # bet type tracking
    types = {}
    for x in db:
        if x["result"] in ["WIN", "LOSS"]:
            t = x.get("bet", "UNKNOWN")
            types.setdefault(t, {"w": 0, "l": 0})
            if x["result"] == "WIN":
                types[t]["w"] += 1
            else:
                types[t]["l"] += 1

    breakdown = "\n".join([f"{k}: {v['w']}W-{v['l']}L" for k, v in types.items()])

    return f"""📊 STATS
Bankroll: {bankroll:.2f}
ROI: {roi:.2f}%
Wins: {wins} | Losses: {losses} | Total: {total}

📈 Bet Types:
{breakdown if breakdown else 'No data'}
"""

# =========================
# SYSTEM PROMPT
# =========================
SYSTEM_PROMPT = """
Act as an elite eFootball betting analyst.

STRICT:
- No explanation
- Output only final result

Best Bet:
Safest Option:
Highest Value Option:
Confidence Level (1–10):
Risk Level:

If no edge: PASS
"""

# =========================
# AI
# =========================
def analyze_image(file_bytes):
    base64_image = base64.b64encode(file_bytes).decode("utf-8")

    response = client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyze betting screenshot."},
                    {
                        "type": "image_url",
                        "image_url": f"data:image/jpeg;base64,{base64_image}"
                    }
                ]
            }
        ],
        max_tokens=200
    )

    return response.choices[0].message.content

# =========================
# TELEGRAM
# =========================
@bot.message_handler(commands=["start"])
def start(msg):
    bot.reply_to(msg, "📸 Send screenshot\n/win ID ODDS\n/loss ID\n/stats")

@bot.message_handler(commands=["stats"])
def send_stats(msg):
    bot.reply_to(msg, stats())

@bot.message_handler(commands=["win"])
def win(msg):
    try:
        _, pid, odds = msg.text.split()
        odds = float(odds)

        db = load_db()
        for x in db:
            if x["id"] == pid:
                profit = x["stake"] * (odds - 1)
                x["result"] = "WIN"
                x["profit"] = profit

        save_db(db)
        bot.reply_to(msg, "✅ WIN recorded")
    except:
        bot.reply_to(msg, "Usage: /win ID ODDS")

@bot.message_handler(commands=["loss"])
def loss(msg):
    try:
        _, pid = msg.text.split()

        db = load_db()
        for x in db:
            if x["id"] == pid:
                x["result"] = "LOSS"

        save_db(db)
        bot.reply_to(msg, "❌ LOSS recorded")
    except:
        bot.reply_to(msg, "Usage: /loss ID")

@bot.message_handler(content_types=["photo"])
def photo(msg):
    try:
        bankroll, loss_streak, max_dd = compute_bankroll()

        # HARD RISK STOP
        if loss_streak >= MAX_LOSS_STREAK or max_dd >= MAX_DRAWDOWN:
            bot.reply_to(msg, "⛔ STOP: Risk limits hit")
            return

        file_info = bot.get_file(msg.photo[-1].file_id)
        file = bot.download_file(file_info.file_path)

        result = analyze_image(file)
        parsed = parse_prediction(result)

        if parsed["confidence"] < 7:
            bot.reply_to(msg, "PASS")
            return

        # stake sizing (safe model)
        stake_pct = (parsed["confidence"] / 10) * 0.02
        stake = round(bankroll * stake_pct, 2)

        pred_id = str(int(datetime.now().timestamp()))

        add_record({
            "id": pred_id,
            "time": str(datetime.now()),
            "prediction": result,
            "bet": parsed["bet"],
            "confidence": parsed["confidence"],
            "stake": stake,
            "result": "PENDING"
        })

        bot.reply_to(msg, f"""{result}

💰 Stake: {stake}
📉 Bankroll: {bankroll:.2f}
🆔 ID: {pred_id}
""")

    except Exception as e:
        bot.reply_to(msg, f"Error: {e}")

# =========================
# RUN
# =========================
def run_bot():
    bot.infinity_polling()

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=10000)
