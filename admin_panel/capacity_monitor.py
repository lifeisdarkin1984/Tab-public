import asyncio, os
import database as db

MAX_TOTAL_ACCOUNTS = int(os.environ.get("MAX_TOTAL_ACCOUNTS", 0))
OWNER_ID = int(os.environ.get("PANEL_OWNER_ID", 0))

_last_alert_threshold = 0


async def run(bot_client):
    global _last_alert_threshold
    if not MAX_TOTAL_ACCOUNTS:
        print("ℹ️ MAX_TOTAL_ACCOUNTS تنظیم نشده — مانیتور ظرفیت خاموشه.")
        return
    print("📈 Capacity monitor started")
    while True:
        try:
            active = db.count_total_active_accounts()
            pct = int(active * 100 / MAX_TOTAL_ACCOUNTS)
            threshold = 90 if pct >= 90 else (75 if pct >= 75 else 0)
            if threshold and threshold > _last_alert_threshold:
                try:
                    await bot_client.send_message(
                        OWNER_ID,
                        f"⚠️ **هشدار ظرفیت سرور**\n\n"
                        f"اکانت‌های تلگرامی فعال: {active} / {MAX_TOTAL_ACCOUNTS} ({pct}%)\n"
                        f"وقتشه ظرفیت سرور رو افزایش بدی یا فروش رو موقتاً محدود کنی."
                    )
                except Exception as e:
                    print(f"[CapacityMonitor] خطا در ارسال هشدار: {e}")
                _last_alert_threshold = threshold
            elif pct < 75:
                _last_alert_threshold = 0
        except Exception as e:
            print(f"[CapacityMonitor] خطا: {e}")
        await asyncio.sleep(1800)
