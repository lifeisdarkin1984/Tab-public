import asyncio, random, re, time, hashlib, json
from pyrogram import Client, filters
from pyrogram.errors import (FloodWait, UserAlreadyParticipant,
    InviteHashExpired, InviteHashInvalid, ChannelsTooMuch,
    UsernameOccupied, UsernameInvalid, ChatWriteForbidden,
    UserBannedInChannel, ChatAdminRequired, ChatRestricted,
    SlowmodeWait, UserBlocked, ChatSendMediaForbidden, RPCError,
    InviteRequestSent)
from database import q, u
from utils import (get_step, get_step_data, set_step,
                   clear_step, get_user_client, save_account, is_stopped, set_stop,
                   detect_and_handle_bot_forced_join, get_current_layer,
                   get_account_owner, is_tenant_locked, get_tenant_status,
                   get_plan_limits, count_layers)
from keyboards import (manage_kb, back_kb, confirm_kb, global_kb, reply_rand_kb,
                       react_rand_kb, reply_banner_list_kb, tag_select_kb,
                       main_menu_kb, global_sch_panel_kb, tags_list_kb, billing_kb)
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from handlers.login import send_code, sign_in
import os

OWNER_ID = int(os.environ.get("PANEL_OWNER_ID", 0))

def _locked_reply_text(uid):
    status, expires_at = get_tenant_status(uid)
    if status == "suspended":
        return "⛔️ دسترسی شما مسدود شده."
    from datetime import datetime
    exp_txt = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else "-"
    return f"⌛️ اشتراک شما در {exp_txt} تموم شده. داده‌هاتون محفوظه؛ برای ادامه تمدید کنید."

