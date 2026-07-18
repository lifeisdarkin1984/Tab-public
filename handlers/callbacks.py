import asyncio, time
from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from database import q, u
from utils import (get_step, get_step_data, set_step,
                   clear_step, get_user_client, is_stopped, set_stop,
                   clear_chat_history, get_current_layer,
                   get_account_owner, is_tenant_locked, get_tenant_status)
from keyboards import *
from payment_client import create_zarinpal_payment
import os
import workers.reply_worker as rw
import workers.react_worker as rcw
import workers.secretary as sec_worker

OWNER_ID = int(os.environ.get("PANEL_OWNER_ID", 0))
CARD_NUMBER = os.environ.get("CARD_NUMBER", "")
CARD_HOLDER = os.environ.get("CARD_HOLDER", "")

# این callback dataها حتی برای تننت قفل‌شده (اشتراک تموم‌شده/مسدود) هم مجازن —
# چون خودِ مسیر خرید/تمدید اشتراکه
_ALLOWED_WHEN_LOCKED_PREFIXES = ("buy_",)
_ALLOWED_WHEN_LOCKED_EXACT = {"back_main", "support_start"}


def register(app):

    @app.on_callback_query()
    async def on_cb(client, cb: CallbackQuery):
        uid = cb.from_user.id
        d = cb.data

        if is_tenant_locked(uid) and d not in _ALLOWED_WHEN_LOCKED_EXACT \
                and not d.startswith(_ALLOWED_WHEN_LOCKED_PREFIXES):
            status, expires_at = get_tenant_status(uid)
            from datetime import datetime
            exp_txt = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else "-"
            msg = ("⛔️ دسترسی شما مسدود شده." if status == "suspended"
                   else f"⌛️ اشتراک شما در {exp_txt} تموم شده. داده‌هاتون محفوظه.")
            await cb.answer(msg, show_alert=True)
            try:
                await cb.message.edit_text(msg, reply_markup=billing_kb())
            except Exception:
                pass
            return

        try:
            if d == "back_main":
                await cb.message.edit_text("یک گزینه را انتخاب کنید:", reply_markup=main_menu_kb())

            elif d == "buy_plans":
                plans = q("SELECT id,name,price,duration_days FROM plans "
                          "WHERE is_active=1 ORDER BY price")
                if not plans:
                    await cb.answer("هنوز پلنی تعریف نشده. با پشتیبانی تماس بگیرید.", show_alert=True)
                else:
                    await cb.message.edit_text("📦 یکی از پلن‌ها رو انتخاب کن:",
                                               reply_markup=plans_buy_kb(plans))

            elif d.startswith("buy_plan_"):
                plan_id = int(d.split("_")[-1])
                await cb.message.edit_text("روش پرداخت رو انتخاب کن:",
                                           reply_markup=pay_method_kb(plan_id))

            elif d.startswith("buy_manual_"):
                plan_id = int(d.split("_")[-1])
                row = q("SELECT name, price FROM plans WHERE id=%s", (plan_id,))
                if not row:
                    await cb.answer("پلن پیدا نشد.", show_alert=True)
                else:
                    name, price = row[0]
                    set_step(uid, "awaiting_receipt", str(plan_id))
                    card_txt = (
                        f"💳 **پرداخت کارت‌به‌کارت — پلن {name}**\n\n"
                        f"مبلغ: **{price:,} تومان**\n"
                        f"شماره کارت: `{CARD_NUMBER or 'تنظیم نشده'}`\n"
                        f"به‌نام: {CARD_HOLDER or '—'}\n\n"
                        f"بعد از واریز، عکس رسید رو همینجا بفرست تا برای بررسی ارسال بشه."
                    )
                    await cb.message.edit_text(card_txt, reply_markup=manual_receipt_kb(plan_id))

            elif d.startswith("buy_zarinpal_"):
                plan_id = int(d.split("_")[-1])
                row = q("SELECT name, price FROM plans WHERE id=%s", (plan_id,))
                if not row:
                    await cb.answer("پلن پیدا نشد.", show_alert=True)
                else:
                    name, price = row[0]
                    await cb.answer("در حال اتصال به درگاه...")
                    authority, result = await create_zarinpal_payment(
                        price, f"خرید پلن {name} — کاربر {uid}"
                    )
                    if not authority:
                        await cb.message.edit_text(
                            f"❌ خطا در اتصال به درگاه:\n{result}",
                            reply_markup=pay_method_kb(plan_id)
                        )
                    else:
                        u("INSERT INTO payments (tenant_id, plan_id, amount, method, status, authority) "
                          "VALUES (%s,%s,%s,'zarinpal','pending',%s)",
                          (uid, plan_id, price, authority))
                        await cb.message.edit_text(
                            f"🌐 **پرداخت آنلاین — پلن {name}**\n"
                            f"مبلغ: {price:,} تومان\n\n"
                            f"روی دکمه‌ی زیر بزن و پرداخت رو کامل کن. بعد از پرداخت، اشتراکت "
                            f"خودکار فعال می‌شه.",
                            reply_markup=zarinpal_pay_kb(result)
                        )

            elif d == "support_start":
                set_step(uid, "awaiting_ticket")
                await cb.message.edit_text(
                    "🆘 **پشتیبانی**\n\n"
                    "پیامتون رو بنویسید تا برای بررسی برای پشتیبانی ارسال بشه. "
                    "معمولاً در کمتر از چند ساعت پاسخ داده می‌شه.",
                    reply_markup=support_cancel_kb()
                )

            elif d == "layers_menu":
                layers = q(
                    "SELECT l.id, l.name, COUNT(a.id) FROM layers l "
                    "LEFT JOIN accounts a ON a.layer_id=l.id "
                    "WHERE l.admin_id=%s GROUP BY l.id, l.name ORDER BY l.id",
                    (uid,)
                )
                await cb.message.edit_text("یک لایه را انتخاب کنید:", reply_markup=layers_kb(layers))

            elif d.startswith("layer_sel_"):
                layer_id = d[10:]
                lyr = q("SELECT name FROM layers WHERE id=%s AND admin_id=%s", (layer_id, uid))
                if not lyr:
                    await cb.answer("لایه یافت نشد", show_alert=True); return
                u("UPDATE admins SET current_layer_id=%s WHERE id=%s", (layer_id, uid))
                await cb.message.edit_text(
                    f"✅ لایه‌ی **{lyr[0][0]}** فعال شد.\n\nیک گزینه را انتخاب کنید:",
                    reply_markup=main_menu_kb()
                )

            elif d == "layer_new":
                set_step(uid, "layer_new")
                await cb.message.edit_text(
                    "📝 نام لایه‌ی جدید را وارد کنید:\nمثال: `تبلیغاتی`",
                    reply_markup=back_kb("layers_menu")
                )

            elif d.startswith("layer_mng_"):
                layer_id = d[10:]
                lyr = q("SELECT name FROM layers WHERE id=%s AND admin_id=%s", (layer_id, uid))
                if not lyr:
                    await cb.answer("لایه یافت نشد", show_alert=True); return
                await cb.message.edit_text(
                    f"⚙️ مدیریت لایه‌ی **{lyr[0][0]}**",
                    reply_markup=layer_manage_kb(layer_id)
                )

            elif d.startswith("layer_ren_"):
                layer_id = d[10:]
                set_step(uid, f"layer_ren_{layer_id}")
                await cb.message.edit_text(
                    "✏️ نام جدید لایه را وارد کنید:",
                    reply_markup=back_kb(f"layer_mng_{layer_id}")
                )

            elif d.startswith("layer_del_yes_"):
                layer_id = d[14:]
                cnt = q("SELECT COUNT(*) FROM accounts WHERE layer_id=%s AND admin_id=%s",
                        (layer_id, uid))
                if cnt and cnt[0][0] > 0:
                    await cb.answer("❌ این لایه اکانت دارد و قابل حذف نیست.", show_alert=True)
                    return
                u("DELETE FROM layers WHERE id=%s AND admin_id=%s", (layer_id, uid))
                cur_lyr = q("SELECT current_layer_id FROM admins WHERE id=%s", (uid,))
                if cur_lyr and str(cur_lyr[0][0]) == str(layer_id):
                    remaining = q("SELECT id FROM layers WHERE admin_id=%s ORDER BY id LIMIT 1", (uid,))
                    new_cur = remaining[0][0] if remaining else None
                    u("UPDATE admins SET current_layer_id=%s WHERE id=%s", (new_cur, uid))
                await cb.answer("✅ لایه حذف شد", show_alert=True)
                layers = q(
                    "SELECT l.id, l.name, COUNT(a.id) FROM layers l "
                    "LEFT JOIN accounts a ON a.layer_id=l.id "
                    "WHERE l.admin_id=%s GROUP BY l.id, l.name ORDER BY l.id",
                    (uid,)
                )
                await cb.message.edit_text("یک لایه را انتخاب کنید:", reply_markup=layers_kb(layers))

            elif d.startswith("layer_del_"):
                layer_id = d[10:]
                cnt = q("SELECT COUNT(*) FROM accounts WHERE layer_id=%s AND admin_id=%s",
                        (layer_id, uid))
                if cnt and cnt[0][0] > 0:
                    await cb.answer(
                        f"❌ این لایه {cnt[0][0]} اکانت دارد. اول اکانت‌ها را حذف یا منتقل کن.",
                        show_alert=True
                    )
                    return
                await cb.message.edit_text(
                    "⚠️ حذف این لایه (خالی از اکانت) قطعی است؟",
                    reply_markup=confirm_kb(f"layer_del_yes_{layer_id}", f"layer_mng_{layer_id}")
                )

            elif d == "menu_tabchi":
                cur_lyr = q("SELECT current_layer_id FROM admins WHERE id=%s", (uid,))
                layer_id = cur_lyr[0][0] if cur_lyr else None
                accs = q("SELECT id,phone,name FROM accounts WHERE admin_id=%s AND layer_id=%s",
                         (uid, layer_id))
                if not accs:
                    await cb.answer("/add_account برای افزودن", show_alert=True); return
                await cb.message.edit_text("📌 **لیست تبچیزهای شما:**", reply_markup=tabchi_list_kb(accs))

            elif d == "menu_global":
                await cb.message.edit_text("🌐 **مدیریت همگانی**", reply_markup=global_kb())

            # ══ منوی حذف همگانی ══
            elif d == "g_del_menu":
                await cb.message.edit_text("🗑 **حذف همگانی**\n\nچه چیزی حذف بشه؟", reply_markup=global_del_menu_kb())

            elif d == "g_delpv":
                await cb.message.edit_text("⚠️ حذف **تمام پیوی‌ها** در همه اکانت‌ها؟",
                                            reply_markup=confirm_kb("g_delpv_yes", "g_del_menu"))

            elif d == "g_delpv_yes":
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                         (uid, layer_id))
                set_stop(False)
                await cb.message.edit_text(f"⏳ حذف پیوی‌ها برای {len(accs)} اکانت شروع شد...")
                for (aid,) in accs:
                    t = asyncio.create_task(_delete_pvs_task(client, aid)); t.set_name("del_pv_task")

            elif d == "g_delbot":
                await cb.message.edit_text("⚠️ حذف **تمام گفتگو با ربات‌ها** در همه اکانت‌ها؟",
                                            reply_markup=confirm_kb("g_delbot_yes", "g_del_menu"))

            elif d == "g_delbot_yes":
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                         (uid, layer_id))
                set_stop(False)
                await cb.message.edit_text(f"⏳ حذف ربات‌ها برای {len(accs)} اکانت شروع شد...")
                for (aid,) in accs:
                    t = asyncio.create_task(_delete_bots_task(client, aid)); t.set_name("del_bot_task")

            elif d == "g_delchannel":
                await cb.message.edit_text("⚠️ خروج از **تمام کانال‌ها** در همه اکانت‌ها؟",
                                            reply_markup=confirm_kb("g_delchannel_yes", "g_del_menu"))

            elif d == "g_delchannel_yes":
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                         (uid, layer_id))
                set_stop(False)
                await cb.message.edit_text(f"⏳ خروج از کانال‌ها برای {len(accs)} اکانت شروع شد...")
                for (aid,) in accs:
                    t = asyncio.create_task(_delete_channels_task(client, aid)); t.set_name("del_channel_task")

            elif d == "g_delgrp":
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                if not tag_list:
                    await cb.message.edit_text(
                        "⚠️ خروج از **تمام گروه‌ها** در همه اکانت‌ها؟",
                        reply_markup=confirm_kb("gdelgrp_yes_ALL_ALL", "g_del_menu"))
                    return
                await cb.message.edit_text("👤 حذف گروه‌های کدوم اکانت‌ها؟",
                                            reply_markup=tag_select_kb(tag_list, "gdelgrpatag"))

            elif d.startswith("gdelgrpatag_tag_"):
                acc_tag = d[16:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text("👥 حذف کدوم گروه‌ها؟",
                                            reply_markup=tag_select_kb(tag_list, f"gdelgrpgtag_{acc_tag}"))

            elif d.startswith("gdelgrpgtag_"):
                rest = d[12:]  # {acc_tag}_tag_{group_tag}
                acc_tag, _, group_tag = rest.partition("_tag_")
                await cb.message.edit_text(
                    f"⚠️ خروج از گروه‌های منطبق [👤 {acc_tag} | 👥 {group_tag}]؟\nمطمئنید؟",
                    reply_markup=confirm_kb(f"gdelgrp_yes_{acc_tag}_{group_tag}", "g_del_menu"))

            elif d.startswith("gdelgrp_yes_"):
                rest = d[12:]  # {acc_tag}_{group_tag}
                acc_tag, _, group_tag = rest.partition("_")
                from handlers.text_handler import get_filtered_accounts
                accs = get_filtered_accounts(acc_tag, uid)
                set_stop(False)
                await cb.message.edit_text(f"⏳ خروج از گروه‌ها برای {len(accs)} اکانت شروع شد...")
                for (aid,) in accs:
                    t = asyncio.create_task(_delete_groups_task(client, aid, group_tag_filter=group_tag))
                    t.set_name("del_grp_task")

            # ══ توقف تمام عملیات ══
            elif d == "g_stopall":
                set_stop(True)
                rw.STOP_FLAG = True
                rcw.STOP_FLAG = True
                for t in asyncio.all_tasks():
                    if t.get_name() in ("join_task","send_pv_task","send_grp_task","reply_task","react_task"):
                        t.cancel()
                await cb.message.edit_text(
                    "🛑 **تمام عملیات متوقف شد**\n\n"
                    "✅ عضویت در گروه‌ها\n✅ ارسال پیام‌ها\n✅ ریپلای و ری‌اکت",
                    reply_markup=global_kb()
                )

            # ══ حذف اکانت ══
            elif d.startswith("acc_del_yes_"):
                acc_id = d[12:]
                for tbl in ["accounts","secretary","scheduler","banners","join_settings","reply_rand","react_rand"]:
                    col = "id" if tbl == "accounts" else "account_id"
                    u(f"DELETE FROM {tbl} WHERE {col}=%s", (acc_id,))
                cur_lyr = q("SELECT current_layer_id FROM admins WHERE id=%s", (uid,))
                layer_id = cur_lyr[0][0] if cur_lyr else None
                accs = q("SELECT id,phone,name FROM accounts WHERE admin_id=%s AND layer_id=%s",
                         (uid, layer_id))
                if accs:
                    await cb.message.edit_text("✅ حذف شد.\n\n📌 لیست:", reply_markup=tabchi_list_kb(accs))
                else:
                    await cb.message.edit_text("✅ حذف شد.\n\n/add_account برای افزودن",
                                                reply_markup=back_kb("back_main"))

            elif d.startswith("acc_del_"):
                acc_id = d[8:]
                acc = q("SELECT name,phone FROM accounts WHERE id=%s", (acc_id,))
                name = f"{acc[0][0]} | {acc[0][1]}" if acc else acc_id
                await cb.message.edit_text(
                    f"❗️ حذف اکانت:\n👤 {name}",
                    reply_markup=confirm_kb(f"acc_del_yes_{acc_id}", "menu_tabchi")
                )

            elif d.startswith("acc_sel_"):
                acc_id = d[8:]
                acc = q("SELECT id,phone,name,username FROM accounts WHERE id=%s", (acc_id,))
                if not acc:
                    await cb.answer("اکانت یافت نشد", show_alert=True); return
                a = acc[0]
                await cb.message.edit_text(
                    f"👤 **{a[2]}** | `{a[1]}`\n🔗 @{a[3] or 'ندارد'}",
                    reply_markup=acc_info_kb(acc_id)
                )

            elif d.startswith("acc_manage_"):
                acc_id = d[11:]
                acc = q("SELECT name,phone FROM accounts WHERE id=%s", (acc_id,))
                if not acc:
                    await cb.answer("اکانت یافت نشد", show_alert=True); return
                await cb.message.edit_text(
                    f"⚙️ پنل مدیریت **{acc[0][0]}** | `{acc[0][1]}`",
                    reply_markup=manage_kb(acc_id)
                )

            elif d.startswith("acc_movelyr_do_"):
                rest = d[15:]
                acc_id, _, layer_id = rest.rpartition("_")
                acc = q("SELECT name, layer_id FROM accounts WHERE id=%s AND admin_id=%s",
                        (acc_id, uid))
                if not acc:
                    await cb.answer("اکانت یافت نشد", show_alert=True); return
                lyr = q("SELECT name FROM layers WHERE id=%s AND admin_id=%s", (layer_id, uid))
                if not lyr:
                    await cb.answer("لایه یافت نشد", show_alert=True); return
                u("UPDATE accounts SET layer_id=%s WHERE id=%s AND admin_id=%s",
                  (layer_id, acc_id, uid))
                await cb.answer(f"✅ به لایه‌ی «{lyr[0][0]}» منتقل شد", show_alert=True)
                await cb.message.edit_text(
                    f"⚙️ پنل مدیریت **{acc[0][0]}**",
                    reply_markup=manage_kb(acc_id)
                )

            elif d.startswith("acc_movelyr_"):
                acc_id = d[12:]
                acc = q("SELECT layer_id FROM accounts WHERE id=%s AND admin_id=%s", (acc_id, uid))
                if not acc:
                    await cb.answer("اکانت یافت نشد", show_alert=True); return
                cur_layer_id = acc[0][0]
                others = q("SELECT id,name FROM layers WHERE admin_id=%s AND id!=%s ORDER BY id",
                           (uid, cur_layer_id))
                if not others:
                    await cb.answer("لایه‌ی دیگری برای انتقال وجود ندارد. اول یه لایه‌ی جدید بساز.",
                                     show_alert=True)
                    return
                await cb.message.edit_text(
                    "📦 به کدوم لایه منتقل بشه؟",
                    reply_markup=layer_move_kb(acc_id, others)
                )

            elif d.startswith("acc_code_"):
                acc_id = d[9:]
                acc = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
                if not acc:
                    await cb.answer("اکانت یافت نشد", show_alert=True); return
                await cb.answer(f"📱 کد به {acc[0][0]} ارسال می‌شود", show_alert=True)

            elif d.startswith("acc_status_"):
                acc_id = d[11:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ سشن پیدا نشد", show_alert=True); return
                try:
                    await uc.start(); me = await uc.get_me(); await uc.stop()
                    await cb.answer(f"✅ فعال\n👤 {me.first_name}", show_alert=True)
                except Exception as e:
                    await cb.answer(f"❌ {str(e)[:80]}", show_alert=True)

            elif d == "acc_refresh":
                cur_lyr = q("SELECT current_layer_id FROM admins WHERE id=%s", (uid,))
                layer_id = cur_lyr[0][0] if cur_lyr else None
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s",
                         (uid, layer_id))
                ok = 0
                for (aid,) in accs:
                    try:
                        uc = await get_user_client(aid)
                        await uc.start(); me = await uc.get_me(); await uc.stop()
                        u("UPDATE accounts SET name=%s,username=%s,status='active' WHERE id=%s",
                          (me.first_name or str(me.id), me.username or "", aid))
                        ok += 1
                    except Exception:
                        u("UPDATE accounts SET status='inactive' WHERE id=%s", (aid,))
                await cb.answer(f"✅ {ok} اکانت بروزرسانی شد", show_alert=True)

            # ══ آمار اکانت ══
            elif d.startswith("m_stats_"):
                acc_id = d[8:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                try:
                    from pyrogram import enums as en
                    await uc.start()
                    me = await uc.get_me()
                    if me is None:
                        raise RuntimeError("get_me() مقدار None برگردوند — سشن ناقص/نامعتبره")
                    grps = chns = pvs = 0
                    async for dlg in uc.get_dialogs():
                        if dlg.chat is None:
                            continue
                        if dlg.chat.type in (en.ChatType.GROUP, en.ChatType.SUPERGROUP): grps += 1
                        elif dlg.chat.type == en.ChatType.CHANNEL: chns += 1
                        elif dlg.chat.type == en.ChatType.PRIVATE: pvs += 1
                    await uc.stop()
                    await cb.message.edit_text(
                        f"⚙️ پنل مدیریت `{me.id}`\n\n"
                        f"👤 {me.first_name or ''} {me.last_name or ''}\n"
                        f"📊 وضعیت: فعال ✅\n💬 کانال: {chns}\n"
                        f"🗣 گروه‌ها: {grps}\n🪡 پیوی‌ها: {pvs}\n"
                        f"🔗 @{me.username or 'ندارد'}",
                        reply_markup=back_kb(f"acc_manage_{acc_id}")
                    )
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    print(f"[m_stats_ ERROR] acc_id={acc_id}\n{tb}")
                    try:
                        await uc.stop()
                    except Exception:
                        pass
                    await cb.answer(f"❌ {type(e).__name__}: {str(e)[:150]}", show_alert=True)


            # ══ خروج خودکار از گروه‌های محدود ══
            elif d.startswith("autoleave_tog_"):
                acc_id = d[14:]
                row = q("SELECT auto_leave_limited FROM accounts WHERE id=%s", (acc_id,))
                cur = row[0][0] if row else 0
                new = 0 if cur else 1
                u("UPDATE accounts SET auto_leave_limited=%s WHERE id=%s", (new, acc_id))
                await cb.answer(f"خروج خودکار {'فعال' if new else 'غیرفعال'} شد", show_alert=True)
                await cb.message.edit_reply_markup(auto_leave_kb(acc_id, new))

            elif d.startswith("m_autoleave_"):
                acc_id = d[12:]
                row = q("SELECT auto_leave_limited FROM accounts WHERE id=%s", (acc_id,))
                active = row[0][0] if row else 0
                await cb.message.edit_text(
                    f"🚫 **خروج خودکار از گروه‌های محدود**\n\n"
                    f"با فعال بودن این گزینه، اگر در هنگام ارسال پیام محدود شوید "
                    f"به‌صورت خودکار از آن گروه خارج می‌شوید.\n\n"
                    f"وضعیت: {'✅ فعال' if active else '❌ غیرفعال'}",
                    reply_markup=auto_leave_kb(acc_id, active)
                )

            # ══ global خروج خودکار ══
            elif d == "g_autoleave_tog":
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s", (uid, layer_id))
                row = q("SELECT auto_leave_limited FROM accounts WHERE admin_id=%s AND layer_id=%s LIMIT 1",
                        (uid, layer_id))
                cur = row[0][0] if row else 0
                new = 0 if cur else 1
                for (aid,) in accs:
                    u("UPDATE accounts SET auto_leave_limited=%s WHERE id=%s", (new, aid))
                await cb.answer(f"خروج خودکار {'فعال' if new else 'غیرفعال'} برای همه", show_alert=True)

            # ══ global عضویت اجبار ══
            elif d == "g_fj_tog":
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s", (uid, layer_id))
                row = q("SELECT force_join_active FROM join_settings WHERE admin_id=%s "
                        "AND account_id IN (SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s) LIMIT 1",
                        (uid, uid, layer_id))
                cur = row[0][0] if row else 0
                new = 0 if cur else 1
                for (aid,) in accs:
                    u("INSERT INTO join_settings (account_id,admin_id,force_join_active) "
                      "VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE force_join_active=%s",
                      (aid, uid, new, new))
                await cb.answer(f"عضویت اجبار {'فعال' if new else 'غیرفعال'} شد")
                await cb.message.edit_text(
                    f"🕵️ **عضویت اجبار (همگانی)**\n\n"
                    f"وضعیت: {'✅ فعال' if new else '❌ غیرفعال'}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"{'🔴 غیرفعال' if new else '🟢 فعال'} برای همه",
                            callback_data="g_fj_tog")],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_global")]
                    ])
                )

            # ══ منشی خودکار ══
            elif d.startswith("m_sec_"):
                acc_id = d[6:]
                row = q("SELECT is_active FROM secretary WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0
                await cb.message.edit_text(
                    f"👽 **پنل منشی خودکار**\n\nوضعیت: {'✅ فعال' if active else '❌ غیرفعال'}",
                    reply_markup=secretary_kb(acc_id, active)
                )

            elif d.startswith("sec_tog_"):
                acc_id = d[8:]
                row = q("SELECT is_active FROM secretary WHERE account_id=%s", (acc_id,))
                new = 0 if (row[0][0] if row else 0) else 1
                u("INSERT INTO secretary (account_id,admin_id,is_active) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE is_active=%s", (acc_id, uid, new, new))
                await cb.answer(f"منشی {'فعال' if new else 'غیرفعال'} شد")
                await cb.message.edit_reply_markup(secretary_kb(acc_id, new))

            elif d.startswith("sec_b"):
                slot = int(d[5]); acc_id = d[7:]
                bnrs = q("SELECT slot,text,file_id FROM banners "
                         "WHERE account_id=%s AND context='secretary' ORDER BY slot", (acc_id,))
                txt = "✏️ **مدیریت بنرها**\n\n"
                for b in bnrs:
                    txt += f"═-═ {b[0]} ═-═\n💬 [{(b[1] or '')[:40]}...]\n📁 {'✅' if b[2] else '❌'}\n\n"
                if not bnrs: txt += "هیچ بنری ثبت نشده."
                await cb.message.edit_text(txt, reply_markup=banner_slot_kb(acc_id, slot, "secretary"))

            elif d.startswith("bn_add_"):
                _, _, acc_id, slot, ctx = d.split("_", 4)
                slot = int(slot)
                set_step(uid, f"bn_text_{acc_id}_{slot}_{ctx}")
                await cb.message.edit_text("📝 متن بنر را وارد کنید:")

            elif d.startswith("bn_del_"):
                _, _, acc_id, slot, ctx = d.split("_", 4)
                slot = int(slot)
                u("DELETE FROM banners WHERE account_id=%s AND slot=%s AND context=%s", (acc_id, slot, ctx))
                await cb.answer(f"✅ بنر {slot} حذف شد")
                await cb.message.edit_reply_markup(banner_slot_kb(acc_id, slot, ctx))

            elif d.startswith("bn_delall_"):
                _, _, acc_id, ctx = d.split("_", 3)
                u("DELETE FROM banners WHERE account_id=%s AND context=%s", (acc_id, ctx))
                await cb.answer("✅ همه بنرها حذف شدند")

            elif d.startswith("bn_back_"):
                _, _, acc_id, ctx = d.split("_", 3)
                if ctx == "secretary":
                    row = q("SELECT is_active FROM secretary WHERE account_id=%s", (acc_id,))
                    active = row[0][0] if row else 0
                    await cb.message.edit_text("👽 پنل منشی:", reply_markup=secretary_kb(acc_id, active))
                elif ctx == "g_secretary":
                    layer_id = get_current_layer(uid)
                    row = q("SELECT is_active FROM global_secretary_settings WHERE admin_id=%s AND layer_id=%s",
                            (uid, layer_id))
                    active = bool(row[0][0]) if row else False
                    slots = [b[0] for b in q("SELECT slot FROM banners WHERE account_id=%s AND context='g_secretary' ORDER BY slot", (acc_id,))]
                    await cb.message.edit_text("🤖 **منشی خودکار همگانی**", reply_markup=global_sec_kb(active, slots=slots))
                else:
                    row2 = q("SELECT is_active FROM scheduler WHERE account_id=%s", (acc_id,))
                    active2 = row2[0][0] if row2 else 0
                    await cb.message.edit_text("⏰ پنل زمان‌بند:", reply_markup=scheduler_kb(acc_id, active2))


            # ══ زمان‌بند ══
            elif d.startswith("m_sch_"):
                acc_id = d[6:]
                row = q("SELECT is_active,interval_minutes FROM scheduler WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0; interval = row[0][1] if row else 10
                await cb.message.edit_text(
                    f"⏰ **پنل زمان‌بندی**\nفاصله: {interval} دقیقه\nوضعیت: {'✅' if active else '❌'}",
                    reply_markup=scheduler_kb(acc_id, active)
                )

            elif d.startswith("sch_tog_"):
                acc_id = d[8:]
                row = q("SELECT is_active FROM scheduler WHERE account_id=%s", (acc_id,))
                new = 0 if (row[0][0] if row else 0) else 1
                u("INSERT INTO scheduler (account_id,admin_id,is_active) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE is_active=%s", (acc_id, uid, new, new))
                await cb.answer(f"زمان‌بند {'فعال' if new else 'غیرفعال'} شد")
                await cb.message.edit_reply_markup(scheduler_kb(acc_id, new))

            elif d.startswith("sch_time_"):
                acc_id = d[9:]
                set_step(uid, f"sch_int_{acc_id}")
                await cb.message.edit_text("⏱ زمان ارسال (دقیقه):")

            elif d.startswith("sch_txt_"):
                acc_id = d[8:]
                set_step(uid, f"bn_text_{acc_id}_1_scheduler")
                await cb.message.edit_text("📝 متن بنر زمان‌بند:")

            elif d.startswith("sch_ref_"):
                await cb.answer("🔄 بروزرسانی شد", show_alert=True)

            elif d.startswith("sch_fwd_"):
                acc_id = d[8:]
                set_step(uid, f"sch_fwd_link_{acc_id}")
                await cb.message.edit_text("📤 لینک پیام برای فوروارد:", reply_markup=back_kb(f"m_sch_{acc_id}"))

            # ══ استخراج لینک ══
            elif d.startswith("m_ext_"):
                acc_id = d[6:]
                await cb.message.edit_text(
                    "🔗 **استخراج لینک**\nنوع استخراج را انتخاب کنید:",
                    reply_markup=extract_kb(acc_id)
                )

            elif d.startswith("ext_one_"):
                acc_id = d[8:]
                set_step(uid, f"ext_ch_{acc_id}")
                await cb.message.edit_text(
                    "🔗 یوزرنیم لینکدونی را بفرستید:\nمثال: `@link4you`",
                    reply_markup=back_kb(f"m_ext_{acc_id}")
                )

            elif d.startswith("ext_multi_"):
                acc_id = d[10:]
                set_step(uid, f"ext_multi_ch_{acc_id}")
                await cb.message.edit_text(
                    "🔗🔗 یوزرنیم لینکدونی‌ها را هر کدام در یک خط بفرستید:",
                    reply_markup=back_kb(f"m_ext_{acc_id}")
                )

            # ══ لیست گروه‌ها ══
            elif d.startswith("m_grps_"):
                acc_id = d[7:]
                row = q("SELECT force_join_active FROM join_settings WHERE account_id=%s", (acc_id,))
                fj = row[0][0] if row else 0
                await cb.message.edit_text("👥 **لیست گروه‌های من**", reply_markup=groups_kb(acc_id, fj))

            elif d.startswith("grp_stats_"):
                acc_id = d[10:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                from pyrogram import enums as en
                grps = 0
                await uc.start()
                async for dlg in uc.get_dialogs():
                    if dlg.chat.type in (en.ChatType.GROUP, en.ChatType.SUPERGROUP): grps += 1
                await uc.stop()
                await cb.answer(f"👥 گروه‌ها: {grps}", show_alert=True)

            elif d.startswith("grp_leave_limited_"):
                acc_id = d[18:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                from pyrogram import enums as en
                from pyrogram.errors import ChatWriteForbidden
                left = 0
                await uc.start()
                async for dlg in uc.get_dialogs():
                    if dlg.chat.type in (en.ChatType.GROUP, en.ChatType.SUPERGROUP):
                        try:
                            await uc.send_message(dlg.chat.id, ".")
                        except ChatWriteForbidden:
                            await uc.leave_chat(dlg.chat.id); left += 1
                await uc.stop()
                await cb.answer(f"✅ از {left} گروه محدود خارج شد", show_alert=True)

            elif d.startswith("grp_fj_tog_"):
                acc_id = d[11:]
                row = q("SELECT force_join_active FROM join_settings WHERE account_id=%s", (acc_id,))
                new = 0 if (row[0][0] if row else 0) else 1
                u("INSERT INTO join_settings (account_id,admin_id,force_join_active) "
                  "VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE force_join_active=%s", (acc_id, uid, new, new))
                await cb.answer(f"عضویت اجبار {'فعال' if new else 'غیرفعال'} شد", show_alert=True)
                await cb.message.edit_reply_markup(groups_kb(acc_id, new))

            # ══ حذف پیوی‌ها ══
            elif d.startswith("delpv_yes_"):
                acc_id = d[10:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                from pyrogram import enums as en
                count = 0
                await uc.start()
                async for dlg in uc.get_dialogs():
                    if dlg.chat.type == en.ChatType.PRIVATE:
                        try:
                            await clear_chat_history(uc, dlg.chat.id); count += 1
                        except Exception: pass
                await uc.stop()
                await cb.message.edit_text(f"✅ {count} پیوی حذف شد.", reply_markup=back_kb(f"acc_manage_{acc_id}"))

            elif d.startswith("m_delpv_"):
                acc_id = d[8:]
                await cb.message.edit_text("🗑 حذف همه پیوی‌ها؟",
                                            reply_markup=confirm_kb(f"delpv_yes_{acc_id}", f"acc_manage_{acc_id}"))

            # ══ منوی حذف (تک‌اکانت) ══
            elif d.startswith("m_del_menu_"):
                acc_id = d[11:]
                await cb.message.edit_text("🗑 **حذف**\n\nچه چیزی حذف بشه؟", reply_markup=del_menu_kb(acc_id))

            # ══ حذف گروه‌ها (تک‌اکانت) ══
            elif d.startswith("m_delgrp_"):
                acc_id = d[9:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                if not tag_list:
                    await cb.message.edit_text(
                        "⚠️ خروج از **تمام گروه‌های** این اکانت؟",
                        reply_markup=confirm_kb(f"delgrp_yes_{acc_id}_ALL", f"acc_manage_{acc_id}"))
                    return
                await cb.message.edit_text("👥 حذف کدوم گروه‌ها؟",
                                            reply_markup=tag_select_kb(tag_list, f"delgrptag_{acc_id}"))

            elif d.startswith("delgrptag_"):
                rest = d[10:]  # {acc_id}_tag_{group_tag}
                acc_id, _, group_tag = rest.partition("_tag_")
                tag_lbl = "بدون برچسب" if group_tag == "NOTAG" else ("همه گروه‌ها" if group_tag == "ALL" else f"«{group_tag}»")
                await cb.message.edit_text(
                    f"⚠️ خروج از گروه‌های {tag_lbl}؟",
                    reply_markup=confirm_kb(f"delgrp_yes_{acc_id}_{group_tag}", f"acc_manage_{acc_id}"))

            elif d.startswith("delgrp_yes_"):
                rest = d[11:]  # {acc_id}_{group_tag}
                acc_id, _, group_tag = rest.partition("_")
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                from pyrogram import enums as en
                from handlers.text_handler import get_filtered_chat_ids, _chat_allowed
                filter_result = get_filtered_chat_ids(acc_id, group_tag)
                count = 0
                await uc.start()
                async for dlg in uc.get_dialogs():
                    if dlg.chat.type in (en.ChatType.GROUP, en.ChatType.SUPERGROUP) and _chat_allowed(dlg.chat.id, filter_result):
                        try:
                            await uc.leave_chat(dlg.chat.id); count += 1; await asyncio.sleep(0.5)
                        except Exception: pass
                await uc.stop()
                await cb.message.edit_text(f"✅ از {count} گروه خارج شد.", reply_markup=back_kb(f"acc_manage_{acc_id}"))

            # ══ حذف ربات‌ها (تک‌اکانت) ══
            elif d.startswith("m_delbot_"):
                acc_id = d[9:]
                await cb.message.edit_text("🗑 حذف همه گفتگو با ربات‌ها؟",
                                            reply_markup=confirm_kb(f"delbot_yes_{acc_id}", f"acc_manage_{acc_id}"))

            elif d.startswith("delbot_yes_"):
                acc_id = d[11:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                from pyrogram import enums as en
                count = 0
                await uc.start()
                async for dlg in uc.get_dialogs():
                    if dlg.chat.type == en.ChatType.BOT:
                        try:
                            await clear_chat_history(uc, dlg.chat.id); count += 1
                        except Exception: pass
                await uc.stop()
                await cb.message.edit_text(f"✅ {count} گفتگو با ربات حذف شد.", reply_markup=back_kb(f"acc_manage_{acc_id}"))

            # ══ حذف کانال‌ها (تک‌اکانت) ══
            elif d.startswith("m_delchannel_"):
                acc_id = d[13:]
                await cb.message.edit_text("🗑 خروج از همه کانال‌ها؟",
                                            reply_markup=confirm_kb(f"delchannel_yes_{acc_id}", f"acc_manage_{acc_id}"))

            elif d.startswith("delchannel_yes_"):
                acc_id = d[15:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                from pyrogram import enums as en
                count = 0
                await uc.start()
                async for dlg in uc.get_dialogs():
                    if dlg.chat.type == en.ChatType.CHANNEL:
                        try:
                            await uc.leave_chat(dlg.chat.id); count += 1; await asyncio.sleep(0.5)
                        except Exception: pass
                await uc.stop()
                await cb.message.edit_text(f"✅ از {count} کانال خارج شد.", reply_markup=back_kb(f"acc_manage_{acc_id}"))

            # ══ عضویت در لینک‌ها ══
            elif d.startswith("join_go_"):
                acc_id = d[8:]
                links = [l for l in get_step_data(uid).splitlines() if l.strip()]
                row = q("SELECT min_delay,max_delay FROM join_settings WHERE account_id=%s", (acc_id,))
                mn, mx = (row[0][0], row[0][1]) if row else (180, 420)
                set_stop(False)
                await cb.message.edit_text(f"🚀 عملیات عضویت {len(links)} لینک شروع شد...")
                from handlers.text_handler import _join_links
                t = asyncio.create_task(_join_links(client, acc_id, links, mn, mx))
                t.set_name("join_task")
                clear_step(uid)

            # ── انتخاب برچسب برای جوین تک‌اکانت ──
            elif d.startswith("jointag_"):
                # d = jointag_{acc_id}_tag_{tagname}
                rest = d[8:]  # {acc_id}_tag_{tagname}
                acc_id, _, tag_name = rest.partition("_tag_")
                links = [l for l in get_step_data(uid).splitlines() if l.strip()]
                chosen_tag = "" if tag_name == "NOTAG" else tag_name
                row = q("SELECT min_delay,max_delay FROM join_settings WHERE account_id=%s", (acc_id,))
                mn, mx = (row[0][0], row[0][1]) if row else (180, 420)
                tag_lbl = f"«{chosen_tag}»" if chosen_tag else "بدون برچسب"
                set_stop(False)
                await cb.message.edit_text(
                    f"🚀 عملیات عضویت {len(links)} لینک شروع شد...\n🏷 برچسب: {tag_lbl}"
                )
                from handlers.text_handler import _join_links
                t = asyncio.create_task(_join_links(client, acc_id, links, mn, mx,
                                                     tag=chosen_tag))
                t.set_name("join_task")
                clear_step(uid)

            # ── انتخاب برچسب برای جوین همگانی ──
            elif d == "gjoin_nodup" or d == "gjoin_all":
                import json
                raw = get_step_data(uid)
                try:
                    data = json.loads(raw)
                except Exception:
                    await cb.answer("❌ خطا در بازیابی داده.", show_alert=True)
                    return
                if d == "gjoin_nodup":
                    chosen_links = data.get("new", [])
                else:
                    chosen_links = data.get("all", [])
                if not chosen_links:
                    await cb.answer("❌ هیچ لینکی برای جوین وجود ندارد.", show_alert=True)
                    clear_step(uid)
                    return
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                set_step(uid, "g_join_tag", "\n".join(chosen_links))
                await cb.message.edit_text(
                    f"✅ {len(chosen_links)} لینک انتخاب شد.\nبرچسب گروه‌ها را انتخاب کنید:",
                    reply_markup=tag_select_kb(tag_list, "gjointag", show_all=False)
                )

            elif d.startswith("gjointag_tag_"):
                tag_name = d[13:]
                chosen_tag = "" if tag_name == "NOTAG" else tag_name
                links = [l for l in get_step_data(uid).splitlines() if l.strip()]
                tag_lbl = f"«{chosen_tag}»" if chosen_tag else "بدون برچسب"
                # ذخیره برچسب در step_data با جداکننده
                set_step(uid, "g_join_links",
                         f"TAG:{chosen_tag}\n" + "\n".join(links))
                await cb.message.edit_text(
                    f"✅ {len(links)} لینک — 🏷 {tag_lbl}\nنوع عضویت را انتخاب کنید:",
                    reply_markup=global_join_kb()
                )

            elif d.startswith("m_join_"):
                acc_id = d[7:]
                row = q("SELECT min_delay,max_delay FROM join_settings WHERE account_id=%s", (acc_id,))
                mn, mx = (row[0][0]//60, row[0][1]//60) if row else (3, 7)
                set_step(uid, f"join_{acc_id}")
                await cb.message.edit_text(
                    f"➕ **عضو شدن در لینک گروه‌ها**\n\n⏱ فاصله: {mn}–{mx} دقیقه\n\n"
                    f"لینک‌ها را هر کدام در یک خط وارد کنید:",
                    reply_markup=back_kb(f"acc_manage_{acc_id}")
                )

            # ══ عضویت اجبار ══
            elif d.startswith("fj_tog_"):
                acc_id = d[7:]
                row = q("SELECT force_join_active FROM join_settings WHERE account_id=%s", (acc_id,))
                new = 0 if (row[0][0] if row else 0) else 1
                u("INSERT INTO join_settings (account_id,admin_id,force_join_active) "
                  "VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE force_join_active=%s", (acc_id, uid, new, new))
                await cb.answer(f"عضویت اجبار {'فعال' if new else 'غیرفعال'} شد", show_alert=True)

            elif d.startswith("m_fj_"):
                acc_id = d[5:]
                row = q("SELECT force_join_active FROM join_settings WHERE account_id=%s", (acc_id,))
                fj = row[0][0] if row else 0
                await cb.message.edit_text(
                    f"🕵️ **عضویت اجباری**\n\nبا فعال بودن، هنگام ارسال پیام اگر با عضویت اجبار مواجه شد "
                    f"خودکار عضو کانال می‌شود و پیام را ارسال می‌کند.\n\n"
                    f"وضعیت: {'✅ فعال' if fj else '❌ غیرفعال'}",
                    reply_markup=confirm_kb(f"fj_tog_{acc_id}", f"acc_manage_{acc_id}")
                )

            # ══ ارسال پیام ══
            elif d.startswith("spv_go_"):
                acc_id = d[7:]
                text = get_step_data(uid)
                set_stop(False)
                await cb.message.edit_text("⏳ در حال ارسال به پیوی‌ها...")
                t = asyncio.create_task(_send_to_pvs(client, acc_id, text))
                t.set_name("send_pv_task")
                clear_step(uid)

            elif d.startswith("m_spv_"):
                acc_id = d[6:]
                set_step(uid, f"spv_{acc_id}")
                await cb.message.edit_text("💬 متن پیام برای پیوی‌ها:", reply_markup=back_kb(f"acc_manage_{acc_id}"))

            elif d.startswith("sgrp_go_"):
                acc_id = d[8:]
                msg_text = get_step_data(uid)
                # ذخیره متن + acc_id برای مرحله بعد
                set_step(uid, f"sgrp_gtag_{acc_id}", msg_text)
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text(
                    "📢 ارسال به کدوم گروه‌ها؟",
                    reply_markup=tag_select_kb(tag_list, f"sgrpgtag_{acc_id}")
                )

            # ── انتخاب برچسب گروه برای ارسال تک‌اکانت ──
            elif d.startswith("sgrpgtag_"):
                rest = d[9:]  # {acc_id}_tag_{tagname}
                acc_id, _, tag_name = rest.partition("_tag_")
                msg_text = get_step_data(uid)
                group_tag = "" if tag_name == "ALL" else ("" if tag_name == "NOTAG" else tag_name)
                group_tag_filter = tag_name  # ALL / NOTAG / tagname
                set_stop(False)
                await cb.message.edit_text("⏳ در حال ارسال به گروه‌ها...")
                t = asyncio.create_task(_send_to_groups_task(client, acc_id, msg_text,
                                                              group_tag_filter=group_tag_filter))
                t.set_name("send_grp_task")
                clear_step(uid)

            elif d.startswith("m_sgrp_"):
                acc_id = d[7:]
                set_step(uid, f"sgrp_{acc_id}")
                await cb.message.edit_text("📢 متن پیام برای گروه‌ها:", reply_markup=back_kb(f"acc_manage_{acc_id}"))

            # ══ خروج از همه ══
            elif d.startswith("leave_yes_"):
                acc_id = d[10:]
                uc = await get_user_client(acc_id)
                if not uc:
                    await cb.answer("❌ در دسترس نیست", show_alert=True); return
                from pyrogram import enums as en
                count = 0
                await uc.start()
                async for dlg in uc.get_dialogs():
                    if dlg.chat.type in (en.ChatType.GROUP, en.ChatType.SUPERGROUP, en.ChatType.CHANNEL):
                        try:
                            await uc.leave_chat(dlg.chat.id); count += 1; await asyncio.sleep(0.5)
                        except Exception: pass
                await uc.stop()
                await cb.message.edit_text(f"✅ از {count} گروه/کانال خارج شد.",
                                            reply_markup=back_kb(f"acc_manage_{acc_id}"))

            elif d.startswith("m_leave_"):
                acc_id = d[8:]
                await cb.message.edit_text("⚠️ خروج از **تمام گروه‌ها و کانال‌ها**؟",
                                            reply_markup=confirm_kb(f"leave_yes_{acc_id}", f"acc_manage_{acc_id}"))

            # ══ پروفایل ══
            elif d.startswith("m_bio_"):
                set_step(uid, f"set_bio_{d[6:]}"); await cb.message.edit_text("📝 بیو جدید:", reply_markup=back_kb(f"acc_manage_{d[6:]}"))
            elif d.startswith("m_uname_"):
                set_step(uid, f"set_uname_{d[8:]}"); await cb.message.edit_text("🆔 نام کاربری جدید:", reply_markup=back_kb(f"acc_manage_{d[8:]}"))
            elif d.startswith("m_fname_"):
                set_step(uid, f"set_fname_{d[8:]}"); await cb.message.edit_text("👤 نام جدید:", reply_markup=back_kb(f"acc_manage_{d[8:]}"))
            elif d.startswith("m_lname_"):
                set_step(uid, f"set_lname_{d[8:]}"); await cb.message.edit_text("👤 فامیلی جدید:", reply_markup=back_kb(f"acc_manage_{d[8:]}"))

            # ══ ریپلای رندم ══
            elif d.startswith("m_reply_"):
                acc_id = d[8:]
                row = q("SELECT is_active,interval_minutes,message_text FROM reply_rand WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0; interval = row[0][1] if row else 30; msg = (row[0][2] if row else "") or "تنظیم نشده"
                await cb.message.edit_text(
                    f"↩️ **پنل ریپلای رندم**\n\nپیام: {msg}\nفاصله: {interval} دقیقه\nوضعیت: {'✅' if active else '❌'}",
                    reply_markup=reply_rand_kb(acc_id, active)
                )

            elif d.startswith("rr_banners_"):
                acc_id = d[11:]
                bnrs = q("SELECT slot,text,file_id FROM reply_rand_banners "
                         "WHERE account_id=%s ORDER BY slot", (acc_id,))
                back = "g_rr" if acc_id.startswith("global") else f"m_reply_{acc_id}"
                txt = "📋 **مدیریت متن‌های ریپلای**\n\n"
                if bnrs:
                    for b in bnrs:
                        short = (b[1] or "")[:40]
                        txt += f"═-═ {b[0]} ═-═\n💬 {short}{'...' if b[1] and len(b[1])>40 else ''}\n📁 {'✅' if b[2] else '❌'}\n\n"
                else:
                    txt += "هیچ متنی تنظیم نشده."
                txt += "\nهر دوره یکی به ترتیب شماره ارسال می‌شود."
                await cb.message.edit_text(txt, reply_markup=reply_banner_list_kb(acc_id, bnrs, back_to=back))

            elif d.startswith("rr_badd_"):
                acc_id = d[8:]
                set_step(uid, f"rr_badd_{acc_id}")
                back = "g_rr" if acc_id.startswith("global") else f"m_reply_{acc_id}"
                await cb.message.edit_text("📝 متن ریپلای جدید را وارد کنید:",
                                            reply_markup=back_kb(f"rr_banners_{acc_id}"))

            elif d.startswith("rr_bdel_"):
                parts = d.split("_", 3)
                acc_id, slot = parts[2], int(parts[3])
                u("DELETE FROM reply_rand_banners WHERE account_id=%s AND slot=%s", (acc_id, slot))
                # شماره‌گذاری مجدد
                remaining = q("SELECT id FROM reply_rand_banners WHERE account_id=%s ORDER BY slot", (acc_id,))
                for i, (rid,) in enumerate(remaining, 1):
                    u("UPDATE reply_rand_banners SET slot=%s WHERE id=%s", (i, rid))
                await cb.answer(f"✅ متن {slot} حذف شد")
                bnrs = q("SELECT slot,text,file_id FROM reply_rand_banners WHERE account_id=%s ORDER BY slot", (acc_id,))
                back = "g_rr" if acc_id.startswith("global") else f"m_reply_{acc_id}"
                await cb.message.edit_reply_markup(reply_banner_list_kb(acc_id, bnrs, back_to=back))

            elif d.startswith("rr_bdelall_"):
                acc_id = d[11:]
                u("DELETE FROM reply_rand_banners WHERE account_id=%s", (acc_id,))
                await cb.answer("✅ همه متن‌ها حذف شدند")
                back = "g_rr" if acc_id.startswith("global") else f"m_reply_{acc_id}"
                await cb.message.edit_reply_markup(reply_banner_list_kb(acc_id, [], back_to=back))

            elif d.startswith("rr_gtag_"):
                acc_id = d[8:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text(
                    "🏷 فیلتر گروه‌ها برای ریپلای:",
                    reply_markup=tag_select_kb(tag_list, f"rr_gtag_set_{acc_id}")
                )

            elif d.startswith("rr_gtag_set_"):
                rest = d[12:]  # {acc_id}_tag_{tagname}
                acc_id, _, tag_name = rest.partition("_tag_")
                u("INSERT INTO reply_rand (account_id,admin_id,group_tag_filter) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE group_tag_filter=%s", (acc_id, uid, tag_name, tag_name))
                await cb.answer(f"✅ فیلتر گروه: {tag_name}")
                row = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM reply_rand WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0
                gtag = row[0][1] if row else "ALL"
                atag = row[0][2] if row else "ALL"
                back = "menu_global" if acc_id.startswith("global") else None
                await cb.message.edit_reply_markup(reply_rand_kb(acc_id, active, back_to=back, group_tag=gtag, acc_tag=atag))

            elif d.startswith("rr_atag_"):
                acc_id = d[8:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text(
                    "👤 فیلتر اکانت‌ها برای ریپلای:",
                    reply_markup=tag_select_kb(tag_list, f"rr_atag_set_{acc_id}")
                )

            elif d.startswith("rr_atag_set_"):
                rest = d[12:]
                acc_id, _, tag_name = rest.partition("_tag_")
                u("INSERT INTO reply_rand (account_id,admin_id,acc_tag_filter) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE acc_tag_filter=%s", (acc_id, uid, tag_name, tag_name))
                await cb.answer(f"✅ فیلتر اکانت: {tag_name}")
                row = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM reply_rand WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0
                gtag = row[0][1] if row else "ALL"
                atag = row[0][2] if row else "ALL"
                back = "menu_global" if acc_id.startswith("global") else None
                await cb.message.edit_reply_markup(reply_rand_kb(acc_id, active, back_to=back, group_tag=gtag, acc_tag=atag))

            elif d.startswith("rc_gtag_"):
                acc_id = d[8:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text(
                    "🏷 فیلتر گروه‌ها برای ری‌اکت:",
                    reply_markup=tag_select_kb(tag_list, f"rc_gtag_set_{acc_id}")
                )

            elif d.startswith("rc_gtag_set_"):
                rest = d[12:]
                acc_id, _, tag_name = rest.partition("_tag_")
                u("INSERT INTO react_rand (account_id,admin_id,group_tag_filter) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE group_tag_filter=%s", (acc_id, uid, tag_name, tag_name))
                await cb.answer(f"✅ فیلتر گروه: {tag_name}")
                row = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM react_rand WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0
                gtag = row[0][1] if row else "ALL"
                atag = row[0][2] if row else "ALL"
                back = "menu_global" if acc_id.startswith("global") else None
                await cb.message.edit_reply_markup(react_rand_kb(acc_id, active, back_to=back, group_tag=gtag, acc_tag=atag))

            elif d.startswith("rc_atag_"):
                acc_id = d[8:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text(
                    "👤 فیلتر اکانت‌ها برای ری‌اکت:",
                    reply_markup=tag_select_kb(tag_list, f"rc_atag_set_{acc_id}")
                )

            elif d.startswith("rc_atag_set_"):
                rest = d[12:]
                acc_id, _, tag_name = rest.partition("_tag_")
                u("INSERT INTO react_rand (account_id,admin_id,acc_tag_filter) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE acc_tag_filter=%s", (acc_id, uid, tag_name, tag_name))
                await cb.answer(f"✅ فیلتر اکانت: {tag_name}")
                row = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM react_rand WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0
                gtag = row[0][1] if row else "ALL"
                atag = row[0][2] if row else "ALL"
                back = "menu_global" if acc_id.startswith("global") else None
                await cb.message.edit_reply_markup(react_rand_kb(acc_id, active, back_to=back, group_tag=gtag, acc_tag=atag))

            elif d.startswith("rr_time_"):
                acc_id = d[8:]
                set_step(uid, f"rr_int_{acc_id}")
                await cb.message.edit_text("⏱ فاصله ریپلای (دقیقه):")

            elif d.startswith("rr_tog_"):
                acc_id = d[7:]
                row = q("SELECT is_active FROM reply_rand WHERE account_id=%s", (acc_id,))
                new = 0 if (row[0][0] if row else 0) else 1
                u("INSERT INTO reply_rand (account_id,admin_id,is_active) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE is_active=%s", (acc_id, uid, new, new))
                if new:
                    set_stop(False); rw.STOP_FLAG = False
                await cb.answer(f"ریپلای {'فعال' if new else 'غیرفعال'} شد")
                row2 = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM reply_rand WHERE account_id=%s", (acc_id,))
                active = row2[0][0] if row2 else 0
                gtag = row2[0][1] if row2 else "ALL"
                atag = row2[0][2] if row2 else "ALL"
                back = "menu_global" if acc_id.startswith("global") else None
                await cb.message.edit_reply_markup(reply_rand_kb(acc_id, active, back_to=back, group_tag=gtag, acc_tag=atag))

            elif d.startswith("rr_run_"):
                acc_id = d[7:]
                bnrs = q("SELECT text FROM reply_rand_banners WHERE account_id=%s ORDER BY slot", (acc_id,))
                if not bnrs:
                    await cb.answer("❌ اول متن تنظیم کنید", show_alert=True); return
                set_stop(False); rw.STOP_FLAG = False
                row_cfg = q("SELECT last_index, group_tag_filter, acc_tag_filter "
                            "FROM reply_rand WHERE account_id=%s", (acc_id,))
                gtag = (row_cfg[0][1] if row_cfg else None) or "ALL"
                atag = (row_cfg[0][2] if row_cfg else None) or "ALL"
                if acc_id.startswith("global"):
                    lyr_id = int(acc_id[6:]) if acc_id[6:].isdigit() else get_current_layer(uid)
                    # فیلتر اکانت‌ها (فقط همین لایه)
                    if atag not in ("ALL", ""):
                        if atag == "NOTAG":
                            accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' "
                                     "AND layer_id=%s AND (tag='' OR tag IS NULL)", (uid, lyr_id))
                        else:
                            accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' "
                                     "AND layer_id=%s AND tag=%s", (uid, lyr_id, atag))
                    else:
                        accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                                 (uid, lyr_id))
                    idx = (row_cfg[0][0] if row_cfg else 0) % len(bnrs)
                    msg_text = bnrs[idx][0]
                    for (aid,) in accs:
                        asyncio.create_task(rw.run_once(uid, aid, msg_text, group_tag_filter=gtag))
                    u("INSERT INTO reply_rand (account_id,admin_id,last_index) VALUES(%s,%s,%s) "
                      "ON DUPLICATE KEY UPDATE last_index=%s", (acc_id, uid, idx+1, idx+1))
                    await cb.answer(f"🚀 برای {len(accs)} اکانت شروع شد", show_alert=True)
                else:
                    idx = (row_cfg[0][0] if row_cfg else 0) % len(bnrs)
                    msg_text = bnrs[idx][0]
                    u("INSERT INTO reply_rand (account_id,admin_id,last_index) VALUES(%s,%s,%s) "
                      "ON DUPLICATE KEY UPDATE last_index=%s", (acc_id, uid, idx+1, idx+1))
                    await cb.answer("🚀 شروع شد")
                    asyncio.create_task(rw.run_once(uid, acc_id, msg_text, group_tag_filter=gtag))

            # ══ ری‌اکت رندم ══
            elif d.startswith("m_react_"):
                acc_id = d[8:]
                row = q("SELECT is_active,interval_minutes FROM react_rand WHERE account_id=%s", (acc_id,))
                active = row[0][0] if row else 0; interval = row[0][1] if row else 30
                await cb.message.edit_text(
                    f"😀 **پنل ری‌اکت رندم**\n\nفاصله: {interval} دقیقه\nوضعیت: {'✅' if active else '❌'}",
                    reply_markup=react_rand_kb(acc_id, active)
                )

            elif d.startswith("rc_time_"):
                acc_id = d[8:]
                set_step(uid, f"rc_int_{acc_id}")
                await cb.message.edit_text("⏱ فاصله ری‌اکت (دقیقه):")

            elif d.startswith("rc_tog_"):
                acc_id = d[7:]
                row = q("SELECT is_active FROM react_rand WHERE account_id=%s", (acc_id,))
                new = 0 if (row[0][0] if row else 0) else 1
                u("INSERT INTO react_rand (account_id,admin_id,is_active) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE is_active=%s", (acc_id, uid, new, new))
                if new:
                    set_stop(False); rcw.STOP_FLAG = False
                await cb.answer(f"ری‌اکت {'فعال' if new else 'غیرفعال'} شد")
                row2 = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM react_rand WHERE account_id=%s", (acc_id,))
                active = row2[0][0] if row2 else 0
                gtag = row2[0][1] if row2 else "ALL"
                atag = row2[0][2] if row2 else "ALL"
                back = "menu_global" if acc_id.startswith("global") else None
                await cb.message.edit_reply_markup(react_rand_kb(acc_id, active, back_to=back, group_tag=gtag, acc_tag=atag))

            elif d.startswith("rc_run_"):
                acc_id = d[7:]
                set_stop(False); rcw.STOP_FLAG = False
                row_cfg = q("SELECT group_tag_filter, acc_tag_filter FROM react_rand WHERE account_id=%s", (acc_id,))
                gtag = (row_cfg[0][0] if row_cfg else None) or "ALL"
                atag = (row_cfg[0][1] if row_cfg else None) or "ALL"
                if acc_id.startswith("global"):
                    lyr_id = int(acc_id[6:]) if acc_id[6:].isdigit() else get_current_layer(uid)
                    if atag not in ("ALL", ""):
                        if atag == "NOTAG":
                            accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' "
                                     "AND layer_id=%s AND (tag='' OR tag IS NULL)", (uid, lyr_id))
                        else:
                            accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' "
                                     "AND layer_id=%s AND tag=%s", (uid, lyr_id, atag))
                    else:
                        accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                                 (uid, lyr_id))
                    for (aid,) in accs:
                        asyncio.create_task(rcw.run_once(uid, aid, group_tag_filter=gtag))
                    await cb.answer(f"🚀 برای {len(accs)} اکانت شروع شد", show_alert=True)
                else:
                    await cb.answer("🚀 شروع شد")
                    asyncio.create_task(rcw.run_once(uid, acc_id, group_tag_filter=gtag))

            # ══ global ══
            elif d == "g_stats":
                layer_id = get_current_layer(uid)
                accs = q("SELECT id,name,phone FROM accounts WHERE admin_id=%s AND layer_id=%s",
                         (uid, layer_id))
                total_g = total_p = 0
                txt = f"📊 **آمار ({len(accs)} اکانت)**\n\n"
                for a in accs:
                    uc = await get_user_client(a[0]); grps = pvs = 0
                    err = None
                    try:
                        from pyrogram import enums as en
                        await uc.start()
                        me = await uc.get_me()
                        if me is None:
                            raise RuntimeError("get_me() → None")
                        async for dlg in uc.get_dialogs():
                            if dlg.chat is None:
                                continue
                            if dlg.chat.type in (en.ChatType.GROUP, en.ChatType.SUPERGROUP): grps += 1
                            elif dlg.chat.type == en.ChatType.PRIVATE: pvs += 1
                        await uc.stop()
                    except Exception as e:
                        err = f"{type(e).__name__}: {str(e)[:100]}"
                        print(f"[g_stats ERROR] acc_id={a[0]}\n{err}")
                        try:
                            await uc.stop()
                        except Exception:
                            pass
                    total_g += grps; total_p += pvs
                    if err:
                        txt += f"👤 {a[1]} | `{a[2]}`\n⚠️ خطا: {err}\n\n"
                    else:
                        txt += f"👤 {a[1]} | `{a[2]}`\n🗣 {grps} گروه | 🪡 {pvs} پیوی\n\n"
                txt += f"─────\n🗣 کل گروه‌ها: {total_g}\n🪡 کل پیوی‌ها: {total_p}"
                await cb.message.edit_text(txt, reply_markup=back_kb("menu_global"))

            elif d == "g_status":
                layer_id = get_current_layer(uid)
                accs = q("SELECT id,name,phone FROM accounts WHERE admin_id=%s AND layer_id=%s",
                         (uid, layer_id))
                txt = "♻️ **وضعیت:**\n\n"
                for a in accs:
                    uc = await get_user_client(a[0])
                    try:
                        await uc.start(); await uc.get_me(); await uc.stop()
                        txt += f"✅ {a[1]} | `{a[2]}`\n"
                    except Exception:
                        txt += f"❌ {a[1]} | `{a[2]}`\n"
                await cb.message.edit_text(txt, reply_markup=back_kb("menu_global"))

            elif d == "g_bio":
                set_step(uid, "g_bio"); await cb.message.edit_text("📝 بیو جدید برای همه:", reply_markup=back_kb("menu_global"))
            elif d == "g_fname":
                set_step(uid, "g_fname"); await cb.message.edit_text("👤 نام جدید برای همه:", reply_markup=back_kb("menu_global"))
            elif d == "g_lname":
                set_step(uid, "g_lname"); await cb.message.edit_text("👤 فامیلی جدید برای همه:", reply_markup=back_kb("menu_global"))

            elif d == "g_spv":
                set_step(uid, "g_spv"); await cb.message.edit_text("💬 متن پیام برای پیوی‌های همه:", reply_markup=back_kb("menu_global"))

            elif d == "g_spv_go":
                text = get_step_data(uid)
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s", (uid, layer_id))
                set_stop(False)
                for (aid,) in accs:
                    t = asyncio.create_task(_send_to_pvs(client, aid, text)); t.set_name("send_pv_task")
                await cb.message.edit_text("✅ شروع شد.", reply_markup=global_kb()); clear_step(uid)

            elif d == "g_sgrp":
                set_step(uid, "g_sgrp"); await cb.message.edit_text("📢 متن پیام برای گروه‌های همه:", reply_markup=back_kb("menu_global"))

            elif d == "g_sgrp_go":
                msg_text = get_step_data(uid)
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name",
                         (uid, layer_id))
                tag_list = [t[0] for t in tags]
                if not tag_list:
                    # هیچ برچسبی تعریف نشده - مستقیم با همه شروع کن
                    accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                             (uid, layer_id))
                    set_stop(False)
                    await cb.message.edit_text(f"⏳ ارسال به {len(accs)} اکانت شروع شد...")
                    for (aid,) in accs:
                        t = asyncio.create_task(_send_to_groups_task(client, aid, msg_text,
                                                                       group_tag_filter="ALL"))
                        t.set_name("send_grp_task")
                    clear_step(uid)
                    return
                set_step(uid, "g_sgrp_atag", msg_text)
                await cb.message.edit_text(
                    "👤 ارسال با کدوم اکانت‌ها؟",
                    reply_markup=tag_select_kb(tag_list, "gsgrpatag")
                )

            # ── انتخاب برچسب اکانت برای ارسال همگانی ──
            elif d.startswith("gsgrpatag_tag_"):
                acc_tag = d[14:]
                msg_text = get_step_data(uid)
                # ذخیره acc_tag در step_data برای مرحله بعد
                set_step(uid, f"g_sgrp_gtag_{acc_tag}", msg_text)
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text(
                    "👥 ارسال به کدوم گروه‌ها؟",
                    reply_markup=tag_select_kb(tag_list, f"gsgrpgtag_{acc_tag}")
                )

            # ── انتخاب برچسب گروه برای ارسال همگانی ──
            elif d.startswith("gsgrpgtag_"):
                rest = d[10:]  # {acc_tag}_tag_{group_tag}
                acc_tag, _, group_tag_filter = rest.partition("_tag_")
                msg_text = get_step_data(uid)
                from handlers.text_handler import get_filtered_accounts
                accs = get_filtered_accounts(acc_tag, uid)
                set_stop(False)
                await cb.message.edit_text(f"⏳ ارسال به {len(accs)} اکانت شروع شد...")
                for (aid,) in accs:
                    t = asyncio.create_task(_send_to_groups_task(client, aid, msg_text,
                                                                   group_tag_filter=group_tag_filter))
                    t.set_name("send_grp_task")
                clear_step(uid)

            elif d == "g_join":
                set_step(uid, "g_join"); await cb.message.edit_text("➕ لینک‌ها را هر کدام در یک خط:", reply_markup=back_kb("menu_global"))

            elif d == "g_join_split":
                raw = get_step_data(uid)
                lines = raw.splitlines()
                chosen_tag = ""
                if lines and lines[0].startswith("TAG:"):
                    chosen_tag = lines[0][4:]
                    links = [l for l in lines[1:] if l.strip()]
                else:
                    links = [l for l in lines if l.strip()]
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s", (uid, layer_id))
                if not accs:
                    await cb.answer("اکانتی وجود ندارد", show_alert=True); return
                # تقسیم عادلانه — لینک‌های باقی‌مانده (باقیمانده تقسیم) هم بین اکانت‌های اول پخش می‌شن
                n_accs = len(accs)
                base = len(links) // n_accs
                extra = len(links) % n_accs
                set_stop(False)
                tag_lbl = f"«{chosen_tag}»" if chosen_tag else "بدون برچسب"
                await cb.message.edit_text(
                    f"🔀 {len(links)} لینک بین {len(accs)} اکانت تقسیم شد.\n🏷 برچسب: {tag_lbl}"
                )
                idx = 0
                for i, (aid,) in enumerate(accs):
                    size = base + (1 if i < extra else 0)
                    chunk = links[idx:idx+size]
                    idx += size
                    if chunk:
                        row = q("SELECT min_delay,max_delay FROM join_settings WHERE account_id=%s", (aid,))
                        mn, mx = (row[0][0], row[0][1]) if row else (180, 420)
                        from handlers.text_handler import _join_links
                        t = asyncio.create_task(_join_links(client, aid, chunk, mn, mx,
                                                             tag=chosen_tag))
                        t.set_name("join_task")
                clear_step(uid)

            elif d == "g_join_all":
                raw = get_step_data(uid)
                lines = raw.splitlines()
                chosen_tag = ""
                if lines and lines[0].startswith("TAG:"):
                    chosen_tag = lines[0][4:]
                    links = [l for l in lines[1:] if l.strip()]
                else:
                    links = [l for l in lines if l.strip()]
                layer_id = get_current_layer(uid)
                accs = q("SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s", (uid, layer_id))
                set_stop(False)
                tag_lbl = f"«{chosen_tag}»" if chosen_tag else "بدون برچسب"
                await cb.message.edit_text(
                    f"📋 همه اکانت‌ها {len(links)} لینک — 🏷 {tag_lbl}"
                )
                for (aid,) in accs:
                    row = q("SELECT min_delay,max_delay FROM join_settings WHERE account_id=%s", (aid,))
                    mn, mx = (row[0][0], row[0][1]) if row else (180, 420)
                    from handlers.text_handler import _join_links
                    t = asyncio.create_task(_join_links(client, aid, links, mn, mx,
                                                         tag=chosen_tag))
                    t.set_name("join_task")
                clear_step(uid)

            elif d == "g_fj":
                layer_id = get_current_layer(uid)
                row = q("SELECT force_join_active FROM join_settings WHERE admin_id=%s "
                        "AND account_id IN (SELECT id FROM accounts WHERE admin_id=%s AND layer_id=%s) LIMIT 1",
                        (uid, uid, layer_id))
                active = row[0][0] if row else 0
                await cb.message.edit_text(
                    f"🕵️ **عضویت اجبار (همگانی)**\n\n"
                    f"وضعیت: {'✅ فعال' if active else '❌ غیرفعال'}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"{'🔴 غیرفعال' if active else '🟢 فعال'} برای همه",
                            callback_data="g_fj_tog")],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_global")]
                    ])
                )

            elif d == "g_sch_menu":
                await cb.message.edit_text(
                    "⏰ **ارسال زمان‌دار همگانی**\n\nنوع ارسال را انتخاب کنید:",
                    reply_markup=global_sch_menu_kb()
                )

            elif d.startswith("gsch_panel_"):
                target = d[11:]
                layer_id = get_current_layer(uid)
                row = q("SELECT is_active, interval_minutes, group_tag_filter, acc_tag_filter, "
                        "max_rounds, current_round FROM global_scheduler "
                        "WHERE admin_id=%s AND target=%s AND layer_id=%s", (uid, target, layer_id))
                active = row[0][0] if row else 0
                interval = row[0][1] if row else 60
                gtag = (row[0][2] if row else None) or "ALL"
                atag = (row[0][3] if row else None) or "ALL"
                max_rounds = row[0][4] if row else 0
                current_round = row[0][5] if row else 0
                title = "📢 گروه‌ها" if target == "groups" else "💬 پیوی‌ها"
                bnrs = q("SELECT slot, text, file_id FROM global_banners "
                         "WHERE admin_id=%s AND target=%s AND layer_id=%s ORDER BY slot", (uid, target, layer_id))
                txt = f"⏰ **ارسال زمان‌دار به {title}**\n\n"
                txt += f"فاصله: هر {interval} دقیقه\nوضعیت: {'✅ فعال' if active else '❌ غیرفعال'}\n"
                txt += f"🏷 گروه: {gtag} | 👤 اکانت: {atag}\n"
                if max_rounds == 0:
                    txt += "🔄 دور: نامحدود\n\n"
                else:
                    txt += f"🔄 دور: {current_round}/{max_rounds}\n\n"
                if bnrs:
                    for i, b in enumerate(bnrs, 1):
                        short = (b[1] or "")[:30]
                        txt += f"💬 پیام {i}: [{short}{'...' if b[1] and len(b[1])>30 else ''}] {'📁' if b[2] else ''}\n"
                else:
                    txt += "هیچ پیامی تنظیم نشده.\n"
                txt += "\nهر دوره یکی از این پیام‌ها به‌ترتیب ارسال می‌شود."
                await cb.message.edit_text(txt, reply_markup=global_sch_panel_kb(
                    target, active, gtag=gtag, atag=atag,
                    max_rounds=max_rounds, current_round=current_round,
                    slots=[b[0] for b in bnrs]))

            elif d.startswith("gsch_rounds_"):
                target = d[12:]
                set_step(uid, f"gsch_rounds_{target}")
                await cb.message.edit_text(
                    "🔄 تعداد دور را وارد کنید:\n\n"
                    "`0` = نامحدود\n`1` = یک دور (یه بار همه‌ی پیام‌ها)\n`2` = دو دور\n...",
                    reply_markup=back_kb(f"gsch_panel_{target}")
                )

            elif d.startswith("gsch_gtag_set_"):
                rest = d[14:]
                target, _, tag_name = rest.partition("_tag_")
                layer_id = get_current_layer(uid)
                u("INSERT INTO global_scheduler (admin_id,target,layer_id,group_tag_filter) VALUES(%s,%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE group_tag_filter=%s", (uid, target, layer_id, tag_name, tag_name))
                await cb.answer(f"✅ فیلتر گروه: {tag_name}")
                row = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM global_scheduler "
                        "WHERE admin_id=%s AND target=%s AND layer_id=%s", (uid, target, layer_id))
                active = row[0][0] if row else 0
                gtag = row[0][1] if row else "ALL"
                atag = row[0][2] if row else "ALL"
                slots = [b[0] for b in q("SELECT slot FROM global_banners WHERE admin_id=%s AND target=%s AND layer_id=%s ORDER BY slot", (uid, target, layer_id))]
                await cb.message.edit_reply_markup(global_sch_panel_kb(target, active, gtag=gtag, atag=atag, slots=slots))

            elif d.startswith("gsch_gtag_"):
                target = d[10:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text("🏷 فیلتر گروه‌ها برای زمان‌بند:",
                    reply_markup=tag_select_kb(tag_list, f"gsch_gtag_set_{target}"))

            elif d.startswith("gsch_atag_set_"):
                rest = d[14:]
                target, _, tag_name = rest.partition("_tag_")
                layer_id = get_current_layer(uid)
                u("INSERT INTO global_scheduler (admin_id,target,layer_id,acc_tag_filter) VALUES(%s,%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE acc_tag_filter=%s", (uid, target, layer_id, tag_name, tag_name))
                await cb.answer(f"✅ فیلتر اکانت: {tag_name}")
                row = q("SELECT is_active,group_tag_filter,acc_tag_filter FROM global_scheduler "
                        "WHERE admin_id=%s AND target=%s AND layer_id=%s", (uid, target, layer_id))
                active = row[0][0] if row else 0
                gtag = row[0][1] if row else "ALL"
                atag = row[0][2] if row else "ALL"
                slots = [b[0] for b in q("SELECT slot FROM global_banners WHERE admin_id=%s AND target=%s AND layer_id=%s ORDER BY slot", (uid, target, layer_id))]
                await cb.message.edit_reply_markup(global_sch_panel_kb(target, active, gtag=gtag, atag=atag, slots=slots))

            elif d.startswith("gsch_atag_"):
                target = d[10:]
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_text("👤 فیلتر اکانت‌ها برای زمان‌بند:",
                    reply_markup=tag_select_kb(tag_list, f"gsch_atag_set_{target}"))

            elif d.startswith("gsch_time_"):
                target = d[10:]
                set_step(uid, f"gsch_int_{target}")
                await cb.message.edit_text(
                    "⏱ فاصله ارسال (دقیقه) را وارد کنید:\nمثال: `60`",
                    reply_markup=back_kb(f"gsch_panel_{target}")
                )

            elif d.startswith("gsch_tog_"):
                target = d[9:]
                layer_id = get_current_layer(uid)
                row = q("SELECT is_active FROM global_scheduler WHERE admin_id=%s AND target=%s AND layer_id=%s",
                        (uid, target, layer_id))
                new = 0 if (row[0][0] if row else 0) else 1
                if new:
                    # روشن کردن — reset دور
                    u("INSERT INTO global_scheduler (admin_id,target,layer_id,is_active,current_round) "
                      "VALUES(%s,%s,%s,%s,0) ON DUPLICATE KEY UPDATE is_active=%s, current_round=0",
                      (uid, target, layer_id, new, new))
                    set_stop(False)
                else:
                    u("INSERT INTO global_scheduler (admin_id,target,layer_id,is_active) VALUES(%s,%s,%s,%s) "
                      "ON DUPLICATE KEY UPDATE is_active=%s", (uid, target, layer_id, new, new))
                await cb.answer(f"ارسال زمان‌دار {'فعال' if new else 'غیرفعال'} شد")
                row2 = q("SELECT is_active,group_tag_filter,acc_tag_filter,max_rounds,current_round "
                         "FROM global_scheduler WHERE admin_id=%s AND target=%s AND layer_id=%s", (uid, target, layer_id))
                active = row2[0][0] if row2 else 0
                gtag = (row2[0][1] if row2 else None) or "ALL"
                atag = (row2[0][2] if row2 else None) or "ALL"
                max_r = row2[0][3] if row2 else 0
                cur_r = row2[0][4] if row2 else 0
                slots = [b[0] for b in q("SELECT slot FROM global_banners WHERE admin_id=%s AND target=%s AND layer_id=%s ORDER BY slot", (uid, target, layer_id))]
                await cb.message.edit_reply_markup(global_sch_panel_kb(
                    target, active, gtag=gtag, atag=atag, max_rounds=max_r, current_round=cur_r, slots=slots))

            elif d.startswith("gsch_bv_"):
                rest = d[8:]
                target, _, slot = rest.rpartition("_")
                slot = int(slot)
                await cb.message.edit_text(
                    f"✏️ مدیریت پیام",
                    reply_markup=global_banner_slot_kb(target, slot)
                )

            elif d.startswith("gsch_badd_"):
                target = d[10:]
                layer_id = get_current_layer(uid)
                existing = q("SELECT MAX(slot) FROM global_banners WHERE admin_id=%s AND target=%s AND layer_id=%s",
                             (uid, target, layer_id))
                next_slot = (existing[0][0] or 0) + 1
                set_step(uid, f"gbn_text_{target}_{next_slot}")
                await cb.message.edit_text("📝 متن پیام جدید را وارد کنید:")

            elif d.startswith("gbn_add_"):
                _, _, target, slot = d.split("_", 3)
                slot = int(slot)
                set_step(uid, f"gbn_text_{target}_{slot}")
                await cb.message.edit_text("📝 متن پیام را وارد کنید:")

            elif d.startswith("gbn_del_"):
                _, _, target, slot = d.split("_", 3)
                slot = int(slot)
                layer_id = get_current_layer(uid)
                u("DELETE FROM global_banners WHERE admin_id=%s AND target=%s AND slot=%s AND layer_id=%s",
                  (uid, target, slot, layer_id))
                await cb.answer(f"✅ پیام {slot} حذف شد")
                await cb.message.edit_reply_markup(global_banner_slot_kb(target, slot))

            elif d.startswith("gbn_delall_"):
                target = d[11:]
                layer_id = get_current_layer(uid)
                u("DELETE FROM global_banners WHERE admin_id=%s AND target=%s AND layer_id=%s",
                  (uid, target, layer_id))
                await cb.answer("✅ همه پیام‌ها حذف شدند")

            elif d.startswith("gbn_back_"):
                target = d[9:]
                layer_id = get_current_layer(uid)
                row = q("SELECT is_active, interval_minutes, group_tag_filter, acc_tag_filter, "
                        "max_rounds, current_round FROM global_scheduler "
                        "WHERE admin_id=%s AND target=%s AND layer_id=%s", (uid, target, layer_id))
                active = row[0][0] if row else 0
                interval = row[0][1] if row else 60
                gtag = (row[0][2] if row else None) or "ALL"
                atag = (row[0][3] if row else None) or "ALL"
                max_r = row[0][4] if row else 0
                cur_r = row[0][5] if row else 0
                title = "📢 گروه‌ها" if target == "groups" else "💬 پیوی‌ها"
                bnrs = q("SELECT slot, text, file_id FROM global_banners "
                         "WHERE admin_id=%s AND target=%s AND layer_id=%s ORDER BY slot", (uid, target, layer_id))
                txt = f"⏰ **ارسال زمان‌دار به {title}**\n\n"
                txt += f"فاصله: هر {interval} دقیقه\nوضعیت: {'✅ فعال' if active else '❌ غیرفعال'}\n\n"
                if bnrs:
                    for i, b in enumerate(bnrs, 1):
                        short = (b[1] or "")[:30]
                        txt += f"💬 پیام {i}: [{short}{'...' if b[1] and len(b[1])>30 else ''}] {'📁' if b[2] else ''}\n"
                else:
                    txt += "هیچ پیامی تنظیم نشده.\n"
                txt += "\nهر دوره (هر X دقیقه) یکی از این پیام‌ها به‌ترتیب ارسال می‌شود."
                await cb.message.edit_text(txt, reply_markup=global_sch_panel_kb(
                    target, active, gtag=gtag, atag=atag, max_rounds=max_r, current_round=cur_r,
                    slots=[b[0] for b in bnrs]))

            elif d == "g_fwdgrp":
                set_step(uid, "g_fwdgrp"); await cb.message.edit_text("📤 لینک پیام:", reply_markup=back_kb("menu_global"))

            elif d == "g_autoleave":
                layer_id = get_current_layer(uid)
                row = q("SELECT auto_leave_limited FROM accounts WHERE admin_id=%s AND layer_id=%s LIMIT 1",
                        (uid, layer_id))
                active = row[0][0] if row else 0
                await cb.message.edit_text(
                    f"🚫 **خروج خودکار از گروه‌های محدود (همگانی)**\n\n"
                    f"وضعیت: {'✅ فعال' if active else '❌ غیرفعال'}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"{'🔴 غیرفعال' if active else '🟢 فعال'} برای همه",
                            callback_data="g_autoleave_tog")],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_global")]
                    ])
                )

            # ══ مدیریت برچسب‌ها ══
            elif d == "tags_menu":
                await cb.message.edit_text(
                    "🏷 **مدیریت برچسب‌ها**\n\nبرچسب‌ها برای دسته‌بندی گروه‌ها و اکانت‌ها استفاده می‌شوند.",
                    reply_markup=tags_menu_kb()
                )

            elif d == "tags_groups":
                layer_id = get_current_layer(uid)
                tags = q("SELECT DISTINCT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                txt = "👥 **برچسب گروه‌ها**\n\n"
                if tag_list:
                    txt += "برچسب‌های موجود:\n" + "\n".join(f"• {t}" for t in tag_list)
                else:
                    txt += "هیچ برچسبی ساخته نشده."
                await cb.message.edit_text(txt, reply_markup=tags_list_kb(tag_list, "groups"))

            elif d == "tags_accounts":
                layer_id = get_current_layer(uid)
                accs = q(
                    "SELECT a.id, a.name, a.phone, "
                    "GROUP_CONCAT(at.tag_name ORDER BY at.tag_name SEPARATOR ', ') "
                    "FROM accounts a "
                    "LEFT JOIN account_tags at ON at.account_id=a.id AND at.admin_id=a.admin_id "
                    "WHERE a.admin_id=%s AND a.layer_id=%s GROUP BY a.id, a.name, a.phone",
                    (uid, layer_id)
                )
                txt = "👤 **برچسب اکانت‌ها**\n\n"
                for a in accs:
                    tag_str = f"🏷 {a[3]}" if a[3] else "بدون برچسب"
                    txt += f"👤 {a[1]} | {a[2]} — {tag_str}\n"
                await cb.message.edit_text(txt, reply_markup=account_tag_kb(accs))

            elif d == "tags_accounts_manage":
                layer_id = get_current_layer(uid)
                tags = q("SELECT DISTINCT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                txt = "👤 **برچسب اکانت‌ها (مدیریت)**\n\n"
                if tag_list:
                    txt += "برچسب‌های موجود:\n" + "\n".join(f"• {t}" for t in tag_list)
                else:
                    txt += "هیچ برچسبی ساخته نشده."
                await cb.message.edit_text(txt, reply_markup=tags_list_kb(tag_list, "accounts"))

            elif d.startswith("tag_new_"):
                context = d[8:]
                set_step(uid, f"tag_new_{context}")
                await cb.message.edit_text(
                    "📝 نام برچسب جدید را وارد کنید:\nمثال: `تبلیغاتی`",
                    reply_markup=back_kb("tags_menu")
                )

            elif d.startswith("tag_del_"):
                _, _, context, tag_name = d.split("_", 3)
                category = "groups" if context == "groups" else "accounts"
                layer_id = get_current_layer(uid)
                u("DELETE FROM tags WHERE admin_id=%s AND name=%s AND category=%s AND layer_id=%s",
                  (uid, tag_name, category, layer_id))
                if category == "groups":
                    u("UPDATE group_tags SET tag_name='' WHERE admin_id=%s AND tag_name=%s AND layer_id=%s",
                      (uid, tag_name, layer_id))
                else:
                    u("DELETE FROM account_tags WHERE admin_id=%s AND tag_name=%s AND layer_id=%s",
                      (uid, tag_name, layer_id))
                await cb.answer(f"✅ برچسب «{tag_name}» حذف شد", show_alert=True)
                tags = q("SELECT DISTINCT name FROM tags WHERE admin_id=%s AND category=%s AND layer_id=%s ORDER BY name",
                         (uid, category, layer_id))
                tag_list = [t[0] for t in tags]
                await cb.message.edit_reply_markup(tags_list_kb(tag_list, context))

            elif d.startswith("acctag_sel_"):
                acc_id = d[11:]
                acc = q("SELECT name,phone FROM accounts WHERE id=%s", (acc_id,))
                if not acc:
                    await cb.answer("اکانت یافت نشد", show_alert=True); return
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                if not tag_list:
                    await cb.answer("ابتدا یک برچسب اکانت بسازید (دکمه ➕ برچسب جدید)", show_alert=True)
                    return
                cur = q("SELECT tag_name FROM account_tags WHERE admin_id=%s AND account_id=%s",
                        (uid, acc_id))
                cur_tags = [c[0] for c in cur]
                txt = f"👤 **{acc[0][0]}** | {acc[0][1]}\n\nبرچسب‌ها را انتخاب کنید (چند مورد مجاز است):"
                await cb.message.edit_text(txt, reply_markup=account_tag_multi_kb(acc_id, tag_list, cur_tags))

            elif d.startswith("acctagm_tog_"):
                _, _, acc_id, tag_name = d.split("_", 3)
                layer_id = get_current_layer(uid)
                exists = q("SELECT id FROM account_tags WHERE admin_id=%s AND account_id=%s AND tag_name=%s",
                           (uid, acc_id, tag_name))
                if exists:
                    u("DELETE FROM account_tags WHERE admin_id=%s AND account_id=%s AND tag_name=%s",
                      (uid, acc_id, tag_name))
                else:
                    u("INSERT IGNORE INTO account_tags (admin_id,account_id,tag_name,layer_id) VALUES (%s,%s,%s,%s)",
                      (uid, acc_id, tag_name, layer_id))
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='accounts' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                cur = q("SELECT tag_name FROM account_tags WHERE admin_id=%s AND account_id=%s",
                        (uid, acc_id))
                cur_tags = [c[0] for c in cur]
                await cb.message.edit_reply_markup(account_tag_multi_kb(acc_id, tag_list, cur_tags))

            elif d == "g_sec":
                layer_id = get_current_layer(uid)
                gl_id = f"global{layer_id}"
                row = q("SELECT is_active FROM global_secretary_settings WHERE admin_id=%s AND layer_id=%s",
                        (uid, layer_id))
                active = bool(row[0][0]) if row else False
                slots = [b[0] for b in q("SELECT slot FROM banners WHERE account_id=%s AND context='g_secretary' ORDER BY slot", (gl_id,))]
                await cb.message.edit_text("🤖 **منشی خودکار همگانی**", reply_markup=global_sec_kb(active, slots=slots))

            elif d.startswith("gsec_bv_"):
                slot = int(d[8:])
                layer_id = get_current_layer(uid)
                gl_id = f"global{layer_id}"
                bnrs = q("SELECT slot,text,file_id FROM banners WHERE account_id=%s AND context='g_secretary' ORDER BY slot",
                         (gl_id,))
                txt = "✏️ **بنرهای همگانی منشی**\n\n"
                for b in bnrs:
                    txt += f"═-═ {b[0]} ═-═\n💬 [{(b[1] or '')[:40]}...]\n📁 {'✅' if b[2] else '❌'}\n\n"
                if not bnrs: txt += "هیچ بنری."
                await cb.message.edit_text(txt, reply_markup=banner_slot_kb(gl_id, slot, "g_secretary"))

            elif d == "gsec_badd":
                layer_id = get_current_layer(uid)
                gl_id = f"global{layer_id}"
                existing = q("SELECT MAX(slot) FROM banners WHERE account_id=%s AND context='g_secretary'", (gl_id,))
                next_slot = (existing[0][0] or 0) + 1
                set_step(uid, f"bn_text_{gl_id}_{next_slot}_g_secretary")
                await cb.message.edit_text("📝 متن پیام جدید را وارد کنید:")

            elif d == "gsec_tog":
                layer_id = get_current_layer(uid)
                gl_id = f"global{layer_id}"
                row = q("SELECT is_active FROM global_secretary_settings WHERE admin_id=%s AND layer_id=%s",
                        (uid, layer_id))
                new = 0 if (row and row[0][0]) else 1
                u("INSERT INTO global_secretary_settings (admin_id,layer_id,is_active) VALUES(%s,%s,%s) "
                  "ON DUPLICATE KEY UPDATE is_active=%s", (uid, layer_id, new, new))
                await cb.answer(f"منشی همگانی {'فعال' if new else 'غیرفعال'} شد", show_alert=True)
                slots = [b[0] for b in q("SELECT slot FROM banners WHERE account_id=%s AND context='g_secretary' ORDER BY slot", (gl_id,))]
                await cb.message.edit_reply_markup(global_sec_kb(bool(new), slots=slots))

            elif d == "gsec_now":
                from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionExpired
                await cb.answer("⚡ در حال بررسی و ارسال...", show_alert=False)
                layer_id = get_current_layer(uid)
                gl_id = f"global{layer_id}"
                g_banners = q(
                    "SELECT text,file_id,file_type FROM banners "
                    "WHERE account_id=%s AND context='g_secretary' ORDER BY slot",
                    (gl_id,)
                )
                if not g_banners:
                    row = q("SELECT is_active FROM global_secretary_settings WHERE admin_id=%s AND layer_id=%s",
                            (uid, layer_id))
                    active = bool(row[0][0]) if row else False
                    await cb.message.edit_text("⚠️ هیچ بنری برای منشی همگانی تنظیم نشده.",
                                                reply_markup=global_sec_kb(active))
                    return
                await cb.message.edit_text("🔄 در حال بررسی پی‌وی‌های همه‌ی اکانت‌ها...")
                accs = q("SELECT id,phone FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                         (uid, layer_id))
                report_lines = []
                total_new = 0
                for acc_id, phone in accs:
                    g_replied_row = q(
                        "SELECT replied_users FROM secretary WHERE account_id=%s",
                        (f"g_{acc_id}",)
                    )
                    g_replied = set(g_replied_row[0][0].split(",")) if (g_replied_row and g_replied_row[0][0]) else set()
                    uc = await get_user_client(acc_id)
                    if not uc:
                        report_lines.append(f"❌ {phone}: اتصال اکانت ناموفق")
                        continue
                    try:
                        await uc.start()
                        g_replied, g_new_replied = await sec_worker._reply_to_pvs(uc, acc_id, g_banners, g_replied)
                        await uc.stop()
                        u("INSERT INTO secretary (account_id,admin_id,is_active,replied_users) "
                          "VALUES(%s,%s,0,%s) ON DUPLICATE KEY UPDATE replied_users=%s",
                          (f"g_{acc_id}", uid, ",".join(g_replied), ",".join(g_replied)))
                        total_new += len(g_new_replied)
                        report_lines.append(f"✅ {phone}: {len(g_new_replied)} کاربر جدید")
                    except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
                        u("UPDATE accounts SET status='inactive' WHERE id=%s", (acc_id,))
                        report_lines.append(f"⚠️ {phone}: منقضی شده")
                        try: await uc.stop()
                        except Exception: pass
                    except Exception as e:
                        report_lines.append(f"❌ {phone}: خطا ({e})")
                        try: await uc.stop()
                        except Exception: pass

                row = q("SELECT is_active FROM global_secretary_settings WHERE admin_id=%s AND layer_id=%s",
                        (uid, layer_id))
                active = bool(row[0][0]) if row else False
                txt = f"⚡ **نتیجه ارسال فوری**\n\nجمعاً به {total_new} کاربر جدید پاسخ داده شد.\n\n"
                txt += "\n".join(report_lines) if report_lines else "هیچ اکانت فعالی یافت نشد."
                await cb.message.edit_text(txt, reply_markup=global_sec_kb(active))

            elif d == "g_rr":
                gl_id = f"global{get_current_layer(uid)}"
                row = q("SELECT is_active,interval_minutes,group_tag_filter,acc_tag_filter FROM reply_rand WHERE account_id=%s AND admin_id=%s", (gl_id, uid))
                active = row[0][0] if row else 0
                interval = row[0][1] if row else 30
                gtag = (row[0][2] if row else None) or "ALL"
                atag = (row[0][3] if row else None) or "ALL"
                await cb.message.edit_text(
                    f"↩️ **ریپلای رندم همگانی**\n\n"
                    f"این تنظیم برای **همه اکانت‌های این لایه** یکسان اعمال می‌شود؛ "
                    f"هر اکانت مستقل با همین متن و زمان ریپلای می‌زند.\n\n"
                    f"فاصله: {interval} دقیقه\nوضعیت: {'✅' if active else '❌'}",
                    reply_markup=reply_rand_kb(gl_id, active, back_to="menu_global", group_tag=gtag, acc_tag=atag)
                )

            elif d == "g_rc":
                gl_id = f"global{get_current_layer(uid)}"
                row = q("SELECT is_active,interval_minutes,group_tag_filter,acc_tag_filter FROM react_rand WHERE account_id=%s AND admin_id=%s", (gl_id, uid))
                active = row[0][0] if row else 0
                interval = row[0][1] if row else 30
                gtag = (row[0][2] if row else None) or "ALL"
                atag = (row[0][3] if row else None) or "ALL"
                await cb.message.edit_text(
                    f"😀 **ری‌اکت رندم همگانی**\n\n"
                    f"این تنظیم برای **همه اکانت‌های این لایه** یکسان اعمال می‌شود؛ "
                    f"هر اکانت مستقل ری‌اکت می‌زند.\n\n"
                    f"فاصله: {interval} دقیقه\nوضعیت: {'✅' if active else '❌'}",
                    reply_markup=react_rand_kb(gl_id, active, back_to="menu_global", group_tag=gtag, acc_tag=atag)
                )

            # ══ جوین از پیوی‌ها ══
            elif d == "g_pvjoin":
                cnt_row = q("SELECT COUNT(*) FROM pv_links WHERE admin_id=%s", (uid,))
                link_count = cnt_row[0][0] if cnt_row else 0
                st_row = q("SELECT last_scan FROM pv_join_settings WHERE admin_id=%s", (uid,))
                last_scan = st_row[0][0] if st_row else None
                await cb.message.edit_text(
                    "📥 **جوین از پیوی‌ها**\n\n"
                    "اکانت‌ها پیوی‌هایشان را اسکن می‌کنند و لینک‌های تلگرامی استخراج می‌شوند.",
                    reply_markup=pv_join_kb(link_count, last_scan)
                )

            elif d == "g_pvjoin_scan":
                await cb.message.edit_text("🔍 در حال اسکن پیوی‌ها...\nلطفاً صبر کنید.")
                asyncio.create_task(_scan_pvs_for_links(cb._client, uid))

            elif d == "g_pvjoin_show":
                rows = q("SELECT link FROM pv_links WHERE admin_id=%s ORDER BY found_at DESC", (uid,))
                if not rows:
                    await cb.answer("❌ لینکی یافت نشده. ابتدا اسکن کنید.", show_alert=True)
                else:
                    out = "\n".join(r[0] for r in rows)
                    chunks = [out[i:i+4000] for i in range(0, len(out), 4000)]
                    for chunk in chunks:
                        await cb._client.send_message(uid, chunk)
                    await cb.answer(f"✅ {len(rows)} لینک ارسال شد", show_alert=True)

            elif d == "g_pvjoin_join":
                rows = q("SELECT link FROM pv_links WHERE admin_id=%s ORDER BY found_at DESC", (uid,))
                if not rows:
                    await cb.answer("❌ لینکی یافت نشده. ابتدا اسکن کنید.", show_alert=True)
                else:
                    links = [r[0] for r in rows]
                    set_step(uid, "g_join_tag", "\n".join(links))
                    layer_id = get_current_layer(uid)
                    tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                    tag_list = [t[0] for t in tags]
                    await cb.message.edit_text(
                        f"✅ **{len(links)} لینک از پیوی‌ها آماده جوین**\n\nبرچسب گروه‌ها را انتخاب کنید:",
                        reply_markup=tag_select_kb(tag_list, "gjointag", show_all=False)
                    )

            elif d == "g_pvjoin_clear":
                u("DELETE FROM pv_links WHERE admin_id=%s", (uid,))
                await cb.answer("✅ لیست لینک‌ها پاک شد", show_alert=True)
                await cb.message.edit_text(
                    "📥 **جوین از پیوی‌ها**\n\n"
                    "اکانت‌ها پیوی‌هایشان را اسکن می‌کنند و لینک‌های تلگرامی استخراج می‌شوند.",
                    reply_markup=pv_join_kb(0, None)
                )

            elif d == "g_pvjoin_settings":
                st_row = q(
                    "SELECT auto_scan, scan_interval_hours, daily_limit "
                    "FROM pv_join_settings WHERE admin_id=%s",
                    (uid,)
                )
                auto_scan = st_row[0][0] if st_row else 0
                interval_hours = st_row[0][1] if st_row else 6
                daily_limit = st_row[0][2] if st_row else 20
                await cb.message.edit_text(
                    "⚙️ **تنظیمات اسکن خودکار پیوی‌ها**",
                    reply_markup=pv_join_settings_kb(auto_scan, interval_hours, daily_limit)
                )

            elif d == "g_pvjoin_tog_auto":
                st_row = q("SELECT auto_scan FROM pv_join_settings WHERE admin_id=%s", (uid,))
                new = 0 if (st_row and st_row[0][0]) else 1
                u(
                    "INSERT INTO pv_join_settings (admin_id, auto_scan) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE auto_scan=%s",
                    (uid, new, new)
                )
                await cb.answer(f"اسکن خودکار {'فعال' if new else 'غیرفعال'} شد", show_alert=True)
                st_row2 = q(
                    "SELECT auto_scan, scan_interval_hours, daily_limit "
                    "FROM pv_join_settings WHERE admin_id=%s",
                    (uid,)
                )
                auto_scan = st_row2[0][0] if st_row2 else new
                interval_hours = st_row2[0][1] if st_row2 else 6
                daily_limit = st_row2[0][2] if st_row2 else 20
                await cb.message.edit_reply_markup(pv_join_settings_kb(auto_scan, interval_hours, daily_limit))

            elif d == "g_pvjoin_set_interval":
                set_step(uid, "g_pvjoin_interval")
                await cb.message.edit_text(
                    "⏰ **فاصله اسکن خودکار**\n\nعدد ساعت را وارد کنید (۱ تا ۲۴):",
                    reply_markup=back_kb("g_pvjoin_settings")
                )

            elif d == "g_pvjoin_set_limit":
                set_step(uid, "g_pvjoin_limit")
                await cb.message.edit_text(
                    "📊 **سقف روزانه جوین**\n\nتعداد لینک در روز را وارد کنید (۱ تا ۱۰۰):",
                    reply_markup=back_kb("g_pvjoin_settings")
                )

            # ══════════════════════════════════════════
            # ══ لینکدونی هوشمند ══
            # ══════════════════════════════════════════

            elif d == "ld_menu":
                layer_id = get_current_layer(uid)
                src_row = q("SELECT COUNT(*) FROM linkdoni_sources "
                            "WHERE admin_id=%s AND is_active=1 AND layer_id=%s", (uid, layer_id))
                source_count = src_row[0][0] if src_row else 0
                pend_row = q("SELECT COUNT(*) FROM linkdoni_links "
                             "WHERE admin_id=%s AND joined=0", (uid,))
                pending_count = pend_row[0][0] if pend_row else 0
                st_row = q("SELECT auto_scan, auto_join FROM linkdoni_settings "
                           "WHERE admin_id=%s", (uid,))
                auto_scan = st_row[0][0] if st_row else 0
                auto_join = st_row[0][1] if st_row else 0
                await cb.message.edit_text(
                    f"📡 **لینکدونی هوشمند**\n\n"
                    f"🗂 لینکدونی‌های فعال: {source_count}\n"
                    f"🔗 لینک‌های در انتظار جوین: {pending_count}",
                    reply_markup=ld_menu_kb(source_count, pending_count,
                                           auto_scan, auto_join)
                )

            elif d == "ld_sources":
                layer_id = get_current_layer(uid)
                srcs = q("SELECT id, chat_id, chat_title, is_active "
                         "FROM linkdoni_sources WHERE admin_id=%s AND layer_id=%s ORDER BY added_at DESC",
                         (uid, layer_id))
                src_list = [{"id": r[0], "chat_id": r[1],
                             "chat_title": r[2], "is_active": r[3]} for r in srcs]
                await cb.message.edit_text(
                    "📋 **مدیریت لینکدونی‌ها**",
                    reply_markup=ld_sources_kb(src_list)
                )

            elif d == "ld_add_source":
                set_step(uid, "ld_add_source")
                await cb.message.edit_text(
                    "➕ **لینک یا یوزرنیم لینکدونی‌ها را وارد کنید:**\n"
                    "هر لینکدونی در یک خط — می‌توانید چند مورد را یکجا ارسال کنید.\n\n"
                    "مثال:\n`@linkdoni1`\n`https://t.me/linkdoni2`",
                    reply_markup=back_kb("ld_sources")
                )

            elif d == "ld_src_getall":
                layer_id = get_current_layer(uid)
                srcs = q("SELECT chat_title, chat_id, source_link FROM linkdoni_sources "
                         "WHERE admin_id=%s AND layer_id=%s ORDER BY added_at DESC", (uid, layer_id))
                if not srcs:
                    await cb.answer("❌ هیچ لینکدونی‌ای ثبت نشده.", show_alert=True)
                    return
                lines = [(src_link or title or str(chat_id)) for title, chat_id, src_link in srcs]
                out = "\n".join(lines)
                if len(out) <= 4000:
                    await cb.message.reply(out)
                else:
                    chunks = [out[i:i+4000] for i in range(0, len(out), 4000)]
                    for chunk in chunks:
                        await cb.message.reply(chunk)
                await cb.answer("✅ لینک‌ها ارسال شد")

            elif d.startswith("ld_src_tog_"):
                src_id = int(d[11:])
                row = q("SELECT is_active FROM linkdoni_sources "
                        "WHERE id=%s AND admin_id=%s", (src_id, uid))
                if not row:
                    await cb.answer("❌ یافت نشد", show_alert=True)
                else:
                    new_val = 0 if row[0][0] else 1
                    u("UPDATE linkdoni_sources SET is_active=%s "
                      "WHERE id=%s AND admin_id=%s", (new_val, src_id, uid))
                    src = q("SELECT id, chat_id, chat_title, is_active, last_scan, last_message_id "
                            "FROM linkdoni_sources WHERE id=%s AND admin_id=%s",
                            (src_id, uid))
                    if src:
                        r = src[0]
                        last_scan_str = r[4].strftime("%Y/%m/%d %H:%M") if r[4] else "هرگز"
                        await cb.message.edit_text(
                            f"📋 **{r[2] or r[1]}**\n🆔 `{r[1]}`\n"
                            f"📅 آخرین اسکن: {last_scan_str}\n"
                            f"📨 آخرین پیام اسکن‌شده: {r[5]}",
                            reply_markup=ld_source_detail_kb(src_id, r[3])
                        )

            elif d.startswith("ld_src_del_"):
                src_id = int(d[11:])
                u("DELETE FROM linkdoni_sources WHERE id=%s AND admin_id=%s",
                  (src_id, uid))
                layer_id = get_current_layer(uid)
                srcs = q("SELECT id, chat_id, chat_title, is_active "
                         "FROM linkdoni_sources WHERE admin_id=%s AND layer_id=%s ORDER BY added_at DESC",
                         (uid, layer_id))
                src_list = [{"id": r[0], "chat_id": r[1],
                             "chat_title": r[2], "is_active": r[3]} for r in srcs]
                await cb.message.edit_text(
                    "✅ لینکدونی حذف شد.",
                    reply_markup=ld_sources_kb(src_list)
                )

            elif d.startswith("ld_src_"):
                # جزئیات یک لینکدونی — بعد از بررسی tog و del
                src_id = int(d[7:])
                src = q("SELECT id, chat_id, chat_title, is_active, last_scan, last_message_id "
                        "FROM linkdoni_sources WHERE id=%s AND admin_id=%s",
                        (src_id, uid))
                if not src:
                    await cb.answer("❌ یافت نشد", show_alert=True)
                else:
                    r = src[0]
                    last_scan_str = r[4].strftime("%Y/%m/%d %H:%M") if r[4] else "هرگز"
                    await cb.message.edit_text(
                        f"📋 **{r[2] or r[1]}**\n🆔 `{r[1]}`\n"
                        f"📅 آخرین اسکن: {last_scan_str}\n"
                        f"📨 آخرین پیام اسکن‌شده: {r[5]}",
                        reply_markup=ld_source_detail_kb(src_id, r[3])
                    )

            elif d == "ld_scan_now":
                await cb.message.edit_text("🔍 در حال اسکن لینکدونی‌ها...")

                async def _do_scan_now(bot_client, msg):
                    from workers.linkdoni_worker import scan_linkdonis, _get_settings, _do_join
                    total, new_count, links = await scan_linkdonis(uid, "manual")
                    report = (
                        f"✅ **اسکن فوری تمام شد**\n\n"
                        f"🔍 لینک‌های بررسی‌شده: {total}\n"
                        f"✅ لینک‌های جدید: {new_count}\n"
                        f"🔄 تکراری حذف‌شده: {total - new_count}"
                    )
                    try:
                        await msg.edit_text(report, reply_markup=back_kb("ld_menu"))
                    except Exception:
                        await bot_client.send_message(uid, report)
                    # جوین خودکار فقط برای اسکن manual فعاله
                    settings = _get_settings(uid)
                    if settings["auto_join"] and links:
                        await _do_join(uid, links, settings)

                asyncio.create_task(_do_scan_now(client, cb.message))

            elif d == "ld_show_links":
                cnt_row = q("SELECT COUNT(*) FROM linkdoni_links "
                            "WHERE admin_id=%s AND joined=0", (uid,))
                count = cnt_row[0][0] if cnt_row else 0
                if count == 0:
                    await cb.message.edit_text(
                        "❌ لینک جدیدی موجود نیست. ابتدا اسکن کنید.",
                        reply_markup=back_kb("ld_menu")
                    )
                else:
                    await cb.message.edit_text(
                        f"🔗 **لینک‌های دریافتی**\n\n{count} لینک در انتظار:",
                        reply_markup=ld_links_kb(count)
                    )

            elif d == "ld_links_view":
                links_rows = q("SELECT link FROM linkdoni_links "
                               "WHERE admin_id=%s AND joined=0 ORDER BY found_at DESC",
                               (uid,))
                if not links_rows:
                    await cb.message.edit_text(
                        "❌ لینکی موجود نیست.",
                        reply_markup=back_kb("ld_menu")
                    )
                else:
                    out = "\n".join(r[0] for r in links_rows)
                    cnt = len(links_rows)
                    if len(out) <= 4000:
                        await cb.message.edit_text(
                            out, reply_markup=back_kb("ld_show_links")
                        )
                    else:
                        chunks = [out[i:i+4000] for i in range(0, len(out), 4000)]
                        await cb.message.edit_text(
                            f"🔗 {cnt} لینک در انتظار جوین:",
                            reply_markup=back_kb("ld_show_links")
                        )
                        for chunk in chunks:
                            await client.send_message(uid, chunk)

            elif d == "ld_links_clear":
                u("DELETE FROM linkdoni_links WHERE admin_id=%s AND joined=0",
                  (uid,))
                await cb.message.edit_text(
                    "✅ لینک‌های دریافتی پاک شدند.",
                    reply_markup=back_kb("ld_menu")
                )

            elif d == "ld_join_manual":
                links_rows = q("SELECT link FROM linkdoni_links "
                               "WHERE admin_id=%s AND joined=0 ORDER BY found_at DESC",
                               (uid,))
                if not links_rows:
                    await cb.message.edit_text(
                        "❌ لینک جدیدی برای جوین وجود ندارد.",
                        reply_markup=back_kb("ld_menu")
                    )
                else:
                    import json
                    from handlers.text_handler import _check_duplicate_links
                    links = [r[0] for r in links_rows]
                    new_links, dup_links = _check_duplicate_links(links, uid)
                    set_step(uid, "g_join_dup_check", json.dumps({
                        "all": links,
                        "new": new_links,
                        "dup": dup_links
                    }))
                    await cb.message.edit_text(
                        f"📋 **{len(links)} لینک دریافت شد**\n"
                        f"✅ جدید: {len(new_links)} لینک\n"
                        f"🔄 تکراری: {len(dup_links)} لینک (قبلاً استفاده شده)",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                f"❌ حذف تکراری‌ها و جوین با {len(new_links)} لینک",
                                callback_data="gjoin_nodup"
                            )],
                            [InlineKeyboardButton(
                                f"✅ جوین با همه {len(links)} لینک",
                                callback_data="gjoin_all"
                            )]
                        ])
                    )

            elif d == "ld_settings":
                st_row = q("SELECT auto_scan, scan_interval_hours, auto_join, join_mode, join_tag "
                           "FROM linkdoni_settings WHERE admin_id=%s", (uid,))
                if not st_row:
                    u("INSERT IGNORE INTO linkdoni_settings (admin_id) VALUES (%s)", (uid,))
                    auto_scan, interval, auto_join, join_mode, join_tag = 0, 6, 0, "split", ""
                else:
                    auto_scan, interval, auto_join, join_mode, join_tag = (
                        st_row[0][0], st_row[0][1], st_row[0][2],
                        st_row[0][3], st_row[0][4] or ""
                    )
                await cb.message.edit_text(
                    "⚙️ **تنظیمات لینکدونی هوشمند**",
                    reply_markup=ld_settings_kb(auto_scan, interval, auto_join,
                                               join_mode, join_tag)
                )

            elif d == "ld_tog_autoscan":
                st_row = q("SELECT auto_scan FROM linkdoni_settings WHERE admin_id=%s",
                           (uid,))
                if not st_row:
                    u("INSERT IGNORE INTO linkdoni_settings (admin_id) VALUES (%s)", (uid,))
                    cur_val = 0
                else:
                    cur_val = st_row[0][0]
                new_val = 0 if cur_val else 1
                u("UPDATE linkdoni_settings SET auto_scan=%s WHERE admin_id=%s",
                  (new_val, uid))
                st_row2 = q("SELECT auto_scan, scan_interval_hours, auto_join, join_mode, join_tag "
                            "FROM linkdoni_settings WHERE admin_id=%s", (uid,))
                r = st_row2[0] if st_row2 else (new_val, 6, 0, "split", "")
                await cb.message.edit_reply_markup(
                    ld_settings_kb(r[0], r[1], r[2], r[3], r[4] or "")
                )

            elif d == "ld_set_interval":
                set_step(uid, "ld_interval")
                await cb.message.edit_text(
                    "⏰ **فاصله اسکن خودکار**\n\n"
                    "عدد ساعت را وارد کنید (۱ تا ۱۶۸):",
                    reply_markup=back_kb("ld_settings")
                )

            elif d == "ld_tog_autojoin":
                st_row = q("SELECT auto_join FROM linkdoni_settings WHERE admin_id=%s",
                           (uid,))
                if not st_row:
                    u("INSERT IGNORE INTO linkdoni_settings (admin_id) VALUES (%s)", (uid,))
                    cur_val = 0
                else:
                    cur_val = st_row[0][0]
                new_val = 0 if cur_val else 1
                u("UPDATE linkdoni_settings SET auto_join=%s WHERE admin_id=%s",
                  (new_val, uid))
                st_row2 = q("SELECT auto_scan, scan_interval_hours, auto_join, join_mode, join_tag "
                            "FROM linkdoni_settings WHERE admin_id=%s", (uid,))
                r = st_row2[0] if st_row2 else (0, 6, new_val, "split", "")
                await cb.message.edit_reply_markup(
                    ld_settings_kb(r[0], r[1], r[2], r[3], r[4] or "")
                )

            elif d == "ld_set_joinmode":
                st_row = q("SELECT join_mode FROM linkdoni_settings WHERE admin_id=%s",
                           (uid,))
                current_mode = st_row[0][0] if st_row else "split"
                await cb.message.edit_text(
                    "🔄 **حالت جوین را انتخاب کنید:**",
                    reply_markup=ld_joinmode_kb(current_mode)
                )

            elif d.startswith("ld_joinmode_"):
                join_mode = d[12:]
                if join_mode not in ("random", "split", "all"):
                    await cb.answer("❌ حالت نامعتبر", show_alert=True)
                else:
                    u("INSERT INTO linkdoni_settings (admin_id, join_mode) VALUES (%s,%s) "
                      "ON DUPLICATE KEY UPDATE join_mode=%s",
                      (uid, join_mode, join_mode))
                    st_row = q("SELECT auto_scan, scan_interval_hours, auto_join, join_mode, join_tag "
                               "FROM linkdoni_settings WHERE admin_id=%s", (uid,))
                    r = st_row[0] if st_row else (0, 6, 0, join_mode, "")
                    await cb.message.edit_text(
                        "✅ حالت جوین ذخیره شد.",
                        reply_markup=ld_settings_kb(r[0], r[1], r[2], r[3], r[4] or "")
                    )

            elif d == "ld_set_tag":
                set_step(uid, "ld_tag")
                await cb.message.edit_text(
                    "🏷 **برچسب گروه‌های جوین‌شده**\n\n"
                    "برچسب را وارد کنید یا برای بدون برچسب، یک فاصله بفرستید:",
                    reply_markup=back_kb("ld_settings")
                )

        except Exception as e:
            print(f"[CB ERROR] {d}: {e}")
            try:
                await cb.answer(f"❌ {str(e)[:100]}", show_alert=True)
            except Exception:
                pass

        try:
            await cb.answer()
        except Exception:
            pass


