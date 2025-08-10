# app.py
import os, sqlite3, threading, time
from datetime import datetime, date, timedelta
from contextlib import closing
import streamlit as st

# ================== CONFIG ==================
DB_PATH = "checkup.db"
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

try:
    from twilio.rest import Client
    _twilio_ok = True
except Exception:
    _twilio_ok = False

# ================== DB ==================
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def column_exists(conn, table, column) -> bool:
    with closing(conn.cursor()) as c:
        c.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in c.fetchall())

def init_db():
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS patients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            age INTEGER,
            gender TEXT,
            visit_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        # --- MIGRATIONS: add department + visit_time if missing ---
        if not column_exists(conn, "patients", "department"):
            c.execute("ALTER TABLE patients ADD COLUMN department TEXT")
            c.execute("UPDATE patients SET department = 'Genel' WHERE department IS NULL")
        if not column_exists(conn, "patients", "visit_time"):
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")  # "HH:MM"
            c.execute("UPDATE patients SET visit_time = '00:00' WHERE visit_time IS NULL")

        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,   -- ok | hata
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")

init_db()

# ================== UTILS ==================
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def to_iso(d:date) -> str: return d.strftime("%Y-%m-%d")
def to_display(d:date) -> str: return d.strftime("%d/%m/%Y")  # gün/ay/yıl
def normalize_phone(p:str)->str: return p.replace(" ","").replace("-","")

# ================== PERSONNEL ==================
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q = "SELECT id,name,phone,active FROM personnel"
        if active_only: q += " WHERE active=1"
        q += " ORDER BY name"
        c.execute(q)
        return c.fetchall()

def add_personnel(name:str, phone:str):
    phone = normalize_phone(phone)
    if not phone.startswith("+"):
        raise ValueError("Telefon + ile başlamalı (ör. +90...)")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO personnel(name,phone,created_at) VALUES(?,?,?)",
                  (name.strip(), phone.strip(), now_str()))