def register(app):

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start","add_account","list_account"]))
    async def on_text(client, message):
        uid = message.from_user.id
        step = get_step(uid)
        text = message.text.strip()

        if step == "awaiting_ticket":
            if not text:
                await message.reply("لطفاً متن پیام رو بنویسید.")
                return
            ticket_id = u("INSERT INTO tickets (tenant_id, message) VALUES (%s,%s)", (uid, text))
            clear_step(uid)
            await message.reply(
                "✅ پیام شما ثبت شد و برای پشتیبانی ارسال شد.\n"
                "به‌محض پاسخ، همینجا بهتون اطلاع داده می‌شه.",
                reply_markup=main_menu_kb()
            )
            if OWNER_ID:
                try:
                    uname = f"@{message.from_user.username}" if message.from_user.username else str(uid)
                    await client.send_message(
                        OWNER_ID,
                        f"🆘 **تیکت پشتیبانی جدید #{ticket_id}**\n\n"
                        f"از: {uname} (`{uid}`)\n\n"
                        f"«{text}»\n\n"
                        f"پاسخ از: پنل مدیریتی → 🎫 تیکت‌های پشتیبانی"
                    )
                except Exception as e:
                    print(f"[Support] خطا در اطلاع‌رسانی به مالک: {e}")
            return

        if is_tenant_locked(uid):
            await message.reply(_locked_reply_text(uid), reply_markup=billing_kb())
            return

        if step == "login_phone":
            if not text.startswith("+"):
                await message.reply("❌ شماره باید با + شروع شود.\nمثال: `+989123456789`")
                return
            msg = await message.reply("⏳ در حال ارسال کد...")
            try:
                await send_code(text, uid)
                set_step(uid, "login_code", text)
                await msg.edit_text(f"✅ کد به `{text}` ارسال شد.\n\nکد دریافتی را وارد کنید:")
            except FloodWait as e:
                await msg.edit_text(f"❌ محدودیت. {e.value} ثانیه صبر کنید.")
                clear_step(uid)
            except Exception as e:
                await msg.edit_text(f"❌ خطا: `{e}`")
                clear_step(uid)

        elif step == "login_code":
            phone = get_step_data(uid)
            result, err = await sign_in(phone, code=text)
            if err == "2fa":
                set_step(uid, "login_2fa", phone)
                await message.reply("🔐 رمز دو مرحله‌ای را وارد کنید:")
                return
            await _handle_login_result(message, result, err, phone, uid)

        elif step == "login_2fa":
            phone = get_step_data(uid)
            result, err = await sign_in(phone, password=text)
            await _handle_login_result(message, result, err, phone, uid)

        elif step.startswith("set_bio_"):
            await _profile_action(message, step[8:], "bio", text, uid)

        elif step.startswith("set_fname_"):
            await _profile_action(message, step[10:], "fname", text, uid)

        elif step.startswith("set_lname_"):
            await _profile_action(message, step[10:], "lname", text, uid)

        elif step.startswith("set_uname_"):
            acc_id = step[10:]
            uname = text.lstrip("@")
            uc = await get_user_client(acc_id)
            if not uc:
                await message.reply("❌ اکانت در دسترس نیست.", reply_markup=manage_kb(acc_id))
                clear_step(uid); return
            try:
                await uc.start()
                await uc.set_username(uname)
                await uc.stop()
                u("UPDATE accounts SET username=%s WHERE id=%s", (uname, acc_id))
                await message.reply("✅ نام کاربری تنظیم شد.", reply_markup=manage_kb(acc_id))
            except (UsernameOccupied, UsernameInvalid) as e:
                await message.reply(f"❌ {e}", reply_markup=manage_kb(acc_id))
            except Exception as e:
                await message.reply(f"❌ خطا: {e}", reply_markup=manage_kb(acc_id))
            clear_step(uid)

        elif step.startswith("bn_text_"):
            _, _, acc_id, slot, ctx = step.split("_", 4)
            slot = int(slot)
            set_step(uid, f"bn_file_{acc_id}_{slot}_{ctx}", text)
            u("INSERT INTO banners (account_id,admin_id,slot,text,context) "
              "VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE text=%s",
              (acc_id, uid, slot, text, ctx, text))
            await message.reply(
                "📎 فایل پیوست بفرستید یا بدون فایل ادامه دهید:",
                reply_markup=back_kb(f"bn_back_{acc_id}_{ctx}")
            )

        elif step.startswith("gbn_text_"):
            _, _, target, slot = step.split("_", 3)
            slot = int(slot)
            layer_id = get_current_layer(uid)
            set_step(uid, f"gbn_file_{target}_{slot}", text)
            u("INSERT INTO global_banners (admin_id,target,slot,text,layer_id) "
              "VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE text=%s",
              (uid, target, slot, text, layer_id, text))
            await message.reply(
                "📎 فایل پیوست بفرستید یا بدون فایل ادامه دهید:",
                reply_markup=back_kb(f"gbn_back_{target}")
            )

        elif step.startswith("sgrp_"):
            acc_id = step[5:]
            set_step(uid, f"sgrp_confirm_{acc_id}", text)
            await message.reply(
                f"📢 متن:\n\n{text}\n\nارسال به همه گروه‌ها؟",
                reply_markup=confirm_kb(f"sgrp_go_{acc_id}", f"acc_manage_{acc_id}")
            )

        elif step.startswith("spv_"):
            acc_id = step[4:]
            set_step(uid, f"spv_confirm_{acc_id}", text)
            await message.reply(
                f"💬 متن:\n\n{text}\n\nارسال به همه پیوی‌ها؟",
                reply_markup=confirm_kb(f"spv_go_{acc_id}", f"acc_manage_{acc_id}")
            )

        elif step.startswith("ext_ch_"):
            # استخراج از یک لینکدونی
            acc_id = step[7:]
            ch = text.lstrip("@")
            set_step(uid, f"ext_cnt_{acc_id}", ch)
            await message.reply(
                "📩 چند پیام آخر بررسی شود؟ (۱ تا ۱۰۰۰):",
                reply_markup=back_kb(f"m_ext_{acc_id}")
            )

        elif step.startswith("ext_cnt_"):
            acc_id = step[8:]
            ch = get_step_data(uid)
            if not text.isdigit() or not (1 <= int(text) <= 1000):
                await message.reply("❌ عدد بین ۱ تا ۱۰۰۰ وارد کنید.")
                return
            msg = await message.reply("⏳ در حال استخراج...")
            links = await _extract_links(acc_id, ch, int(text))
            if not links:
                await msg.edit_text("🔍 لینکی یافت نشد.")
            else:
                out = "\n".join(links)
                if len(out) > 4000:
                    chunks = [out[i:i+4000] for i in range(0, len(out), 4000)]
                    await msg.delete()
                    for chunk in chunks:
                        await message.reply(chunk)
                else:
                    await msg.edit_text(out)
            clear_step(uid)

        elif step.startswith("ext_multi_ch_"):
            # استخراج از چند لینکدونی - دریافت آیدی‌ها
            acc_id = step[13:]
            channels = [c.strip().lstrip("@") for c in text.splitlines() if c.strip()]
            if not channels:
                await message.reply("❌ هیچ کانالی وارد نشد.")
                return
            set_step(uid, f"ext_multi_cnt_{acc_id}", "\n".join(channels))
            await message.reply(
                f"✅ {len(channels)} لینکدونی دریافت شد.\n\nچند لینک آخر از هر لینکدونی استخراج شود؟",
                reply_markup=back_kb(f"m_ext_{acc_id}")
            )

        elif step.startswith("ext_multi_cnt_"):
            acc_id = step[14:]
            channels = get_step_data(uid).splitlines()
            if not text.isdigit() or int(text) < 1:
                await message.reply("❌ عدد معتبر وارد کنید.")
                return
            limit = int(text)
            msg = await message.reply(f"⏳ استخراج {limit} لینک از {len(channels)} لینکدونی...")
            all_links = []
            for ch in channels:
                links = await _extract_links(acc_id, ch, limit)
                all_links.extend(links)
            # حذف تکراری
            all_links = list(dict.fromkeys(all_links))
            if not all_links:
                await msg.edit_text("🔍 لینکی یافت نشد.")
            else:
                out = "\n".join(all_links)
                if len(out) > 4000:
                    chunks = [out[i:i+4000] for i in range(0, len(out), 4000)]
                    await msg.delete()
                    for chunk in chunks:
                        await message.reply(chunk)
                else:
                    await msg.edit_text(out)
            clear_step(uid)

        elif step.startswith("join_"):
            acc_id = step[5:]
            links = [l.strip() for l in text.splitlines() if l.strip()]
            if not links:
                await message.reply("❌ لینکی وارد نشد.")
                return
            # ذخیره لینک‌ها و رفتن به مرحله انتخاب برچسب
            set_step(uid, f"join_tag_{acc_id}", "\n".join(links))
            layer_id = get_current_layer(uid)
            tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
            tag_list = [t[0] for t in tags]
            await message.reply(
                f"✅ **{len(links)} لینک دریافت شد**\n\nبرچسب گروه‌ها را انتخاب کنید:",
                reply_markup=tag_select_kb(tag_list, f"jointag_{acc_id}", show_all=False)
            )

        elif step.startswith("joindelay_"):
            acc_id = step[10:]
            parts = text.split()
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                await message.reply("❌ فرمت: MIN MAX (دقیقه)\nمثال: `3 7`")
                return
            mn, mx = int(parts[0])*60, int(parts[1])*60
            u("INSERT INTO join_settings (account_id,admin_id,min_delay,max_delay) "
              "VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE min_delay=%s,max_delay=%s",
              (acc_id, uid, mn, mx, mn, mx))
            await message.reply(f"✅ فاصله: {parts[0]}–{parts[1]} دقیقه", reply_markup=manage_kb(acc_id))
            clear_step(uid)

        elif step.startswith("sch_int_"):
            acc_id = step[8:]
            if not text.isdigit() or int(text) < 1:
                await message.reply("❌ عدد دقیقه وارد کنید."); return
            u("INSERT INTO scheduler (account_id,admin_id,interval_minutes) "
              "VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE interval_minutes=%s",
              (acc_id, uid, int(text), int(text)))
            await message.reply(f"✅ هر {text} دقیقه.", reply_markup=back_kb(f"m_sch_{acc_id}"))
            clear_step(uid)

        elif step.startswith("gsch_int_"):
            target = step[9:]
            if not text.isdigit() or int(text) < 1:
                await message.reply("❌ عدد دقیقه وارد کنید."); return
            layer_id = get_current_layer(uid)
            u("INSERT INTO global_scheduler (admin_id,target,layer_id,interval_minutes) "
              "VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE interval_minutes=%s",
              (uid, target, layer_id, int(text), int(text)))
            row = q("SELECT is_active,group_tag_filter,acc_tag_filter,max_rounds,current_round "
                    "FROM global_scheduler WHERE admin_id=%s AND target=%s AND layer_id=%s", (uid, target, layer_id))
            active = row[0][0] if row else 0
            gtag = (row[0][1] if row else None) or "ALL"
            atag = (row[0][2] if row else None) or "ALL"
            max_r = row[0][3] if row else 0
            cur_r = row[0][4] if row else 0
            await message.reply(f"✅ هر {text} دقیقه ارسال می‌شود.",
                                 reply_markup=global_sch_panel_kb(target, active, gtag=gtag,
                                     atag=atag, max_rounds=max_r, current_round=cur_r))
            clear_step(uid)

        elif step.startswith("gsch_rounds_"):
            target = step[12:]
            if not text.isdigit() or int(text) < 0:
                await message.reply("❌ عدد معتبر وارد کنید (۰ = نامحدود)."); return
            rounds = int(text)
            layer_id = get_current_layer(uid)
            u("INSERT INTO global_scheduler (admin_id,target,layer_id,max_rounds,current_round) "
              "VALUES(%s,%s,%s,%s,0) ON DUPLICATE KEY UPDATE max_rounds=%s, current_round=0",
              (uid, target, layer_id, rounds, rounds))
            row = q("SELECT is_active,group_tag_filter,acc_tag_filter,max_rounds,current_round "
                    "FROM global_scheduler WHERE admin_id=%s AND target=%s AND layer_id=%s", (uid, target, layer_id))
            active = row[0][0] if row else 0
            gtag = (row[0][1] if row else None) or "ALL"
            atag = (row[0][2] if row else None) or "ALL"
            max_r = row[0][3] if row else 0
            cur_r = row[0][4] if row else 0
            lbl = "نامحدود" if rounds == 0 else f"{rounds} دور"
            await message.reply(f"✅ تعداد دور: {lbl}",
                                 reply_markup=global_sch_panel_kb(target, active, gtag=gtag,
                                     atag=atag, max_rounds=max_r, current_round=cur_r))
            clear_step(uid)

        elif step == "g_bio":
            await _global_profile(message, "bio", text, uid)
        elif step == "g_fname":
            await _global_profile(message, "fname", text, uid)
        elif step == "g_lname":
            await _global_profile(message, "lname", text, uid)

        elif step == "layer_new":
            name = text.strip()
            if not name or len(name) > 50:
                await message.reply("❌ نام لایه باید بین ۱ تا ۵۰ کاراکتر باشد."); return
            max_layers = get_plan_limits(uid)[1]
            if max_layers and count_layers(uid) >= max_layers:
                clear_step(uid)
                await message.reply(
                    f"🚫 **سقف لایه‌ی پلن شما پر شده** ({max_layers} لایه).\n\n"
                    "برای ساخت لایه‌ی بیشتر، پلن‌تون رو ارتقا بدید.",
                    reply_markup=billing_kb()
                )
                return
            try:
                u("INSERT INTO layers (admin_id,name) VALUES(%s,%s)", (uid, name))
                new_lyr = q("SELECT id FROM layers WHERE admin_id=%s AND name=%s", (uid, name))
                new_layer_id = new_lyr[0][0]
                u("UPDATE admins SET current_layer_id=%s WHERE id=%s", (new_layer_id, uid))
                await message.reply(
                    f"✅ لایه‌ی «{name}» ساخته و فعال شد.\n\nیک گزینه را انتخاب کنید:",
                    reply_markup=main_menu_kb()
                )
            except Exception:
                await message.reply(f"❌ لایه‌ای با نام «{name}» از قبل وجود دارد.")
            clear_step(uid)

        elif step.startswith("layer_ren_"):
            layer_id = step[10:]
            new_name = text.strip()
            if not new_name or len(new_name) > 50:
                await message.reply("❌ نام لایه باید بین ۱ تا ۵۰ کاراکتر باشد."); return
            try:
                u("UPDATE layers SET name=%s WHERE id=%s AND admin_id=%s",
                  (new_name, layer_id, uid))
                from keyboards import layer_manage_kb
                await message.reply(
                    f"✅ نام لایه به «{new_name}» تغییر کرد.",
                    reply_markup=layer_manage_kb(layer_id)
                )
            except Exception:
                await message.reply(f"❌ لایه‌ای با نام «{new_name}» از قبل وجود دارد.")
            clear_step(uid)

        elif step.startswith("tag_new_"):
            context = step[8:]
            category = "accounts" if context == "accounts" else "groups"
            layer_id = get_current_layer(uid)
            tag_name = text.strip()
            if not tag_name or len(tag_name) > 50:
                await message.reply("❌ نام برچسب باید بین ۱ تا ۵۰ کاراکتر باشد."); return
            try:
                u("INSERT INTO tags (admin_id,name,category,layer_id) VALUES(%s,%s,%s,%s)",
                  (uid, tag_name, category, layer_id))
                if category == "accounts":
                    from keyboards import account_tag_kb
                    accs = q(
                        "SELECT a.id, a.name, a.phone, "
                        "GROUP_CONCAT(at.tag_name ORDER BY at.tag_name SEPARATOR ', ') "
                        "FROM accounts a "
                        "LEFT JOIN account_tags at ON at.account_id=a.id AND at.admin_id=a.admin_id "
                        "WHERE a.admin_id=%s AND a.layer_id=%s GROUP BY a.id, a.name, a.phone",
                        (uid, layer_id)
                    )
                    await message.reply(f"✅ برچسب «{tag_name}» ساخته شد.\n\nحالا اکانتی رو انتخاب کن تا بهش این برچسب رو بزنی:",
                                         reply_markup=account_tag_kb(accs))
                else:
                    tags = q("SELECT DISTINCT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name",
                             (uid, layer_id))
                    tag_list = [t[0] for t in tags]
                    await message.reply(f"✅ برچسب «{tag_name}» ساخته شد.",
                                         reply_markup=tags_list_kb(tag_list, context))
            except Exception:
                await message.reply(f"❌ برچسب «{tag_name}» از قبل وجود دارد.")
            clear_step(uid)

        elif step == "g_sgrp":
            set_step(uid, "g_sgrp_confirm", text)
            await message.reply(
                f"📢 ارسال به گروه‌های **همه اکانت‌ها**:\n\n{text}\n\nتایید؟",
                reply_markup=confirm_kb("g_sgrp_go", "menu_global")
            )

        elif step == "ld_add_source":
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                await message.reply("❌ هیچ لینکدونی‌ای وارد نشد.")
                return
            set_stop(False)
            await message.reply(f"🚀 افزودن {len(lines)} لینکدونی شروع شد...")
            asyncio.create_task(_add_linkdoni_sources_bulk(client, lines, uid))
            clear_step(uid)

        elif step == "ld_interval":
            if not text.isdigit() or not (1 <= int(text) <= 168):
                await message.reply("❌ عدد بین ۱ تا ۱۶۸ وارد کنید.")
                return
            val = int(text)
            u("INSERT INTO linkdoni_settings (admin_id, scan_interval_hours) "
              "VALUES (%s,%s) ON DUPLICATE KEY UPDATE scan_interval_hours=%s",
              (uid, val, val))
            row = q("SELECT auto_scan, scan_interval_hours, auto_join, join_mode, join_tag "
                    "FROM linkdoni_settings WHERE admin_id=%s", (uid,))
            from keyboards import ld_settings_kb
            if row:
                kb = ld_settings_kb(row[0][0], row[0][1], row[0][2],
                                    row[0][3], row[0][4] or "")
            else:
                kb = ld_settings_kb(0, val, 0, "split", "")
            await message.reply(f"✅ فاصله اسکن: هر {val} ساعت", reply_markup=kb)
            clear_step(uid)

        elif step == "ld_tag":
            tag_val = text.strip() if text.strip() else ""
            u("INSERT INTO linkdoni_settings (admin_id, join_tag) "
              "VALUES (%s,%s) ON DUPLICATE KEY UPDATE join_tag=%s",
              (uid, tag_val, tag_val))
            row = q("SELECT auto_scan, scan_interval_hours, auto_join, join_mode, join_tag "
                    "FROM linkdoni_settings WHERE admin_id=%s", (uid,))
            from keyboards import ld_settings_kb
            if row:
                kb = ld_settings_kb(row[0][0], row[0][1], row[0][2],
                                    row[0][3], row[0][4] or "")
            else:
                kb = ld_settings_kb(0, 6, 0, "split", tag_val)
            lbl = f"«{tag_val}»" if tag_val else "بدون برچسب"
            await message.reply(f"✅ برچسب ذخیره شد: {lbl}", reply_markup=kb)
            clear_step(uid)

        elif step == "g_pvjoin_interval":
            if not text.isdigit() or not (1 <= int(text) <= 24):
                await message.reply("❌ عدد بین ۱ تا ۲۴ وارد کنید.")
                return
            val = int(text)
            u(
                "INSERT INTO pv_join_settings (admin_id, scan_interval_hours) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE scan_interval_hours=%s",
                (uid, val, val)
            )
            st_row = q(
                "SELECT auto_scan, scan_interval_hours, daily_limit "
                "FROM pv_join_settings WHERE admin_id=%s",
                (uid,)
            )
            from keyboards import pv_join_settings_kb
            await message.reply(
                f"✅ فاصله اسکن خودکار: {val} ساعت",
                reply_markup=pv_join_settings_kb(
                    st_row[0][0] if st_row else 0,
                    st_row[0][1] if st_row else val,
                    st_row[0][2] if st_row else 20
                )
            )
            clear_step(uid)

        elif step == "g_pvjoin_limit":
            if not text.isdigit() or not (1 <= int(text) <= 100):
                await message.reply("❌ عدد بین ۱ تا ۱۰۰ وارد کنید.")
                return
            val = int(text)
            u(
                "INSERT INTO pv_join_settings (admin_id, daily_limit) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE daily_limit=%s",
                (uid, val, val)
            )
            st_row = q(
                "SELECT auto_scan, scan_interval_hours, daily_limit "
                "FROM pv_join_settings WHERE admin_id=%s",
                (uid,)
            )
            from keyboards import pv_join_settings_kb
            await message.reply(
                f"✅ سقف روزانه جوین: {val} لینک",
                reply_markup=pv_join_settings_kb(
                    st_row[0][0] if st_row else 0,
                    st_row[0][1] if st_row else 6,
                    st_row[0][2] if st_row else val
                )
            )
            clear_step(uid)

        elif step == "g_spv":
            set_step(uid, "g_spv_confirm", text)
            await message.reply(
                f"💬 ارسال به پیوی‌های **همه اکانت‌ها**:\n\n{text}\n\nتایید؟",
                reply_markup=confirm_kb("g_spv_go", "menu_global")
            )

        elif step == "g_join":

            links = [l.strip() for l in text.splitlines() if l.strip()]
            if not links:
                await message.reply("❌ لینکی وارد نشد.")
                return

            # بررسی تکراری بودن لینک‌ها با جدول used_links
            new_links, dup_links = _check_duplicate_links(links, uid)

            if dup_links:
                set_step(uid, "g_join_dup_check", json.dumps({
                    "all": links,
                    "new": new_links,
                    "dup": dup_links
                }))
                await message.reply(
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
            else:
                set_step(uid, "g_join_tag", "\n".join(links))
                layer_id = get_current_layer(uid)
                tags = q("SELECT name FROM tags WHERE admin_id=%s AND category='groups' AND layer_id=%s ORDER BY name", (uid, layer_id))
                tag_list = [t[0] for t in tags]
                await message.reply(
                    f"✅ {len(links)} لینک دریافت شد.\nبرچسب گروه‌ها را انتخاب کنید:",
                    reply_markup=tag_select_kb(tag_list, "gjointag", show_all=False)
                )

        elif step.startswith("rr_badd_"):
            acc_id = step[8:]
            # پیدا کردن slot بعدی
            rows = q("SELECT MAX(slot) FROM reply_rand_banners WHERE account_id=%s", (acc_id,))
            next_slot = (rows[0][0] or 0) + 1
            u("INSERT INTO reply_rand_banners (account_id,admin_id,slot,text) VALUES(%s,%s,%s,%s)",
              (acc_id, uid, next_slot, text))
            bnrs = q("SELECT slot,text,file_id FROM reply_rand_banners WHERE account_id=%s ORDER BY slot", (acc_id,))
            back = "g_rr" if acc_id.startswith("global") else f"m_reply_{acc_id}"
            await message.reply(f"✅ متن {next_slot} اضافه شد.",
                                 reply_markup=reply_banner_list_kb(acc_id, bnrs, back_to=back))
            clear_step(uid)

        elif step.startswith("rr_int_"):
            acc_id = step[7:]
            if not text.isdigit() or int(text) < 1:
                await message.reply("❌ عدد دقیقه وارد کنید."); return
            u("INSERT INTO reply_rand (account_id,admin_id,interval_minutes) VALUES(%s,%s,%s) "
              "ON DUPLICATE KEY UPDATE interval_minutes=%s", (acc_id, uid, int(text), int(text)))
            row = q("SELECT is_active FROM reply_rand WHERE account_id=%s", (acc_id,))
            active = row[0][0] if row else 0
            back = "menu_global" if acc_id.startswith("global") else None
            await message.reply(f"✅ هر {text} دقیقه ریپلای.", reply_markup=reply_rand_kb(acc_id, active, back_to=back))
            clear_step(uid)

        elif step.startswith("rc_int_"):
            acc_id = step[7:]
            if not text.isdigit() or int(text) < 1:
                await message.reply("❌ عدد دقیقه وارد کنید."); return
            u("INSERT INTO react_rand (account_id,admin_id,interval_minutes) VALUES(%s,%s,%s) "
              "ON DUPLICATE KEY UPDATE interval_minutes=%s", (acc_id, uid, int(text), int(text)))
            row = q("SELECT is_active FROM react_rand WHERE account_id=%s", (acc_id,))
            active = row[0][0] if row else 0
            back = "menu_global" if acc_id.startswith("global") else None
            await message.reply(f"✅ هر {text} دقیقه ری‌اکت.", reply_markup=react_rand_kb(acc_id, active, back_to=back))
            clear_step(uid)


    @app.on_message(filters.private & (filters.photo | filters.video | filters.document))
    async def on_media(client, message):
        uid = message.from_user.id
        step = get_step(uid)

        if step == "awaiting_receipt":
            plan_id_raw = get_step_data(uid)
            row = q("SELECT name, price FROM plans WHERE id=%s", (plan_id_raw,))
            if not row or not message.photo:
                clear_step(uid)
                return
            name, price = row[0]
            payment_id = u(
                "INSERT INTO payments (tenant_id, plan_id, amount, method, status) "
                "VALUES (%s,%s,%s,'manual','pending')",
                (uid, int(plan_id_raw), price)
            )
            clear_step(uid)
            await message.reply(
                "✅ رسید دریافت شد. بعد از تایید ادمین، اشتراکت فعال می‌شه "
                "(معمولاً کمتر از چند ساعت).",
                reply_markup=main_menu_kb()
            )
            if OWNER_ID:
                try:
                    uname = f"@{message.from_user.username}" if message.from_user.username else str(uid)
                    await client.send_photo(
                        OWNER_ID, message.photo.file_id,
                        caption=(
                            f"💳 **رسید پرداخت جدید**\n\n"
                            f"از: {uname} (`{uid}`)\n"
                            f"پلن: {name} — {price:,} تومان\n"
                            f"شماره پرداخت: #{payment_id}\n\n"
                            f"برای تایید/رد: پنل مدیریتی → 💳 پرداخت‌های در انتظار"
                        )
                    )
                except Exception as e:
                    print(f"[Billing] خطا در اطلاع‌رسانی به مالک: {e}")
            return

        if is_tenant_locked(uid):
            await message.reply(_locked_reply_text(uid), reply_markup=billing_kb())
            return

        if step.startswith("gbn_file_"):
            _, _, target, slot = step.split("_", 3)
            slot = int(slot)
            layer_id = get_current_layer(uid)
            if message.photo:
                fid, ftype = message.photo.file_id, "photo"
            elif message.video:
                fid, ftype = message.video.file_id, "video"
            elif message.document:
                fid, ftype = message.document.file_id, "document"
            else:
                return
            u("UPDATE global_banners SET file_id=%s, file_type=%s "
              "WHERE admin_id=%s AND target=%s AND slot=%s AND layer_id=%s",
              (fid, ftype, uid, target, slot, layer_id))
            await message.reply("✅ پیام با فایل ذخیره شد.", reply_markup=back_kb(f"gbn_back_{target}"))
            clear_step(uid)
            return

        if not step.startswith("bn_file_"):
            return
        _, _, acc_id, slot, ctx = step.split("_", 4)
        slot = int(slot)
        if message.photo:
            fid, ftype = message.photo.file_id, "photo"
        elif message.video:
            fid, ftype = message.video.file_id, "video"
        elif message.document:
            fid, ftype = message.document.file_id, "document"
        else:
            return
        u("UPDATE banners SET file_id=%s, file_type=%s "
          "WHERE account_id=%s AND slot=%s AND context=%s",
          (fid, ftype, acc_id, slot, ctx))
        await message.reply("✅ بنر با فایل ذخیره شد.", reply_markup=back_kb(f"bn_back_{acc_id}_{ctx}"))
        clear_step(uid)


# ─── helpers ───────────────────────────────────────────────────

async def _handle_login_result(message, result, err, phone, admin_id):
    if err:
        errs = {
            "bad_code": "❌ کد اشتباه است.",
            "expired_code": "❌ کد منقضی شد. دوباره /add_account بزنید.",
            "expired": "❌ جلسه منقضی شد. دوباره /add_account بزنید.",
            "bad_pass": "❌ پسورد اشتباه است.",
        }
        if err.startswith("flood:"):
            await message.reply(f"❌ محدودیت. {err.split(':')[1]} ثانیه صبر کنید.")
        else:
            await message.reply(errs.get(err, f"❌ خطا: {err}"))
        if err in ("expired_code", "expired"):
            clear_step(admin_id)
        return
    me, ss = result
    save_account(me, ss, phone, admin_id)
    cnt = q("SELECT COUNT(*) FROM accounts WHERE admin_id=%s AND layer_id=%s",
            (admin_id, get_current_layer(admin_id)))[0][0]
    await message.reply(
        f"✅ **اکانت اضافه شد!**\n\n"
        f"👤 {me.first_name or ''} {me.last_name or ''}\n"
        f"📱 `{phone}`\n🤖 تعداد تبچیزها: `{cnt}`",
        reply_markup=main_menu_kb()
    )
    clear_step(admin_id)

async def _profile_action(message, acc_id, action, value, admin_id):
    uc = await get_user_client(acc_id)
    if not uc:
        await message.reply("❌ اکانت در دسترس نیست.", reply_markup=manage_kb(acc_id))
        clear_step(admin_id); return
    try:
        await uc.start()
        me = await uc.get_me()
        if action == "bio":
            await uc.update_profile(bio=value)
        elif action == "fname":
            await uc.update_profile(first_name=value, last_name=me.last_name or "")
            u("UPDATE accounts SET name=%s WHERE id=%s", (value, acc_id))
        elif action == "lname":
            await uc.update_profile(first_name=me.first_name or "", last_name=value)
        await uc.stop()
        await message.reply("✅ تنظیم شد.", reply_markup=manage_kb(acc_id))
    except Exception as e:
        await message.reply(f"❌ خطا: {e}", reply_markup=manage_kb(acc_id))
    clear_step(admin_id)

async def _add_linkdoni_sources_bulk(bot_client, lines, admin_id):
    """افزودن دسته‌ای لینکدونی‌ها (مشابه عضویت گروهی) — مخصوص لایه‌ی فعلی"""
    import random as _random
    layer_id = get_current_layer(admin_id)
    accs_ld = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                (admin_id, layer_id))
    if not accs_ld:
        await bot_client.send_message(admin_id, "❌ هیچ اکانت فعالی تو این لایه وجود ندارد.")
        return

    acc_id_ld = str(_random.choice(accs_ld)[0])
    uc_ld = await get_user_client(acc_id_ld)
    if not uc_ld:
        await bot_client.send_message(admin_id, "❌ اکانت در دسترس نیست.")
        return

    ok, fail = 0, 0
    try:
        await uc_ld.start()
        for i, raw in enumerate(lines, 1):
            if is_stopped():
                await bot_client.send_message(admin_id, "🛑 عملیات توسط کاربر متوقف شد.")
                break

            raw = raw.strip()
            if raw.startswith("https://t.me/"):
                chat_input = raw.split("t.me/")[-1].strip("/").split("?")[0]
            elif raw.startswith("@"):
                chat_input = raw.lstrip("@")
            elif raw.lstrip("-").isdigit():
                chat_input = raw
            else:
                chat_input = raw.lstrip("@")

            try:
                target_ld = int(chat_input)
            except ValueError:
                target_ld = chat_input

            try:
                chat_info = await uc_ld.get_chat(target_ld)
                real_id = str(chat_info.id)
                title = getattr(chat_info, 'title', '') or chat_input
            except FloodWait as e:
                await bot_client.send_message(
                    admin_id,
                    f"❗️ محدودیت تلگرام {e.value} ثانیه؛ صبر می‌کند و ادامه می‌دهد..."
                )
                await asyncio.sleep(e.value)
                try:
                    chat_info = await uc_ld.get_chat(target_ld)
                    real_id = str(chat_info.id)
                    title = getattr(chat_info, 'title', '') or chat_input
                except Exception:
                    real_id = chat_input
                    title = chat_input
            except Exception:
                real_id = chat_input
                title = chat_input

            try:
                u("INSERT IGNORE INTO linkdoni_sources "
                  "(admin_id, chat_id, chat_title, source_link, layer_id) VALUES (%s,%s,%s,%s,%s)",
                  (admin_id, real_id, title[:200], raw[:300], layer_id))
                ok += 1
                await bot_client.send_message(admin_id, f"✅ [{i}/{len(lines)}] افزوده شد: {title}")
            except Exception as ex:
                fail += 1
                await bot_client.send_message(admin_id, f"❌ [{i}/{len(lines)}] خطا در «{raw}»: {ex}")

        try:
            await uc_ld.stop()
        except Exception:
            pass
    except Exception as ex:
        try:
            await uc_ld.stop()
        except Exception:
            pass
        print(f"[LinkdoniBulkAdd] {ex}")

    from keyboards import ld_sources_kb
    srcs = q("SELECT id, chat_id, chat_title, is_active "
             "FROM linkdoni_sources WHERE admin_id=%s AND layer_id=%s ORDER BY added_at DESC",
             (admin_id, layer_id))
    src_list = [{"id": r[0], "chat_id": r[1],
                 "chat_title": r[2], "is_active": r[3]} for r in srcs]
    await bot_client.send_message(
        admin_id,
        f"📋 **پایان افزودن دسته‌ای لینکدونی‌ها**\n✅ موفق: {ok}\n❌ ناموفق: {fail}",
        reply_markup=ld_sources_kb(src_list)
    )