# ─── helpers ────────────────────────────────────────────────

async def _send_to_pvs(bot_client, acc_id, text):
    admin_id = get_account_owner(acc_id)
    from pyrogram import enums as en
    from pyrogram.errors import FloodWait, UserIsBlocked, PeerIdInvalid, UserDeactivated as UD

    uc = await get_user_client(acc_id)
    if not uc:
        print(f"[SendPV] اکانت {acc_id} session نداره")
        return

    me_info = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
    display = me_info[0][0] if me_info else acc_id

    ok = fail = blocked = 0
    error_samples = []

    try:
        await uc.start()
    except Exception as e:
        print(f"[SendPV] خطا در اتصال {acc_id}: {e}")
        await bot_client.send_message(admin_id, f"❌ اتصال اکانت {display} ناموفق: {e}")
        return

    try:
        dialogs = []
        async for dlg in uc.get_dialogs():
            if dlg.chat.type == en.ChatType.PRIVATE:
                dialogs.append(dlg)
    except Exception as e:
        print(f"[SendPV] خطا در گرفتن لیست پیوی‌ها {acc_id}: {e}")
        await bot_client.send_message(admin_id, f"❌ خطا در خواندن پیوی‌های {display}: {e}")
        try: await uc.stop()
        except Exception: pass
        return

    for dlg in dialogs:
        if is_stopped():
            break
        try:
            await uc.send_message(dlg.chat.id, text)
            ok += 1
            await asyncio.sleep(2)
        except FloodWait as e:
            # صبر می‌کنیم و همین کاربر رو دوباره امتحان می‌کنیم، بقیه رو ول نمی‌کنیم
            wait_s = min(e.value, 120)
            await asyncio.sleep(wait_s)
            try:
                await uc.send_message(dlg.chat.id, text); ok += 1
            except Exception:
                fail += 1
        except UserIsBlocked:
            blocked += 1
        except (PeerIdInvalid, UD):
            fail += 1
        except Exception as e:
            fail += 1
            if len(error_samples) < 3:
                error_samples.append(str(e)[:80])
            print(f"[SendPV] خطا در ارسال به {dlg.chat.id} ({acc_id}): {e}")

    try:
        await uc.stop()
    except Exception:
        pass

    report = f"✅ پیوی‌ها\n👤 {display}\n✔️ موفق: {ok}\n🚫 بلاک شده: {blocked}\n❌ خطا: {fail}"
    if error_samples:
        report += "\n\n❗️ نمونه خطاها:\n" + "\n".join(error_samples)
    await bot_client.send_message(admin_id, report)


