import os, json, time, asyncio
from pyrogram import filters
from pyrogram.types import Message, CallbackQuery

import database as db
import keyboards as kb
from notifier import notify_tenant

OWNER_ID = int(os.environ["PANEL_OWNER_ID"])
PAGE_LIMIT = 8

owner_filter = filters.user(OWNER_ID)


def _fmt_ts(ts):
    if not ts:
        return "—"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


MAX_TOTAL_ACCOUNTS = int(os.environ.get("MAX_TOTAL_ACCOUNTS", 0))  # 0 = بدون هشدار ظرفیت


def _dashboard_text():
    counts = db.count_tenants()
    total = sum(counts.values())
    active_accounts = db.count_total_active_accounts()
    txt = (
        "🖥 **داشبورد پنل مدیریت Tabchiz | تبچیز (نسخه آزمایشی)**\n\n"
        f"👥 کل مشتری‌ها: {total}\n"
        f"✅ فعال: {counts.get('active', 0)}\n"
        f"🆕 آزمایشی: {counts.get('trial', 0)}\n"
        f"⛔️ مسدود: {counts.get('suspended', 0)}\n"
        f"⌛️ منقضی: {counts.get('expired', 0)}\n\n"
        f"📱 اکانت‌های تلگرامی فعال (کل سرور): **{active_accounts}**"
    )
    if MAX_TOTAL_ACCOUNTS:
        pct = int(active_accounts * 100 / MAX_TOTAL_ACCOUNTS) if MAX_TOTAL_ACCOUNTS else 0
        txt += f" / {MAX_TOTAL_ACCOUNTS} ({pct}%)"
        if pct >= 90:
            txt += "\n\n🔴 **هشدار: ظرفیت سرور تقریباً تکمیله!**"
        elif pct >= 75:
            txt += "\n\n🟠 ظرفیت سرور داره پر می‌شه."
    return txt


