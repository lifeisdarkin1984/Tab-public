import asyncio
from pyrogram import enums
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionExpired, FloodWait
from database import q, u
from utils import get_user_client

BOT_CLIENT = None  # توسط main.py تنظیم می‌شود


async def _send_banner(uc, chat_id, txt, fid, ftype):
    if fid:
        if ftype == "photo":
            await uc.send_photo(chat_id, fid, caption=txt or "")
        elif ftype == "video":
            await uc.send_video(chat_id, fid, caption=txt or "")
        else:
            await uc.send_document(chat_id, fid, caption=txt or "")
    else:
        await uc.send_message(chat_id, txt)


async def _reply_to_pvs(uc, acc_id, banners, replied):
    """ارسال بنرها به پیوی‌های جدید — برمی‌گردونه (replied کامل, new_replied این اجرا)"""
    new_replied = set()
    async for dlg in uc.get_dialogs():
        if dlg.chat.type != enums.ChatType.PRIVATE:
            continue
        uid = str(dlg.chat.id)
        if uid in replied:
            continue
        # چک می‌کنیم آیا کاربر اصلاً پیامی فرستاده (نه فقط آخرین پیام)
        user_sent = False
        async for msg in uc.get_chat_history(dlg.chat.id, limit=20):
            if msg.from_user and str(msg.from_user.id) == uid:
                user_sent = True
                break
        if not user_sent:
            continue

        all_sent = True
        for b in banners:
            txt, fid, ftype = b
            if not txt and not fid:
                print(f"[Secretary] بنر خالی برای {acc_id}، رد شد")
                continue
            try:
                await _send_banner(uc, dlg.chat.id, txt, fid, ftype)
                await asyncio.sleep(2)
            except FloodWait as e:
                # هیچ‌وقت break/skip نمی‌زنیم؛ صبر می‌کنیم و همون بنر رو دوباره امتحان می‌کنیم
                wait_s = min(e.value, 300)
                print(f"[Secretary] FloodWait {e.value}s برای {acc_id}؛ صبر می‌کند و دوباره تلاش می‌کند...")
                await asyncio.sleep(wait_s)
                try:
                    await _send_banner(uc, dlg.chat.id, txt, fid, ftype)
                    await asyncio.sleep(2)
                except Exception as e2:
                    print(f"[Secretary] ارسال بنر به {uid} بعد از صبر هم ناموفق بود: {e2}")
                    all_sent = False
            except Exception as e:
                print(f"[Secretary] خطا در ارسال بنر به {uid}: {e}")
                all_sent = False

        if all_sent:
            # فقط وقتی همه‌ی بنرها واقعاً ارسال شدن، کاربر replied علامت می‌خوره
            replied.add(uid)
            new_replied.add(uid)
        # وگرنه دور بعدی منشی دوباره همین کاربر رو امتحان می‌کند

        # فاصله‌ی کوتاه بین PV های مختلف، برای کاهش احتمال FloodWait وقتی PV/اکانت زیاده
        await asyncio.sleep(1)
    return replied, new_replied


async def _report(admin_id, text):
    if not BOT_CLIENT:
        return
    try:
        await BOT_CLIENT.send_message(admin_id, text)
    except Exception as e:
        print(f"[Secretary] خطا در ارسال گزارش: {e}")