async def _send_to_groups_task(bot_client, acc_id, text, group_tag_filter="ALL"):
    from handlers.text_handler import send_to_groups_smart
    admin_id = get_account_owner(acc_id)
    result = await send_to_groups_smart(bot_client, acc_id, text,
                                         group_tag_filter=group_tag_filter)
    tag_lbl = f" [🏷 {group_tag_filter}]" if group_tag_filter not in ("ALL", "") else ""
    report = (
        f"✅ ارسال به گروه‌ها تمام شد{tag_lbl}\n"
        f"👤 {result.get('display', acc_id)}\n"
        f"✔️ موفق: {result['ok']}\n"
        f"❌ ناموفق: {result['fail']}\n"
        f"🚫 محدود شده: {result['limited']}\n"
        f"🔗 عضویت اجبار انجام شد: {result['force_joined']}\n"
        f"🚪 از گروه‌های محدود خارج شد: {result['left']}"
    )
    await bot_client.send_message(admin_id, report)


async def _delete_pvs_task(bot_client, acc_id):
    """حذف تمام پیوی‌های یک اکانت — تسک همگانی"""
    from pyrogram import enums as en
    admin_id = get_account_owner(acc_id)
    uc = await get_user_client(acc_id)
    if not uc:
        return
    me_info = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
    display = me_info[0][0] if me_info else acc_id
    count = 0
    try:
        await uc.start()
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ اتصال اکانت {display} ناموفق: {e}")
        return
    try:
        async for dlg in uc.get_dialogs():
            if dlg.chat.type == en.ChatType.PRIVATE:
                try:
                    await clear_chat_history(uc, dlg.chat.id); count += 1
                except Exception: pass
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ خطا در خواندن پیوی‌های {display}: {e}")
    try:
        await uc.stop()
    except Exception:
        pass
    await bot_client.send_message(admin_id, f"✅ حذف پیوی‌ها\n👤 {display}\n🗑 حذف‌شده: {count}")


