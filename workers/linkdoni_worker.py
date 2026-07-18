import asyncio, re, random, hashlib
from pyrogram.errors import (FloodWait, AuthKeyUnregistered,
                              UserDeactivated, SessionExpired,
                              ChannelPrivate, ChatForbidden,
                              UsernameNotOccupied, UsernameInvalid,
                              PeerIdInvalid)
from database import q, u
from utils import get_user_client

BOT_CLIENT = None

LINK_PATTERN = re.compile(r'https?://t\.me/[^\s\]\)"\']+')


def _normalize_link(link: str) -> str:
    # فقط query string و slash آخر رو حذف کن — lowercase نزن چون invite hash حساسه
    link = link.strip().split("?")[0].rstrip("/")
    return link


def _link_hash(link: str) -> str:
    return hashlib.sha256(_normalize_link(link).encode()).hexdigest()


def _get_settings(admin_id):
    r = q("SELECT auto_scan, scan_interval_hours, auto_join, join_mode, "
          "join_tag, last_auto_scan FROM linkdoni_settings WHERE admin_id=%s",
          (admin_id,))
    if not r:
        u("INSERT IGNORE INTO linkdoni_settings (admin_id) VALUES (%s)",
          (admin_id,))
        return {"auto_scan": 0, "scan_interval_hours": 6, "auto_join": 0,
                "join_mode": "split", "join_tag": "", "last_auto_scan": None}
    return {"auto_scan": r[0][0], "scan_interval_hours": r[0][1],
            "auto_join": r[0][2], "join_mode": r[0][3],
            "join_tag": r[0][4] or "", "last_auto_scan": r[0][5]}


async def _get_history(uc, target, limit=500):
    """
    تاریخچه یه کانال/گروه رو می‌گیره.
    خروجی: لیست پیام‌ها یا None اگه دسترسی نداشت.
    """
    msgs = []
    try:
        async for msg in uc.get_chat_history(target, limit=limit):
            msgs.append(msg)
        return msgs
    except (ChannelPrivate, ChatForbidden, UsernameNotOccupied,
            UsernameInvalid, PeerIdInvalid):
        return None
    except FloodWait as e:
        await asyncio.sleep(e.value)
        # یه بار دیگه امتحان کن
        try:
            msgs = []
            async for msg in uc.get_chat_history(target, limit=limit):
                msgs.append(msg)
            return msgs
        except Exception:
            return None
    except Exception:
        return None


async def scan_linkdonis(admin_id, triggered_by="auto"):
    """
    اسکن همه لینکدونی‌های فعال (همه‌ی لایه‌ها) و استخراج لینک‌های جدید.
    هر منبع فقط با اکانت‌های همون لایه‌ای که توش ثبت شده اسکن می‌شه.
    triggered_by: "auto" یا "manual"
    برمی‌گردونه: (total_found, new_count, links_list)
    """
    sources = q("SELECT id, chat_id, chat_title, last_message_id, layer_id "
                "FROM linkdoni_sources "
                "WHERE admin_id=%s AND is_active=1", (admin_id,))
    if not sources:
        return 0, 0, []

    accs = q("SELECT id, layer_id FROM accounts WHERE admin_id=%s AND status='active'",
             (admin_id,))
    if not accs:
        return 0, 0, []

    # اکانت‌ها رو به تفکیک لایه گروه‌بندی می‌کنیم تا هر منبع فقط با اکانت‌های همون لایه اسکن بشه
    acc_ids_by_layer = {}
    for acc_id, acc_layer_id in accs:
        acc_ids_by_layer.setdefault(acc_layer_id, []).append(str(acc_id))
    for lyr in acc_ids_by_layer:
        random.shuffle(acc_ids_by_layer[lyr])

    clients = {}
    for acc_id in [str(r[0]) for r in accs]:
        uc = await get_user_client(acc_id)
        if not uc:
            continue
        try:
            await uc.start()
            clients[acc_id] = uc
        except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
            print(f"[Linkdoni] اکانت {acc_id} منقضی.")
        except Exception as e:
            print(f"[Linkdoni] خطا در start اکانت {acc_id}: {e}")

    if not clients:
        return 0, 0, []

    total_found = 0
    new_count = 0
    all_new_links = []

    for src_id, chat_id, chat_title, last_msg_id, src_layer_id in sources:
        try:
            try:
                target = int(chat_id)
            except ValueError:
                target = chat_id

            # فقط اکانت‌های همین لایه رو امتحان می‌کنیم
            layer_acc_ids = acc_ids_by_layer.get(src_layer_id, [])
            raw_msgs = None
            for acc_id in layer_acc_ids:
                uc = clients.get(acc_id)
                if not uc:
                    continue
                raw_msgs = await _get_history(uc, target, limit=500)
                if raw_msgs is not None:
                    break

            if raw_msgs is None:
                print(f"[Linkdoni] هیچ اکانتی (لایه {src_layer_id}) به {chat_id} دسترسی نداشت.")
                continue

            new_last_id = last_msg_id
            raw_links = []

            for msg in raw_msgs:
                if msg.id <= last_msg_id:
                    continue
                if msg.id > new_last_id:
                    new_last_id = msg.id

                # ۱. متن پیام
                txt = (msg.text or "") + " " + (msg.caption or "")
                raw_links += LINK_PATTERN.findall(txt)

                # ۲. entities (لینک‌های کلیک‌پذیر داخل متن)
                for entities in [msg.entities or [], msg.caption_entities or []]:
                    for e in entities:
                        if hasattr(e, 'url') and e.url:
                            raw_links += LINK_PATTERN.findall(e.url)

                # ۳. دکمه‌های inline
                if msg.reply_markup:
                    try:
                        rows = getattr(msg.reply_markup, 'inline_keyboard', None)
                        if rows:
                            for row in rows:
                                for btn in row:
                                    url = getattr(btn, 'url', None)
                                    if url:
                                        raw_links += LINK_PATTERN.findall(url)
                    except Exception:
                        pass

                # ۴. web preview
                if msg.web_page and hasattr(msg.web_page, 'url') and msg.web_page.url:
                    raw_links += LINK_PATTERN.findall(msg.web_page.url)

            # پاکسازی: حذف کاراکترهای اضافی از انتها + فیلتر t.me + حذف تکراری
            batch_links = []
            for lnk in raw_links:
                lnk = lnk.rstrip('.,;:!?)\'"')
                if 't.me/' in lnk and lnk not in batch_links:
                    batch_links.append(lnk)

            total_found += len(batch_links)

            # فیلتر تکراری با used_links و linkdoni_links
            for lnk in batch_links:
                norm = _normalize_link(lnk)
                h = _link_hash(lnk)

                r1 = q("SELECT 1 FROM used_links WHERE admin_id=%s AND link_hash=%s",
                       (admin_id, h))
                if r1:
                    continue

                r2 = q("SELECT 1 FROM linkdoni_links WHERE admin_id=%s AND link_hash=%s",
                       (admin_id, h))
                if r2:
                    continue

                try:
                    u("INSERT IGNORE INTO linkdoni_links "
                      "(admin_id, link, link_hash, source_id) VALUES (%s,%s,%s,%s)",
                      (admin_id, norm[:500], h, src_id))
                    all_new_links.append(norm)
                    new_count += 1
                except Exception:
                    pass

            # آپدیت last_message_id و last_scan
            if new_last_id > last_msg_id:
                u("UPDATE linkdoni_sources SET last_message_id=%s, last_scan=NOW() WHERE id=%s",
                  (new_last_id, src_id))

            await asyncio.sleep(2)

        except Exception as ex:
            print(f"[Linkdoni] خطا در اسکن {chat_id}: {ex}")
            continue

    # stop همه اکانت‌ها
    for uc in clients.values():
        try:
            await uc.stop()
        except Exception:
            pass

    return total_found, new_count, all_new_links


