import mysql.connector, os, time
from dotenv import load_dotenv
load_dotenv()


def get_db():
    return mysql.connector.connect(
        host=os.environ["MYSQLHOST"],
        port=int(os.environ.get("MYSQLPORT", 3306)),
        user=os.environ["MYSQLUSER"],
        password=os.environ["MYSQLPASSWORD"],
        database=os.environ["MYSQLDATABASE"],
        autocommit=True,
        connection_timeout=10
    )


def q(sql, params=None):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, params or ())
    res = cur.fetchall()
    db.close()
    return res


def u(sql, params=None):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, params or ())
    db.commit()
    lid = cur.lastrowid
    db.close()
    return lid


def init_panel_db():
    """
    جداول پنل مدیریتی — کاملاً جدا از دیتابیس اصلی تبچی (accounts/layers/tags/...).
    فقط از طریق telegram_id (که در فاز ۲ به tenant_id تبدیل می‌شه) به مشتری‌ها اشاره می‌کنن.
    """
    db = get_db()
    cur = db.cursor()
    stmts = [
        """CREATE TABLE IF NOT EXISTS plans (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            price BIGINT DEFAULT 0,
            duration_days INT DEFAULT 30,
            max_accounts INT DEFAULT 0,
            max_layers INT DEFAULT 0,
            features_json MEDIUMTEXT,
            is_active TINYINT DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS tenants (
            telegram_id BIGINT PRIMARY KEY,
            username VARCHAR(100) DEFAULT '',
            full_name VARCHAR(255) DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(20) DEFAULT 'trial',
            plan_id INT DEFAULT NULL,
            expires_at BIGINT DEFAULT 0,
            note VARCHAR(500) DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            actor BIGINT,
            action VARCHAR(100),
            target_tenant_id BIGINT DEFAULT NULL,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            note VARCHAR(500) DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS panel_state (
            id BIGINT PRIMARY KEY,
            step VARCHAR(100) DEFAULT 'idle',
            step_data MEDIUMTEXT DEFAULT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS payments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            tenant_id BIGINT,
            plan_id INT,
            amount BIGINT DEFAULT 0,
            method VARCHAR(20) DEFAULT 'manual',
            status VARCHAR(20) DEFAULT 'pending',
            authority VARCHAR(100) DEFAULT NULL,
            ref_id VARCHAR(100) DEFAULT '',
            note VARCHAR(500) DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_authority (authority)
        )""",
        """CREATE TABLE IF NOT EXISTS reminder_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            tenant_id BIGINT,
            days_left INT,
            expires_at BIGINT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_reminder (tenant_id, days_left, expires_at)
        )""",
        """CREATE TABLE IF NOT EXISTS tickets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            tenant_id BIGINT,
            message MEDIUMTEXT,
            status VARCHAR(20) DEFAULT 'open',
            reply_text MEDIUMTEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            replied_at TIMESTAMP NULL
        )""",
        # ── درخواست تست رایگان: مشتری درخواست می‌ده، ادمین از پنل تایید/رد می‌کنه ──
        """CREATE TABLE IF NOT EXISTS trial_requests (
            id INT AUTO_INCREMENT PRIMARY KEY,
            tenant_id BIGINT,
            status VARCHAR(20) DEFAULT 'pending',
            days INT DEFAULT NULL,
            max_accounts INT DEFAULT NULL,
            max_layers INT DEFAULT NULL,
            note VARCHAR(500) DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP NULL
        )""",
        """CREATE TABLE IF NOT EXISTS trial_settings (
            id INT PRIMARY KEY,
            days INT DEFAULT 3,
            max_accounts INT DEFAULT 1,
            max_layers INT DEFAULT 1
        )""",
        # سقف‌های تستِ تاییدشده برای هر تننت (چون تست پلن واقعی نیست و plan_id نداره)
        "ALTER TABLE tenants ADD COLUMN trial_max_accounts INT DEFAULT NULL",
        "ALTER TABLE tenants ADD COLUMN trial_max_layers INT DEFAULT NULL",
    ]
    for s in stmts:
        try:
            cur.execute(s)
        except Exception as e:
            print(f"[Panel DB init] {e}")
    try:
        cur.execute("INSERT IGNORE INTO trial_settings (id, days, max_accounts, max_layers) "
                     "VALUES (1, 3, 1, 1)")
    except Exception as e:
        print(f"[Panel DB init] {e}")
    db.commit()
    db.close()
    print("✅ Panel DB ready")