async def _delete_bots_task(bot_client, acc_id):
    """حذف تمام گفتگو با ربات‌های یک اکانت — تسک همگانی"""
    from pyrogram import enums as en
    admin_id = get_account_owner(acc_id)
    uc = await get_user_client(acc_id)
    if not uc:
        return
    me_info = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
    display = me_info[0][0] if me_info else acc_id
    count = 0
    try:
        await uc.start()
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ اتصال اکانت {display} ناموفق: {e}")
        return
    try:
        async for dlg in uc.get_dialogs():
            if dlg.chat.type == en.ChatType.BOT:
                try:
                    await clear_chat_history(uc, dlg.chat.id); count += 1
                except Exception: pass
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ خطا در خواندن ربات‌های {display}: {e}")
    try:
        await uc.stop()
    except Exception:
        pass
    await bot_client.send_message(admin_id, f"✅ حذف ربات‌ها\n👤 {display}\n🗑 حذف‌شده: {count}")


async def _delete_channels_task(bot_client, acc_id):
    """خروج از تمام کانال‌های یک اکانت — تسک همگانی"""
    from pyrogram import enums as en
    admin_id = get_account_owner(acc_id)
    uc = await get_user_client(acc_id)
    if not uc:
        return
    me_info = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
    display = me_info[0][0] if me_info else acc_id
    count = 0
    try:
        await uc.start()
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ اتصال اکانت {display} ناموفق: {e}")
        return
    try:
        async for dlg in uc.get_dialogs():
            if dlg.chat.type == en.ChatType.CHANNEL:
                try:
                    await uc.leave_chat(dlg.chat.id); count += 1
                    await asyncio.sleep(0.5)
                except Exception: pass
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ خطا در خواندن کانال‌های {display}: {e}")
    try:
        await uc.stop()
    except Exception:
        pass
    await bot_client.send_message(admin_id, f"✅ خروج از کانال‌ها\n👤 {display}\n🚪 خارج‌شده: {count}")


