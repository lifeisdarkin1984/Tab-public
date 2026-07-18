import os, time
from pyrogram import Client
from database import q, u
from dotenv import load_dotenv
load_dotenv()

API_ID   = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

def get_current_layer(admin_id):
    """لایه‌ای که این مشتری (تننت) الان توش ایستاده"""
    r = q("SELECT current_layer_id FROM admins WHERE id=%s", (admin_id,))
    return r[0][0] if r and r[0][0] else 0

def get_account_owner(acc_id):
    """admin_id (شناسه‌ی مشتری/تننت) صاحبِ این اکانت تلگرامی"""
    r = q("SELECT admin_id FROM accounts WHERE id=%s", (acc_id,))
    return r[0][0] if r else None

TRIAL_DAYS_DEFAULT = 3

def ensure_tenant(uid, username="", full_name=""):
    """
    اولین باری که یه کاربر تلگرام /start می‌زنه، فضای مستقل خودش رو می‌گیره:
    یه ردیف تو admins و یه لایه‌ی پیش‌فرض (اگه از قبل نداشته باشه)، به‌علاوه‌ی
    یه ردیف تو جدول tenants (همون دیتابیس پنل مدیریتی) با ۳ روز آزمایشی رایگان —
    اگه از قبل تننت بوده، وضعیت/انقضاش دست‌نخورده می‌مونه.
    """
    u("INSERT INTO admins (id) VALUES(%s) ON DUPLICATE KEY UPDATE step='idle'", (uid,))
    layers = q("SELECT id FROM layers WHERE admin_id=%s ORDER BY id LIMIT 1", (uid,))
    if not layers:
        lid = u("INSERT INTO layers (admin_id, name) VALUES (%s,%s)", (uid, "لایه ۱"))
        u("UPDATE admins SET current_layer_id=%s WHERE id=%s AND current_layer_id IS NULL",
          (lid, uid))
    else:
        u("UPDATE admins SET current_layer_id=%s WHERE id=%s AND current_layer_id IS NULL",
          (layers[0][0], uid))

    trial_exp = int(time.time()) + TRIAL_DAYS_DEFAULT * 86400
    u("INSERT INTO tenants (telegram_id, username, full_name, status, expires_at) "
      "VALUES (%s,%s,%s,'trial',%s) "
      "ON DUPLICATE KEY UPDATE username=%s, full_name=%s",
      (uid, username, full_name, trial_exp, username, full_name))


def get_tenant_status(uid):
    """(status, expires_at) از جدول tenants؛ اگه ردیفی نبود (تننت خیلی قدیمی) قفل نمی‌کنیم"""
    r = q("SELECT status, expires_at FROM tenants WHERE telegram_id=%s", (uid,))
    if not r:
        return "active", 0
    return r[0][0], r[0][1]


def is_tenant_locked(uid):
    """True یعنی اشتراک تموم شده یا مسدوده — داده‌ها می‌مونن، فقط استفاده قفله"""
    status, expires_at = get_tenant_status(uid)
    if status == "suspended":
        return True
    if expires_at and expires_at < int(time.time()):
        return True
    return False

def get_step(uid):
    r = q("SELECT step FROM admins WHERE id=%s", (uid,))
    return r[0][0] if r else "idle"

def get_step_data(uid):
    r = q("SELECT step_data FROM admins WHERE id=%s", (uid,))
    return r[0][0] if r else ""

def set_step(uid, step, data=""):
    u("INSERT INTO admins (id,step,step_data) VALUES(%s,%s,%s) "
      "ON DUPLICATE KEY UPDATE step=%s, step_data=%s",
      (uid, step, data, step, data))

def clear_step(uid):
    set_step(uid, "idle", "")

async def get_user_client(acc_id):
    r = q("SELECT session_string FROM accounts WHERE id=%s", (acc_id,))
    if not r or not r[0][0]:
        return None
    return Client(
        name=f"uc_{acc_id}",
        session_string=r[0][0],
        api_id=API_ID,
        api_hash=API_HASH,
        no_updates=True,
        in_memory=True
    )

def save_account(me, session_string, phone, admin_id):
    lyr = q("SELECT current_layer_id FROM admins WHERE id=%s", (admin_id,))
    layer_id = lyr[0][0] if lyr and lyr[0][0] else None
    u("INSERT INTO accounts (id,phone,name,username,session_string,admin_id,added_at,layer_id) "
      "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
      "name=%s, session_string=%s, status='active'",
      (str(me.id), phone, me.first_name or str(me.id), me.username or "",
       session_string, admin_id, int(time.time()), layer_id,
       me.first_name or str(me.id), session_string))