async def _extract_links(acc_id, channel, limit):
    uc = await get_user_client(acc_id)
    if not uc:
        return []
    pattern = re.compile(r'https?://t\.me/[^\s\]\)\"\']+')
    links = []
    BATCH = 200
    offset_id = 0
    try:
        await uc.start()
        while len(links) < limit:
            batch = []
            async for msg in uc.get_chat_history(channel, limit=BATCH, offset_id=offset_id):
                batch.append(msg)

            if not batch:
                # تاریخچهٔ کانال تمام شده
                break

            for msg in batch:
                # ۱. متن پیام
                txt = (msg.text or "") + " " + (msg.caption or "")
                links += pattern.findall(txt)

                # ۲. entities (لینک‌های کلیک‌پذیر داخل متن)
                for entities in [msg.entities or [], msg.caption_entities or []]:
                    for e in entities:
                        if hasattr(e, 'url') and e.url:
                            links += pattern.findall(e.url)

                # ۳. دکمه‌های inline
                if msg.reply_markup:
                    try:
                        kb = msg.reply_markup
                        rows = getattr(kb, 'inline_keyboard', None)
                        if rows:
                            for row in rows:
                                for btn in row:
                                    url = getattr(btn, 'url', None)
                                    if url:
                                        links += pattern.findall(url)
                    except Exception:
                        pass

                # ۴. web preview
                if msg.web_page and hasattr(msg.web_page, 'url') and msg.web_page.url:
                    links += pattern.findall(msg.web_page.url)

            # برای صفحهٔ بعدی، از آخرین پیام این batch ادامه بده
            offset_id = batch[-1].id

            if len(batch) < BATCH:
                # یعنی به انتهای تاریخچهٔ کانال رسیدیم
                break

        await uc.stop()
    except Exception as ex:
        print(f"[ExtractLinks] {ex}")
        try:
            await uc.stop()
        except Exception:
            pass

    # پاکسازی: حذف کاراکترهای اضافی از انتها + حذف تکراری
    cleaned = []
    for lnk in links:
        lnk = lnk.rstrip('.,;:!?)\"\'')
        if lnk not in cleaned:
            cleaned.append(lnk)

    return cleaned[:limit]


