import os, aiohttp

ZARINPAL_MERCHANT_ID  = os.environ.get("ZARINPAL_MERCHANT_ID", "")
ZARINPAL_CALLBACK_URL = os.environ.get("ZARINPAL_CALLBACK_URL", "")

ZP_REQUEST_URL  = "https://payment.zarinpal.com/pg/v4/payment/request.json"
ZP_VERIFY_URL   = "https://payment.zarinpal.com/pg/v4/payment/verify.json"
ZP_STARTPAY_URL = "https://www.zarinpal.com/pg/StartPay/"


async def create_zarinpal_payment(amount_toman: int, description: str):
    """
    درخواست پرداخت جدید از زرین‌پال.
    خروجی موفق: (authority, pay_url)
    خروجی ناموفق: (None, error_message)
    """
    if not ZARINPAL_MERCHANT_ID or not ZARINPAL_CALLBACK_URL:
        return None, "درگاه زرین‌پال هنوز تنظیم نشده (ZARINPAL_MERCHANT_ID / ZARINPAL_CALLBACK_URL)"

    payload = {
        "merchant_id": ZARINPAL_MERCHANT_ID,
        "amount": amount_toman,
        "currency": "IRT",  # تومان
        "callback_url": ZARINPAL_CALLBACK_URL,
        "description": description,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ZP_REQUEST_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
    except Exception as e:
        return None, f"خطا در اتصال به زرین‌پال: {e}"

    d = data.get("data") or {}
    if d.get("code") in (100,):
        authority = d.get("authority")
        return authority, ZP_STARTPAY_URL + authority

    errs = data.get("errors") or {}
    msg = errs.get("message") if isinstance(errs, dict) else str(errs)
    return None, msg or "خطای نامشخص از زرین‌پال"


async def verify_zarinpal_payment(authority: str, amount_toman: int):
    """
    تایید پرداخت بعد از بازگشت کاربر از درگاه.
    خروجی موفق: (True, ref_id)
    خروجی ناموفق: (False, error_message)
    """
    payload = {
        "merchant_id": ZARINPAL_MERCHANT_ID,
        "amount": amount_toman,
        "authority": authority,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ZP_VERIFY_URL, json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
    except Exception as e:
        return False, f"خطا در اتصال به زرین‌پال: {e}"

    d = data.get("data") or {}
    if d.get("code") in (100, 101):  # 101 یعنی قبلاً هم تایید شده بود
        return True, str(d.get("ref_id", ""))

    errs = data.get("errors") or {}
    msg = errs.get("message") if isinstance(errs, dict) else str(errs)
    return False, msg or "پرداخت تایید نشد"
