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
BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apk_converter_banner.png")

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
    raise SystemExit("দয়া করে চালুর আগে সঠিক BOT_TOKEN পরিবেশ ভেরিয়েবল সেট করুন।")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
sessions = {}


def ensure_banner():
    if os.path.exists(BANNER_PATH):
        return
    w, h = 1200, 675
    im = Image.new("RGB", (w, h), "#10162d")
    px = im.load()
    for y in range(h):
        for x in range(w):
            t = (x / w + y / h) / 2
            px[x, y] = (int(15 + 18 * t), int(22 + 26 * t), int(53 + 55 * t))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((760, -160, 1320, 400), fill=(101, 88, 255, 95))
    gd.ellipse((-180, 410, 430, 1030), fill=(0, 207, 191, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    im = Image.alpha_composite(im.convert("RGBA"), glow)
    d = ImageDraw.Draw(im)
    phone = (435, 85, 765, 590)
    d.rounded_rectangle(phone, radius=42, fill="#202a4b", outline="#6976b6", width=7)
    d.rounded_rectangle((458, 132, 742, 535), radius=14, fill="#0c1329")
    d.rounded_rectangle((555, 101, 645, 119), radius=9, fill="#6976b6")
    d.ellipse((579, 551, 621, 593), fill="#6976b6")
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
    kb.add(types.InlineKeyboardButton("🚀 নতুন APK তৈরি করুন", callback_data="menu:new"))
    kb.add(
        types.InlineKeyboardButton("📘 ব্যবহারের নিয়ম", callback_data="menu:help"),
        types.InlineKeyboardButton("ℹ️ আমাদের সম্পর্কে", callback_data="menu:about"),
    )
    return kb


def kb_cancel():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✖️ বাতিল করুন", callback_data="flow:cancel"))
    return kb


def kb_back():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ প্রধান মেনুতে ফিরুন", callback_data="menu:home"))
    return kb


def kb_confirm():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⚡ কনভার্ট শুরু করুন", callback_data="flow:build"),
        types.InlineKeyboardButton("✏️ নাম পরিবর্তন", callback_data="flow:rename"),
    )
    kb.add(types.InlineKeyboardButton("✖️ বাতিল করুন", callback_data="flow:cancel"))
    return kb


def progress(step, label):
    labels = ["অ্যাপের নাম", "ওয়েবসাইট ফাইল", "আইকন", "যাচাইকরণ", "বিল্ড"]
    cells = ["✅" if i < step else ("🟢" if i == step else "⚪") for i in range(5)]
    return f"<b>ধাপ {step + 1}/৫ — {label}</b>\n{'  '.join(cells)}\n\n"


def reset(chat_id):
    sessions.pop(chat_id, None)


def safe_name(value):
    return escape(value or "")


def validate_app_name(value):
    """অ্যাপের নাম যাচাইকরণ"""
    value = (value or "").strip()
    if not value:
        return "দয়া করে একটি সঠিক অ্যাপের নাম লিখুন।"
    utf16_units = len(value.encode("utf-16-le")) // 2
    utf8_bytes = len(value.encode("utf-8"))
    if utf16_units > MAX_APP_NAME_UTF16:
        return f"নামটি অনেক বড় হয়ে গেছে ({utf16_units}/৮০ UTF-16 লিমিট)।"
    if utf8_bytes > MAX_APP_NAME_UTF8:
        return f"নামের সাইজ অনেক বেশি ({utf8_bytes}/১২০ বাইট লিমিট)।"
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return "অ্যাপের নামে কোনো নিষিদ্ধ বা কন্ট্রোল ক্যারেক্টার ব্যবহার করা যাবে না।"
    return None


def validate_icon(content):
    """আইকন ফাইল ভ্যালিডেশন"""
    if not content:
        return "আইকন ফাইলটি ফাঁকা।"
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ("PNG", "JPEG"):
                return "শুধুমাত্র PNG এবং JPEG ফরম্যাটের আইকন গ্রহণযোগ্য।"
            width, height = image.size
            if width * height > MAX_ICON_PIXELS:
                return "আইকনের সাইজ অতিরিক্ত বড়: সর্বোচ্চ ৪ মেগাপিক্সেল সমর্থিত।"
            if image.format == "PNG" and image.info.get("interlace"):
                return "ইন্টারলেসড (Interlaced) PNG সমর্থিত নয়। দয়া করে রেগুলার PNG আপলোড করুন।"
    except Exception:
        return "ফাইলটি সঠিক PNG বা JPEG ফরম্যাটের ছবি নয়।"
    return None


def send_home(chat_id, intro=True):
    text = (
        "<b>📱 APK কনভার্টার স্টুডিও</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 ওয়েবসাইট / HTML ➔ 📱 অ্যান্ড্রয়েড APK\n\n"
        "আপনার যেকোনো HTML ওয়েবসাইট বা জিপ (ZIP) প্রজেক্টকে মুহূর্তেই রূপান্তর করুন অ্যান্ড্রয়েড টেস্ট অ্যাপে!\n\n"
        "✨ <i>দ্রুতগতির • ঝামেলাহীন • অটোমেটিক সাইনিং</i>"
        if intro else
        "<b>📱 APK কনভার্টার স্টুডিও</b>\n\nনিচের অপশনগুলো থেকে আপনার পছন্দ বেছে নিন:"
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
        logger.debug("UI স্ট্যাটাস আপডেট ব্যর্থ: %s", exc)


def api_error_message(resp):
    """API এরর রেসপন্স বাংলায় অনুবাদ"""
    try:
        payload = resp.json()
        detail = payload.get("error", resp.text)
    except ValueError:
        detail = resp.text or f"HTTP কোড {resp.status_code}"
    status_help = {
        400: "অ্যাপের নাম এবং আপলোড করা ফাইলগুলো পুনরায় পরীক্ষা করুন।",
        413: "ফাইলের মোট সাইজ অনেক বড় হয়ে গেছে। ছোট ফাইল দিয়ে চেষ্টা করুন।",
        415: "ফাইলের ফরম্যাট সঠিক নয়। অনুগ্রহ করে নিয়ম মেনে আপলোড করুন।",
        429: "সার্ভারে অতিরিক্ত রিকোয়েস্ট জমা পড়েছে। দয়া করে ১ মিনিট পর আবার চেষ্টা করুন।",
        500: "সার্ভারে অপ্রত্যাশিত সমস্যা হয়েছে। ছোট ফাইল দিয়ে আবার চেষ্টা করুন।",
    }.get(resp.status_code, "দয়া করে কিছুক্ষণ পর আবার চেষ্টা করুন।")
    return f"<b>ত্রুটির বিবরণ:</b> {escape(str(detail))}\n\n💡 <i>পরামর্শ: {status_help}</i>"


def animate_build(message, stop_event):
    """মারাত্মক ফিউচারিস্টিক প্রোগ্রেস অ্যানিমেশন লুপ"""
    # স্পিনার ফ্রেম
    spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    
    # ধাপসমূহ ও স্টাইলিশ প্রোগ্রেস ট্র্যাকিং
    stages = [
        ("ক্লাউডে ফাইল আপলোড হচ্ছে...", "🚀", 15, "আর্কাইভ প্যাকেজিং চলছে"),
        ("ওয়েবভিউ কাঠামো সংকলন চলছে...", "🧩", 35, "রিসোর্স কনফিগারেশন হচ্ছে"),
        ("সোর্স কোড প্রসেসিং ও অপটিমাইজেশন...", "⚡", 55, "জাভাস্ক্রিপ্ট ইঞ্জিন রেডি"),
        ("অ্যান্ড্রয়েড প্যাকেজ কম্পাইল হচ্ছে...", "⚙️", 75, "DEX ও বাইনারি জেনারেশন"),
        ("টেস্ট কী দিয়ে ডিজিটাল সাইনিং চলছে...", "🔐", 90, "V2 সিগনেচার ভেরিফাই হচ্ছে"),
        ("চূড়ান্ত APK তৈরি সম্পন্ন হচ্ছে...", "📦", 98, "ডাউনলোড লিঙ্ক তৈরি হচ্ছে"),
    ]
    
    index = 0
    while not stop_event.is_set():
        stage_idx = (index // 3) % len(stages)
        label, icon, percent, sub_status = stages[stage_idx]
        spinner = spinners[index % len(spinners)]

        # মারাত্মক সাইবার প্রোগ্রেস বার (১০ ঘর)
        filled = max(1, percent // 10)
        bar = "▰" * filled + "▱" * (10 - filled)

        text = (
            f"<b>{spinner} APK কনভার্টার ইঞ্জিন সক্রিয়</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>⚙️ বিল্ড প্রসেস চলমান...</b>\n\n"
            f"{icon} <b>বর্তমান ধাপ:</b> {label}\n"
            f"📊 <b>অগ্রগতি:</b> <code>[{bar}]</code> <b>{percent}%</b>\n"
            f"🔍 <i>{sub_status}...</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "☕ <i>দয়া করে অপেক্ষা করুন, আপনার ফাইল নিরাপদে প্রোসেস করা হচ্ছে...</i>"
        )
        edit_ui_message(message, text)
        index += 1
        stop_event.wait(1.2)


HELP_TEXT = (
    "<b>📘 কিভাবে ব্যবহার করবেন?</b>\n\n"
    "<b>১.</b> <b>নতুন APK তৈরি করুন</b> বাটনে ক্লিক করুন।\n"
    "<b>২.</b> আপনার অ্যাপের নাম লিখে পাঠান (১ থেকে ৮০ অক্ষরের মধ্যে)।\n"
    "<b>৩.</b> ওয়েবসাইটের <code>.html</code>, <code>.htm</code> অথবা <code>index.html</code> সহ <code>.zip</code> ফাইল আপলোড করুন (সর্বোচ্চ ১০ মেগাবাইট)।\n"
    "<b>৪.</b> সুন্দর একটি PNG বা JPEG অ্যাপ আইকন দিন (সর্বোচ্চ ৬ মেগাবাইট)।\n"
    "<b>৫.</b> সবকিছু যাচাই করে <b>বিল্ড শুরু করুন</b> বাটনে চাপ দিন।\n\n"
    "⚠️ <i>সতর্কতা: তৈরি হওয়া APK ফাইলগুলো শুধুমাত্র টেস্ট করার উদ্দেশ্যে। প্রতিটি অ্যাপের সাথে নতুন স্বয়ংক্রিয় টেস্ট কী ব্যবহার করা হয়।</i>"
)

ABOUT_TEXT = (
    "<b>ℹ️ আমাদের সম্পর্কে</b>\n\n"
    "<b>APK কনভার্টার স্টুডিও</b> একটি শক্তিশালী এবং সম্পূর্ণ অটোমেটেড টেলিগ্রাম বট।\n\n"
    "🔹 <b>সাপোর্টেড ফরম্যাট:</b> HTML, HTM, ZIP, PNG, JPEG\n"
    "🔹 <b>আউটপুট:</b> অ্যান্ড্রয়েড 6.0+ সমর্থিত রেডি APK\n"
    "🔹 <b>ইঞ্জিন:</b> CreatorX হাইস্পিড বিল্ড সার্ভিস\n\n"
    "<i>যেকোনো ওয়েবসাইটকে অ্যাপে রূপান্তর করার সবচেয়ে সহজ মাধ্যম!</i>"
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
    bot.send_message(message.chat.id, "✖️ <b>বাতিল করা হয়েছে!</b>\n\nপূর্বের সকল তথ্য মুছে ফেলা হয়েছে।", reply_markup=kb_main_menu())


@bot.message_handler(commands=["newapk"])
def cmd_newapk(message):
    start_flow(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:home")
def cb_home(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id)
    text = "<b>📱 APK কনভার্টার স্টুডিও</b>\n\nনিচের অপশনগুলো থেকে আপনার পছন্দ বেছে নিন:"
    if call.message.content_type == "photo":
        bot.edit_message_caption(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb_main_menu(),
        )
    else:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb_main_menu(),
        )


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
    bot.answer_callback_query(call.id, "প্রক্রিয়াটি বাতিল করা হয়েছে")
    text = "✖️ <b>বাতিল করা হয়েছে</b>\n\nনতুন কিছু শুরু করতে নিচের মেনু ব্যবহার করুন:"
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
        bot.answer_callback_query(call.id, "সেশনের মেয়াদ শেষ! নতুন করে শুরু করুন।")
        return
    bot.answer_callback_query(call.id)
    sessions[chat_id]["ui_message"] = call.message
    flow_next(
        chat_id, 
        progress(0, "নতুন নাম নির্ধারণ") + "📛 <b>আপনার অ্যাপের নতুন নামটি লিখুন:</b>\n\nসর্বোচ্চ ৮০ অক্ষরের মধ্যে নাম হতে হবে।", 
        step_app_name, 
        True
    )


@bot.callback_query_handler(func=lambda c: c.data == "flow:build")
def cb_build(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions or "icon" not in sessions[chat_id]:
        bot.answer_callback_query(call.id, "সেশনের মেয়াদ শেষ! আবার শুরু করুন।")
        return
    bot.answer_callback_query(call.id, "বিল্ড প্রক্রিয়া শুরু হয়েছে!")
    sessions[chat_id]["ui_message"] = call.message
    flow_screen(
        chat_id, 
        progress(4, "বিল্ড শুরু হচ্ছে") + "⚙️ <b>বিল্ড প্রসেস শুরু হচ্ছে...</b>\n\n<i>দয়া করে ২ মিনিট অপেক্ষা করুন।</i>", 
        source=call.message
    )
    threading.Thread(target=build_apk, args=(chat_id,), daemon=True).start()


def start_flow(chat_id, edit=None):
    sessions[chat_id] = {}
    text = progress(0, "অ্যাপের নাম") + "📛 <b>আপনার অ্যাপের নাম কি রাখতে চান?</b>\n\n১ থেকে ৮০ অক্ষরের মধ্যে যেকোনো নাম লিখে পাঠান:"
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
            progress(0, "অ্যাপের নাম") + f"⚠️ <b>সঠিক নাম দিন:</b>\n\n{escape(name_error)}\n\nদয়া করে পুনরায় চেষ্টা করুন:", 
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
        progress(1, "ওয়েবসাইট ফাইল") + f"✅ অ্যাপের নাম সংরক্ষিত: <b>{safe_name(name)}</b>\n\n📄 <b>আপনার ওয়েবসাইট আপলোড করুন</b>\nএকটি <code>.html</code>, <code>.htm</code> বা <code>.zip</code> ফাইল ডকুমেন্ট হিসেবে পাঠান:", 
        step_html_file
    )


def step_html_file(message):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    if not message.document:
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট ফাইল") + "⚠️ <b>ফাইল আবশ্যক!</b>\n\nওয়েবসাইটের ফাইলটি ডকুমেন্ট (Document) হিসেবে আপলোড করুন:", 
            step_html_file
        )
        return
    doc = message.document
    filename = doc.file_name or "index.html"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("html", "htm", "zip"):
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট ফাইল") + "⚠️ <b>অসমর্থিত ফরম্যাট!</b>\n\nশুধুমাত্র .html, .htm অথবা .zip ফাইল গ্রহণযোগ্য:", 
            step_html_file
        )
        return
    limit = MAX_ZIP_SIZE if ext == "zip" else MAX_HTML_SIZE
    if doc.file_size and doc.file_size > limit:
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট ফাইল") + f"⚠️ <b>ফাইলের সাইজ অনেক বড়!</b>\n\n.{ext} ফাইলের সর্বোচ্চ সীমা {limit // (1024*1024)} MB। ছোট ফাইল দিন:", 
            step_html_file
        )
        return
    try:
        info = bot.get_file(doc.file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("ওয়েবসাইট ফাইল ডাউনলোড ব্যর্থ")
        flow_next(
            chat_id, 
            progress(1, "ওয়েবসাইট ফাইল") + f"⚠️ <b>ডাউনলোড ব্যর্থ হয়েছে!</b>\n\n{escape(str(exc))}\n\nপুনরায় চেষ্টা করুন:", 
            step_html_file
        )
        return
    sessions[chat_id]["html"] = (filename, content)
    flow_next(
        chat_id, 
        progress(2, "অ্যাপ আইকন") + f"✅ ওয়েবসাইট ফাইল গৃহীত: <code>{escape(filename)}</code>\n\n🖼️ <b>অ্যাপের আইকন পাঠান</b>\nএকটি স্পষ্ট PNG অথবা JPEG ছবি পাঠান (সর্বোচ্চ ৬ MB):", 
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
                progress(2, "অ্যাপ আইকন") + "⚠️ <b>ভুল ফরম্যাট!</b>\n\nশুধুমাত্র PNG অথবা JPEG ফরম্যাটের আইকন পাঠান:", 
                step_icon_file
            )
            return
        if doc.file_size and doc.file_size > MAX_ICON_SIZE:
            flow_next(
                chat_id, 
                progress(2, "অ্যাপ আইকন") + "⚠️ <b>আইকন অনেক বড়!</b>\n\nসর্বোচ্চ সাইজ ৬ MB। ছোট আইকন পাঠান:", 
                step_icon_file
            )
            return
        file_id = doc.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    else:
        flow_next(
            chat_id, 
            progress(2, "অ্যাপ আইকন") + "⚠️ <b>আইকন আবশ্যক!</b>\n\nদয়া করে একটি PNG বা JPEG ছবি পাঠান:", 
            step_icon_file
        )
        return
    try:
        info = bot.get_file(file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("আইকন ডাউনলোড ব্যর্থ")
        flow_next(
            chat_id, 
            progress(2, "অ্যাপ আইকন") + f"⚠️ <b>ডাউনলোড ব্যর্থ!</b>\n\n{escape(str(exc))}\n\nপুনরায় চেষ্টা করুন:", 
            step_icon_file
        )
        return
    if len(content) > MAX_ICON_SIZE:
        flow_next(
            chat_id, 
            progress(2, "অ্যাপ আইকন") + "⚠️ <b>আইকন সাইজ অতিরিক্ত!</b>\n\nসর্বোচ্চ সীমা ৬ MB। ছোট ফাইল পাঠান:", 
            step_icon_file
        )
        return
    icon_error = validate_icon(content)
    if icon_error:
        flow_next(
            chat_id, 
            progress(2, "অ্যাপ আইকন") + f"⚠️ <b>ভুল আইকন ছবি:</b>\n\n{escape(icon_error)}\n\nঅন্য একটি ছবি পাঠান:", 
            step_icon_file
        )
        return
    html_size = len(sessions[chat_id]["html"][1])
    if html_size + len(content) > MAX_TOTAL_FILES:
        reset(chat_id)
        bot.send_message(
            chat_id, 
            "⚠️ <b>ফাইলের সমন্বিত সাইজ বেশি হয়ে গেছে!</b>\n\nওয়েবসাইট এবং আইকন মিলিয়ে সীমা অতিক্রম করেছে। নতুন করে ছোট ফাইল দিয়ে চেষ্টা করুন।", 
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
        progress(3, "যাচাইকরণ") + "<b>📋 সবকিছু তৈরি, তথ্য মিলিয়ে নিন:</b>\n\n"
        f"📛 <b>অ্যাপের নাম:</b> {safe_name(session['app_name'])}\n"
        f"📄 <b>ওয়েবসাইট ফাইল:</b> <code>{escape(html_name)}</code> ({len(html_bytes):,} বাইট)\n"
        f"🖼️ <b>অ্যাপ আইকন:</b> <code>{escape(icon_name)}</code> ({len(icon_bytes):,} বাইট)\n\n"
        "সবকিছু ঠিক থাকলে নিচের <b>'কনভার্ট শুরু করুন'</b> বাটনে চাপ দিন।"
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
                edit_ui_message(ui_message, "<b>🎉 অভিনন্দন! APK বিল্ড সফল হয়েছে!</b>\n\n📦 আপনার অ্যাপ্লিকেশন তৈরি। ফাইলটি পাঠানো হচ্ছে...")
            disposition = resp.headers.get("Content-Disposition", "")
            match = re.search(r"filename=(?:\"([^\"]+)\"|([^;]+))", disposition)
            apk_name = (match.group(1) or match.group(2)).strip() if match else f"{app_name}.apk"
            if not apk_name.lower().endswith(".apk"):
                apk_name += ".apk"
            bot.send_document(
                chat_id, 
                (apk_name, resp.content), 
                caption=f"🚀 <b>আপনার অ্যাপ্লিকেশন তৈরি সম্পন্ন!</b>\n\n"
                        f"📱 <b>ফাইল:</b> <code>{escape(apk_name)}</code>\n"
                        f"🏷 <b>অ্যাপের নাম:</b> {safe_name(app_name)}\n\n"
                        f"⚠️ <i>এটি একটি টেস্ট বিল্ড APK। টেস্ট কী দিয়ে সাইন করা হয়েছে।</i>\n"
                        f"ধন্যবাদ আমাদের সার্ভিস ব্যবহারের জন্য!", 
                reply_markup=kb_main_menu()
            )
        else:
            err_msg = f"❌ <b>বিল্ড ব্যর্থ হয়েছে!</b> (কোড: {resp.status_code})\n\n{api_error_message(resp)}"
            if ui_message:
                edit_ui_message(ui_message, err_msg, kb_main_menu())
            else:
                bot.send_message(chat_id, err_msg, reply_markup=kb_main_menu())
    except requests.RequestException as exc:
        logger.exception("বিল্ড API রিকোয়েস্ট ব্যর্থ")
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
    bot.send_message(message.chat.id, "নিচের অপশনগুলো থেকে বেছে নিন অথবা বাতিল করতে /cancel চাপুন:", reply_markup=kb_main_menu())


# ==========================================
# আপটাইম মনিটর (UPTIME MONITOR)
# ==========================================

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
MONITOR_INTERVAL = 300  # ৫ মিনিট


def uptime_monitor():
    was_online = None

    while True:
        try:
            bot.get_me()

            if was_online is False and ADMIN_CHAT_ID:
                bot.send_message(
                    ADMIN_CHAT_ID,
                    "✅ <b>বট পুনরায় সক্রিয় হয়েছে!</b>\n\n"
                    "APK কনভার্টার স্টুডিও অনলাইনে ফিরে এসেছে।"
                )

            if was_online is not True:
                logger.info("আপটাইম মনিটর: বট অনলাইনে আছে")

            was_online = True

        except Exception as exc:
            logger.error("আপটাইম মনিটর ব্যর্থ: %s", exc)

            if was_online is not False and ADMIN_CHAT_ID:
                try:
                    bot.send_message(
                        ADMIN_CHAT_ID,
                        "⚠️ <b>বট সংযোগে ত্রুটি!</b>\n\n"
                        "টেলিগ্রাম API-এর সাথে সংযোগ বিচ্ছিন্ন হয়েছে।\n"
                        "Render বা হোস্টিং লগ পরীক্ষা করুন।"
                    )
                except Exception:
                    logger.error("এডমিন সতর্কবার্তা পাঠানো যায়নি।")

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