# ── helpers مربوط به مشتری‌ها ──

def get_tenant(telegram_id):
    r = q("SELECT telegram_id, username, full_name, created_at, status, plan_id, "
          "expires_at, note FROM tenants WHERE telegram_id=%s", (telegram_id,))
    return r[0] if r else None


def list_tenants(offset=0, limit=8):
    return q("SELECT telegram_id, username, full_name, status, expires_at FROM tenants "
              "ORDER BY created_at DESC LIMIT %s OFFSET %s", (limit, offset))


def count_tenants():
    r = q("SELECT status, COUNT(*) FROM tenants GROUP BY status")
    return {row[0]: row[1] for row in r}


TRIAL_DAYS_DEFAULT = 3

def add_tenant(telegram_id, full_name="", username=""):
    trial_exp = int(time.time()) + TRIAL_DAYS_DEFAULT * 86400
    u("INSERT INTO tenants (telegram_id, username, full_name, status, expires_at) "
      "VALUES (%s,%s,%s,'trial',%s) ON DUPLICATE KEY UPDATE username=%s, full_name=%s",
      (telegram_id, username, full_name, trial_exp, username, full_name))


def set_tenant_status(telegram_id, status):
    u("UPDATE tenants SET status=%s WHERE telegram_id=%s", (status, telegram_id))


def extend_tenant(telegram_id, days):
    now = int(time.time())
    row = q("SELECT expires_at FROM tenants WHERE telegram_id=%s", (telegram_id,))
    base = row[0][0] if row and row[0][0] and row[0][0] > now else now
    new_exp = base + days * 86400
    u("UPDATE tenants SET expires_at=%s, status='active' WHERE telegram_id=%s",
      (new_exp, telegram_id))
    return new_exp


def set_tenant_plan(telegram_id, plan_id):
    u("UPDATE tenants SET plan_id=%s WHERE telegram_id=%s", (plan_id, telegram_id))


# ── helpers مربوط به پلن‌ها ──

def list_plans(only_active=False):
    if only_active:
        return q("SELECT id, name, price, duration_days, max_accounts, max_layers, "
                  "is_active FROM plans WHERE is_active=1 ORDER BY id")
    return q("SELECT id, name, price, duration_days, max_accounts, max_layers, "
              "is_active FROM plans ORDER BY id")


def get_plan(plan_id):
    r = q("SELECT id, name, price, duration_days, max_accounts, max_layers, "
          "features_json, is_active FROM plans WHERE id=%s", (plan_id,))
    return r[0] if r else None


def add_plan(name, price, duration_days, max_accounts, max_layers):
    return u("INSERT INTO plans (name, price, duration_days, max_accounts, max_layers) "
             "VALUES (%s,%s,%s,%s,%s)", (name, price, duration_days, max_accounts, max_layers))


def toggle_plan(plan_id):
    row = q("SELECT is_active FROM plans WHERE id=%s", (plan_id,))
    if not row:
        return
    new_val = 0 if row[0][0] else 1
    u("UPDATE plans SET is_active=%s WHERE id=%s", (new_val, plan_id))


def update_plan_field(plan_id, field, value):
    allowed = {"price", "duration_days", "max_accounts", "max_layers", "name"}
    if field not in allowed:
        return
    u(f"UPDATE plans SET {field}=%s WHERE id=%s", (value, plan_id))


# ── state ادمینِ پنل (برای مراحل چندقدمی مثل «تمدید» یا «افزودن پلن») ──

def get_panel_step(uid):
    r = q("SELECT step, step_data FROM panel_state WHERE id=%s", (uid,))
    return r[0] if r else ("idle", "")


