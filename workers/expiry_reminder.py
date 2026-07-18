import asyncio, time
from database import q, u

BOT_CLIENT = None
REMINDER_DAYS = (3, 1, 0)  # روزهای مونده به انقضا که باید یادآوری بشه


async def run():
    print("⏰ Expiry reminder worker started")
    while True:
        try:
            now = int(time.time())
            rows = q("SELECT telegram_id, expires_at, status FROM tenants WHERE expires_at>0")
            for telegram_id, expires_at, status in rows:
                if status == "suspended":
                    continue
                days_left = (expires_at - now) // 86400
                if days_left not in REMINDER_DAYS:
                    continue
                already = q(
                    "SELECT 1 FROM reminder_log WHERE tenant_id=%s AND days_left=%s AND expires_at=%s",
                    (telegram_id, days_left, expires_at)
                )
                if already:
                    continue
                if BOT_CLIENT:
                    try:
                        if days_left > 0:
                            txt = (f"⏳ **یادآوری اشتراک**\n\n"
                                   f"اشتراک شما {days_left} روز دیگه تموم می‌شه.\n"
                                   f"برای تمدید: /start")
                        else:
                            txt = ("⌛️ **اشتراک شما امروز تموم شد.**\n\n"
                                   "داده‌هاتون محفوظه؛ برای ادامه‌ی کار، تمدید کنید.\n/start")
                        await BOT_CLIENT.send_message(telegram_id, txt)
                    except Exception as e:
                        print(f"[ExpiryReminder] خطا در ارسال به {telegram_id}: {e}")
                u("INSERT INTO reminder_log (tenant_id, days_left, expires_at) VALUES (%s,%s,%s)",
                  (telegram_id, days_left, expires_at))
        except Exception as e:
            print(f"[ExpiryReminder] خطای کلی: {e}")
        await asyncio.sleep(3600)
