import asyncio, os
from pyrogram import Client, idle
from dotenv import load_dotenv
from database import init_panel_db
from aiohttp import web
from webapp import build_app
import capacity_monitor

load_dotenv()

API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
PANEL_BOT_TOKEN = os.environ["PANEL_BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 8080))

import handlers


async def main():
    init_panel_db()

    app = Client(
        "tabchi_admin_panel",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=PANEL_BOT_TOKEN
    )

    handlers.register(app)

    await app.start()
    print("✅ Tabchi Admin Panel bot is running...")

    # وب‌سرویس callback زرین‌پال — روی همون سرویس Railway، برای دریافت نتیجه‌ی پرداخت
    web_app = build_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ Payment webhook listening on :{PORT}")

    await asyncio.gather(
        capacity_monitor.run(app),
        idle(),
    )

    await runner.cleanup()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
