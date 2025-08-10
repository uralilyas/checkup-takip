# app.py — Sadece personele mesaj gönderir
import os
import time
from datetime import date
import streamlit as st
from twilio.rest import Client
import psycopg2

# ---- Secrets / Config ----
def S(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)

ADMIN_USERNAME = S("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = S("ADMIN_PASSWORD", "changeme")
TWILIO_SID     = S("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = S("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM    = S("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
STAFF_TO       = S("STAFF_WHATSAPP_TO", "")
DATABASE_URL   = S("DATABASE_URL", "")
DEBUG          = S("DEBUG", "false").lower() == "true"

st.set_page_config(page_title="Check-up Takip", layout="wide")

# ---- Auth ----
def ensure_auth():
    if "ok" not in st.session_state: st.session_state.ok = False
    if st.session_state.ok:
        return True
    with st.sidebar:
        st.subheader("🔐 Giriş")
        u = st.text_input("Kullanıcı adı")
        p = st.text_input("Parola", type="password")
        if st.button("Giriş"):
            if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                st.session_state.ok = True
                st.rerun()
            else:
                st.error("Hatalı bilgiler")
    return st.session_state.ok

if not ensure_auth():
    st.stop()

# ---- Güvenli DB init ----
def db_conn():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=5,
    )

def db_exec(sql, params=None):
    with db_conn() as con:
        with con.cursor() as cur:
            cur.execute(sql, params or ())
            try:
                return cur.fetchall()
            except psycopg2.ProgrammingError:
                return None

def db_init_safe():
    retries = 3
    for attempt in range(1, retries+1):
        try:
            db_exec("""
                CREATE TABLE IF NOT EXISTS patients (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    pkg TEXT NOT NULL,
                    cdate DATE NOT NULL
                );
            """)
            return True
        except Exception as e:
            if attempt == retries:
                st.error("Veritabanına bağlanılamadı. Lütfen sonra tekrar deneyin.")
                if DEBUG: st.caption(str(e))
                return False
            time.sleep(2)
            continue

if "db_ready" not in st.session_state:
    st.session_state.db_ready = db_init_safe()

# ---- In-memory kayıtlar (DB kapalıysa) ----
if "records" not in st.session_state:
    st.session_state.records = []

# ---- WhatsApp gönderim fonksiyonu ----
def send_whatsapp_to_staff(body: str):
    try:
        to_number = f"whatsapp:{STAFF_TO}" if not STAFF_TO.startswith("whatsapp:") else STAFF_TO
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(from_=TWILIO_FROM, to=to_number, body=body)
        return True, None
    except Exception as e:
        return False, str(e)

# ---- UI ----
st.title("✅ Check-up Takip (Sadece Personel Mesajı)")
st.caption("Hasta numarası alınmaz, tüm bildirimler personele gider.")

# Yeni kayıt formu
st.subheader("📝 Yeni Check-up Kaydı")
with st.form("new"):
    name  = st.text_input("Ad Soyad")
    pkg   = st.text_input("Paket", value="Standart")
    cdate = st.date_input("Tarih", value=date.today())
    tasks_raw = st.text_area("Görevler (her satır bir görev)",
                             "Kan Tahlili\nEKG\nRadyoloji (Akciğer)\nVücut Analizi\nSon Doktor Değerlendirmesi")
    if st.form_submit_button("Kaydı Ekle"):
        if not name:
            st.warning("Ad zorunlu.")
        else:
            tasks = [{"title": t.strip(), "done": False} for t in tasks_raw.splitlines() if t.strip()]
            st.session_state.records.append({
                "name": name, "pkg": pkg, "cdate": cdate, "tasks": tasks
            })
            st.success(f"Kayıt eklendi: {name} • {pkg} • {cdate}")

# Bugünün listesi
st.subheader("📆 Bugünün Check-up Listesi")
if not st.session_state.records:
    st.info("Henüz kayıt yok.")
else:
    for idx, rec in enumerate(st.session_state.records):
        pending = [t for t in rec["tasks"] if not t["done"]]
        done    = [t for t in rec["tasks"] if t["done"]]
        with st.expander(f"{rec['name']} • {rec['pkg']} • {rec['cdate']}"):
            # görevler
            for j, t in enumerate(rec["tasks"]):
                col1, col2 = st.columns([6,2])
                with col1:
                    st.write(("✅ " if t["done"] else "⏳ ") + t["title"])
                with col2:
                    if not t["done"] and st.button("Tamamla", key=f"done_{idx}_{j}"):
                        t["done"] = True
                        st.rerun()

            # WhatsApp gönderim (sadece personele)
            if st.button("Durumu Personele WhatsApp ile Gönder", key=f"msg_{idx}"):
                body = f"Check-up Durumu ({rec['name']} - {rec['pkg']} - {rec['cdate']}):\n"
                body += "- Bekleyen: " + (", ".join([t['title'] for t in pending]) if pending else "Yok") + "\n"
                body += "- Tamamlanan: " + (", ".join([t['title'] for t in done]) if done else "Yok")
                ok, err = send_whatsapp_to_staff(body)
                if ok:
                    st.success("Personel WhatsApp mesajı gönderildi.")
                else:
                    st.error("Gönderilemedi.")
                    if DEBUG: st.caption(err)

st.divider()
st.caption("Versiyon 2.1 — Hasta numarası yok, sadece personele mesaj gönderilir.")