def register(app):

    @app.on_message(filters.command("start") & owner_filter)
    async def panel_start(client, message: Message):
        db.clear_panel_step(OWNER_ID)
        await message.reply(_dashboard_text(), reply_markup=kb.home_kb())

    @app.on_message(filters.command("start") & ~owner_filter)
    async def panel_deny(client, message: Message):
        await message.reply("⛔️ این پنل فقط برای مدیر سرویس در دسترسه.")

    @app.on_callback_query(owner_filter)
    async def panel_callbacks(client, cq: CallbackQuery):
        data = cq.data

        if data == "pnl_top_usage":
            rows = db.top_tenants_by_usage(limit=10)
            if not rows:
                await cq.message.edit_text("هنوز هیچ اکانت فعالی تو سیستم نیست.",
                                           reply_markup=kb.home_kb())
                return
            lines = ["📊 **پرمصرف‌ترین مشتری‌ها (تعداد اکانت فعال)**\n"]
            for admin_id, cnt in rows:
                t = db.get_tenant(admin_id)
                label = (t[2] or t[1] or str(admin_id)) if t else str(admin_id)
                lines.append(f"👤 {label} — {cnt} اکانت")
            await cq.message.edit_text("\n".join(lines), reply_markup=kb.home_kb())
            return

        if data == "pnl_home":
            db.clear_panel_step(OWNER_ID)
            await cq.message.edit_text(_dashboard_text(), reply_markup=kb.home_kb())
            return

        if data.startswith("pnl_tenants:"):
            offset = int(data.split(":")[1])
            counts = db.count_tenants()
            total = sum(counts.values())
            rows = db.list_tenants(offset=offset, limit=PAGE_LIMIT)
            if not rows and offset == 0:
                await cq.message.edit_text(
                    "هنوز هیچ مشتری‌ای ثبت نشده.",
                    reply_markup=kb.tenants_list_kb([], 0, PAGE_LIMIT, 0)
                )
                return
            await cq.message.edit_text(
                "👥 **لیست مشتری‌ها**",
                reply_markup=kb.tenants_list_kb(rows, offset, PAGE_LIMIT, total)
            )
            return

        if data == "pnl_tenant_add":
            db.set_panel_step(OWNER_ID, "awaiting_tenant_add")
            await cq.message.edit_text(
                "آیدی عددی تلگرام مشتری رو بفرست.\n"
                "می‌تونی اسم رو هم با `|` جدا کنی، مثلاً: `123456789|علی رضایی`",
                reply_markup=kb.cancel_kb("pnl_tenants:0")
            )
            return

        if data.startswith("pnl_tenant_extend:"):
            tid = int(data.split(":")[1])
            db.set_panel_step(OWNER_ID, "awaiting_extend_days", str(tid))
            await cq.message.edit_text(
                "چند روز تمدید بشه؟ فقط عدد بفرست (مثلاً 30)",
                reply_markup=kb.cancel_kb(f"pnl_tenant:{tid}")
            )
            return

        if data.startswith("pnl_tenant_setplan:"):
            tid = int(data.split(":")[1])
            rows = db.list_plans(only_active=True)
            if not rows:
                await cq.answer("هیچ پلن فعالی وجود نداره — اول یه پلن بساز.", show_alert=True)
                return
            await cq.message.edit_text(
                "یکی از پلن‌ها رو انتخاب کن:",
                reply_markup=kb.plan_picker_kb(rows, tid)
            )
            return

        if data.startswith("pnl_tenant_plan:"):
            _, tid, plan_id = data.split(":")
            tid, plan_id = int(tid), int(plan_id)
            db.set_tenant_plan(tid, plan_id)
            db.log_action(OWNER_ID, "set_plan", tid, note=f"plan_id={plan_id}")
            await cq.answer("پلن تغییر کرد ✅")
            await _show_tenant(cq, tid)
            return

        if data.startswith("pnl_tenant_toggle:"):
            tid = int(data.split(":")[1])
            row = db.get_tenant(tid)
            if not row:
                await cq.answer("مشتری پیدا نشد.", show_alert=True)
                return
            cur_status = row[4]
            new_status = "active" if cur_status == "suspended" else "suspended"
            db.set_tenant_status(tid, new_status)
            db.log_action(OWNER_ID, "toggle_status", tid, note=f"{cur_status}->{new_status}")
            await cq.answer("انجام شد ✅")
            await _show_tenant(cq, tid)
            return

        if data.startswith("pnl_tenant:"):
            tid = int(data.split(":")[1])
            await _show_tenant(cq, tid)
            return

        if data == "pnl_plans":
            rows = db.list_plans()
            await cq.message.edit_text("📦 **مدیریت پلن‌ها**", reply_markup=kb.plans_list_kb(rows))
            return

        if data == "pnl_plan_add":
            db.set_panel_step(OWNER_ID, "awaiting_plan_add")
            await cq.message.edit_text(
                "پلن رو با این فرمت بفرست:\n"
                "`نام|قیمت|مدت‌روز|سقف‌اکانت|سقف‌لایه`\n"
                "مثال: `طلایی|150000|30|10|3`\n"
                "(برای سقف نامحدود عدد 0 بذار)",
                reply_markup=kb.cancel_kb("pnl_plans")
            )
            return

        if data.startswith("pnl_plan_toggle:"):
            plan_id = int(data.split(":")[1])
            db.toggle_plan(plan_id)
            db.log_action(OWNER_ID, "toggle_plan", note=f"plan_id={plan_id}")
            await cq.answer("انجام شد ✅")
            await _show_plan(cq, plan_id)
            return

        if data.startswith("pnl_plan_edit:"):
            _, plan_id, field = data.split(":")
            plan_id = int(plan_id)
            db.set_panel_step(OWNER_ID, "awaiting_plan_field",
                               json.dumps({"plan_id": plan_id, "field": field}))
            field_fa = {
                "price": "قیمت جدید (تومان)",
                "duration_days": "مدت جدید (روز)",
                "max_accounts": "سقف اکانت جدید (0 = نامحدود)",
                "max_layers": "سقف لایه جدید (0 = نامحدود)",
            }.get(field, field)
            await cq.message.edit_text(
                f"{field_fa} رو بفرست:",
                reply_markup=kb.cancel_kb(f"pnl_plan:{plan_id}")
            )
            return

        if data.startswith("pnl_plan:"):
            plan_id = int(data.split(":")[1])
            await _show_plan(cq, plan_id)
            return

        if data.startswith("pnl_payments:"):
            offset = int(data.split(":")[1])
            total = db.count_pending_payments()
            rows = db.list_pending_payments(offset=offset, limit=PAGE_LIMIT)
            if not rows and offset == 0:
                await cq.message.edit_text(
                    "هیچ پرداخت در انتظاری وجود نداره.",
                    reply_markup=kb.payments_list_kb([], 0, PAGE_LIMIT, 0)
                )
                return
            await cq.message.edit_text(
                "💳 **پرداخت‌های در انتظار تایید**",
                reply_markup=kb.payments_list_kb(rows, offset, PAGE_LIMIT, total)
            )
            return

        if data.startswith("pnl_payment_approve:"):
            payment_id = int(data.split(":")[1])
            result = db.approve_payment(payment_id, OWNER_ID)
            if not result:
                await cq.answer("این پرداخت قبلاً پردازش شده یا پیدا نشد.", show_alert=True)
                return
            tenant_id, new_exp = result
            await cq.answer("تایید شد ✅")
            asyncio.create_task(notify_tenant(
                tenant_id,
                f"✅ **پرداخت شما تایید شد!**\n\nاشتراک شما تا {_fmt_ts(new_exp)} تمدید شد.\n"
                f"برای شروع: /start"
            ))
            await cq.message.edit_text(
                f"✅ پرداخت #{payment_id} تایید و اشتراک تمدید شد.",
                reply_markup=kb.payments_list_kb(db.list_pending_payments(0, PAGE_LIMIT), 0,
                                                  PAGE_LIMIT, db.count_pending_payments())
            )
            return

        if data.startswith("pnl_payment_reject:"):
            payment_id = int(data.split(":")[1])
            tenant_id = db.reject_payment(payment_id, OWNER_ID)
            if not tenant_id:
                await cq.answer("این پرداخت پیدا نشد.", show_alert=True)
                return
            await cq.answer("رد شد ❌")
            asyncio.create_task(notify_tenant(
                tenant_id,
                "❌ **پرداخت شما تایید نشد.**\n\nاگه فکر می‌کنید اشتباهی رخ داده، با پشتیبانی تماس بگیرید."
            ))
            await cq.message.edit_text(
                f"❌ پرداخت #{payment_id} رد شد.",
                reply_markup=kb.payments_list_kb(db.list_pending_payments(0, PAGE_LIMIT), 0,
                                                  PAGE_LIMIT, db.count_pending_payments())
            )
            return

        if data.startswith("pnl_payment:"):
            payment_id = int(data.split(":")[1])
            p = db.get_payment(payment_id)
            if not p:
                await cq.answer("این پرداخت پیدا نشد.", show_alert=True)
                return
            pid, tenant_id, plan_id, amount, method, status, authority, ref_id, note, created_at = p
            plan_name = "—"
            if plan_id:
                pl = db.get_plan(plan_id)
                if pl:
                    plan_name = pl[1]
            method_fa = "💳 کارت‌به‌کارت" if method == "manual" else "🌐 زرین‌پال"
            text = (
                f"💳 **پرداخت #{pid}**\n\n"
                f"مشتری: `{tenant_id}`\n"
                f"پلن: {plan_name}\n"
                f"مبلغ: {amount:,} تومان\n"
                f"روش: {method_fa}\n"
                f"وضعیت: {status}\n"
                f"تاریخ: {created_at}\n"
            )
            await cq.message.edit_text(text, reply_markup=kb.payment_detail_kb(pid))
            return

        if data.startswith("pnl_tickets:"):
            offset = int(data.split(":")[1])
            total = db.count_tickets("open")
            rows = db.list_tickets("open", offset=offset, limit=PAGE_LIMIT)
            if not rows and offset == 0:
                await cq.message.edit_text(
                    "هیچ تیکت باز/در انتظاری وجود نداره.",
                    reply_markup=kb.tickets_list_kb([], 0, PAGE_LIMIT, 0)
                )
                return
            await cq.message.edit_text(
                "🎫 **تیکت‌های باز**",
                reply_markup=kb.tickets_list_kb(rows, offset, PAGE_LIMIT, total)
            )
            return

        if data.startswith("pnl_ticket_reply:"):
            ticket_id = int(data.split(":")[1])
            db.set_panel_step(OWNER_ID, "awaiting_ticket_reply", str(ticket_id))
            await cq.message.edit_text(
                "متن پاسخ رو بفرست تا مستقیم برای مشتری ارسال بشه:",
                reply_markup=kb.cancel_kb(f"pnl_ticket:{ticket_id}")
            )
            return

        if data.startswith("pnl_ticket_close:"):
            ticket_id = int(data.split(":")[1])
            db.close_ticket(ticket_id)
            db.log_action(OWNER_ID, "close_ticket", note=f"ticket_id={ticket_id}")
            await cq.answer("بسته شد ✅")
            await cq.message.edit_text(
                "🎫 **تیکت‌های باز**",
                reply_markup=kb.tickets_list_kb(db.list_tickets("open", 0, PAGE_LIMIT), 0,
                                                PAGE_LIMIT, db.count_tickets("open"))
            )
            return

        if data.startswith("pnl_ticket:"):
            ticket_id = int(data.split(":")[1])
            t = db.get_ticket(ticket_id)
            if not t:
                await cq.answer("این تیکت پیدا نشد.", show_alert=True)
                return
            tid, tenant_id, message_txt, status, reply_text, created_at, replied_at = t
            text = (
                f"🎫 **تیکت #{tid}**\n\n"
                f"از: `{tenant_id}`\n"
                f"وضعیت: {status}\n\n"
                f"«{message_txt}»\n"
            )
            if reply_text:
                text += f"\n✅ پاسخ داده‌شده:\n«{reply_text}»"
            await cq.message.edit_text(text, reply_markup=kb.ticket_detail_kb(tid, status))
            return

        if data == "pnl_broadcast":
            plans = db.list_plans(only_active=True)
            await cq.message.edit_text(
                "📢 **اطلاع‌رسانی همگانی**\n\nبه کی ارسال بشه؟",
                reply_markup=kb.broadcast_target_kb(plans)
            )
            return

        if data == "pnl_bc_all" or data.startswith("pnl_bc_plan:"):
            target = "all" if data == "pnl_bc_all" else data.split(":")[1]
            db.set_panel_step(OWNER_ID, "awaiting_broadcast_text", target)
            await cq.message.edit_text(
                "متن پیام همگانی رو بفرست:",
                reply_markup=kb.cancel_kb("pnl_broadcast")
            )
            return

        if data.startswith("pnl_bc_send:"):
            step, step_data = db.get_panel_step(OWNER_ID)
            if step != "awaiting_broadcast_confirm":
                await cq.answer("این درخواست منقضی شده، دوباره امتحان کن.", show_alert=True)
                return
            info = json.loads(step_data)
            target, text = info["target"], info["text"]
            db.clear_panel_step(OWNER_ID)
            await cq.message.edit_text("⏳ در حال ارسال...")
            plan_id = None if target == "all" else int(target)
            tenant_ids = db.list_tenant_ids(plan_id)
            asyncio.create_task(_run_broadcast(cq.message, tenant_ids, text))
            return

    async def _run_broadcast(message, tenant_ids, text):
        ok = fail = 0
        for tid in tenant_ids:
            sent = await notify_tenant(tid, f"📢 **اطلاع‌رسانی**\n\n{text}")
            if sent:
                ok += 1
            else:
                fail += 1
            await asyncio.sleep(0.05)  # جلوگیری از فلود روی ربات تبچی
        db.log_action(OWNER_ID, "broadcast", note=f"sent={ok} failed={fail} text={text[:100]}")
        try:
            await message.reply(
                f"📢 **اطلاع‌رسانی همگانی تمام شد**\n\n✅ ارسال‌شده: {ok}\n❌ ناموفق: {fail}",
                reply_markup=kb.home_kb()
            )
        except Exception as e:
            print(f"[Broadcast] خطا در گزارش نهایی: {e}")

    async def _show_tenant(cq, tid):
        row = db.get_tenant(tid)
        if not row:
            await cq.message.edit_text("این مشتری پیدا نشد.", reply_markup=kb.tenants_list_kb([], 0, PAGE_LIMIT, 0))
            return
        telegram_id, username, full_name, created_at, status, plan_id, expires_at, note = row
        plan_name = "—"
        max_accounts = 0
        if plan_id:
            p = db.get_plan(plan_id)
            if p:
                plan_name = p[1]
                max_accounts = p[4]
        used_accounts = db.count_tenant_active_accounts(tid)
        cap_txt = f"{used_accounts} / {max_accounts if max_accounts else '∞'}"
        text = (
            f"👤 **{full_name or username or telegram_id}**\n"
            f"آیدی: `{telegram_id}`\n"
            f"یوزرنیم: {('@' + username) if username else '—'}\n"
            f"وضعیت: {kb.STATUS_EMOJI.get(status, '•')} {status}\n"
            f"پلن: {plan_name}\n"
            f"انقضا: {_fmt_ts(expires_at)}\n"
            f"📱 اکانت‌های فعال: {cap_txt}\n"
        )
        await cq.message.edit_text(text, reply_markup=kb.tenant_detail_kb(telegram_id, status))

    async def _show_plan(cq, plan_id):
        p = db.get_plan(plan_id)
        if not p:
            await cq.message.edit_text("این پلن پیدا نشد.", reply_markup=kb.plans_list_kb(db.list_plans()))
            return
        pid, name, price, duration_days, max_accounts, max_layers, features_json, is_active = p
        text = (
            f"📦 **{name}**\n"
            f"قیمت: {price:,} تومان\n"
            f"مدت: {duration_days} روز\n"
            f"سقف اکانت: {max_accounts if max_accounts else 'نامحدود'}\n"
            f"سقف لایه: {max_layers if max_layers else 'نامحدود'}\n"
            f"وضعیت: {'✅ فعال' if is_active else '🚫 غیرفعال'}\n"
        )
        await cq.message.edit_text(text, reply_markup=kb.plan_detail_kb(plan_id, is_active))

    @app.on_message(filters.text & owner_filter & ~filters.command("start"))
    async def panel_text(client, message: Message):
        step, step_data = db.get_panel_step(OWNER_ID)
        txt = message.text.strip()

        if step == "awaiting_tenant_add":
            parts = txt.split("|", 1)
            try:
                tid = int(parts[0].strip())
            except ValueError:
                await message.reply("آیدی نامعتبره — فقط عدد بفرست.")
                return
            name = parts[1].strip() if len(parts) > 1 else ""
            db.add_tenant(tid, full_name=name)
            db.log_action(OWNER_ID, "add_tenant", tid)
            db.clear_panel_step(OWNER_ID)
            await message.reply(f"مشتری `{tid}` اضافه شد ✅", reply_markup=kb.home_kb())
            return

        if step == "awaiting_extend_days":
            tid = int(step_data)
            try:
                days = int(txt)
            except ValueError:
                await message.reply("فقط عدد بفرست (مثلاً 30).")
                return
            new_exp = db.extend_tenant(tid, days)
            db.log_action(OWNER_ID, "extend", tid, note=f"+{days}d")
            db.clear_panel_step(OWNER_ID)
            await message.reply(
                f"اشتراک تا {_fmt_ts(new_exp)} تمدید شد ✅",
                reply_markup=kb.tenant_detail_kb(tid, "active")
            )
            return

        if step == "awaiting_plan_add":
            parts = [p.strip() for p in txt.split("|")]
            if len(parts) < 5:
                await message.reply("فرمت درست نیست. مثال:\n`طلایی|150000|30|10|3`")
                return
            name, price, duration_days, max_accounts, max_layers = parts[:5]
            try:
                price = int(price)
                duration_days = int(duration_days)
                max_accounts = int(max_accounts)
                max_layers = int(max_layers)
            except ValueError:
                await message.reply("قیمت/مدت/سقف‌ها باید عدد باشن.")
                return
            db.add_plan(name, price, duration_days, max_accounts, max_layers)
            db.log_action(OWNER_ID, "add_plan", note=name)
            db.clear_panel_step(OWNER_ID)
            await message.reply(f"پلن «{name}» ساخته شد ✅", reply_markup=kb.plans_list_kb(db.list_plans()))
            return

        if step == "awaiting_plan_field":
            info = json.loads(step_data)
            plan_id, field = info["plan_id"], info["field"]
            try:
                value = int(txt)
            except ValueError:
                await message.reply("فقط عدد بفرست.")
                return
            db.update_plan_field(plan_id, field, value)
            db.log_action(OWNER_ID, "edit_plan", note=f"plan_id={plan_id} {field}={value}")
            db.clear_panel_step(OWNER_ID)
            p = db.get_plan(plan_id)
            await message.reply("بروزرسانی شد ✅", reply_markup=kb.plan_detail_kb(plan_id, p[7]))
            return

        if step == "awaiting_ticket_reply":
            ticket_id = int(step_data)
            t = db.get_ticket(ticket_id)
            db.clear_panel_step(OWNER_ID)
            if not t:
                await message.reply("این تیکت پیدا نشد.", reply_markup=kb.home_kb())
                return
            tenant_id = t[1]
            db.reply_ticket(ticket_id, txt)
            db.log_action(OWNER_ID, "reply_ticket", tenant_id, note=f"ticket_id={ticket_id}")
            sent = await notify_tenant(
                tenant_id,
                f"💬 **پاسخ پشتیبانی به تیکت #{ticket_id}:**\n\n{txt}"
            )
            await message.reply(
                ("✅ پاسخ ارسال شد." if sent else "⚠️ پاسخ ثبت شد ولی ارسال پیام ناموفق بود "
                 "(احتمالاً TABCI_BOT_TOKEN تنظیم نشده)."),
                reply_markup=kb.home_kb()
            )
            return

        if step == "awaiting_broadcast_text":
            target = step_data  # "all" یا plan_id
            db.set_panel_step(OWNER_ID, "awaiting_broadcast_confirm",
                               json.dumps({"target": target, "text": txt}))
            plan_txt = "همه‌ی مشتری‌ها" if target == "all" else f"پلن #{target}"
            await message.reply(
                f"📢 **پیش‌نمایش پیام برای {plan_txt}:**\n\n{txt}\n\n"
                "ارسال بشه؟",
                reply_markup=kb.broadcast_confirm_kb(target)
            )
            return