async def _join_links(bot_client, acc_id, links, min_d, max_d, tag=""):
    """عضویت در لینک‌ها با هندل کامل خطاها + ذخیره برچسب"""
    admin_id = get_account_owner(acc_id)
    uc = await get_user_client(acc_id)
    if not uc:
        return
    await uc.start()
    me = await uc.get_me()
    acc_display = me.phone_number or str(me.id)
    ok_links, fail_links = [], []
    row = q("SELECT auto_leave_limited FROM accounts WHERE id=%s", (acc_id,))
    auto_leave = row[0][0] if row else 0
    lyr_row = q("SELECT layer_id FROM accounts WHERE id=%s", (acc_id,))
    acc_layer_id = lyr_row[0][0] if lyr_row and lyr_row[0][0] else 0

    for i, link in enumerate(links, 1):
        if is_stopped():
            await bot_client.send_message(admin_id, "🛑 عملیات توسط کاربر متوقف شد.")
            break

        # تبدیل لینک به فرمت قابل استفاده برای Pyrogram
        link_clean = link.strip()
        if link_clean.startswith("@"):
            target = link_clean.lstrip("@")
        elif "t.me/+" in link_clean or "t.me/joinchat/" in link_clean:
            # لینک دعوت خصوصی — کامل بده
            target = link_clean
        elif "t.me/" in link_clean:
            # لینک عمومی — فقط username بگیر
            target = link_clean.split("t.me/")[-1].strip("/").split("?")[0]
        else:
            target = link_clean
        try:
            result = await uc.join_chat(target)
            ok_links.append(link)
            # ذخیره گروه با برچسب
            if result and hasattr(result, 'id'):
                chat_title = getattr(result, 'title', '') or ''
                u("INSERT INTO group_tags (admin_id,account_id,chat_id,chat_title,tag_name,layer_id) "
                  "VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE tag_name=%s, chat_title=%s",
                  (admin_id, acc_id, result.id, chat_title, tag, acc_layer_id, tag, chat_title))
                # چک عضویت اجباری بات‌محور (ربات گروه ممکنه بخواد کانال دیگه‌ای رو هم جوین کنیم)
                try:
                    fj_result = await detect_and_handle_bot_forced_join(uc, result.id)
                    if fj_result.get("forced_join_detected"):
                        channel = fj_result.get("channel", "")
                        joined = fj_result.get("joined", False)
                        if joined:
                            await bot_client.send_message(
                                admin_id,
                                f"🔗 **عضویت اجباری تشخیص داده شد**\n"
                                f"گروه: `{link}`\n"
                                f"اکانت: `{acc_display}`\n"
                                f"کانال اجباری: @{channel}\n"
                                f"✅ عضو کانال شدید"
                            )
                        else:
                            await bot_client.send_message(
                                admin_id,
                                f"⚠️ **عضویت اجباری تشخیص داده شد**\n"
                                f"گروه: `{link}`\n"
                                f"اکانت: `{acc_display}`\n"
                                f"کانال اجباری: @{channel}\n"
                                f"❌ عضویت در کانال ناموفق بود"
                            )
                except Exception as fj_err:
                    print(f"[JoinLinks] خطا در تشخیص عضویت اجباری: {fj_err}")
            await bot_client.send_message(admin_id, f"✅ [{i}/{len(links)}] عضو شد: `{link}`")

        except FloodWait as e:
            wait_s = e.value
            safe_s = int(wait_s * 3.5)
            await bot_client.send_message(
                admin_id,
                f"❗️ محدودیت تلگرام {wait_s} ثانیه\n"
                f"پس از {safe_s} ثانیه ادامه می‌دهد\n👤 {acc_display}"
            )
            await asyncio.sleep(safe_s)
            try:
                result = await uc.join_chat(target)
                ok_links.append(link)
                if result and hasattr(result, 'id'):
                    chat_title = getattr(result, 'title', '') or ''
                    u("INSERT INTO group_tags (admin_id,account_id,chat_id,chat_title,tag_name,layer_id) "
                      "VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE tag_name=%s, chat_title=%s",
                      (admin_id, acc_id, result.id, chat_title, tag, acc_layer_id, tag, chat_title))
            except Exception:
                fail_links.append(link)

        except UserAlreadyParticipant:
            ok_links.append(link)

        except InviteRequestSent:
            ok_links.append(link)
            await bot_client.send_message(
                admin_id,
                f"⏳ **درخواست عضویت ارسال شد**\n"
                f"گروه: `{link}`\n"
                f"اکانت: `{acc_display}`\n"
                f"منتظر تأیید ادمین گروه..."
            )

        except (InviteHashExpired, InviteHashInvalid):
            fail_links.append(link)
            await bot_client.send_message(admin_id, f"❌ [{i}/{len(links)}] لینک منقضی: `{link}`")

        except ChannelsTooMuch:
            fail_links.append(link)
            await bot_client.send_message(admin_id, f"⛔️ اکانت پر شده. متوقف شد.")
            break

        except Exception as e:
            fail_links.append(link)
            await bot_client.send_message(admin_id, f"❌ [{i}/{len(links)}] خطا: `{link}`\n{e}")

        if i < len(links) and not is_stopped():
            delay = random.randint(min_d, max_d)
            await bot_client.send_message(
                admin_id, f"⏳ صبر {delay//60}دقیقه {delay%60}ثانیه...")
            await asyncio.sleep(delay)

    await uc.stop()
    # ذخیره لینک‌های موفق در used_links تا دفعه بعد تکراری شناخته بشن
    if ok_links:
        _save_used_links(ok_links, admin_id)
        # آپدیت وضعیت joined در linkdoni_links
        for lnk in ok_links:
            h = _link_hash(lnk)
            try:
                u("UPDATE linkdoni_links SET joined=1 "
                  "WHERE admin_id=%s AND link_hash=%s", (admin_id, h))
            except Exception:
                pass
    tag_lbl = f" — 🏷 {tag}" if tag else ""
    report = (f"✅ عملیات عضویت تمام شد{tag_lbl}\n👤 {acc_display}\n"
              f"موفق: {len(ok_links)}\nناموفق: {len(fail_links)}")
    if fail_links:
        report += "\n\n❗️ ناموفق‌ها:\n" + "\n".join(fail_links)
    await bot_client.send_message(admin_id, report)


