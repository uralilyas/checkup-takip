import os, sqlite3
from datetime import datetime, date
from contextlib import closing
import streamlit as st

# --- Config ---
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
            visit_date TEXT NOT NULL,
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

# --- Utils ---
def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def normalize_phone(p): return p.replace(" ","").replace("-","")

# --- Personnel ---
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q = "SELECT id,name,phone,active FROM personnel"
        if active_only: q += " WHERE active=1"
        c.execute(q)
        return c.fetchall()

def add_personnel(name, phone):
    phone = normalize_phone(phone)
    if not phone.startswith("+"):
        raise ValueError("Numara + ile başlamalı")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO personnel(name,phone,created_at) VALUES(?,?,?)",
                  (name.strip(), phone.strip(), now()))

def delete_personnel(pid):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM personnel WHERE id=?", (pid,))
        c.execute("DELETE FROM msg_logs WHERE personnel_id=?", (pid,))

# --- Patients ---
def add_patient(fn, ln, age, gender, visit_date):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patients(first_name,last_name,age,gender,visit_date,created_at)
                     VALUES(?,?,?,?,?,?)""",
                  (fn.strip(), ln.strip(), age, gender, visit_date, now()))

def delete_patient(pid):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE patient_id=?", (pid,))
        c.execute("DELETE FROM patients WHERE id=?", (pid,))

def list_patients(visit_date=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date:
            c.execute("""SELECT id,first_name,last_name,age,gender FROM patients
                         WHERE visit_date=? ORDER BY last_name""", (visit_date,))
        else:
            c.execute("SELECT id,first_name,last_name,age,gender FROM patients ORDER BY last_name")
        return c.fetchall()

# --- Patient Tests ---
def add_patient_test(pid, test_name):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO patient_tests(patient_id,test_name,status,updated_at) VALUES(?,?,?,?)",
                  (pid, test_name.strip(), "bekliyor", now()))

def list_patient_tests(pid=None, status=None):
    q = """SELECT t.id,t.patient_id,p.first_name,p.last_name,t.test_name,t.status,t.updated_at
           FROM patient_tests t JOIN patients p ON p.id=t.patient_id"""
    conds, params = [], []
    if pid: conds.append("t.patient_id=?"); params.append(pid)
    if status: conds.append("t.status=?"); params.append(status)
    if conds: q += " WHERE " + " AND ".join(conds)
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute(q, tuple(params))
        return c.fetchall()

def update_patient_test_status(tid, new_status):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patient_tests SET status=?, updated_at=? WHERE id=?",
                  (new_status, now(), tid))

# --- Messaging ---
def send_whatsapp_message(to_phone, body):
    if not _twilio_ok:
        return False, "Twilio yok"
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        return False, "Twilio ayarları eksik"
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{normalize_phone(to_phone)}",
            body=body
        )
        return True, msg.sid
    except Exception as e:
        return False, str(e)

def log_message(pid, body, ok, info):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO msg_logs(personnel_id,body,result,info,created_at)
                     VALUES(?,?,?,?,?)""",
                  (pid, body, "ok" if ok else "hata", info, now()))

# --- Auto message on complete ---
def auto_message_for_patient(pid):
    # hastanın tetkik listesini al
    tests_all = list_patient_tests(pid)
    done = [t[4] for t in tests_all if t[5]=="tamamlandi"]
    remain = [t[4] for t in tests_all if t[5]=="bekliyor"]
    # hasta adı
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT first_name,last_name FROM patients WHERE id=?", (pid,))
        fn, ln = c.fetchone()
    msg_body = f"{fn} {ln} için tetkik güncellemesi:\nTamamlananlar: {', '.join(done) if done else '-'}\nKalanlar: {', '.join(remain) if remain else '-'}"
    # tüm personele gönder
    for staff in list_personnel(active_only=True):
        ok, info = send_whatsapp_message(staff[2], msg_body)
        log_message(staff[0], msg_body, ok, info)

# --- Auth ---
def require_login():
    if "auth" not in st.session_state:
        st.session_state.auth = {"logged_in": False}
    if not st.session_state.auth["logged_in"]:
        u = st.text_input("Kullanıcı", key="user")
        p = st.text_input("Parola", type="password", key="pass")
        if st.button("Giriş", key="login_btn"):
            if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                st.session_state.auth["logged_in"] = True
                st.rerun()
            else:
                st.error("Hatalı giriş")
        st.stop()