async def clear_chat_history(uc, chat_id, revoke=False):
    """
    حذف کامل گفتگو با یک چت (پیوی/ربات) — هم پیام‌ها هم خودِ چت از لیست مکالمات.
    توجه: پایروگرام متد delete_history ندارد؛ delete_messages فقط پیام‌ها را
    پاک می‌کند ولی چت خالی را در لیست نگه می‌دارد (همون چیزی که به‌صورت
    «تاریخچه پاک شد» دیده می‌شه). برای حذف واقعی خودِ گفتگو از raw API
    messages.DeleteHistory با just_clear=False استفاده می‌کنیم.
    revoke=False (پیش‌فرض) → فقط سمت خودمان حذف می‌شود، طرف مقابل چیزی نمی‌بیند.
    revoke=True → دوطرفه حذف می‌شود.
    """
    from pyrogram import raw
    peer = await uc.resolve_peer(chat_id)
    await uc.invoke(
        raw.functions.messages.DeleteHistory(
            peer=peer, max_id=0, just_clear=False, revoke=revoke
        )
    )

# ─── توقف عملیات ──────────────────────────────
stop_all = False

def set_stop(val: bool):
    global stop_all
    stop_all = val

def is_stopped():
    return stop_all


# ─── تشخیص عضویت اجباری بات‌محور (نه از طرف Telegram، بلکه ربات داخل گروه) ──
import re, asyncio as _asyncio

_FORCED_JOIN_LINK_PATTERN = re.compile(r'https?://t\.me/[^\s\]\)\"\']+|@[\w]{4,}')
_FORCED_JOIN_KEYWORDS = ["عضو شو", "عضویت", "join", "membership", "عضو شوید", "عضو کانال"]

async def detect_and_handle_bot_forced_join(uc, chat_id, original_text=None):
    """
    بعد از ارسال پیام به یک گروه، چک می‌کند آیا ربات داخل گروه با درخواست
    عضویت اجباری در یک کانال دیگر پاسخ داده. اگر بله، عضو آن کانال می‌شود
    و در صورت داده‌شدن original_text، پیام اصلی را دوباره ارسال می‌کند.

    خروجی: {"forced_join_detected": bool, "channel": str|None,
            "joined": bool, "resent": bool, "error": str|None}
    """
    result = {"forced_join_detected": False, "channel": None,
              "joined": False, "resent": False, "error": None}
    try:
        sent_time = time.time()
        await _asyncio.sleep(2.5)

        target_msg = None
        async for msg in uc.get_chat_history(chat_id, limit=5):
            if msg.date and msg.date.timestamp() <= sent_time:
                continue
            if msg.from_user and msg.from_user.is_self:
                continue
            if not msg.reply_markup:
                continue
            rows = getattr(msg.reply_markup, 'inline_keyboard', None)
            if not rows:
                continue

            link = None
            for row in rows:
                for btn in row:
                    url = getattr(btn, 'url', None)
                    if url and ("t.me/" in url or url.startswith("@")):
                        link = url
                        break
                if link:
                    break
            if not link:
                continue

            # برای کاهش false-positive، حضور کلمات کلیدی را هم چک می‌کنیم
            msg_text = (msg.text or msg.caption or "").lower()
            if not any(kw in msg_text for kw in _FORCED_JOIN_KEYWORDS):
                continue

            target_msg = msg
            result["channel"] = link
            break

        if not target_msg:
            return result

        result["forced_join_detected"] = True
        found = _FORCED_JOIN_LINK_PATTERN.findall(result["channel"])
        channel = found[0] if found else result["channel"]
        channel_clean = channel.split("t.me/")[-1].lstrip("@") if "t.me/" in channel else channel.lstrip("@")

        try:
            await uc.join_chat(channel_clean)
            result["joined"] = True
        except Exception as e:
            result["error"] = f"join failed: {e}"
            return result

        await _asyncio.sleep(2)

        if original_text:
            try:
                await uc.send_message(chat_id, original_text)
                result["resent"] = True
            except Exception as e:
                result["error"] = f"resend failed: {e}"

    except Exception as e:
        print(f"[ForcedJoinDetect] خطا: {e}")
        result["error"] = str(e)

    return result


# ─── محدودیت منابع (فاز ۴) ──────────────────────────────

TRIAL_MAX_ACCOUNTS = 1
TRIAL_MAX_LAYERS = 1

def get_plan_limits(uid):
    """(max_accounts, max_layers) طبق پلن فعلی تننت؛ 0 یعنی نامحدود.
    اگه هنوز پلنی نداره (آزمایشیه)، سقف پیش‌فرض دوره‌ی آزمایشی اعمال می‌شه."""
    row = q("SELECT plan_id FROM tenants WHERE telegram_id=%s", (uid,))
    plan_id = row[0][0] if row and row[0][0] else None
    if plan_id:
        p = q("SELECT max_accounts, max_layers FROM plans WHERE id=%s", (plan_id,))
        if p:
            return p[0][0], p[0][1]
    return TRIAL_MAX_ACCOUNTS, TRIAL_MAX_LAYERS

def count_active_accounts(uid):
    r = q("SELECT COUNT(*) FROM accounts WHERE admin_id=%s AND status='active'", (uid,))
    return r[0][0] if r else 0

def count_layers(uid):
    r = q("SELECT COUNT(*) FROM layers WHERE admin_id=%s", (uid,))
    return r[0][0] if r else 0

