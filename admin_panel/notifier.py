import os, aiohttp

TABCI_BOT_TOKEN = os.environ.get("TABCI_BOT_TOKEN", "")


async def notify_tenant(telegram_id: int, text: str):
    """
    پیام مستقیم به مشتری از طریق ربات تبچی (نه ربات پنل) — چون مشتری با اون ربات
    /start زده، نه لزوماً با ربات پنل مدیریتی.
    """
    if not TABCI_BOT_TOKEN:
        print("[Notify] TABCI_BOT_TOKEN تنظیم نشده — پیام ارسال نشد.")
        return False
    url = f"https://api.telegram.org/bot{TABCI_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json={"chat_id": telegram_id, "text": text, "parse_mode": "Markdown"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    print(f"[Notify] خطا در ارسال به {telegram_id}: {data}")
                    return False
                return True
    except Exception as e:
        print(f"[Notify] خطا در اتصال: {e}")
        return False