def set_panel_step(uid, step, data=""):
    u("INSERT INTO panel_state (id, step, step_data) VALUES (%s,%s,%s) "
      "ON DUPLICATE KEY UPDATE step=%s, step_data=%s", (uid, step, data, step, data))


def clear_panel_step(uid):
    set_panel_step(uid, "idle", "")


def log_action(actor, action, target_tenant_id=None, note=""):
    u("INSERT INTO audit_log (actor, action, target_tenant_id, note) VALUES (%s,%s,%s,%s)",
      (actor, action, target_tenant_id, note))


# ── مانیتور مصرف منابع (فاز ۴) — accounts تویِ همون دیتابیس تبچیه، مستقیم می‌خونیم ──

def count_total_active_accounts():
    r = q("SELECT COUNT(*) FROM accounts WHERE status='active'")
    return r[0][0] if r else 0


def count_tenant_active_accounts(telegram_id):
    r = q("SELECT COUNT(*) FROM accounts WHERE admin_id=%s AND status='active'", (telegram_id,))
    return r[0][0] if r else 0


def top_tenants_by_usage(limit=5):
    """پرمصرف‌ترین مشتری‌ها (تعداد اکانت فعال)"""
    return q(
        "SELECT admin_id, COUNT(*) c FROM accounts WHERE status='active' "
        "GROUP BY admin_id ORDER BY c DESC LIMIT %s", (limit,)
    )


# ── helpers مربوط به پرداخت‌ها ──

def list_pending_payments(offset=0, limit=8):
    return q("SELECT id, tenant_id, plan_id, amount, method, created_at FROM payments "
              "WHERE status='pending' ORDER BY created_at DESC LIMIT %s OFFSET %s",
              (limit, offset))


def count_pending_payments():
    r = q("SELECT COUNT(*) FROM payments WHERE status='pending'")
    return r[0][0] if r else 0


def get_payment(payment_id):
    r = q("SELECT id, tenant_id, plan_id, amount, method, status, authority, ref_id, "
          "note, created_at FROM payments WHERE id=%s", (payment_id,))
    return r[0] if r else None


def get_payment_by_authority(authority):
    r = q("SELECT id, tenant_id, plan_id, amount, method, status, authority, ref_id, "
          "note, created_at FROM payments WHERE authority=%s", (authority,))
    return r[0] if r else None


def approve_payment(payment_id, actor, ref_id=""):
    """تایید پرداخت: پلن رو ست و اشتراک رو به اندازه‌ی مدت پلن تمدید می‌کنه"""
    p = get_payment(payment_id)
    if not p:
        return None
    _, tenant_id, plan_id, amount, method, status, authority, old_ref_id, note, created_at = p
    if status != "pending":
        return None
    plan = get_plan(plan_id) if plan_id else None
    duration = plan[3] if plan else 30
    set_tenant_plan(tenant_id, plan_id)
    new_exp = extend_tenant(tenant_id, duration)
    u("UPDATE payments SET status='success', ref_id=%s WHERE id=%s", (ref_id, payment_id))
    log_action(actor, "approve_payment", tenant_id, note=f"payment_id={payment_id}")
    return tenant_id, new_exp


def fail_payment(payment_id, note=""):
    u("UPDATE payments SET status='failed', note=%s WHERE id=%s", (note, payment_id))


def reject_payment(payment_id, actor, note=""):
    u("UPDATE payments SET status='rejected', note=%s WHERE id=%s", (note, payment_id))
    p = get_payment(payment_id)
    tenant_id = p[1] if p else None
    log_action(actor, "reject_payment", tenant_id, note=f"payment_id={payment_id} {note}")
    return tenant_id


# ── درخواست تست رایگان ──

def get_trial_settings():
    r = q("SELECT days, max_accounts, max_layers FROM trial_settings WHERE id=1")
    return r[0] if r else (3, 1, 1)


def update_trial_settings_field(field, value):
    allowed = {"days", "max_accounts", "max_layers"}
    if field not in allowed:
        return
    u(f"UPDATE trial_settings SET {field}=%s WHERE id=1", (value,))


