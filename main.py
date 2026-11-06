import logging
import io
import os
import re
import threading
from html import escape

import requests
import telebot
from PIL import Image, ImageDraw, ImageFilter
from telebot import types

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_URL = "https://app-creator-x.ueu.workers.dev/api/v1/build"
BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apk_converter_banner_bn.png")

MAX_HTML_SIZE = 10 * 1024 * 1024
MAX_ZIP_SIZE = 10 * 1024 * 1024
MAX_ICON_SIZE = 6 * 1024 * 1024
MAX_ICON_PIXELS = 4_000_000
MAX_TOTAL_BODY = 16_842_752
MAX_TOTAL_FILES = MAX_TOTAL_BODY - 64 * 1024
MAX_APP_NAME_UTF16 = 80
MAX_APP_NAME_UTF8 = 120

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("apk_converter_bot")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise SystemExit("দয়া করে চালুর আগে সঠিক BOT_TOKEN এনভায়রনমেন্ট ভেরিয়েবল সেট করুন।")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
sessions = {}


def ensure_banner():
    if os.path.exists(BANNER_PATH):
        return
    w, h = 1200, 675
    im = Image.new("RGB", (w, h), "#0d1326")
    px = im.load()
    for y in range(h):
        for x in range(w):
            t = (x / w + y / h) / 2
            px[x, y] = (int(10 + 20 * t), int(16 + 30 * t), int(45 + 60 * t))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((760, -160, 1320, 400), fill=(101, 88, 255, 110))
    gd.ellipse((-180, 410, 430, 1030), fill=(0, 220, 200, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    im = Image.alpha_composite(im.convert("RGBA"), glow)
    d = ImageDraw.Draw(im)
    phone = (435, 85, 765, 590)
    d.rounded_rectangle(phone, radius=42, fill="#1c2544", outline="#5e6db2", width=7)
    d.rounded_rectangle((458, 132, 742, 535), radius=14, fill="#080e22")
    d.rounded_rectangle((555, 101, 645, 119), radius=9, fill="#5e6db2")
    d.ellipse((579, 551, 621, 593), fill="#5e6db2")
    d.rounded_rectangle((535, 230, 665, 390), radius=22, fill="#665cff")
    d.polygon([(600, 198), (646, 240), (554, 240)], fill="#8d86ff")
    d.line((574, 219, 560, 200), fill="#8d86ff", width=8)
    d.line((626, 219, 640, 200), fill="#8d86ff", width=8)
    d.ellipse((568, 278, 584, 294), fill="white")
    d.ellipse((616, 278, 632, 294), fill="white")
    d.rounded_rectangle((570, 330, 630, 347), radius=8, fill="#b9b5ff")
    im.convert("RGB").save(BANNER_PATH, "PNG", optimize=True)


def kb_main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🚀 নতুন অ্যাপ তৈরি করুন", callback_data="menu:new"))
    kb.add(
        types.InlineKeyboardButton("📘 ব্যবহারের নিয়ম", callback_data="menu:help"),
        types.InlineKeyboardButton("ℹ️ বিবরণ ও তথ্য", callback_data="menu:about"),
    )
    return kb


def kb_cancel():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✖️ প্রক্রিয়া বাতিল করুন", callback_data="flow:cancel"))
    return kb


def kb_back():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ প্রধান মেন্যুতে ফিরুন", callback_data="menu:home"))
    return kb


def kb_confirm():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⚡ এখনই বিল্ড শুরু করুন", callback_data="flow:build"),
        types.InlineKeyboardButton("✏️ নাম পরিবর্তন", callback_data="flow:rename"),
    )
    kb.add(types.InlineKeyboardButton("✖️ বাতিল করুন", callback_data="flow:cancel"))
    return kb


def progress(step, label):
    labels = ["নামকরণ", "ওয়েবসাইট", "আইকন", "যাচাইকরণ", "বিল্ড প্রসেস"]
    cells = ["✅" if i < step else ("🟢" if i == step else "⚪") for i in range(5)]
    return f"<b>ধাপ {step + 1}/৫ ➔ {label}</b>\n{'  '.join(cells)}\n\n"


def reset(chat_id):
    sessions.pop(chat_id, None)


def safe_name(value):
    return escape(value or "")


def validate_app_name(value):
    value = (value or "").strip()
    if not value:
        return "দয়া করে একটি সঠিক অ্যাপের নাম পাঠান।"
    utf16_units = len(value.encode("utf-16-le")) // 2
    utf8_bytes = len(value.encode("utf-8"))
    if utf16_units > MAX_APP_NAME_UTF16:
        return f"নামটি অতিরিক্ত বড় হয়ে গেছে ({utf16_units}/৮০ ক্যারেক্টার লিমিট)।"
    if utf8_bytes > MAX_APP_NAME_UTF8:
        return f"নামের বাইট সাইজ বেশি ({utf8_bytes}/১২০ বাইট লিমিট)।"
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return "অ্যাপের নামে কোনো নিষিদ্ধ বা কন্ট্রোল ক্যারেক্টার ব্যবহার করা যাবে না।"
    return None


def validate_icon(content):
    if not content:
        return "আইকন ফাইলটি শূন্য বা খালি।"
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ("PNG", "JPEG"):
                return "শুধুমাত্র PNG এবং JPEG ফরম্যাটের আইকন গ্রহণ করা হবে।"
            width, height = image.size
            if width * height > MAX_ICON_PIXELS:
                return "আইকন অতিরিক্ত বড়: সর্বোচ্চ ৪ মেগাপিক্সেল গ্রহণযোগ্য।"
            if image.format == "PNG" and image.info.get("interlace"):
                return "ইন্টারলেসড (Interlaced) PNG সমর্থিত নয়। সাধারণ রেগুলার PNG দিন।"
    except Exception:
        return "ফাইলটি বৈধ ছবি (PNG/JPEG) নয়।"
    return None


def send_home(chat_id, intro=True):
    text = (
        "<b>📱 APK কনভার্টার স্টুডিও প্রো</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 ওয়েবসাইট / কোড ➔ 📱 অ্যান্ড্রয়েড APK\n\n"
        "আপনার যেকোনো HTML সাইট বা জিপ প্রজেক্টকে কয়েক ক্লিকেই সম্পূর্ণ স্বয়ংক্রিয়ভাবে রূপান্তর করুন মোবাইল অ্যাপে।\n\n"
        "⚡ <i>তাত্ক্ষণিক বিল্ড • V2 সাইনিং • টেস্ট উপযোগী</i>"
        if intro else
        "<b>📱 APK কনভার্টার স্টুডিও</b>\n\nনিচের অপশনগুলো থেকে আপনার কাজ বেছে নিন:"
    )
    if os.path.exists(BANNER_PATH):
        with open(BANNER_PATH, "rb") as photo:
            bot.send_photo(chat_id, photo, caption=text, reply_markup=kb_main_menu())
    else:
        bot.send_message(chat_id, text, reply_markup=kb_main_menu())


def flow_screen(chat_id, text, markup=None, source=None):
    session = sessions.get(chat_id)
    if session is None:
        return None
    msg = source
    if msg is None:
        msg = bot.send_message(chat_id, text, reply_markup=markup)
    elif msg.content_type == "photo":
        bot.edit_message_caption(text, chat_id, msg.message_id, reply_markup=markup)
    else:
        bot.edit_message_text(text, chat_id, msg.message_id, reply_markup=markup)
    session["ui_message"] = msg
    return msg


def flow_next(chat_id, text, handler, *args, source=None, markup=None):
    msg = flow_screen(chat_id, text, markup or kb_cancel(), source=source)
    bot.register_next_step_handler(msg, handler, *args)
    return msg


def edit_ui_message(message, text, markup=None):
    try:
        if message.content_type == "photo":
            bot.edit_message_caption(text, message.chat.id, message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)
    except Exception as exc:
        logger.debug("UI আপডেট এরর: %s", exc)


def api_error_message(resp):
    try:
        payload = resp.json()
        detail = payload.get("error", resp.text)
    except ValueError:
        detail = resp.text or f"HTTP Status {resp.status_code}"
    status_help = {
        400: "অ্যাপের নাম এবং আপলোড করা ফাইল ফরম্যাট চেক করুন।",
        413: "ফাইল সাইজ বেশি বড় হয়ে গেছে। সাইজ কমিয়ে দিন।",
        415: "ভুল ফাইল ফরম্যাট পাঠানো হয়েছে।",
        429: "একসাথে অনেক রিকোয়েস্ট এসেছে। ১ মিনিট অপেক্ষা করুন।",
        500: "বিল্ড সার্ভারে সাময়িক সমস্যা। কিছুক্ষণ পর ছোট ফাইল দিয়ে ট্রাই করুন।",
    }.get(resp.status_code, "পুনরায় চেষ্টা করুন।")
    return f"<b>সমস্যা:</b> {escape(str(detail))}\n\n💡 <i>পরামর্শ: {status_help}</i>"


def animate_build(message, stop_event):
    """মারাত্মক ফিউচারিস্টিক প্রোগ্রেস এনিমেশন"""
    spinners = ["🌕", "🌖", "🌗", "🌘", "🌑", "🌒", "🌓", "🌔"]
    cyber_bars = [
        "▒▒▒▒▒▒▒▒▒▒",
        "█▒▒▒▒▒▒▒▒▒",
        "███▒▒▒▒▒▒▒",
        "█████▒▒▒▒▒",
        "███████▒▒▒",
        "█████████▒",
        "██████████"
    ]
    
    stages = [
        ("ক্লাউডে ফাইলসমূহ ট্রান্সফার করা হচ্ছে...", "📡", 15, 1, "আর্কাইভ ভেরিফিকেশন চলছে"),
        ("ওয়েবভিউ কাঠামো ও রেন্ডার ইঞ্জিন ইনিশিয়ালাইজ...", "🧩", 38, 2, "জাভাস্ক্রিপ্ট ইন্টারফেস লোড হচ্ছে"),
        ("Android DEX ও রিসোর্স সংকলন চলছে...", "⚙️", 62, 3, "লেআউট ও এসেট মার্জিং"),
        ("টেস্ট কী দিয়ে ডিজিটাল সাইনিং সম্পন্ন হচ্ছে...", "🔐", 85, 5, "V2 ক্রিপ্টো সিগনেচার"),
        ("চূড়ান্ত APK প্যাকেজিং ও চেকসাম তৈরি...", "📦", 96, 6, "ডাউনলোড লিঙ্ক জেনারেট হচ্ছে"),
    ]
    
    index = 0
    while not stop_event.is_set():
        stage_idx = (index // 2) % len(stages)
        label, icon, percent, bar_idx, sub_text = stages[stage_idx]
        spinner = spinners[index % len(spinners)]
        bar = cyber_bars[bar_idx]

        text = (
            f"<b>{spinner} APK কনভার্টার ইঞ্জিন [বিল্ড মোড]</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>⚙️ প্রসেসিং চলছে...</b>\n\n"
            f"{icon} <b>ধাপ:</b> {label}\n"
            f"📊 <b>অগ্রগতি:</b> <code>[{bar}]</code> <b>{percent}%</b>\n"
            f"🔄 <i>{sub_text}...</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "☕ <i>দয়া করে ২ মিনিট অপেক্ষা করুন, ফাইলটি কম্পাইল করা হচ্ছে...</i>"
        )
        edit_ui_message(message, text)
        index += 1
        stop_event.wait(1.1)


HELP_TEXT = (
    "<b>📘 কিভাবে ব্যবহার করবেন?</b>\n\n"
    "<b>১.</b> <b>নতুন অ্যাপ তৈরি করুন</b> বাটনে ক্লিক করুন।\n"
    "<b>২.</b> আপনার অ্যাপের নাম বাংলায় বা ইংরেজিতে লিখুন।\n"
    "<b>৩.</b> ওয়েবসাইটের <code>.html</code> ফাইল অথবা <code>index.html</code> সহ <code>.zip</code> আপলোড দিন (সর্বোচ্চ ১০ MB)।\n"
    "<b>৪.</b> সুন্দর একটি PNG বা JPG আইকন পাঠান (সর্বোচ্চ ৬ MB)।\n"
    "<b>৫.</b> রিভিউ করে <b>বিল্ড শুরু করুন</b> বাটনে ক্লিক করুন।\n\n"
    "⚠️ <i>মনে রাখবেন: এই অ্যাপগুলো সরাসরি ইনস্টল করে টেস্ট করার জন্য সাময়িক ডিজিটাল সাইন দিয়ে তৈরি করা হয়।</i>"
)

ABOUT_TEXT = (
    "<b>ℹ️ অ্যাপ কনভার্টার স্টুডিও সম্পর্কে</b>\n\n"
    "একটি সম্পূর্ণ শক্তিশালী অটোমেটেড টেলিগ্রাম বট যা যেকোনো ওয়েব প্রজেক্টকে স্বয়ংক্রিয়ভাবে মোবাইল অ্যাপে রূপান্তর করে।\n\n"
    "🔹 <b>সাপোর্ট:</b> HTML, HTM, ZIP, PNG, JPEG\n"
    "🔹 <b>সিস্টেম:</b> Android 6.0 থেকে লেটেস্ট সকল ভার্সন\n\n"
    "<i>অত্যন্ত সহজে এবং দ্রুততম সময়ে আপনার প্রজেক্ট টেস্ট করুন।</i>"
)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    reset(message.chat.id)
    send_home(message.chat.id)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=kb_back())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    reset(message.chat.id)
    bot.send_message(message.chat.id, "✖️ <b>সবকিছু বাতিল করা হয়েছে!</b>\n\nপুনরায় শুরু করতে নিচের মেন্যু দেখুন।", reply_markup=kb_main_menu())


@bot.message_handler(commands=["newapk"])
def cmd_newapk(message):
    start_flow(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:home")
def cb_home(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id)
    text = "<b>📱 APK কনভার্টার স্টুডিও</b>\n\nনিচের অপশনগুলো থেকে বেছে নিন:"
    if call.message.content_type == "photo":
        bot.edit_message_caption(text, call.message.chat.id, call.message.message_id, reply_markup=kb_main_menu())
    else:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "menu:help")
def cb_help(call):
    bot.answer_callback_query(call.id)
    if call.message.content_type == "photo":
        bot.edit_message_caption(HELP_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back())
    else:
        bot.edit_message_text(HELP_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:about")
def cb_about(call):
    bot.answer_callback_query(call.id)
    if call.message.content_type == "photo":
        bot.edit_message_caption(ABOUT_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back())
    else:
        bot.edit_message_text(ABOUT_TEXT, call.message.chat.id, call.message.message_id, reply_markup=kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:new")
def cb_new(call):
    bot.answer_callback_query(call.id)
    start_flow(call.message.chat.id, edit=call.message)


@bot.callback_query_handler(func=lambda c: c.data == "flow:cancel")
def cb_cancel(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id, "বাতিল করা হয়েছে")
    text = "✖️ <b>কাজটি বাতিল করা হয়েছে!</b>\n\nনতুন কিছু করতে চাইলে নিচের অপশন বেছে নিন:"
    try:
        if call.message.content_type == "photo":
            bot.edit_message_caption(text, call.message.chat.id, call.message.message_id, reply_markup=kb_main_menu())
        else:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb_main_menu())
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "flow:rename")
def cb_rename(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(call.id, "সেশনের সময় শেষ! আবার শুরু করুন।")
        return
    bot.answer_callback_query(call.id)
    sessions[chat_id]["ui_message"] = call.message
    flow_next(
        chat_id, 
        progress(0, "নতুন নাম") + "📛 <b>আপনার অ্যাপের নতুন নামটি লিখে পাঠান:</b>\n\nসর্বোচ্চ ৮০ অক্ষরের মধ্যে নাম দিন।", 
        step_app_name, 
        True
    )


@bot.callback_query_handler(func=lambda c: c.data == "flow:build")
def cb_build(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions or "icon" not in sessions[chat_id]:
        bot.answer_callback_query(call.id, "সেশন পাওয়া যায়নি! নতুন করে শুরু করুন।")
        return
    bot.answer_callback_query(call.id, "বিল্ড শুরু হয়েছে!")
    sessions[chat_id]["ui_message"] = call.message
    flow_screen(
        chat_id, 
        progress(4, "বিল্ড প্রসেস") + "⚙️ <b>বিল্ড ইঞ্জিন সক্রিয় করা হচ্ছে...</b>\n\n<i>দয়া করে ২ মিনিট অপেক্ষা করুন।</i>", 
        source=call.message
    )
    threading.Thread(target=build_apk, args=(chat_id,), daemon=True).start()


def start_flow(chat_id, edit=None):
    sessions[chat_id] = {}
    text = progress(0, "নামকরণ") + "📛 <b>আপনার অ্যাপের নাম কী রাখতে চান?</b>\n\n১ থেকে ৮০ অক্ষরের মধ্যে নাম লিখে মেসেজ দিন:"
    flow_next(chat_id, text, step_app_name, source=edit)


def step_app_name(message, rename=False):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    name = (message.text or "").strip()
    name_error = validate_app_name(name)
    if name_error:
        flow_next(
            chat_id, 
            progress(0, "নামকরণ") + f"⚠️ <b>সঠিক নাম দিন:</b>\n\n{escape(name_error)}\n\nআবার চেষ্টা করুন:", 
            step_app_name, 
            rename
        )
        return
    sessions[chat_id]["app_name"] = name
    if rename:
        show_confirmation(chat_id)
        return
    flow_next(
        chat_id, 
        progress(1, "ওয়েবসাইট") + f"✅ নাম নির্ধারিত: <b>{safe_name(name)}</b>\n\n📄 <b>আপনার ওয়েবসাইটের ফাইল দিন:</b>\nএকটি <code>.html</code>, <code>.htm</code> অথবা <code>.zip</code> ফাইল ডকুমেন্ট হিসেবে পাঠান:", 
        step_html_file
    )


def step_html_file(message):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    if not message.document:
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট") + "⚠️ <b>ডকুমেন্ট আবশ্যক!</b>\n\nওয়েবসাইটের ফাইলটি ডকুমেন্ট হিসেবে এটাচ করে পাঠান:", 
            step_html_file
        )
        return
    doc = message.document
    filename = doc.file_name or "index.html"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("html", "htm", "zip"):
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট") + "⚠️ <b>ভুল ফাইল ফরম্যাট!</b>\n\nশুধুমাত্র .html, .htm অথবা .zip ফাইল গ্রহণ করা হয়:", 
            step_html_file
        )
        return
    limit = MAX_ZIP_SIZE if ext == "zip" else MAX_HTML_SIZE
    if doc.file_size and doc.file_size > limit:
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট") + f"⚠️ <b>ফাইল সাইজ বেশি!</b>\n\nসর্বোচ্চ লিমিট {limit // (1024*1024)} MB। ছোট ফাইল দিন:", 
            step_html_file
        )
        return
    try:
        info = bot.get_file(doc.file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("ওয়েবসাইট ফাইল ডাউনলোড ফেইল্ড")
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট") + f"⚠️ <b>ডাউনলোড ব্যর্থ!</b>\n\n{escape(str(exc))}\n\nপুনরায় চেষ্টা করুন:", 
            step_html_file
        )
        return
    sessions[chat_id]["html"] = (filename, content)
    flow_next(
        chat_id, 
        progress(2, "আইকন") + f"✅ ওয়েবসাইট ফাইল গৃহীত: <code>{escape(filename)}</code>\n\n🖼️ <b>অ্যাপের আইকন পাঠান:</b>\nএকটি ক্লিয়ার PNG অথবা JPEG ছবি সেন্ড করুন (সর্বোচ্চ ৬ MB):", 
        step_icon_file
    )


def step_icon_file(message):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    filename = "icon.jpg"
    if message.document:
        doc = message.document
        filename = doc.file_name or "icon.png"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("png", "jpg", "jpeg"):
            flow_next(
                chat_id, 
                progress(2, "আইকন") + "⚠️ <b>ছবি ফরম্যাট ভুল!</b>\n\nশুধুমাত্র PNG অথবা JPEG দিন:", 
                step_icon_file
            )
            return
        if doc.file_size and doc.file_size > MAX_ICON_SIZE:
            flow_next(
                chat_id, 
                progress(2, "আইকন") + "⚠️ <b>ছবি সাইজ বেশি!</b>\n\nসর্বোচ্চ ৬ MB ছবি সমর্থিত:", 
                step_icon_file
            )
            return
        file_id = doc.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    else:
        flow_next(
            chat_id, 
            progress(2, "আইকন") + "⚠️ <b>ছবি পাঠান!</b>\n\nঅ্যাপ আইকন হিসেবে একটি ছবি পাঠাতে হবে:", 
            step_icon_file
        )
        return
    try:
        info = bot.get_file(file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("আইকন ডাউনলোড ফেইল্ড")
        flow_next(
            chat_id, 
            progress(2, "আইকন") + f"⚠️ <b>ডাউনলোড সমস্যা!</b>\n\n{escape(str(exc))}\n\nআবার চেষ্টা করুন:", 
            step_icon_file
        )
        return
    if len(content) > MAX_ICON_SIZE:
        flow_next(
            chat_id, 
            progress(2, "আইকন") + "⚠️ <b>আইকন সাইজ বেশি!</b>\n\nসর্বোচ্চ ৬ MB ছবি সমর্থিত:", 
            step_icon_file
        )
        return
    icon_error = validate_icon(content)
    if icon_error:
        flow_next(
            chat_id, 
            progress(2, "আইকন") + f"⚠️ <b>ভুল ছবি:</b>\n\n{escape(icon_error)}\n\nঅন্য ছবি সেন্ড করুন:", 
            step_icon_file
        )
        return
    html_size = len(sessions[chat_id]["html"][1])
    if html_size + len(content) > MAX_TOTAL_FILES:
        reset(chat_id)
        bot.send_message(
            chat_id, 
            "⚠️ <b>ফাইলের সমন্বিত সাইজ বেশি হয়ে গেছে!</b>\n\nদয়া করে ছোট ফাইল নিয়ে আবার চেষ্টা করুন।", 
            reply_markup=kb_main_menu()
        )
        return
    sessions[chat_id]["icon"] = (filename, content)
    show_confirmation(chat_id)


def show_confirmation(chat_id):
    session = sessions[chat_id]
    html_name, html_bytes = session["html"]
    icon_name, icon_bytes = session["icon"]
    text = (
        progress(3, "যাচাইকরণ") + "<b>📋 সবকিছু তৈরি! নিচের তথ্য মিলিয়ে নিন:</b>\n\n"
        f"📛 <b>অ্যাপের নাম:</b> {safe_name(session['app_name'])}\n"
        f"📄 <b>ওয়েবসাইট:</b> <code>{escape(html_name)}</code> ({len(html_bytes):,} বাইট)\n"
        f"🖼️ <b>অ্যাপ আইকন:</b> <code>{escape(icon_name)}</code> ({len(icon_bytes):,} বাইট)\n\n"
        "সবকিছু ঠিক থাকলে নিচের <b>'এখনই বিল্ড শুরু করুন'</b> বাটনে চাপ দিন।"
    )
    flow_screen(chat_id, text, kb_confirm())


def build_apk(chat_id):
    session = sessions.get(chat_id)
    if not session:
        return
    ui_message = session.get("ui_message")
    stop_event = threading.Event()
    animation_thread = threading.Thread(target=animate_build, args=(ui_message, stop_event), daemon=True) if ui_message else None
    if animation_thread:
        animation_thread.start()
    app_name = session["app_name"]
    html_name, html_bytes = session["html"]
    icon_name, icon_bytes = session["icon"]
    html_mime = "application/zip" if html_name.lower().endswith(".zip") else "text/html"
    icon_mime = "image/png" if icon_name.lower().endswith("png") else "image/jpeg"
    files = {"html": (html_name, html_bytes, html_mime), "icon": (icon_name, icon_bytes, icon_mime)}
    try:
        resp = requests.post(API_URL, data={"app_name": app_name}, files=files, timeout=120)
        stop_event.set()
        if animation_thread:
            animation_thread.join(timeout=2)
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "application/vnd.android.package-archive" in content_type:
            if ui_message:
                edit_ui_message(ui_message, "<b>🎉 কনভার্ট সফল হয়েছে!</b>\n\n📦 আপনার অ্যাপ্লিকেশন তৈরি হয়ে গেছে। ফাইলটি টেলিগ্রামে পাঠানো হচ্ছে...")
            disposition = resp.headers.get("Content-Disposition", "")
            match = re.search(r"filename=(?:\"([^\"]+)\"|([^;]+))", disposition)
            apk_name = (match.group(1) or match.group(2)).strip() if match else f"{app_name}.apk"
            if not apk_name.lower().endswith(".apk"):
                apk_name += ".apk"
            bot.send_document(
                chat_id, 
                (apk_name, resp.content), 
                caption=f"🚀 <b>আপনার অ্যাপ্লিকেশন তৈরি সম্পন্ন!</b>\n\n"
                        f"📱 <b>অ্যাপ ফাইল:</b> <code>{escape(apk_name)}</code>\n"
                        f"🏷 <b>অ্যাপের নাম:</b> {safe_name(app_name)}\n\n"
                        f"⚠️ <i>এটি একটি টেস্ট APK। যেকোনো অ্যান্ড্রয়েড ফোনে টেস্ট করতে পারবেন।</i>", 
                reply_markup=kb_main_menu()
            )
        else:
            err_msg = f"❌ <b>বিল্ড ব্যর্থ হয়েছে!</b> (কোড: {resp.status_code})\n\n{api_error_message(resp)}"
            if ui_message:
                edit_ui_message(ui_message, err_msg, kb_main_menu())
            else:
                bot.send_message(chat_id, err_msg, reply_markup=kb_main_menu())
    except requests.RequestException as exc:
        logger.exception("বিল্ড সার্ভিস রিকোয়েস্ট ফেইল্ড")
        stop_event.set()
        if animation_thread:
            animation_thread.join(timeout=2)
        err_msg = f"❌ <b>বিল্ড সার্ভার সংযোগ সমস্যা!</b>\n\n{escape(str(exc))}\n\nদয়া করে কিছুক্ষণ পর পুনরায় চেষ্টা করুন।"
        if ui_message:
            edit_ui_message(ui_message, err_msg, kb_main_menu())
        else:
            bot.send_message(chat_id, err_msg, reply_markup=kb_main_menu())
    finally:
        stop_event.set()
        reset(chat_id)


@bot.message_handler(func=lambda m: True, content_types=["text", "document", "photo"])
def fallback(message):
    bot.send_message(message.chat.id, "নিচের অপশনগুলো থেকে বেছে নিন অথবা বাতিল করতে /cancel কমান্ড দিন:", reply_markup=kb_main_menu())


ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
MONITOR_INTERVAL = 300


def uptime_monitor():
    was_online = None

    while True:
        try:
            bot.get_me()

            if was_online is False and ADMIN_CHAT_ID:
                bot.send_message(
                    ADMIN_CHAT_ID,
                    "✅ <b>বট পুনরায় সক্রিয়!</b>\n\n"
                    "APK কনভার্টার স্টুডিও অনলাইনে সচল রয়েছে।"
                )

            if was_online is not True:
                logger.info("আপটাইম মনিটর: বট অনলাইনে সচল")

            was_online = True

        except Exception as exc:
            logger.error("আপটাইম মনিটর ত্রুটি: %s", exc)

            if was_online is not False and ADMIN_CHAT_ID:
                try:
                    bot.send_message(
                        ADMIN_CHAT_ID,
                        "⚠️ <b>বট সার্ভার বিচ্ছিন্ন!</b>\n\n"
                        "টেলিগ্রাম API কানেকশন ফেইল করেছে। হোস্ট লগ চেক করুন।"
                    )
                except Exception:
                    logger.error("এডমিন সতর্কবার্তা সেন্ড করা যায়নি।")

            was_online = False

        threading.Event().wait(MONITOR_INTERVAL)


if __name__ == "__main__":
    ensure_banner()

    logger.info("APK কনভার্টার স্টুডিও চালু হচ্ছে...")

    monitor_thread = threading.Thread(
        target=uptime_monitor,
        daemon=True
    )
    monitor_thread.start()

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )