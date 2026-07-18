import os, aiohttp

ZARINPAL_MERCHANT_ID = os.environ.get("ZARINPAL_MERCHANT_ID", "")
ZP_VERIFY_URL = "https://payment.zarinpal.com/pg/v4/payment/verify.json"


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