async def _delete_groups_task(bot_client, acc_id, group_tag_filter="ALL"):
    """خروج از گروه‌های یک اکانت با فیلتر برچسب — تسک همگانی"""
    from pyrogram import enums as en
    from handlers.text_handler import get_filtered_chat_ids, _chat_allowed
    admin_id = get_account_owner(acc_id)
    uc = await get_user_client(acc_id)
    if not uc:
        return
    me_info = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
    display = me_info[0][0] if me_info else acc_id
    filter_result = get_filtered_chat_ids(acc_id, group_tag_filter)
    count = 0
    try:
        await uc.start()
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ اتصال اکانت {display} ناموفق: {e}")
        return
    try:
        async for dlg in uc.get_dialogs():
            if dlg.chat.type not in (en.ChatType.GROUP, en.ChatType.SUPERGROUP):
                continue
            if not _chat_allowed(dlg.chat.id, filter_result):
                continue
            try:
                await uc.leave_chat(dlg.chat.id); count += 1
                await asyncio.sleep(0.5)
            except Exception: pass
    except Exception as e:
        await bot_client.send_message(admin_id, f"❌ خطا در خواندن گروه‌های {display}: {e}")
    try:
        await uc.stop()
    except Exception:
        pass
    tag_lbl = f" [🏷 {group_tag_filter}]" if group_tag_filter not in ("ALL", "") else ""
    await bot_client.send_message(admin_id, f"✅ حذف گروه‌ها{tag_lbl}\n👤 {display}\n🚪 خارج‌شده: {count}")


