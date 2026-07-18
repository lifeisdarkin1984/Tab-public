import asyncio, time, random
from pyrogram import enums
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionExpired, FloodWait, \
    ChatWriteForbidden, UserBannedInChannel, ChatRestricted, ChatSendMediaForbidden
from database import q, u
from utils import get_user_client, is_stopped

BOT_CLIENT = None  # توسط main.py تنظیم می‌شود


async def _send_banner(uc, chat_id, bt, bf, bft):
    if bf:
        if bft == "photo":
            await uc.send_photo(chat_id, bf, caption=bt)
        elif bft == "video":
            await uc.send_video(chat_id, bf, caption=bt)
        else:
            await uc.send_document(chat_id, bf, caption=bt)
    elif bt:
        await uc.send_message(chat_id, bt)


async def _run_for_target(admin_id, target, layer_id):
    """target = 'groups' یا 'pvs' — مخصوص یک لایه‌ی مشخص از یک تننت مشخص"""
    now = int(time.time())
    row = q(
        "SELECT interval_minutes, last_run, last_index, group_tag_filter, acc_tag_filter, "
        "max_rounds, current_round FROM global_scheduler "
        "WHERE admin_id=%s AND target=%s AND layer_id=%s AND is_active=1",
        (admin_id, target, layer_id)
    )
    if not row:
        return
    interval_min, last_run, last_index, gtag, atag, max_rounds, current_round = row[0]
    gtag = gtag or "ALL"
    atag = atag or "ALL"
    if now - last_run < interval_min * 60:
        return

    banners = q(
        "SELECT slot, text, file_id, file_type FROM global_banners "
        "WHERE admin_id=%s AND target=%s AND layer_id=%s ORDER BY slot",
        (admin_id, target, layer_id)
    )
    if not banners:
        print(f"[GlobalScheduler:{target}:L{layer_id}] هیچ پیامی تنظیم نشده، رد شد")
        return

    idx = last_index % len(banners)
    _, bt, bf, bft = banners[idx]
    new_index = idx + 1

    round_complete = (new_index >= len(banners))
    new_round = current_round + (1 if round_complete else 0)

    if max_rounds > 0 and new_round > max_rounds:
        u("UPDATE global_scheduler SET is_active=0, last_index=0, current_round=0 "
          "WHERE admin_id=%s AND target=%s AND layer_id=%s", (admin_id, target, layer_id))
        print(f"[GlobalScheduler:{target}:L{layer_id}] {max_rounds} دور کامل شد، غیرفعال شد.")
        if BOT_CLIENT:
            try:
                await BOT_CLIENT.send_message(
                    admin_id, f"✅ ارسال زمان‌دار {target} (لایه {layer_id}) پس از {max_rounds} دور کامل شد و خاموش شد."
                )
            except Exception:
                pass
        return

    chat_type_filter = (
        (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
        if target == "groups" else
        (enums.ChatType.PRIVATE,)
    )

    if atag not in ("ALL", ""):
        if atag == "NOTAG":
            accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' "
                     "AND layer_id=%s AND (tag='' OR tag IS NULL)", (admin_id, layer_id))
        else:
            accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s AND tag=%s",
                     (admin_id, layer_id, atag))
    else:
        accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active' AND layer_id=%s",
                 (admin_id, layer_id))

    if not accs:
        print(f"[GlobalScheduler:{target}:L{layer_id}] هیچ اکانت فعالی با فیلتر {atag} پیدا نشد")
        if BOT_CLIENT:
            try:
                await BOT_CLIENT.send_message(
                    admin_id, f"⚠️ ارسال زمان‌دار {target} (لایه {layer_id}): هیچ اکانتی با فیلتر «{atag}» پیدا نشد."
                )
            except Exception:
                pass
        return

    total_ok = total_fail = total_limited = 0

    for (acc_id,) in accs:
        if is_stopped():
            break
        uc = await get_user_client(acc_id)
        if not uc:
            print(f"[GlobalScheduler:{target}] اکانت {acc_id} session نداره")
            continue
        try:
            await uc.start()

            exclude_chats = None
            if target == "groups" and gtag not in ("ALL", ""):
                if gtag == "NOTAG":
                    # گروه‌هایی که برچسب غیرخالی دارن رو exclude می‌کنیم
                    rows = q("SELECT chat_id FROM group_tags WHERE admin_id=%s AND account_id=%s "
                             "AND tag_name<>'' AND tag_name IS NOT NULL", (admin_id, acc_id))
                    exclude_chats = set(r[0] for r in rows)
                    allowed_chats = None
                else:
                    rows = q("SELECT chat_id FROM group_tags WHERE admin_id=%s AND account_id=%s AND tag_name=%s",
                             (admin_id, acc_id, gtag))
                    allowed_chats = set(r[0] for r in rows)
            else:
                allowed_chats = None

            dialogs = []
            async for dlg in uc.get_dialogs():
                if dlg.chat.type not in chat_type_filter:
                    continue
                if allowed_chats is not None and dlg.chat.id not in allowed_chats:
                    continue
                if exclude_chats is not None and dlg.chat.id in exclude_chats:
                    continue
                dialogs.append(dlg)

            acc_ok = acc_fail = acc_limited = 0
            for dlg in dialogs:
                if is_stopped():
                    break
                try:
                    await _send_banner(uc, dlg.chat.id, bt, bf, bft)
                    acc_ok += 1
                    await asyncio.sleep(random.uniform(1.5, 4))
                except FloodWait as e:
                    # صبر می‌کنیم و همین گروه را دوباره امتحان می‌کنیم، بقیه گروه‌ها رو ول نمی‌کنیم
                    wait_s = min(e.value, 120)
                    await asyncio.sleep(wait_s)
                    try:
                        await _send_banner(uc, dlg.chat.id, bt, bf, bft)
                        acc_ok += 1
                    except Exception:
                        acc_fail += 1
                    await asyncio.sleep(random.uniform(1.5, 4))
                except (ChatWriteForbidden, UserBannedInChannel, ChatRestricted,
                        ChatSendMediaForbidden):
                    # گروه واقعاً محدوده - فقط همین یکی رد میشه
                    acc_limited += 1
                except Exception as e:
                    acc_fail += 1
                    print(f"[GlobalScheduler:{target}] خطا در ارسال به {dlg.chat.id} ({acc_id}): {e}")

            total_ok += acc_ok
            total_fail += acc_fail
            total_limited += acc_limited
            await uc.stop()
        except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
            u("UPDATE accounts SET status='inactive' WHERE id=%s", (acc_id,))
            print(f"[GlobalScheduler:{target}] اکانت {acc_id} منقضی شد")
            try: await uc.stop()
            except Exception: pass
        except Exception as e:
            print(f"[GlobalScheduler:{target}] خطای کلی در {acc_id}: {e}")
            try: await uc.stop()
            except Exception: pass

    u(
        "UPDATE global_scheduler SET last_run=%s, last_index=%s, current_round=%s "
        "WHERE admin_id=%s AND target=%s AND layer_id=%s",
        (now, new_index % len(banners), new_round, admin_id, target, layer_id)
    )

    # گزارش به ادمین
    if BOT_CLIENT:
        title = "📢 گروه‌ها" if target == "groups" else "💬 پیوی‌ها"
        report = (
            f"⏰ ارسال زمان‌دار {title} (لایه {layer_id}) — پیام {idx+1}/{len(banners)} ارسال شد\n"
            f"✔️ موفق: {total_ok}\n❌ ناموفق: {total_fail}\n🚫 محدود: {total_limited}\n"
            f"📨 اکانت‌های پردازش‌شده: {len(accs)}"
        )
        try:
            await BOT_CLIENT.send_message(admin_id, report)
        except Exception as e:
            print(f"[GlobalScheduler:{target}:L{layer_id}] خطا در ارسال گزارش: {e}")


async def run():
    print("📨 Global scheduler worker started")
    while True:
        try:
            # همه‌ی لایه‌های فعال، برای همه‌ی تننت‌ها (بدون فیلتر روی یک ادمین خاص)
            layers = q(
                "SELECT DISTINCT l.id, l.admin_id FROM layers l "
                "JOIN accounts a ON a.layer_id=l.id AND a.status='active'"
            )
            for (layer_id, admin_id) in layers:
                await _run_for_target(admin_id, "groups", layer_id)
                await _run_for_target(admin_id, "pvs", layer_id)
        except Exception as e:
            print(f"[GlobalScheduler] خطای کلی: {e}")
        await asyncio.sleep(60)