def get_filtered_accounts(tag_filter, admin_id):
    """اکانت‌های فیلترشده بر اساس برچسب (فقط در لایه‌ی فعلی)"""
    layer_id = get_current_layer(admin_id)
    if tag_filter == "ALL":
        return q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                  (admin_id, layer_id))
    elif tag_filter == "NOTAG":
        return q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s "
                 "AND (tag='' OR tag IS NULL)", (admin_id, layer_id))
    else:
        return q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s AND tag=%s",
                 (admin_id, layer_id, tag_filter))

def get_filtered_chat_ids(acc_id, tag_filter):
    """
    chat_id های فیلترشده برای یک اکانت.
    خروجی: (mode, set)
      mode='all'      → فیلتری نیست، همه گروه‌ها
      mode='include'  → فقط chat_id های داخل set
      mode='exclude'  → همه گروه‌ها به‌جز chat_id های داخل set (برای NOTAG)
    """
    if tag_filter == "ALL":
        return ("all", None)
    admin_id = get_account_owner(acc_id)
    if tag_filter == "NOTAG":
        # گروه‌هایی که برچسب غیرخالی دارن رو exclude می‌کنیم؛ بقیه (چه ثبت نشده چه خالی) NOTAG حساب میشن
        rows = q("SELECT chat_id FROM group_tags WHERE admin_id=%s AND account_id=%s "
                 "AND tag_name<>'' AND tag_name IS NOT NULL", (admin_id, acc_id))
        return ("exclude", set(r[0] for r in rows))
    else:
        rows = q("SELECT chat_id FROM group_tags WHERE admin_id=%s AND account_id=%s AND tag_name=%s",
                 (admin_id, acc_id, tag_filter))
        return ("include", set(r[0] for r in rows))


def _chat_allowed(chat_id, filter_result):
    mode, ids = filter_result
    if mode == "all":
        return True
    if mode == "include":
        return chat_id in ids
    if mode == "exclude":
        return chat_id not in ids
    return True


async def send_to_groups_smart(bot_client, acc_id, text, force_join=False, group_tag_filter="ALL"):
    """ارسال پیام هوشمند با تشخیص محدودیت و عضویت اجبار"""
    from pyrogram import enums as en
    admin_id = get_account_owner(acc_id)
    uc = await get_user_client(acc_id)
    if not uc:
        return {"ok": 0, "fail": 0, "limited": 0, "force_joined": 0, "left": 0}

    me_info = q("SELECT phone, auto_leave_limited FROM accounts WHERE id=%s", (acc_id,))
    display = me_info[0][0] if me_info else acc_id
    auto_leave = me_info[0][1] if me_info else 0
    row_fj = q("SELECT force_join_active FROM join_settings WHERE account_id=%s", (acc_id,))
    do_force_join = row_fj[0][0] if row_fj else 0

    # فیلتر گروه‌ها بر اساس برچسب
    filter_result = get_filtered_chat_ids(acc_id, group_tag_filter)

    ok = fail = limited = force_joined = left = 0

    try:
        await uc.start()
    except Exception as e:
        print(f"[SendGroups] خطا در اتصال اکانت {acc_id}: {e}")
        await bot_client.send_message(admin_id, f"❌ اتصال اکانت {display} ناموفق بود: {e}")
        return {"ok": 0, "fail": 0, "limited": 0, "force_joined": 0, "left": 0,
                "display": display, "skipped": True}

    try:
        dialogs = []
        async for dlg in uc.get_dialogs():
            if dlg.chat.type not in (en.ChatType.GROUP, en.ChatType.SUPERGROUP):
                continue
            # اعمال فیلتر برچسب
            if not _chat_allowed(dlg.chat.id, filter_result):
                continue
            dialogs.append(dlg)
    except Exception as e:
        print(f"[SendGroups] خطا در گرفتن لیست گروه‌ها برای {acc_id}: {e}")
        try: await uc.stop()
        except Exception: pass
        await bot_client.send_message(admin_id, f"❌ خطا در خواندن گروه‌های {display}: {e}")
        return {"ok": 0, "fail": 0, "limited": 0, "force_joined": 0, "left": 0,
                "display": display, "skipped": True}

    for dlg in dialogs:
        if is_stopped():
            break
        try:
            await uc.send_message(dlg.chat.id, text)
            ok += 1
            # تشخیص عضویت اجباری بات‌محور (نه از طرف Telegram بلکه ربات گروه)
            if do_force_join:
                fj_result = await detect_and_handle_bot_forced_join(uc, dlg.chat.id, original_text=text)
                if fj_result.get("forced_join_detected") and fj_result.get("resent"):
                    force_joined += 1
            # فاصله تصادفی بین گروه‌ها
            await asyncio.sleep(random.uniform(1.5, 4))

        except (ChatWriteForbidden, UserBannedInChannel, ChatRestricted,
                ChatSendMediaForbidden) as e:
            err_str = str(e)
            fj_match = re.search(r'@([\w]+)|t\.me/([\w+]+)', err_str)
            if do_force_join and fj_match:
                ch = fj_match.group(1) or fj_match.group(2)
                try:
                    await uc.join_chat(ch)
                    force_joined += 1
                    await asyncio.sleep(2)
                    await uc.send_message(dlg.chat.id, text)
                    ok += 1
                except Exception:
                    if auto_leave:
                        try:
                            await uc.leave_chat(dlg.chat.id); left += 1
                        except Exception: pass
                    limited += 1
            elif auto_leave:
                try:
                    await uc.leave_chat(dlg.chat.id); left += 1
                except Exception: pass
                limited += 1
            else:
                limited += 1

        except SlowmodeWait:
            limited += 1

        except FloodWait as e:
            # صبر می‌کنیم و همین گروه را دوباره امتحان می‌کنیم، بقیه گروه‌ها رو ول نمی‌کنیم
            wait_s = min(e.value, 120)
            await bot_client.send_message(
                admin_id, f"❗️ محدودیت {wait_s} ثانیه — صبر و ادامه\n👤 {display}"
            )
            await asyncio.sleep(wait_s)
            try:
                await uc.send_message(dlg.chat.id, text); ok += 1
            except Exception: fail += 1

        except RPCError as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ["forbidden","banned","restricted","rights"]):
                if auto_leave:
                    try:
                        await uc.leave_chat(dlg.chat.id); left += 1
                    except Exception: pass
                limited += 1
            else:
                fail += 1

        except Exception:
            fail += 1

    await uc.stop()
    return {"ok": ok, "fail": fail, "limited": limited,
            "force_joined": force_joined, "left": left, "display": display}


async def _global_profile(message, action, value, admin_id):
    layer_id = get_current_layer(admin_id)
    accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
             (admin_id, layer_id))
    ok = fail = 0
    for (aid,) in accs:
        uc = await get_user_client(aid)
        if not uc:
            fail += 1; continue
        try:
            await uc.start()
            me = await uc.get_me()
            if action == "bio":
                await uc.update_profile(bio=value)
            elif action == "fname":
                await uc.update_profile(first_name=value, last_name=me.last_name or "")
            elif action == "lname":
                await uc.update_profile(first_name=me.first_name or "", last_name=value)
            await uc.stop()
            ok += 1
        except Exception:
            fail += 1
    await message.reply(
        f"✅ کامل شد:\n✔️ موفق: {ok}\n❌ ناموفق: {fail}",
        reply_markup=global_kb()
    )
    clear_step(admin_id)


# ─── توابع کمکی تشخیص لینک تکراری ────────────────────────────

def _normalize_link(link: str) -> str:
    """نرمال‌سازی لینک: lowercase + حذف پارامترهای اضافه مثل ?start=..."""
    link = link.strip().lower()
    link = link.split("?")[0]   # حذف query string
    link = link.rstrip("/")     # حذف slash انتهایی
    return link


def _link_hash(link: str) -> str:
    """تبدیل لینک نرمال‌شده به hash برای ذخیره در دیتابیس"""
    return hashlib.sha256(_normalize_link(link).encode()).hexdigest()


