# app.py
import os, sqlite3, csv, io, threading, time
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

# ================== CSS & JS ==================
st.markdown("""
<style>
/* Fade-in effect for content */
.main > div {
    animation: fadeIn 0.4s ease-in-out;
}
@keyframes fadeIn {
    from {opacity: 0; transform: translateY(10px);}
    to {opacity: 1; transform: translateY(0);}
}
/* Button hover */
button[kind="primary"]:hover {
    transform: scale(1.05);
    transition: all 0.2s ease-in-out;
}
/* Table row highlight */
.highlight-done {
    background-color: #d4edda !important;
}
.highlight-pending {
    background-color: #f8f9fa !important;
}
</style>
""", unsafe_allow_html=True)

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
        if not column_exists(conn, "patients", "department"):
            c.execute("ALTER TABLE patients ADD COLUMN department TEXT")
            c.execute("UPDATE patients SET department='Genel' WHERE department IS NULL")
        if not column_exists(conn, "patients", "visit_time"):
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")  # HH:MM
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")
init_db()

# ================== UTILS ==================
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def to_iso(d:date) -> str: return d.strftime("%Y-%m-%d")
def to_display(d:date) -> str: return d.strftime("%d/%m/%Y")
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
        raise ValueError("Telefon + ile başlamalı (örn. +90...)")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO personnel(name,phone,created_at) VALUES(?,?,?)",
                  (name.strip(), phone.strip(), now_str()))