def has_pending_trial_request(tenant_id):
    r = q("SELECT id FROM trial_requests WHERE tenant_id=%s AND status='pending'", (tenant_id,))
    return bool(r)


def create_trial_request(tenant_id):
    return u("INSERT INTO trial_requests (tenant_id, status) VALUES (%s,'pending')", (tenant_id,))


def list_pending_trial_requests(offset=0, limit=8):
    return q("SELECT id, tenant_id, created_at FROM trial_requests "
              "WHERE status='pending' ORDER BY created_at ASC LIMIT %s OFFSET %s",
              (limit, offset))


def count_pending_trial_requests():
    r = q("SELECT COUNT(*) FROM trial_requests WHERE status='pending'")
    return r[0][0] if r else 0


def get_trial_request(req_id):
    r = q("SELECT id, tenant_id, status, days, max_accounts, max_layers, note, created_at "
          "FROM trial_requests WHERE id=%s", (req_id,))
    return r[0] if r else None


def approve_trial_request(req_id, actor, days=None, max_accounts=None, max_layers=None):
    """تایید درخواست تست: مقادیر ندادن یعنی از تنظیمات پیش‌فرض تست استفاده بشه"""
    r = get_trial_request(req_id)
    if not r or r[2] != "pending":
        return None
    tenant_id = r[1]
    def_days, def_accounts, def_layers = get_trial_settings()
    days = days if days is not None else def_days
    max_accounts = max_accounts if max_accounts is not None else def_accounts
    max_layers = max_layers if max_layers is not None else def_layers

    new_exp = int(time.time()) + days * 86400
    u("UPDATE tenants SET status='trial', expires_at=%s, "
      "trial_max_accounts=%s, trial_max_layers=%s WHERE telegram_id=%s",
      (new_exp, max_accounts, max_layers, tenant_id))
    u("UPDATE trial_requests SET status='approved', days=%s, max_accounts=%s, max_layers=%s, "
      "decided_at=NOW() WHERE id=%s", (days, max_accounts, max_layers, req_id))
    log_action(actor, "approve_trial", tenant_id,
               note=f"req_id={req_id} days={days} accounts={max_accounts} layers={max_layers}")
    return tenant_id, new_exp, days, max_accounts, max_layers


def reject_trial_request(req_id, actor, note=""):
    r = get_trial_request(req_id)
    if not r or r[2] != "pending":
        return None
    tenant_id = r[1]
    u("UPDATE trial_requests SET status='rejected', note=%s, decided_at=NOW() WHERE id=%s",
      (note, req_id))
    log_action(actor, "reject_trial", tenant_id, note=f"req_id={req_id} {note}")
    return tenant_id


# ── تیکت پشتیبانی (فاز ۵) ──

def list_tickets(status="open", offset=0, limit=8):
    return q("SELECT id, tenant_id, message, status, created_at FROM tickets "
              "WHERE status=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",
              (status, limit, offset))


def count_tickets(status="open"):
    r = q("SELECT COUNT(*) FROM tickets WHERE status=%s", (status,))
    return r[0][0] if r else 0


def get_ticket(ticket_id):
    r = q("SELECT id, tenant_id, message, status, reply_text, created_at, replied_at "
          "FROM tickets WHERE id=%s", (ticket_id,))
    return r[0] if r else None


def reply_ticket(ticket_id, reply_text):
    u("UPDATE tickets SET status='answered', reply_text=%s, replied_at=NOW() WHERE id=%s",
      (reply_text, ticket_id))


def close_ticket(ticket_id):
    u("UPDATE tickets SET status='closed' WHERE id=%s", (ticket_id,))


# ── اطلاع‌رسانی همگانی / Broadcast (فاز ۵) ──

def list_tenant_ids(plan_id=None):
    """آیدی تلگرام همه‌ی تننت‌ها؛ یا فقط اونایی که پلن مشخصی دارن"""
    if plan_id:
        rows = q("SELECT telegram_id FROM tenants WHERE plan_id=%s", (plan_id,))
    else:
        rows = q("SELECT telegram_id FROM tenants")
    return [r[0] for r in rows]
