import time
from pyrogram import Client, filters
from pyrogram.errors import (PhoneNumberInvalid, PhoneCodeInvalid,
    PhoneCodeExpired, SessionPasswordNeeded, PasswordHashInvalid, FloodWait)
from database import q, u
from utils import (API_ID, API_HASH, save_account, set_step, clear_step,
                    ensure_tenant, get_tenant_status, is_tenant_locked,
                    get_plan_limits, count_active_accounts)
from keyboards import back_kb, main_menu_kb, layers_kb, billing_kb

pending_clients = {}

def _locked_message(status, expires_at):
    if status == "suspended":
        return "⛔️ **دسترسی شما مسدود شده.**\n\nبرای پیگیری با پشتیبانی تماس بگیرید."
    from datetime import datetime
    exp_txt = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else "-"
    return (
        f"⌛️ **اشتراک شما در تاریخ {exp_txt} به پایان رسیده.**\n\n"
        "داده‌ها و تنظیمات‌تون محفوظه؛ فقط با تمدید اشتراک دوباره فعال می‌شه."
    )

def register(app):

    @app.on_message(filters.private & filters.command("start"))
    async def cmd_start(client, message):
        uid = message.from_user.id
        # هر کاربر تلگرام با /start فضای مستقل خودش رو می‌گیره (خودکار + ۳ روز آزمایشی رایگان)
        ensure_tenant(uid, username=message.from_user.username or "",
                      full_name=message.from_user.first_name or "")

        if is_tenant_locked(uid):
            status, expires_at = get_tenant_status(uid)
            await message.reply(_locked_message(status, expires_at),
                                 reply_markup=billing_kb())
            return

        layers = q(
            "SELECT l.id, l.name, COUNT(a.id) FROM layers l "
            "LEFT JOIN accounts a ON a.layer_id=l.id "
            "WHERE l.admin_id=%s GROUP BY l.id, l.name ORDER BY l.id",
            (uid,)
        )
        await message.reply(
            "👋 **به Tabchiz | تبچیز (نسخه آزمایشی) خوش آمدید**\n\nیک لایه را انتخاب کنید:",
            reply_markup=layers_kb(layers)
        )

    @app.on_message(filters.private & filters.command("add_account"))
    async def cmd_add(client, message):
        uid = message.from_user.id
        if is_tenant_locked(uid):
            status, expires_at = get_tenant_status(uid)
            await message.reply(_locked_message(status, expires_at), reply_markup=billing_kb())
            return
        max_accounts, _ = get_plan_limits(uid)
        if max_accounts and count_active_accounts(uid) >= max_accounts:
            await message.reply(
                f"🚫 **سقف اکانت پلن شما پر شده** ({max_accounts} اکانت).\n\n"
                "برای افزودن اکانت بیشتر، پلن‌تون رو ارتقا بدید.",
                reply_markup=billing_kb()
            )
            return
        set_step(uid, "login_phone")
        await message.reply(
            "📱 **افزودن اکانت جدید**\n\n"
            "شماره تلفن را با کد کشور وارد کنید:\n"
            "مثال: `+989123456789`",
            reply_markup=back_kb("back_main")
        )

    @app.on_message(filters.private & filters.command("list_account"))
    async def cmd_list(client, message):
        uid = message.from_user.id
        if is_tenant_locked(uid):
            status, expires_at = get_tenant_status(uid)
            await message.reply(_locked_message(status, expires_at), reply_markup=billing_kb())
            return
        cur = q("SELECT current_layer_id FROM admins WHERE id=%s", (uid,))
        layer_id = cur[0][0] if cur else None
        accs = q("SELECT id,phone,name FROM accounts WHERE admin_id=%s AND layer_id=%s",
                 (uid, layer_id))
        if not accs:
            await message.reply("هیچ اکانتی تو این لایه ثبت نشده.\n\nبرای افزودن: /add_account")
            return
        txt = f"📌 **لیست تبچیزهای شما ({len(accs)} اکانت)**\n\n"
        for a in accs:
            txt += f"👤 {a[2]} | `{a[1]}`\n"
        await message.reply(txt)


async def send_code(phone, admin_id):
    temp = Client(f"tmp_{phone.replace('+','')}", api_id=API_ID, api_hash=API_HASH,
                  no_updates=True, in_memory=True)
    await temp.connect()
    sent = await temp.send_code(phone)
    pending_clients[phone] = temp
    u("INSERT INTO pending_logins (phone,admin_id,phone_code_hash,created_at) "
      "VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE phone_code_hash=%s,created_at=%s",
      (phone, admin_id, sent.phone_code_hash, int(time.time()),
       sent.phone_code_hash, int(time.time())))

async def sign_in(phone, code=None, password=None):
    temp = pending_clients.get(phone)
    if not temp:
        return None, "expired"
    row = q("SELECT phone_code_hash FROM pending_logins WHERE phone=%s", (phone,))
    if not row:
        return None, "no_hash"
    try:
        if password:
            await temp.check_password(password)
        else:
            await temp.sign_in(phone, row[0][0], code)
        me = await temp.get_me()
        ss = await temp.export_session_string()
        await temp.disconnect()
        del pending_clients[phone]
        u("DELETE FROM pending_logins WHERE phone=%s", (phone,))
        return (me, ss), None
    except SessionPasswordNeeded:
        return None, "2fa"
    except PhoneCodeInvalid:
        return None, "bad_code"
    except PhoneCodeExpired:
        return None, "expired_code"
    except PasswordHashInvalid:
        return None, "bad_pass"
    except FloodWait as e:
        return None, f"flood:{e.value}"
    except Exception as e:
        return None, str(e)