def delete_personnel(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM msg_logs WHERE personnel_id=?", (pid,))
        c.execute("DELETE FROM personnel WHERE id=?", (pid,))

# ================== PATIENTS ==================
def add_patient(fn:str, ln:str, age:int, gender:str, visit_date_iso:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patients(first_name,last_name,age,gender,visit_date,created_at,department,visit_time)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (fn.strip(), ln.strip(), age, gender, visit_date_iso, now_str(), "Genel", None))
def delete_patient(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE patient_id=?", (pid,))
        c.execute("DELETE FROM patients WHERE id=?", (pid,))
def list_patients(visit_date_iso:str|None=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date_iso:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients WHERE visit_date=?
                         ORDER BY last_name, first_name""", (visit_date_iso,))
        else:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients ORDER BY visit_date DESC, last_name""")
        return c.fetchall()
def set_patient_alarm_time(patient_id:int, hhmm:str|None):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patients SET visit_time=? WHERE id=?", (hhmm, patient_id))

# ================== TESTS ==================
def add_patient_test(patient_id:int, test_name:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patient_tests(patient_id,test_name,status,updated_at)
                     VALUES(?,?,?,?)""",
                  (patient_id, test_name.strip(), "bekliyor", now_str()))
def list_patient_tests(patient_id:int|None=None, status:str|None=None):
    q = """SELECT t.id,t.patient_id,p.first_name,p.last_name,t.test_name,t.status,t.updated_at
           FROM patient_tests t JOIN patients p ON p.id=t.patient_id"""
    conds, params = [], []
    if patient_id: conds.append("t.patient_id=?"); params.append(patient_id)
    if status: conds.append("t.status=?"); params.append(status)
    if conds: q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY t.updated_at DESC"
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute(q, tuple(params))
        return c.fetchall()
def update_patient_test_status(test_id:int, new_status:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patient_tests SET status=?, updated_at=? WHERE id=?",
                  (new_status, now_str(), test_id))
def delete_patient_test(test_id:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE id=?", (test_id,))

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
def auto_message_for_patient(pid:int):
    trs = list_patient_tests(pid)
    done = [t[4] for t in trs if t[5] == "tamamlandi"]
    remain = [t[4] for t in trs if t[5] == "bekliyor"]
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT first_name,last_name FROM patients WHERE id=?", (pid,))
        row = c.fetchone()
    if not row: return
    fn, ln = row
    body = (f"📌 Tetkik Güncellemesi\n"
            f"Hasta: {fn} {ln}\n"
            f"Tamamlananlar: {', '.join(done) if done else '-'}\n"
            f"Kalanlar: {', '.join(remain) if remain else '-'}")
    for staff in list_personnel(active_only=True):
        ok, info = send_whatsapp_message(staff[2], body)
        log_message(staff[0], body, ok, info)

# ================== ALARM ==================
def check_alarms_loop():
    while True:
        today_iso = datetime.now().strftime("%Y-%m-%d")
        now_plus_10 = (datetime.now() + timedelta(minutes=10)).strftime("%H:%M")
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute("""SELECT first_name,last_name,department,visit_time
                         FROM patients
                         WHERE visit_date=? AND visit_time=?""", (today_iso, now_plus_10))
            matches = c.fetchall()
        if matches:
            for (fn, ln, dept, _vtime) in matches:
                body = (f"📅 Hatırlatma:\n"
                        f"{fn} {ln}'ın 10 dakika sonra {dept or 'İlgili bölüm'} randevusu var.\n"
                        f"Lütfen bölüm ile teyit sağlayın ve hastaya eşlik edin.")
                for staff in list_personnel(active_only=True):
                    ok, info = send_whatsapp_message(staff[2], body)
                    log_message(staff[0], body, ok, info)
        time.sleep(60)
if "alarm_thread_started" not in st.session_state:
    threading.Thread(target=check_alarms_loop, daemon=True).start()
    st.session_state.alarm_thread_started = True

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
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
st.title("✅ Check-up Takip Sistemi")
require_login()

picked_date = st.sidebar.date_input("📅 Tarih seç", value=date.today(), key="dt_pick")
sel_iso = to_iso(picked_date)
sel_disp = to_display(picked_date)

with st.sidebar:
    st.divider()
    st.subheader("🔌 Sistem")
    st.write("• Twilio:", "✅" if (_twilio_ok and TWILIO_ACCOUNT_SID) else "⚠️ Ayarları kontrol edin")
    st.write("• Tarih:", sel_disp)
    if st.button("🚪 Çıkış", key="btn_logout"):
        st.session_state.auth["logged_in"] = False
        st.rerun()

tab_hasta, tab_tetkik, tab_ozet = st.tabs(["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti"])

# -------- Hastalar --------
with tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts = list_patients(visit_date_iso=sel_iso)
    st.dataframe([{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Alarm Saati":p[6] or "-"} for p in pts])

    st.markdown("### ➕ Hasta Ekle")
    with st.form("frm_add_patient", clear_on_submit=True):
        c1,c2,c3 = st.columns([2,2,1])
        fn = c1.text_input("Ad")
        ln = c2.text_input("Soyad")
        age = c3.number_input("Yaş", 0, 120, 0, 1)
        gender = st.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        submitted = st.form_submit_button("Ekle")
    if submitted:
        if fn.strip() and ln.strip():
            add_patient(fn, ln, int(age), gender, sel_iso)
            st.success(f"Eklendi: {fn} {ln}")
            st.rerun()

# -------- Tetkik Takibi --------
with tab_tetkik:
    pts_today = list_patients(visit_date_iso=sel_iso)
    if pts_today:
        sel = st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts_today], format_func=lambda x: x[1])
        pid = sel[0]
        st.markdown("#### Tetkik Ekle")
        with st.form("frm_add_test", clear_on_submit=True):
            tname = st.text_input("Tetkik adı")
            alarm_check = st.checkbox("🔔 Alarm kurmak istiyorum")
            alarm_hhmm = None
            if alarm_check:
                colh, colm = st.columns(2)
                hour = colh.selectbox("Saat", [f"{h:02d}" for h in range(0,24)])
                minute = colm.selectbox("Dakika", [f"{m:02d}" for m in range(0,60,5)])
                alarm_hhmm = f"{hour}:{minute}"
            addt = st.form_submit_button("Ekle")
        if addt and tname.strip():
            add_patient_test(pid, tname)
            if alarm_check and alarm_hhmm:
                set_patient_alarm_time(pid, alarm_hhmm)
            st.success("Tetkik eklendi")
            st.rerun()

        st.markdown("#### Tetkikler")
        trs = list_patient_tests(patient_id=pid)
        for t in trs:
            tid, _, fn, ln, tname, tstatus, updated = t
            icon = "✅" if tstatus=="tamamlandi" else "⏳"
            cols = st.columns([5,1,1,1])
            cols[0].markdown(f"{icon} **{tname}** — {updated}")
            if tstatus == "bekliyor":
                if cols[1].button("Tamamla", key=f"done_{tid}"):
                    update_patient_test_status(tid, "tamamlandi")
                    auto_message_for_patient(pid)
                    st.rerun()
            else:
                if cols[2].button("Geri Al", key=f"undo_{tid}"):
                    update_patient_test_status(tid, "bekliyor")
                    auto_message_for_patient(pid)
                    st.rerun()
            if cols[3].button("Sil", key=f"del_{tid}"):
                delete_patient_test(tid)
                st.rerun()

# -------- Gün Özeti --------
with tab_ozet:
    pts = list_patients(visit_date_iso=sel_iso)
    if pts:
        rows = []
        for p in pts:
            tests = list_patient_tests(p[0])
            done = [f"✅ {t[4]}" for t in tests if t[5]=="tamamlandi"]
            remain = [f"⏳ {t[4]}" for t in tests if t[5]=="bekliyor"]
            rows.append({
                "Hasta": f"{p[1]} {p[2]}",
                "Alarm Saati": p[6] or "-",
                "Tamamlanan": ", ".join(done) if done else "-",
                "Kalan": ", ".join(remain) if remain else "-"
            })
        st.dataframe(rows)