async def run():
    """حلقه اصلی اسکن خودکار — برای همه‌ی تننت‌هایی که auto_scan فعاله"""
    await asyncio.sleep(15)
    while True:
        try:
            tenants = q("SELECT admin_id FROM linkdoni_settings WHERE auto_scan=1")
            for (admin_id,) in tenants:
                settings = _get_settings(admin_id)
                if not settings["auto_scan"]:
                    continue
                from datetime import datetime, timedelta
                last = settings["last_auto_scan"]
                interval = settings["scan_interval_hours"]
                now = datetime.utcnow()
                should_scan = (
                    last is None or
                    (now - last) >= timedelta(hours=interval)
                )
                if should_scan:
                    u("UPDATE linkdoni_settings SET last_auto_scan=NOW() "
                      "WHERE admin_id=%s", (admin_id,))
                    total, new_count, links = await scan_linkdonis(admin_id, "auto")

                    # اسکن خودکار فقط گزارش می‌ده، جوین نمی‌کنه
                    if BOT_CLIENT:
                        try:
                            await BOT_CLIENT.send_message(
                                admin_id,
                                f"📡 **اسکن خودکار لینکدونی تمام شد**\n\n"
                                f"🔍 لینک‌های بررسی‌شده: {total}\n"
                                f"✅ لینک‌های جدید: {new_count}\n"
                                f"🔄 تکراری حذف‌شده: {total - new_count}\n\n"
                                f"برای جوین دستی: مدیریت همگانی → لینکدونی هوشمند"
                            )
                        except Exception as e:
                            print(f"[Linkdoni] خطا در ارسال گزارش: {e}")

        except Exception as e:
            print(f"[Linkdoni] خطا در run: {e}")
        await asyncio.sleep(300)


async def _do_join(admin_id, links, settings):
    """جوین لینک‌ها بر اساس تنظیمات — فقط برای جوین دستی (manual)"""
    from handlers.text_handler import _join_links
    join_mode = settings["join_mode"]
    join_tag = settings["join_tag"]

    accs = q("SELECT id FROM accounts WHERE admin_id=%s AND status='active'",
             (admin_id,))
    if not accs:
        return

    mn_row = q("SELECT min_delay FROM join_settings WHERE admin_id=%s LIMIT 1",
               (admin_id,))
    mx_row = q("SELECT max_delay FROM join_settings WHERE admin_id=%s LIMIT 1",
               (admin_id,))
    min_d = mn_row[0][0] if mn_row else 5
    max_d = mx_row[0][0] if mx_row else 15

    if join_mode == "random":
        acc_id = str(random.choice(accs)[0])
        asyncio.create_task(
            _join_links(BOT_CLIENT, acc_id, links, min_d, max_d, join_tag)
        )

    elif join_mode == "split":
        chunk_size = max(1, len(links) // len(accs))
        for i, (acc_id,) in enumerate(accs):
            chunk = links[i * chunk_size: (i + 1) * chunk_size]
            if chunk:
                asyncio.create_task(
                    _join_links(BOT_CLIENT, str(acc_id),
                                chunk, min_d, max_d, join_tag)
                )

    elif join_mode == "all":
        for (acc_id,) in accs:
            asyncio.create_task(
                _join_links(BOT_CLIENT, str(acc_id),
                            links, min_d, max_d, join_tag)
            )

    # آپدیت وضعیت joined در linkdoni_links
    for lnk in links:
        h = _link_hash(lnk)
        u("UPDATE linkdoni_links SET joined=1 WHERE admin_id=%s AND link_hash=%s",
          (admin_id, h))