def _check_duplicate_links(links: list, admin_id) -> tuple:
    """
    جداسازی لینک‌های جدید از تکراری.
    خروجی: (new_links, dup_links)
    """
    if not links:
        return [], []

    hashes = [_link_hash(l) for l in links]
    # کوئری یکجا برای همه hash‌ها
    placeholders = ",".join(["%s"] * len(hashes))
    existing = q(
        f"SELECT link_hash FROM used_links WHERE admin_id=%s AND link_hash IN ({placeholders})",
        (admin_id, *hashes)
    )
    existing_hashes = set(r[0] for r in existing) if existing else set()

    new_links, dup_links = [], []
    for link, h in zip(links, hashes):
        if h in existing_hashes:
            dup_links.append(link)
        else:
            new_links.append(link)

    return new_links, dup_links


def _save_used_links(links: list, admin_id):
    """ذخیره لینک‌ها در جدول used_links (تکراری‌ها نادیده گرفته می‌شن)"""
    for link in links:
        norm = _normalize_link(link)
        h = _link_hash(link)
        try:
            u(
                "INSERT IGNORE INTO used_links (admin_id, link_hash, link_text) VALUES (%s,%s,%s)",
                (admin_id, h, norm[:500])
            )
        except Exception as e:
            print(f"[UsedLinks] خطا در ذخیره: {e}")
