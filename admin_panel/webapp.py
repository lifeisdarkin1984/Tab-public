import os
from aiohttp import web

import database as db
from notifier import notify_tenant
from payment_verify import verify_zarinpal_payment

TABCI_BOT_USERNAME = os.environ.get("TABCI_BOT_USERNAME", "")

_SUCCESS_HTML = """
<html dir="rtl" lang="fa"><head><meta charset="utf-8">
<title>پرداخت موفق</title>
<style>body{{font-family:sans-serif;text-align:center;padding-top:80px;background:#f4f8f6}}
.card{{display:inline-block;padding:32px 48px;border-radius:16px;background:#fff;
box-shadow:0 4px 20px rgba(0,0,0,.08)}}
h1{{color:#1a7f4b}}</style></head>
<body><div class="card"><h1>✅ پرداخت با موفقیت انجام شد</h1>
<p>اشتراک شما فعال شد.</p>
<p><a href="https://t.me/{bot}">بازگشت به ربات</a></p></div></body></html>
"""

_FAIL_HTML = """
<html dir="rtl" lang="fa"><head><meta charset="utf-8">
<title>پرداخت ناموفق</title>
<style>body{{font-family:sans-serif;text-align:center;padding-top:80px;background:#f8f4f4}}
.card{{display:inline-block;padding:32px 48px;border-radius:16px;background:#fff;
box-shadow:0 4px 20px rgba(0,0,0,.08)}}
h1{{color:#b33}}</style></head>
<body><div class="card"><h1>❌ پرداخت انجام نشد</h1>
<p>{reason}</p>
<p><a href="https://t.me/{bot}">بازگشت به ربات</a></p></div></body></html>
"""


async def zarinpal_callback(request: web.Request):
    authority = request.query.get("Authority", "")
    status = request.query.get("Status", "")

    if not authority:
        return web.Response(
            text=_FAIL_HTML.format(reason="اطلاعات پرداخت ناقصه.", bot=TABCI_BOT_USERNAME),
            content_type="text/html")

    payment = db.get_payment_by_authority(authority)
    if not payment:
        return web.Response(
            text=_FAIL_HTML.format(reason="این تراکنش پیدا نشد.", bot=TABCI_BOT_USERNAME),
            content_type="text/html")

    pid, tenant_id, plan_id, amount, method, pstatus, _auth, ref_id, note, created_at = payment

    if pstatus == "success":
        return web.Response(text=_SUCCESS_HTML.format(bot=TABCI_BOT_USERNAME),
                             content_type="text/html")
    if pstatus != "pending":
        return web.Response(
            text=_FAIL_HTML.format(reason="این پرداخت قبلاً رد یا ناموفق ثبت شده بود.",
                                    bot=TABCI_BOT_USERNAME),
            content_type="text/html")

    if status != "OK":
        db.fail_payment(pid, note="کاربر پرداخت رو لغو کرد")
        return web.Response(
            text=_FAIL_HTML.format(reason="پرداخت توسط شما لغو شد.", bot=TABCI_BOT_USERNAME),
            content_type="text/html")

    ok, result = await verify_zarinpal_payment(authority, amount)
    if not ok:
        db.fail_payment(pid, note=str(result)[:400])
        return web.Response(
            text=_FAIL_HTML.format(reason=f"تایید پرداخت ناموفق بود: {result}",
                                    bot=TABCI_BOT_USERNAME),
            content_type="text/html")

    tenant_result = db.approve_payment(pid, actor=0, ref_id=result)
    if tenant_result:
        tenant_id, new_exp = tenant_result
        from datetime import datetime
        exp_txt = datetime.fromtimestamp(new_exp).strftime("%Y-%m-%d")
        await notify_tenant(
            tenant_id,
            f"✅ **پرداخت شما موفق بود!**\n\nاشتراک شما تا {exp_txt} تمدید شد.\nبرای شروع: /start"
        )

    return web.Response(text=_SUCCESS_HTML.format(bot=TABCI_BOT_USERNAME), content_type="text/html")


async def health(request: web.Request):
    return web.json_response({"ok": True})


def build_app():
    app = web.Application()
    app.router.add_get("/zarinpal/callback", zarinpal_callback)
    app.router.add_get("/health", health)
    return app
