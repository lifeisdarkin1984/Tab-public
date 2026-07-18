from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton as B


def home_kb():
    return InlineKeyboardMarkup([
        [B("👥 مدیریت مشتری‌ها", callback_data="pnl_tenants:0")],
        [B("📦 مدیریت پلن‌ها", callback_data="pnl_plans")],
        [B("💳 پرداخت‌های در انتظار", callback_data="pnl_payments:0")],
        [B("🎫 تیکت‌های پشتیبانی", callback_data="pnl_tickets:0")],
        [B("📢 اطلاع‌رسانی همگانی", callback_data="pnl_broadcast")],
        [B("📊 پرمصرف‌ترین مشتری‌ها", callback_data="pnl_top_usage")],
        [B("🔄 بروزرسانی داشبورد", callback_data="pnl_home")],
    ])


def payments_list_kb(rows, offset, limit, total):
    kb = []
    for pid, tenant_id, plan_id, amount, method, created_at in rows:
        method_fa = "💳 کارت‌به‌کارت" if method == "manual" else "🌐 زرین‌پال"
        kb.append([B(f"{method_fa} — {amount:,} ت — {tenant_id}",
                     callback_data=f"pnl_payment:{pid}")])
    nav = []
    if offset > 0:
        nav.append(B("◀️ قبلی", callback_data=f"pnl_payments:{max(0, offset - limit)}"))
    if offset + limit < total:
        nav.append(B("بعدی ▶️", callback_data=f"pnl_payments:{offset + limit}"))
    if nav:
        kb.append(nav)
    kb.append([B("🏠 بازگشت به داشبورد", callback_data="pnl_home")])
    return InlineKeyboardMarkup(kb)


def payment_detail_kb(payment_id):
    return InlineKeyboardMarkup([
        [B("✅ تایید", callback_data=f"pnl_payment_approve:{payment_id}"),
         B("❌ رد", callback_data=f"pnl_payment_reject:{payment_id}")],
        [B("◀️ بازگشت به لیست", callback_data="pnl_payments:0")],
    ])


STATUS_EMOJI = {
    "trial": "🆕",
    "active": "✅",
    "suspended": "⛔️",
    "expired": "⌛️",
}


def tenants_list_kb(rows, offset, limit, total):
    kb = []
    for telegram_id, username, full_name, status, expires_at in rows:
        label = full_name or username or str(telegram_id)
        emoji = STATUS_EMOJI.get(status, "•")
        kb.append([B(f"{emoji} {label}", callback_data=f"pnl_tenant:{telegram_id}")])

    nav = []
    if offset > 0:
        nav.append(B("◀️ قبلی", callback_data=f"pnl_tenants:{max(0, offset - limit)}"))
    if offset + limit < total:
        nav.append(B("بعدی ▶️", callback_data=f"pnl_tenants:{offset + limit}"))
    if nav:
        kb.append(nav)

    kb.append([B("➕ افزودن مشتری", callback_data="pnl_tenant_add")])
    kb.append([B("🏠 بازگشت به داشبورد", callback_data="pnl_home")])
    return InlineKeyboardMarkup(kb)


def tenant_detail_kb(telegram_id, status):
    toggle_label = "✅ فعال‌سازی" if status == "suspended" else "⛔️ مسدودسازی"
    return InlineKeyboardMarkup([
        [B("➕ تمدید اشتراک", callback_data=f"pnl_tenant_extend:{telegram_id}")],
        [B("📦 تغییر پلن", callback_data=f"pnl_tenant_setplan:{telegram_id}")],
        [B(toggle_label, callback_data=f"pnl_tenant_toggle:{telegram_id}")],
        [B("◀️ بازگشت به لیست", callback_data="pnl_tenants:0")],
    ])


def plans_list_kb(rows):
    kb = []
    for pid, name, price, duration_days, max_accounts, max_layers, is_active in rows:
        emoji = "✅" if is_active else "🚫"
        kb.append([B(f"{emoji} {name}", callback_data=f"pnl_plan:{pid}")])
    kb.append([B("➕ افزودن پلن", callback_data="pnl_plan_add")])
    kb.append([B("🏠 بازگشت به داشبورد", callback_data="pnl_home")])
    return InlineKeyboardMarkup(kb)


def plan_detail_kb(plan_id, is_active):
    toggle_label = "🚫 غیرفعال‌سازی" if is_active else "✅ فعال‌سازی"
    return InlineKeyboardMarkup([
        [B("💰 ویرایش قیمت", callback_data=f"pnl_plan_edit:{plan_id}:price"),
         B("⏳ ویرایش مدت", callback_data=f"pnl_plan_edit:{plan_id}:duration_days")],
        [B("📱 سقف اکانت", callback_data=f"pnl_plan_edit:{plan_id}:max_accounts"),
         B("🗂 سقف لایه", callback_data=f"pnl_plan_edit:{plan_id}:max_layers")],
        [B(toggle_label, callback_data=f"pnl_plan_toggle:{plan_id}")],
        [B("◀️ بازگشت به لیست پلن‌ها", callback_data="pnl_plans")],
    ])


def plan_picker_kb(rows, telegram_id):
    kb = []
    for pid, name, price, duration_days, max_accounts, max_layers, is_active in rows:
        kb.append([B(name, callback_data=f"pnl_tenant_plan:{telegram_id}:{pid}")])
    kb.append([B("◀️ انصراف", callback_data=f"pnl_tenant:{telegram_id}")])
    return InlineKeyboardMarkup(kb)


def cancel_kb(back_callback):
    return InlineKeyboardMarkup([[B("◀️ انصراف", callback_data=back_callback)]])


# ── تیکت پشتیبانی ──

def tickets_list_kb(rows, offset, limit, total):
    kb = []
    for tid, tenant_id, message, status, created_at in rows:
        preview = (message[:25] + "…") if len(message) > 25 else message
        kb.append([B(f"#{tid} — {tenant_id} — {preview}", callback_data=f"pnl_ticket:{tid}")])
    nav = []
    if offset > 0:
        nav.append(B("◀️ قبلی", callback_data=f"pnl_tickets:{max(0, offset - limit)}"))
    if offset + limit < total:
        nav.append(B("بعدی ▶️", callback_data=f"pnl_tickets:{offset + limit}"))
    if nav:
        kb.append(nav)
    kb.append([B("🏠 بازگشت به داشبورد", callback_data="pnl_home")])
    return InlineKeyboardMarkup(kb)


def ticket_detail_kb(ticket_id, status):
    kb = []
    if status == "open":
        kb.append([B("✍️ پاسخ", callback_data=f"pnl_ticket_reply:{ticket_id}")])
        kb.append([B("✅ بستن بدون پاسخ", callback_data=f"pnl_ticket_close:{ticket_id}")])
    kb.append([B("◀️ بازگشت به لیست", callback_data="pnl_tickets:0")])
    return InlineKeyboardMarkup(kb)


# ── اطلاع‌رسانی همگانی ──

def broadcast_target_kb(plans):
    kb = [[B("📣 به همه‌ی مشتری‌ها", callback_data="pnl_bc_all")]]
    for pid, name, *_ in plans:
        kb.append([B(f"📣 فقط پلن «{name}»", callback_data=f"pnl_bc_plan:{pid}")])
    kb.append([B("🏠 بازگشت به داشبورد", callback_data="pnl_home")])
    return InlineKeyboardMarkup(kb)


def broadcast_confirm_kb(target):
    return InlineKeyboardMarkup([
        [B("✅ ارسال کن", callback_data=f"pnl_bc_send:{target}"),
         B("❌ انصراف", callback_data="pnl_home")],
    ])
