import asyncio
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionExpired
from database import q, u
from utils import get_user_client

BOT_CLIENT = None  # توسط main.py تنظیم می‌شود

# شناسه‌های فرستنده‌های سیستمی تلگرام
SYSTEM_SENDER_IDS = {777000, 42777}
SYSTEM_USERNAMES  = {"spambot", "notificationbot", "telegrampassport"}

# فاصلهٔ چک (ثانیه)
CHECK_INTERVAL = 120  # هر ۲ دقیقه


def _get_last_seen(acc_id: str) -> int:
    """آخرین message_id که برای این اکانت فرستادیم"""
    r = q("SELECT last_sys_msg_id FROM accounts WHERE id=%s", (acc_id,))
    if r and r[0][0]:
        return int(r[0][0])
    return 0


def _set_last_seen(acc_id: str, msg_id: int):
    u("UPDATE accounts SET last_sys_msg_id=%s WHERE id=%s", (msg_id, acc_id))


async def _check_account(acc_id: str, phone: str, admin_id: int):
    """یه‌بار پیام‌های سیستمی اکانت رو چک می‌کنه"""
    uc = await get_user_client(acc_id)
    if not uc:
        return

    try:
        await uc.start()
        last_seen = _get_last_seen(acc_id)
        new_last = last_seen
        new_msgs = []

        # آخرین ۲۰ پیام از هر فرستندهٔ سیستمی رو چک کن
        for sender_id in SYSTEM_SENDER_IDS:
            try:
                async for msg in uc.get_chat_history(sender_id, limit=20):
                    if msg.id <= last_seen:
                        break
                    text = msg.text or msg.caption or ""
                    if text:
                        new_msgs.append((msg.id, sender_id, text))
                    if msg.id > new_last:
                        new_last = msg.id
            except Exception:
                # اگه با این فرستنده چتی نداشت، رد می‌شه
                pass

        await uc.stop()

        # مرتب‌سازی از قدیمی به جدید و ارسال به ادمین
        if new_msgs and BOT_CLIENT:
            new_msgs.sort(key=lambda x: x[0])
            for msg_id, sender_id, text in new_msgs:
                try:
                    await BOT_CLIENT.send_message(
                        admin_id,
                        f"📨 **پیام سیستمی — اکانت {phone}**\n"
                        f"از: `{sender_id}`\n\n"
                        f"{text}"
                    )
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"[PvMonitor] خطا در ارسال به ادمین: {e}")

        # آپدیت آخرین پیام دیده‌شده
        if new_last > last_seen:
            _set_last_seen(acc_id, new_last)

    except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
        print(f"[PvMonitor] اکانت {phone} منقضی شده.")
        try:
            await uc.stop()
        except Exception:
            pass
    except Exception as e:
        print(f"[PvMonitor] خطا در چک اکانت {phone}: {e}")
        try:
            await uc.stop()
        except Exception:
            pass


async def run():
    """حلقهٔ اصلی — هر CHECK_INTERVAL ثانیه همهٔ اکانت‌ها رو چک می‌کنه"""
    await asyncio.sleep(10)  # صبر کن ربات کامل بیاد بالا

    while True:
        try:
            accs = q("SELECT id, phone, admin_id FROM accounts")
            if accs:
                for acc_id, phone, admin_id in accs:
                    await _check_account(str(acc_id), phone, admin_id)
                    await asyncio.sleep(2)  # فاصله بین اکانت‌ها
        except Exception as e:
            print(f"[PvMonitor] خطای کلی: {e}")

        await asyncio.sleep(CHECK_INTERVAL)