async def _scan_pvs_for_links(bot_client, admin_id):
    """اسکن پیوی‌های همه اکانت‌ها و استخراج لینک‌های تلگرامی — تابع مشترک"""
    import re
    from pyrogram import enums as en
    from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionExpired
    from handlers.text_handler import _link_hash

    LINK_RE = re.compile(r'https?://t\.me/[^\s\]\)"\']+')

    layer_id = get_current_layer(admin_id)
    accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
             (admin_id, layer_id))
    total_new = 0

    for (acc_id,) in accs:
        uc = await get_user_client(acc_id)
        if not uc:
            continue
        try:
            await uc.start()
            async for dlg in uc.get_dialogs():
                if dlg.chat.type != en.ChatType.PRIVATE:
                    continue
                try:
                    async for msg in uc.get_chat_history(dlg.chat.id, limit=50):
                        txt = msg.text or msg.caption or ""
                        for raw_link in LINK_RE.findall(txt):
                            link = raw_link.rstrip(".,;:!?)")
                            lh = _link_hash(link)
                            # چک نبودن در used_links
                            used = q(
                                "SELECT 1 FROM used_links WHERE admin_id=%s AND link_hash=%s",
                                (admin_id, lh)
                            )
                            if used:
                                continue
                            try:
                                u(
                                    "INSERT IGNORE INTO pv_links (admin_id, link, link_hash) "
                                    "VALUES (%s, %s, %s)",
                                    (admin_id, link[:500], lh)
                                )
                                # اگه واقعاً insert شد (نه ignore) شمارش می‌کنیم
                            except Exception as e:
                                print(f"[PVScan] خطا در ذخیره لینک: {e}")
                except Exception as e:
                    print(f"[PVScan] خطا در خواندن پیام‌های {dlg.chat.id}: {e}")
            await uc.stop()
        except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
            u("UPDATE accounts SET status='inactive' WHERE id=%s", (acc_id,))
            try: await uc.stop()
            except Exception: pass
            continue
        except Exception as e:
            print(f"[PVScan] خطا در اکانت {acc_id}: {e}")
            try: await uc.stop()
            except Exception: pass
            continue
        await asyncio.sleep(2)

    # شمارش لینک‌های جدید بعد از اسکن (ساده‌ترین روش)
    cnt_row = q("SELECT COUNT(*) FROM pv_links WHERE admin_id=%s", (admin_id,))
    total_links = cnt_row[0][0] if cnt_row else 0

    from datetime import datetime
    now = datetime.utcnow()
    u(
        "INSERT INTO pv_join_settings (admin_id, last_scan) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE last_scan=%s",
        (admin_id, now, now)
    )

    if bot_client:
        try:
            await bot_client.send_message(
                admin_id,
                f"✅ اسکن تمام شد\n"
                f"📱 اکانت‌ها: {len(accs)}\n"
                f"🔗 لینک‌های موجود در لیست: {total_links}"
            )
        except Exception as e:
            print(f"[PVScan] خطا در ارسال گزارش: {e}")
