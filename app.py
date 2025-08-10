# app.py
import os
import sqlite3
import threading
from datetime import datetime, date
from typing import List, Optional

import streamlit as st
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
import uvicorn

# ========= Ayarlar (Streamlit Secrets) =========
def _secret(name: str, default: str = "") -> str:
    # Streamlit Cloud: st.secrets
    # Lokal/başka yer: ortam değişkeni fallback
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)

TWILIO_SID   = _secret("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = _secret("TWILIO_AUTH_TOKEN")
TWILIO_FROM  = _secret("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
WEBHOOK_HOST = _secret("WEBHOOK_HOST", "http://127.0.0.1:8000")

ADMIN_USERNAME = _secret("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _secret("ADMIN_PASSWORD", "changeme")

DB_PATH = "checkup.db"

# ========= Veritabanı =========
def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE
    );""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS checkups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        package_name TEXT NOT NULL,
        check_date TEXT NOT NULL,
        FOREIGN KEY(patient_id) REFERENCES patients(id)
    );""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checkup_id INTEGER NOT NULL,
        task_name TEXT NOT NULL,
        is_done INTEGER NOT NULL DEFAULT 0,
        done_at TEXT,
        FOREIGN KEY(checkup_id) REFERENCES checkups(id)
    );""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        direction TEXT NOT NULL,  -- inbound / outbound
        sender TEXT NOT NULL,
        receiver TEXT NOT NULL,
        body TEXT NOT NULL,
        at TEXT NOT NULL
    );""")
    conn.commit()
    conn.close()

def db_query(sql, params=(), fetchone=False, commit=False):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(sql, params)
    if commit:
        conn.commit()
    if fetchone:
        row = cur.fetchone()
        conn.close()
        return row
    rows = cur.fetchall()
    conn.close()
    return rows

def find_or_create_patient(name: str, phone: str):
    row = db_query("SELECT * FROM patients WHERE phone = ?", (phone,), fetchone=True)
    if row:
        return row["id"]
    db_query("INSERT INTO patients(name, phone) VALUES(?, ?)", (name, phone), commit=True)
    row = db_query("SELECT * FROM patients WHERE phone = ?", (phone,), fetchone=True)
    return row["id"]

def create_checkup(patient_id: int, package_name: str, check_date: str):
    db_query("INSERT INTO checkups(patient_id, package_name, check_date) VALUES(?,?,?)",
             (patient_id, package_name, check_date), commit=True)
    row = db_query("SELECT last_insert_rowid() as id")
    return row[0]["id"]

def add_tasks(checkup_id: int, tasks: List[str]):
    for t in tasks:
        if t.strip():
            db_query("INSERT INTO tasks(checkup_id, task_name) VALUES(?,?)",
                     (checkup_id, t.strip()), commit=True)

def list_today_checkups():
    today = date.today().isoformat()
    return db_query("""
    SELECT c.id as checkup_id, p.name, p.phone, c.package_name, c.check_date
    FROM checkups c
    JOIN patients p ON p.id = c.patient_id
    WHERE c.check_date = ?
    ORDER BY p.name
    """, (today,))

def list_tasks_for_checkup(checkup_id: int):
    return db_query("SELECT * FROM tasks WHERE checkup_id=? ORDER BY id", (checkup_id,))

def mark_task_done(task_id: int):
    now = datetime.now().isoformat(timespec='seconds')
    db_query("UPDATE tasks SET is_done=1, done_at=? WHERE id=?", (now, task_id), commit=True)

def insert_message(direction: str, sender: str, receiver: str, body: str):
    at = datetime.now().isoformat(timespec='seconds')
    db_query("INSERT INTO messages(direction, sender, receiver, body, at) VALUES(?,?,?,?,?)",
             (direction, sender, receiver, body, at), commit=True)

# ========= Twilio Gönderim =========
def send_whatsapp(to_phone: str, body: str) -> Optional[str]:
    if not to_phone.startswith("whatsapp:"):
        to_phone = f"whatsapp:{to_phone}"
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        msg = client.messages.create(from_=TWILIO_FROM, to=to_phone, body=body)
        insert_message("outbound", TWILIO_FROM, to_phone, body)
        return msg.sid
    except Exception as e:
        # Streamlit’te göster
        try:
            st.error(f"Twilio gönderim hatası: {e}")
        except Exception:
            pass
        return None

# ========= FastAPI (Webhook) =========
api = FastAPI(title="Check-up WhatsApp Webhook")

@api.post("/twilio/whatsapp")
async def twilio_whatsapp(
    Body: str = Form(...),
    From: str = Form(...),
    To: str = Form(...)
):
    sender = From.replace("whatsapp:", "")
    receiver = To.replace("whatsapp:", "")
    body = (Body or "").strip()

    insert_message("inbound", sender, receiver, body)
    resp = MessagingResponse()

    try:
        text_upper = body.upper()

        if text_upper.startswith("KAYIT"):
            # KAYIT Ad Soyad; +905xx...; Paket; YYYY-MM-DD
            payload = body[5:].strip()
            parts = [p.strip() for p in payload.split(";")]
            if len(parts) != 4:
                resp.message("Format: KAYIT Ad Soyad; +905xx...; Paket; YYYY-MM-DD")
                return PlainTextResponse(str(resp), media_type="application/xml")

            name, phone, package_name, check_date = parts
            pid = find_or_create_patient(name=name, phone=phone)
            cid = create_checkup(patient_id=pid, package_name=package_name, check_date=check_date)
            default_tasks = [
                "Kan Tahlili",
                "EKG",
                "Radyoloji (Akciğer)",
                "Vücut Analizi",
                "Son Doktor Değerlendirmesi"
            ]
            add_tasks(cid, default_tasks)
            resp.message(
                f"Kayıt oluşturuldu. Hasta: {name}, Tarih: {check_date}, "
                f"Paket: {package_name}. Görev sayısı: {len(default_tasks)}"
            )
            return PlainTextResponse(str(resp), media_type="application/xml")

        elif text_upper.startswith("DURUM"):
            row = db_query("""
                SELECT c.id as checkup_id, p.name
                FROM checkups c
                JOIN patients p ON p.id=c.patient_id
                WHERE p.phone = ?
                ORDER BY c.check_date DESC LIMIT 1
            """, (sender,), fetchone=True)

            if not row:
                resp.message("Kayıt bulunamadı. 'KAYIT' komutunu deneyin.")
                return PlainTextResponse(str(resp), media_type="application/xml")

            tasks = list_tasks_for_checkup(row["checkup_id"])
            pending = [t["task_name"] for t in tasks if t["is_done"] == 0]
            done = [t["task_name"] for t in tasks if t["is_done"] == 1]
            msg = "Durum:\n- Bekleyen: " + (", ".join(pending) if pending else "Yok") + \
                  "\n- Tamamlanan: " + (", ".join(done) if done else "Yok")
            resp.message(msg)
            return PlainTextResponse(str(resp), media_type="application/xml")

        elif text_upper.startswith("YAPILDI"):
            # YAPILDI EKG
            task_name = body[7:].strip()
            row = db_query("""
                SELECT t.id as task_id
                FROM tasks t
                JOIN checkups c ON c.id = t.checkup_id
                JOIN patients p ON p.id = c.patient_id
                WHERE p.phone=? AND t.task_name LIKE ?
                ORDER BY c.check_date DESC, t.id ASC LIMIT 1
            """, (sender, f"%{task_name}%"), fetchone=True)

            if not row:
                resp.message("Görev bulunamadı. 'DURUM' ile kontrol edin.")
                return PlainTextResponse(str(resp), media_type="application/xml")

            mark_task_done(row["task_id"])
            resp.message(f"'{task_name}' tamamlandı olarak işaretlendi.")
            return PlainTextResponse(str(resp), media_type="application/xml")

        else:
            resp.message("Komutlar:\n- KAYIT Ad Soyad; +905xx...; Paket; YYYY-MM-DD\n- DURUM\n- YAPILDI GörevAdı")
            return PlainTextResponse(str(resp), media_type="application/xml")

    except Exception as e:
        resp.message(f"Hata: {e}")
        return PlainTextResponse(str(resp), media_type="application/xml")

# ========= API Sunucusunu Arka Planda Başlat =========
_api_started = False
def start_api_server_once():
    """Uvicorn’u 8000’de başlat. Streamlit Cloud dışarıya açmasa da, kod hatasız sürer."""
    global _api_started
    if _api_started:
        return
    def run():
        try:
            uvicorn.run(api, host="0.0.0.0", port=8000, log_level="warning")
        except Exception:
            # Ortam izin vermezse sessiz geç (UI çalışmaya devam etsin)
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    _api_started = True

# ========= Basit Giriş (opsiyonel) =========
def check_basic_auth():
    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False
    if st.session_state.auth_ok:
        return True
    with st.sidebar:
        st.subheader("🔐 Giriş")
        u = st.text_input("Kullanıcı adı", value="", key="auth_user")
        p = st.text_input("Parola", type="password", value="", key="auth_pass")
        if st.button("Giriş"):
            if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                st.session_state.auth_ok = True
                st.success("Giriş başarılı.")
                st.rerun()
            else:
                st.error("Hatalı kullanıcı adı/parola")
    return st.session_state.auth_ok

# ========= UI =========
def ui_header():
    st.title("🏥 Check-up Takip Sistemi")
    st.caption("Streamlit + Twilio WhatsApp")

def ui_send_message_section():
    st.subheader("📤 Hastaya WhatsApp Mesajı Gönder")
    phone = st.text_input("Telefon (+90...)", placeholder="+905xx...")
    msg = st.text_area("Mesaj", placeholder="Merhaba, randevunuz ...")
    col1, col2 = st.columns([1,1])
    with col1:
        if st.button("Gönder"):
            if not phone or not msg:
                st.warning("Telefon ve mesaj gerekli.")
            else:
                sid = send_whatsapp(phone, msg)
                if sid:
                    st.success(f"Gönderildi. SID: {sid}")
                else:
                    st.error("Gönderim hatası. Twilio bilgilerini kontrol edin.")
    with col2:
        st.info(f"Webhook: {WEBHOOK_HOST}/twilio/whatsapp")

def ui_new_checkup_section():
    st.subheader("📝 Yeni Check-up Kaydı")
    with st.form("new_checkup"):
        name = st.text_input("Ad Soyad")
        phone = st.text_input("Telefon (+90...)")
        pkg = st.text_input("Paket Adı", value="Standart")
        cdate = st.date_input("Tarih", value=date.today())
        tasks_text = st.text_area(
            "Görevler (her satıra bir)",
            value="Kan Tahlili\nEKG\nRadyoloji (Akciğer)\nVücut Analizi\nSon Doktor Değerlendirmesi"
        )
        submitted = st.form_submit_button("Kaydı Oluştur")
        if submitted:
            if not (name and phone and pkg and cdate):
                st.warning("Tüm alanlar zorunlu.")
            else:
                pid = find_or_create_patient(name, phone)
                cid = create_checkup(pid, pkg, cdate.isoformat())
                add_tasks(cid, tasks_text.splitlines())
                st.success(f"Kayıt oluşturuldu. Check-up ID: {cid}")

def ui_today_board():
    st.subheader("📆 Bugünün Check-up Listesi")
    rows = list_today_checkups()
    if not rows:
        st.info("Bugün için kayıt yok.")
        return
    for r in rows:
        with st.expander(f"{r['name']} • {r['package_name']} • {r['check_date']} • {r['phone']}"):
            tasks = list_tasks_for_checkup(r["checkup_id"])
            cols = st.columns([3,1])
            with cols[0]:
                for t in tasks:
                    checked = t["is_done"] == 1
                    label = f"{'✅' if checked else '⬜'} {t['task_name']}"
                    if not checked:
                        if st.button(f"Tamamla: {t['task_name']}", key=f"done_{t['id']}"):
                            mark_task_done(t["id"])
                            st.rerun()
                    else:
                        st.write(label)
            with cols[1]:
                if st.button("Durumu WhatsApp ile Gönder", key=f"msg_{r['checkup_id']}"):
                    pending = [t["task_name"] for t in tasks if t["is_done"] == 0]
                    done = [t["task_name"] for t in tasks if t["is_done"] == 1]
                    body = "Check-up Durumunuz:\n"
                    body += "- Bekleyen: " + (", ".join(pending) if pending else "Yok") + "\n"
                    body += "- Tamamlanan: " + (", ".join(done) if done else "Yok")
                    sid = send_whatsapp(r["phone"], body)
                    if sid:
                        st.success("Durum mesajı gönderildi.")
                    else:
                        st.error("Mesaj gönderilemedi (Twilio ayarlarını kontrol edin).")

def ui_messages_log():
    st.subheader("🗒️ Mesaj Günlüğü (Son 50)")
    rows = db_query("SELECT * FROM messages ORDER BY id DESC LIMIT 50")
    if rows:
        for m in rows:
            st.write(f"[{m['at']}] {m['direction'].upper()} | {m['sender']} → {m['receiver']}: {m['body']}")
    else:
        st.info("Kayıtlı mesaj yok.")

def main():
    db_init()
    start_api_server_once()

    if not check_basic_auth():
        st.stop()

    ui_header()
    with st.sidebar:
        st.markdown("**Bağlantılar**")
        st.markdown(f"- Webhook: `{WEBHOOK_HOST}/twilio/whatsapp`")
        st.markdown("- Komutlar:\n  - `KAYIT Ad; +905xx...; Paket; YYYY-MM-DD`\n  - `DURUM`\n  - `YAPILDI Görev`")

    ui_new_checkup_section()
    ui_today_board()
    ui_send_message_section()
    ui_messages_log()

if __name__ == "__main__":
    # Çalıştır: streamlit run app.py
    pass
