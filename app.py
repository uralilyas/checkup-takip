# app.py (Streamlit – Postgres)
import os, threading
from datetime import datetime, date
from typing import List, Optional

import streamlit as st
from twilio.rest import Client
import psycopg2

# ---- Secrets ----
def S(name, default=""):
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)

ADMIN_USERNAME   = S("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD   = S("ADMIN_PASSWORD", "changeme")
TWILIO_SID       = S("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN     = S("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM      = S("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
WEBHOOK_HOST     = S("WEBHOOK_HOST", "")
DATABASE_URL     = S("DATABASE_URL")  # ...?sslmode=require

# ---- DB helpers ----
def db_conn():
    return psycopg2.connect(DATABASE_URL)

def db_exec(sql, params=None):
    with db_conn() as con:
        with con.cursor() as cur:
            cur.execute(sql, params or ())

def db_fetchall(sql, params=None):
    with db_conn() as con:
        with con.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

def db_fetchone(sql, params=None):
    with db_conn() as con:
        with con.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

def db_init():
    db_exec("""
    CREATE TABLE IF NOT EXISTS patients (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE
    );""")
    db_exec("""
    CREATE TABLE IF NOT EXISTS checkups (
        id SERIAL PRIMARY KEY,
        patient_id INTEGER NOT NULL REFERENCES patients(id),
        package_name TEXT NOT NULL,
        check_date DATE NOT NULL
    );""")
    db_exec("""
    CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        checkup_id INTEGER NOT NULL REFERENCES checkups(id),
        task_name TEXT NOT NULL,
        is_done BOOLEAN NOT NULL DEFAULT FALSE,
        done_at TIMESTAMP
    );""")
    db_exec("""
    CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        direction TEXT NOT NULL,
        sender TEXT NOT NULL,
        receiver TEXT NOT NULL,
        body TEXT NOT NULL,
        at TIMESTAMP NOT NULL
    );""")

def find_or_create_patient(name: str, phone: str) -> int:
    r = db_fetchone("SELECT id FROM patients WHERE phone=%s", (phone,))
    if r: return r[0]
    db_exec("INSERT INTO patients(name, phone) VALUES(%s,%s)", (name, phone))
    r = db_fetchone("SELECT id FROM patients WHERE phone=%s", (phone,))
    return r[0]

def create_checkup(patient_id: int, package_name: str, check_date: str) -> int:
    db_exec("INSERT INTO checkups(patient_id, package_name, check_date) VALUES(%s,%s,%s)",
            (patient_id, package_name, check_date))
    r = db_fetchone("SELECT currval(pg_get_serial_sequence('checkups','id'))")
    return r[0]

def add_tasks(checkup_id: int, tasks: List[str]):
    for t in tasks:
        t = t.strip()
        if t:
            db_exec("INSERT INTO tasks(checkup_id, task_name) VALUES(%s,%s)", (checkup_id, t))

def list_today_checkups():
    return db_fetchall("""
        SELECT c.id, p.name, p.phone, c.package_name, c.check_date
        FROM checkups c
        JOIN patients p ON p.id=c.patient_id
        WHERE c.check_date = CURRENT_DATE
        ORDER BY p.name
    """)

def list_tasks_for_checkup(cid: int):
    return db_fetchall("SELECT id, task_name, is_done FROM tasks WHERE checkup_id=%s ORDER BY id", (cid,))

def mark_task_done(tid: int):
    db_exec("UPDATE tasks SET is_done=TRUE, done_at=%s WHERE id=%s", (datetime.now(), tid))

def insert_message(direction: str, sender: str, receiver: str, body: str):
    db_exec("INSERT INTO messages(direction, sender, receiver, body, at) VALUES(%s,%s,%s,%s,%s)",
            (direction, sender, receiver, body, datetime.now()))

# ---- Twilio send ----
def send_whatsapp(to_phone: str, body: str) -> Optional[str]:
    if not to_phone.startswith("whatsapp:"):
        to_phone = f"whatsapp:{to_phone}"
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        msg = client.messages.create(from_=TWILIO_FROM, to=to_phone, body=body)
        insert_message("outbound", TWILIO_FROM, to_phone, body)
        return msg.sid
    except Exception as e:
        st.error(f"Twilio gönderim hatası: {e}")
        return None

# ---- Auth ----
def ensure_auth():
    if "ok" not in st.session_state: st.session_state.ok = False
    if st.session_state.ok: return True
    with st.sidebar:
        st.subheader("🔐 Giriş")
        u = st.text_input("Kullanıcı adı")
        p = st.text_input("Parola", type="password")
        if st.button("Giriş"):
            if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                st.session_state.ok = True
                st.success("Giriş başarılı.")
                st.rerun()
            else:
                st.error("Hatalı bilgiler")
    return st.session_state.ok

# ---- UI ----
st.set_page_config(page_title="Check-up Takip", page_icon="🏥", layout="wide")
db_init()

if not ensure_auth():
    st.stop()

st.title("🏥 Check-up Takip Sistemi")
st.caption("Streamlit (UI) + Render (Webhook) + Postgres (ortak veritabanı)")
with st.sidebar:
    st.markdown(f"**Webhook:** `{S('WEBHOOK_HOST','')}/twilio/whatsapp`")
    st.markdown("Komutlar (WhatsApp): `KAYIT / DURUM / YAPILDI`")

# Yeni kayıt
st.subheader("📝 Yeni Check-up Kaydı")
with st.form("new"):
    name  = st.text_input("Ad Soyad")
    phone = st.text_input("Telefon (+90...)")
    pkg   = st.text_input("Paket", value="Standart")
    cdate = st.date_input("Tarih", value=date.today())
    tasks = st.text_area("Görevler (her satıra bir)",
                         "Kan Tahlili\nEKG\nRadyoloji (Akciğer)\nVücut Analizi\nSon Doktor Değerlendirmesi")
    if st.form_submit_button("Kaydı Oluştur"):
        if not (name and phone and pkg and cdate):
            st.warning("Tüm alanlar zorunlu.")
        else:
            pid = find_or_create_patient(name, phone)
            cid = create_checkup(pid, pkg, cdate.isoformat())
            add_tasks(cid, tasks.splitlines())
            st.success(f"Kayıt oluşturuldu (ID: {cid}).")

# Bugünün listesi
st.subheader("📆 Bugünün Check-up Listesi")
rows = list_today_checkups()
if not rows:
    st.info("Bugün için kayıt yok.")
else:
    for (cid, pname, pphone, pkg, cdate) in rows:
        with st.expander(f"{pname} • {pkg} • {cdate} • {pphone}"):
            trows = list_tasks_for_checkup(cid)
            cols = st.columns([3,1])
            with cols[0]:
                for (tid, tname, done) in trows:
                    if not done:
                        if st.button(f"Tamamla: {tname}", key=f"done_{tid}"):
                            mark_task_done(tid)
                            st.rerun()
                    else:
                        st.write(f"✅ {tname}")
            with cols[1]:
                if st.button("Durumu WhatsApp ile Gönder", key=f"msg_{cid}"):
                    pending = [t for (_, t, d) in trows if not d]
                    done    = [t for (_, t, d) in trows if d]
                    body = "Check-up Durumunuz:\n"
                    body += "- Bekleyen: " + (", ".join(pending) if pending else "Yok") + "\n"
                    body += "- Tamamlanan: " + (", ".join(done)    if done    else "Yok")
                    if send_whatsapp(pphone, body):
                        st.success("Durum mesajı gönderildi.")
                    else:
                        st.error("Mesaj gönderilemedi.")

# Mesaj günlüğü
st.subheader("🗒️ Mesaj Günlüğü (Son 50)")
mrows = db_fetchall("SELECT direction, sender, receiver, body, at FROM messages ORDER BY id DESC LIMIT 50")
if not mrows:
    st.info("Henüz mesaj yok.")
else:
    for d, s, r, b, at in mrows:
        st.write(f"[{at}] {d.upper()} | {s} → {r}: {b}")
