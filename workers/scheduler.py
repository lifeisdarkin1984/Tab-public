import asyncio, time
from pyrogram import enums
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionExpired
from database import q, u
from utils import get_user_client

async def run():
    print("⏰ Scheduler worker started")
    while True:
        try:
            now = int(time.time())
            jobs = q(
                "SELECT sc.account_id, sc.interval_minutes, sc.last_run, "
                "sc.banner_text, sc.banner_file_id, sc.banner_file_type, "
                "sc.forward_from_chat, sc.forward_msg_id, sc.mode "
                "FROM scheduler sc "
                "JOIN accounts a ON sc.account_id=a.id "
                "WHERE sc.is_active=1 AND a.status='active'"
            )
            for job in jobs:
                (acc_id, interval_min, last_run, txt, fid, ftype, fwd_chat, fwd_id, mode) = job
                if now - last_run < interval_min * 60:
                    continue
                bnrs = q(
                    "SELECT text,file_id,file_type FROM banners "
                    "WHERE account_id=%s AND context='scheduler' ORDER BY slot",
                    (acc_id,)
                )
                uc = await get_user_client(acc_id)
                if not uc:
                    continue
                try:
                    await uc.start()
                    async for dlg in uc.get_dialogs():
                        if dlg.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                            continue
                        try:
                            if mode == "forward" and fwd_chat and fwd_id:
                                await uc.forward_messages(dlg.chat.id, fwd_chat, fwd_id)
                            else:
                                for b in (bnrs or [(txt, fid, ftype)]):
                                    bt, bf, bft = b
                                    if bf:
                                        if bft == "photo":
                                            await uc.send_photo(dlg.chat.id, bf, caption=bt)
                                        elif bft == "video":
                                            await uc.send_video(dlg.chat.id, bf, caption=bt)
                                        else:
                                            await uc.send_document(dlg.chat.id, bf, caption=bt)
                                    elif bt:
                                        await uc.send_message(dlg.chat.id, bt)
                                    await asyncio.sleep(1)
                        except Exception:
                            pass
                    await uc.stop()
                    u("UPDATE scheduler SET last_run=%s WHERE account_id=%s", (now, acc_id))
                except (AuthKeyUnregistered, UserDeactivated, SessionExpired):
                    u("UPDATE accounts SET status='inactive' WHERE id=%s", (acc_id,))
                    print(f"[Scheduler] اکانت {acc_id} منقضی شد - غیرفعال شد")
                    try:
                        await uc.stop()
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[Scheduler] خطا در اکانت {acc_id}: {e}")
                    try:
                        await uc.stop()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Scheduler] خطای کلی: {e}")
        await asyncio.sleep(60)
