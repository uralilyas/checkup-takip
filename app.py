# app.py
import os, sqlite3, csv, io, threading, time
from datetime import datetime, date, timedelta
from contextlib import closing
import streamlit as st

# --- CONFIG ---
DB_PATH = "checkup.db"
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

try:
    from twilio.rest import Client
    _twilio_ok = True
except:
    _twilio_ok = False

# --- DB ---
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS patients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            age INTEGER,
            gender TEXT,
            department TEXT,
            visit_date TEXT NOT NULL,
            visit_time TEXT,
            created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id))""")
init_db()

# --- UTILS ---
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def normalize_phone(p): return p.replace(" ","").replace("-","")

# --- PERSONNEL ---
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q = "SELECT id,name,phone,active FROM personnel"
        if active_only: q += " WHERE active=1"
        q += " ORDER BY name"
        c.execute(q)
        return c.fetchall()

def add_personnel(name, phone):
    phone = normalize_phone(phone)
    if not phone.startswith("+"):
        raise ValueError("Numara + ile başlamalı (örn. +90...)")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO personnel(name,phone,created_at) VALUES(?,?,?)",
                  (name.strip(), phone.strip(), now_str()))

# --- PATIENTS ---
def add_patient(fn, ln, age, gender, dept, visit_date, visit_time):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patients(first_name,last_name,age,gender,department,visit_date,visit_time,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (fn.strip(), ln.strip(), age, gender, dept, visit_date, visit_time, now_str()))

def list_patients(visit_date=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients WHERE visit_date=? ORDER BY visit_time""", (visit_date,))
        else:
            c.execute("SELECT id,first_name,last_name,age,gender,department,visit_time FROM patients ORDER BY visit_date DESC")
        return c.fetchall()

# --- MESSAGING ---
def send_whatsapp_message(to_phone, body):
    if not _twilio_ok: return False, "Twilio yok"
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        return False, "Twilio ayarları eksik"
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{normalize_phone(to_phone)}",
            body=body
        )
        return True, getattr(msg, "sid", "")
    except Exception as e:
        return False, str(e)

def log_message(personnel_id, body, ok, info):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO msg_logs(personnel_id,body,result,info,created_at)
                     VALUES(?,?,?,?,?)""",
                  (personnel_id, body, "ok" if ok else "hata", info[:200], now_str()))

# --- ALARM ---
def check_alarms():
    while True:
        today = datetime.now().strftime("%Y-%m-%d")
        now_plus_10 = (datetime.now() + timedelta(minutes=10)).strftime("%H:%M")
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute("""SELECT id,first_name,last_name,department,visit_time FROM patients
                         WHERE visit_date=? AND visit_time=?""", (today, now_plus_10))
            matches = c.fetchall()
        for pid, fn, ln, dept, vtime in matches:
            body = (f"📅 Hatırlatma:\n"
                    f"{fn} {ln}'ın 10 dakika sonra {dept} randevusu bulunmaktadır.\n"
                    f"Lütfen bölüm ile teyit sağlayarak hastaya eşlik ediniz.")
            for staff in list_personnel(active_only=True):
                ok, info = send_whatsapp_message(staff[2], body)
                log_message(staff[0], body, ok, info)
        time.sleep(60)

# Arka planda alarm kontrolünü başlat
threading.Thread(target=check_alarms, daemon=True).start()

# --- AUTH ---
def require_login():
    if "auth" not in st.session_state:
        st.session_state.auth = {"logged_in": False}
    if not st.session_state.auth["logged_in"]:
        with st.form("login_form"):
            st.subheader("🔐 Giriş")
            u = st.text_input("Kullanıcı Adı")
            p = st.text_input("Parola", type="password")
            if st.form_submit_button("Giriş Yap"):
                if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                    st.session_state.auth["logged_in"] = True
                    st.success("Giriş başarılı")
                    st.rerun()
                else:
                    st.error("Hatalı giriş")
        st.stop()

# --- UI ---
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
st.title("✅ Check-up Takip Sistemi")
require_login()

selected_date = st.sidebar.date_input("📅 Tarih seç", value=date.today())
sel_date_str = selected_date.strftime("%Y-%m-%d")

with st.sidebar:
    st.write("• Twilio:", "✅" if (_twilio_ok and TWILIO_ACCOUNT_SID) else "⚠️")
    st.write("• Tarih:", sel_date_str)
    if st.button("🚪 Çıkış"):
        st.session_state.auth["logged_in"] = False
        st.rerun()

tab_hasta, tab_personel = st.tabs(["🧑‍⚕️ Hastalar", "👥 Personel"])

with tab_hasta:
    st.subheader(f"{sel_date_str} - Hasta Listesi")
    pts = list_patients(sel_date_str)
    st.dataframe(
        [{"Ad":p[1],"Soyad":p[2],"Bölüm":p[5],"Saat":p[6]} for p in pts],
        use_container_width=True
    )

    st.markdown("### ➕ Hasta Ekle")
    with st.form("hasta_add", clear_on_submit=True):
        c1,c2,c3 = st.columns(3)
        fn = c1.text_input("Ad")
        ln = c2.text_input("Soyad")
        age = c3.number_input("Yaş", 0, 120, 0, 1)
        gender = st.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        dept = st.selectbox("Bölüm", ["Kardiyoloji","Dahiliye","Göz","Genel Cerrahi"])
        vtime = st.time_input("Randevu Saati")
        submitted = st.form_submit_button("Ekle")
    if submitted:
        add_patient(fn, ln, age, gender, dept, sel_date_str, vtime.strftime("%H:%M"))
        st.success(f"Hasta eklendi: {fn} {ln}")
        st.rerun()

with tab_personel:
    st.subheader("👥 Personel")
    people = list_personnel(active_only=False)
    st.dataframe([{"Ad Soyad":p[1],"Telefon":p[2]} for p in people])
    with st.form("personel_add", clear_on_submit=True):
        name = st.text_input("Ad Soyad")
        phone = st.text_input("Tel (+90...)")
        addp = st.form_submit_button("Ekle")
    if addp:
        add_personnel(name, phone)
        st.success("Personel eklendi")
        st.rerun()