# --- UI ---
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
st.title("✅ Check-up Takip Sistemi")
require_login()

selected_date = st.sidebar.date_input("📅 Tarih seç", value=date.today())
tab_hasta, tab_tetkik, tab_ozet, tab_mesaj, tab_personel = st.tabs(
    ["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti", "📲 WhatsApp Mesaj", "👥 Personel"]
)

# --- Hastalar ---
with tab_hasta:
    st.subheader(f"{selected_date} - Hasta Listesi")
    pts = list_patients(visit_date=str(selected_date))
    st.dataframe([{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Yaş":p[3], "Cinsiyet":p[4]} for p in pts])
    with st.form("hasta_add"):
        fn = st.text_input("Ad")
        ln = st.text_input("Soyad")
        age = st.number_input("Yaş",0,120)
        gender = st.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        if st.form_submit_button("Ekle", key="hasta_ekle_btn"):
            add_patient(fn, ln, age, gender, str(selected_date))
            st.rerun()
    if pts:
        sel = st.selectbox("Silinecek", [(p[0], f"{p[1]} {p[2]}") for p in pts])
        if st.button("Sil", key="hasta_sil_btn"):
            delete_patient(sel[0])
            st.rerun()

# --- Tetkik ---
with tab_tetkik:
    pts = list_patients(visit_date=str(selected_date))
    if not pts:
        st.info("Hasta yok")
    else:
        pid = st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts])[0]
        with st.form("tetkik_add"):
            tname = st.text_input("Tetkik adı")
            if st.form_submit_button("Ekle", key="tetkik_ekle_btn"):
                add_patient_test(pid, tname)
                st.rerun()
        trs = list_patient_tests(pid)
        for t in trs:
            cols = st.columns([4,1,1])
            cols[0].write(f"{t[4]} — {t[5]}")
            if t[5]=="bekliyor":
                if cols[1].button("Tamamla", key=f"done_{t[0]}"):
                    update_patient_test_status(t[0], "tamamlandi")
                    auto_message_for_patient(pid)
                    st.rerun()
            else:
                if cols[2].button("Geri Al", key=f"undo_{t[0]}"):
                    update_patient_test_status(t[0], "bekliyor")
                    auto_message_for_patient(pid)
                    st.rerun()

# --- Gün Özeti ---
with tab_ozet:
    st.subheader(f"{selected_date} Gün Özeti")
    pts = list_patients(visit_date=str(selected_date))
    summary = []
    for p in pts:
        tests = list_patient_tests(p[0])
        done = [t[4] for t in tests if t[5]=="tamamlandi"]
        remain = [t[4] for t in tests if t[5]=="bekliyor"]
        summary.append({
            "Hasta": f"{p[1]} {p[2]}",
            "Tamamlanan": ", ".join(done) if done else "-",
            "Kalan": ", ".join(remain) if remain else "-"
        })
    st.dataframe(summary)

# --- Mesaj ---
with tab_mesaj:
    staff = list_personnel(active_only=True)
    sel_staff = st.multiselect("Personel", [(s[0], s[1]) for s in staff])
    msg = st.text_area("Mesaj")
    if st.button("Gönder", key="mesaj_gonder_btn"):
        for s in sel_staff:
            phone = [x[2] for x in staff if x[0]==s[0]][0]
            ok, info = send_whatsapp_message(phone, msg)
            log_message(s[0], msg, ok, info)
        st.success("Gönderildi")

# --- Personel ---
with tab_personel:
    st.dataframe([{"ID":p[0], "Ad Soyad":p[1], "Tel":p[2]} for p in list_personnel(False)])
    with st.form("personel_add"):
        name = st.text_input("Ad Soyad")
        phone = st.text_input("Tel (+90...)")
        if st.form_submit_button("Ekle", key="personel_ekle_btn"):
            add_personnel(name, phone)
            st.rerun()
    allp = list_personnel(False)
    if allp:
        selp = st.selectbox("Silinecek", [(p[0], p[1]) for p in allp])
        if st.button("Sil", key="personel_sil_btn"):
            delete_personnel(selp[0])
            st.rerun()