async def run():
    print("🤖 Secretary worker started")
    while True:
        try:
            # ── منشی تک‌اکانت ──
            active = q(
                "SELECT s.account_id, s.replied_users, a.admin_id "
                "FROM secretary s "
                "JOIN accounts a ON s.account_id=a.id "
                "WHERE s.is_active=1 AND a.status='active'"
            )
            for (acc_id, replied_raw, admin_id) in active:
                replied = set(replied_raw.split(",")) if replied_raw else set()
                banners = q(
                    "SELECT text,file_id,file_type FROM banners "
                    "WHERE account_id=%s AND context='secretary' ORDER BY slot",
                    (acc_id,)
                )
                if not banners:
                    continue
                uc = await get_user_client(acc_id)
                if not uc:
                    continue
                try:
                    await uc.start()
                    replied, new_replied = await _reply_to_pvs(uc, acc_id, banners, replied)
                    await uc.stop()
                    u("UPDATE secretary SET replied_users=%s WHERE account_id=%s",
                      (",".join(replied), acc_id))
                    if new_replied:
                        phone_row = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
                        phone = phone_row[0][0] if phone_row else acc_id
                        await _report(admin_id, f"🤖 منشی اکانت {phone}: به {len(new_replied)} کاربر جدید پاسخ داده شد.")
                except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
                    u("UPDATE accounts SET status='inactive' WHERE id=%s", (acc_id,))
                    print(f"[Secretary] اکانت {acc_id} منقضی شد")
                    try: await uc.stop()
                    except Exception: pass
                except Exception as e:
                    print(f"[Secretary] خطا در {acc_id}: {e}")
                    try: await uc.stop()
                    except Exception: pass

            # ── منشی همگانی (هر لایه مستقل، برای همه‌ی تننت‌ها) ──
            g_layers = q(
                "SELECT admin_id, layer_id FROM global_secretary_settings WHERE is_active=1"
            )
            for (admin_id, g_layer_id) in g_layers:
                gl_id = f"global{g_layer_id}"
                g_banners = q(
                    "SELECT text,file_id,file_type FROM banners "
                    "WHERE account_id=%s AND context='g_secretary' ORDER BY slot",
                    (gl_id,)
                )
                if not g_banners:
                    continue
                accs = q(
                    "SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                    (admin_id, g_layer_id)
                )
                for (acc_id,) in accs:
                    # چک اگه این اکانت منشی تک‌اکانت داره، از اون استفاده نکن
                    has_own = q(
                        "SELECT COUNT(*) FROM secretary WHERE account_id=%s AND is_active=1",
                        (acc_id,)
                    )
                    if has_own and has_own[0][0] > 0:
                        continue
                    g_replied_row = q(
                        "SELECT replied_users FROM secretary "
                        "WHERE account_id=%s",
                        (f"g_{acc_id}",)
                    )
                    g_replied = set(g_replied_row[0][0].split(",")) if (g_replied_row and g_replied_row[0][0]) else set()
                    uc = await get_user_client(acc_id)
                    if not uc:
                        continue
                    try:
                        await uc.start()
                        g_replied, g_new_replied = await _reply_to_pvs(uc, acc_id, g_banners, g_replied)
                        await uc.stop()
                        u("INSERT INTO secretary (account_id,admin_id,is_active,replied_users) "
                          "VALUES(%s,%s,0,%s) ON DUPLICATE KEY UPDATE replied_users=%s",
                          (f"g_{acc_id}", admin_id, ",".join(g_replied), ",".join(g_replied)))
                        if g_new_replied:
                            phone_row = q("SELECT phone FROM accounts WHERE id=%s", (acc_id,))
                            phone = phone_row[0][0] if phone_row else acc_id
                            await _report(admin_id, f"🤖 منشی همگانی [لایه {g_layer_id} | {phone}]: به {len(g_new_replied)} کاربر جدید پاسخ داده شد.")
                    except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
                        u("UPDATE accounts SET status='inactive' WHERE id=%s", (acc_id,))
                        try: await uc.stop()
                        except Exception: pass
                    except Exception as e:
                        print(f"[Secretary Global] خطا در {acc_id} (لایه {g_layer_id}): {e}")
                        try: await uc.stop()
                        except Exception: pass

        except Exception as e:
            print(f"[Secretary] خطای کلی: {e}")

        # ── اسکن خودکار پیوی‌ها (برای همه‌ی تننت‌هایی که فعالش کردن) ──
        try:
            from datetime import datetime, timedelta
            pv_settings = q(
                "SELECT admin_id, scan_interval_hours, last_scan "
                "FROM pv_join_settings WHERE auto_scan=1"
            )
            now = datetime.utcnow()
            for (admin_id, interval_hours, last_scan) in pv_settings:
                if last_scan is None or (now - last_scan) >= timedelta(hours=interval_hours):
                    from handlers.callbacks import _scan_pvs_for_links
                    await _scan_pvs_for_links(BOT_CLIENT, admin_id)
        except Exception as e:
            print(f"[Secretary] خطا در اسکن خودکار پیوی‌ها: {e}")

        await asyncio.sleep(1800)