def delete_personnel(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM msg_logs WHERE personnel_id=?", (pid,))
        c.execute("DELETE FROM personnel WHERE id=?", (pid,))

# ================== PATIENTS ==================
def add_patient(fn:str, ln:str, age:int, gender:str, dept:str, visit_date_iso:str, visit_time_hhmm:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patients(first_name,last_name,age,gender,visit_date,created_at,department,visit_time)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (fn.strip(), ln.strip(), age, gender, visit_date_iso, now_str(), dept, visit_time_hhmm))

def delete_patient(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patients WHERE id=?", (pid,))

def list_patients(visit_date_iso:str|None=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date_iso:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients WHERE visit_date=? ORDER BY visit_time, last_name, first_name""",
                      (visit_date_iso,))
        else:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients ORDER BY visit_date DESC, visit_time""")
        return c.fetchall()

# ================== MESSAGING ==================
def send_whatsapp_message(to_phone:str, body:str)->tuple[bool,str]:
    if not _twilio_ok: return False, "Twilio paketi yok"
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        return False, "Twilio ortam değişkenleri eksik"
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{normalize_phone(to_phone)}",
            body=body
        )
        return True, getattr(msg, "sid", "ok")
    except Exception as e:
        return False, str(e)

def log_message(personnel_id:int, body:str, ok:bool, info:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO msg_logs(personnel_id,body,result,info,created_at)
                     VALUES(?,?,?,?,?)""",
                  (personnel_id, body, "ok" if ok else "hata", info[:500], now_str()))

# ================== ALARM (10 dk önce) ==================
def check_alarms_loop():
    """Her dakika bugüne ait randevuları kontrol eder, 10 dk kala personele WhatsApp atar."""
    while True:
        today_iso = datetime.now().strftime("%Y-%m-%d")
        now_plus_10 = (datetime.now() + timedelta(minutes=10)).strftime("%H:%M")
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute("""SELECT id,first_name,last_name,department,visit_time
                         FROM patients
                         WHERE visit_date=? AND visit_time=?""", (today_iso, now_plus_10))
            matches = c.fetchall()
        if matches:
            for (_pid, fn, ln, dept, vtime) in matches:
                body = (f"📅 Hatırlatma:\n"
                        f"{fn} {ln}'ın 10 dakika sonra {dept} randevusu bulunmaktadır.\n"
                        f"Lütfen bölüm ile teyit sağlayarak hastaya eşlik ediniz.")
                for staff in list_personnel(active_only=True):
                    ok, info = send_whatsapp_message(staff[2], body)
                    log_message(staff[0], body, ok, info)
        time.sleep(60)

# Arka planda alarmı başlat (daemon thread)
threading.Thread(target=check_alarms_loop, daemon=True).start()

# ================== AUTH ==================
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
                    st.success("Giriş başarılı.")
                    st.rerun()
                else:
                    st.error("Hatalı kullanıcı adı/parola.")
        st.stop()

# ================== UI ==================
st.set_page_config(page_title="Check-up Takip Sistemi", page_icon="✅", layout="wide")
st.title("✅ Check-up Takip Sistemi")
require_login()

# Tarih seçimi (görünüm DD/MM/YYYY; kayıt ISO)
selected_date = st.sidebar.date_input("📅 Tarih seç", value=date.today(), key="dt_pick")
sel_iso = to_iso(selected_date)
sel_disp = to_display(selected_date)

with st.sidebar:
    st.divider()
    st.subheader("🔌 Sistem")
    st.write("• Twilio:", "✅" if (_twilio_ok and TWILIO_ACCOUNT_SID) else "⚠️ Ayarları kontrol edin")
    st.write("• Tarih:", sel_disp)
    if st.button("🚪 Çıkış", key="btn_logout"):
        st.session_state.auth["logged_in"] = False
        st.rerun()

tab_hasta, tab_personel = st.tabs(["🧑‍⚕️ Hastalar", "👥 Personel"])

# -------- Hastalar --------
with tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts = list_patients(visit_date_iso=sel_iso)
    st.dataframe(
        [{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Bölüm":p[5] or "-", "Saat":p[6] or "-"} for p in pts],
        use_container_width=True
    )

    st.markdown("### ➕ Hasta Ekle")
    with st.form("frm_add_patient", clear_on_submit=True):
        c1,c2,c3 = st.columns([2,2,1])
        fn = c1.text_input("Ad")
        ln = c2.text_input("Soyad")
        age = c3.number_input("Yaş", 0, 120, 0, 1)
        c4,c5 = st.columns([2,2])
        gender = c4.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        dept = c5.selectbox("Bölüm", ["Kardiyoloji","Dahiliye","Göz","Genel Cerrahi","Radyoloji","Laboratuvar","Genel"])
        vtime = st.time_input("Randevu Saati", key="time_pick")
        submitted = st.form_submit_button("Ekle")
    if submitted:
        if not fn.strip() or not ln.strip():
            st.warning("Ad ve Soyad zorunludur.")
        else:
            try:
                add_patient(fn, ln, int(age), gender, dept, sel_iso, vtime.strftime("%H:%M"))
                st.success(f"Hasta eklendi: {fn} {ln} • {dept} • {vtime.strftime('%H:%M')}")
                st.rerun()
            except Exception as e:
                st.error(f"Hata: {e}")

    if pts:
        st.markdown("### 🗑️ Hasta Sil")
        choice = st.selectbox("Silinecek hasta", [(p[0], f"{p[1]} {p[2]} — {p[5]} {p[6]}") for p in pts],
                              format_func=lambda x: x[1], key="sel_del_patient")
        if st.button("Sil", type="primary", key="btn_del_patient"):
            delete_patient(choice[0])
            st.success("Hasta silindi.")
            st.rerun()
    else:
        st.caption("Bu tarihte kayıtlı hasta yok.")

# -------- Personel --------
with tab_personel:
    st.subheader("👥 Personel")
    people = list_personnel(active_only=False)
    st.dataframe(
        [{"ID":p[0], "Ad Soyad":p[1], "Telefon":p[2], "Aktif":"Evet" if p[3] else "Hayır"} for p in people],
        use_container_width=True
    )
    st.markdown("### ➕ Personel Ekle")
    with st.form("frm_add_staff", clear_on_submit=True):
        name = st.text_input("Ad Soyad")
        phone = st.text_input("Telefon (+90...)")
        ok = st.form_submit_button("Ekle")
    if ok:
        try:
            add_personnel(name, phone)
            st.success("Personel eklendi.")
            st.rerun()
        except Exception as e:
            st.error(f"Hata: {e}")

    if people:
        sel_staff = st.selectbox("Silinecek personel", [(p[0], f"{p[1]} ({p[2]})") for p in people],
                                 format_func=lambda x: x[1], key="sel_del_staff")
        if st.button("Sil", type="primary", key="btn_del_staff"):
            delete_personnel(sel_staff[0])
            st.success("Personel silindi.")
            st.rerun()
