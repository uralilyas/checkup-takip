# app.py
import os, sqlite3, csv, io, threading, time, random, hashlib
from datetime import datetime, date, timedelta
from contextlib import closing
import streamlit as st

# ================== CONFIG ==================
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")

DB_PATH = "checkup.db"
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # +14155238886 veya whatsapp:+14155238886 (sandbox)

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
        if not column_exists(conn, "patients", "department"):
            c.execute("ALTER TABLE patients ADD COLUMN department TEXT")
            c.execute("UPDATE patients SET department='Genel' WHERE department IS NULL")
        if not column_exists(conn, "patients", "visit_time"):
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")  # HH:MM
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,   -- bekliyor | tamamlandi
            updated_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,   -- ok | hata
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            phone TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            receive_msgs INTEGER NOT NULL DEFAULT 1,
            verified INTEGER NOT NULL DEFAULT 0,
            personnel_id INTEGER,
            created_at TEXT NOT NULL,
            otp_code TEXT,
            otp_expires TEXT,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")
        # basit ayarlar
        c.execute("""CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY,
            val TEXT
        )""")
init_db()

# ================== UTILS ==================
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def to_iso(d:date) -> str: return d.strftime("%Y-%m-%d")
def to_display(d:date) -> str: return d.strftime("%d/%m/%Y")

def normalize_phone(p: str) -> str:
    p = (p or "").strip().replace(" ", "").replace("-", "")
    if p and not p.startswith("+"):
        p = "+" + p
    return p

def _wa_from() -> str:
    f = (os.environ.get("TWILIO_WHATSAPP_FROM", TWILIO_WHATSAPP_FROM) or "").strip()
    if f.startswith("whatsapp:"):
        return f
    f = normalize_phone(f) if f else ""
    return f"whatsapp:{f}" if f else ""

def hash_pw(pw:str)->str: return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()

# ---- Settings helpers ----
def get_setting(key:str, default:str=""):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT val FROM app_settings WHERE key=?", (key,))
        row = c.fetchone()
    return row[0] if row else default

def set_setting(key:str, val:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO app_settings(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=?",
                  (key, val, val))

# ================== PERSONNEL ==================
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q = "SELECT id,name,phone,active FROM personnel"
        if active_only: q += " WHERE active=1"
        q += " ORDER BY name"
        c.execute(q)
        return c.fetchall()

def upsert_personnel(name:str, phone:str, active:int)->int:
    phone = normalize_phone(phone)
    if not phone.startswith("+"):
        raise ValueError("Telefon + ile başlamalı (örn. +90...)")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("SELECT id FROM personnel WHERE phone=?", (phone,))
        row = c.fetchone()
        if row:
            pid = row[0]
            c.execute("UPDATE personnel SET name=?, active=? WHERE id=?", (name.strip(), active, pid))
            return pid
        c.execute("INSERT INTO personnel(name,phone,active,created_at) VALUES(?,?,?,?)",
                  (name.strip(), phone, active, now_str()))
        return c.lastrowid

def set_personnel_active(pid:int, active:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE personnel SET active=? WHERE id=?", (active, pid))

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

# ================== MESSAGING (Twilio WhatsApp) ==================
def send_whatsapp_message(to_phone:str, body:str)->tuple[bool,str]:
    if not _twilio_ok:
        return False, "Twilio paketi yok"
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and (TWILIO_WHATSAPP_FROM or _wa_from())):
        return False, "Twilio ortam değişkenleri eksik"
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=_wa_from(),
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

# ================== ALARM (10 dk önce) ==================
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

# ================== THEME / UI POLISH ==================
def apply_theme(theme_name: str):
    THEMES = {
        "Sistem (varsayılan)": "",
        "Açık": """
        <style>
        body, .stApp { background: #f7f7f9!important; }
        .stButton>button, .stDownloadButton>button { background:#2563eb!important; color:white!important; }
        </style>""",
        "Klinik (mint)": """
        <style>
        body, .stApp { background:#f4fffb!important; }
        .stButton>button, .stDownloadButton>button { background:#10b981!important; color:white!important; }
        </style>""",
        "Yüksek Kontrast": """
        <style>
        body, .stApp { background:black!important; color:white!important; }
        .stButton>button, .stDownloadButton>button { background:#ffcc00!important; color:black!important; }
        .stDataFrame { filter: invert(1) hue-rotate(180deg); }
        </style>""",
    }
    css = THEMES.get(theme_name, "")
    if css:
        st.markdown(css, unsafe_allow_html=True)

# küçük animasyon ve hover
st.markdown("""
<style>
.main > div { animation: fadeIn .35s ease-in-out; }
@keyframes fadeIn { from{opacity:0; transform: translateY(6px);} to{opacity:1; transform:none;} }
button[kind="primary"]:hover { transform: scale(1.03); transition: .15s; }
</style>
""", unsafe_allow_html=True)

# ================== AUTH (BUGÜN HERKES ADMIN) ==================
if "auth" not in st.session_state:
    st.session_state.auth = {"logged_in": True, "is_admin": True, "username": "admin"}

# ================== APPLY THEME ==================
apply_theme(get_setting("theme", "Sistem (varsayılan)"))

# ================== SIDEBAR ==================
picked_date = st.sidebar.date_input("📅 Tarih seç", value=date.today(), key="dt_pick")
sel_iso = to_iso(picked_date); sel_disp = to_display(picked_date)

with st.sidebar:
    st.divider()
    st.subheader("🔌 Sistem")
    st.write("• Twilio:", "✅" if (_twilio_ok and TWILIO_ACCOUNT_SID) else "⚠️ Ayarları kontrol edin")
    st.write("• Tarih:", sel_disp)

    st.divider()
    with st.expander("⚙️ Ayarlar", expanded=False):
        # --- Tema ---
        st.markdown("#### 🎨 Tema")
        saved_theme = get_setting("theme", "Sistem (varsayılan)")
        themes = ["Sistem (varsayılan)", "Açık", "Klinik (mint)", "Yüksek Kontrast"]
        theme = st.selectbox("Tema seç", themes, index=themes.index(saved_theme), key="sel_theme")
        if st.button("Temayı Uygula", key="btn_apply_theme"):
            set_setting("theme", theme)
            st.success("Tema güncellendi.")
            st.rerun()

        st.divider()
        # --- Numara yönetimi (opt-in/opt-out) ---
        st.markdown("#### 👥 Mesaj Alacak Numaralar")
        people = list_personnel(active_only=False)  # [(id,name,phone,active)]
        if people:
            for pid, name, phone, active in people:
                colA, colB, colC = st.columns([3,1,1])
                colA.caption(f"**{name}** — {phone}")
                toggled = colB.toggle("Aktif", value=bool(active), key=f"pact_{pid}")
                if toggled != bool(active):
                    set_personnel_active(pid, 1 if toggled else 0)
                    st.toast(f"{name}: {'Aktif' if toggled else 'Pasif'}", icon="✅")
                if colC.button("Sil", key=f"pdel_{pid}"):
                    delete_personnel(pid)
                    st.success("Silindi.")
                    st.rerun()
        else:
            st.info("Kayıtlı numara yok.")

        st.markdown("#### ➕ Numara Ekle")
        with st.form("frm_add_staff_quick", clear_on_submit=True):
            nm = st.text_input("Ad / not", key="nm_add_quick")
            ph = st.text_input("Telefon (+90...)", key="ph_add_quick")
            act = st.checkbox("Mesaj alsın (Aktif)", value=True, key="ph_add_active")
            submit_add = st.form_submit_button("Ekle")
        if submit_add:
            try:
                upsert_personnel(nm, ph, 1 if act else 0)
                st.success("Eklendi.")
                st.rerun()
            except Exception as e:
                st.error(f"Hata: {e}")

        st.divider()
        st.markdown("#### 🧪 WhatsApp Testi")
        test_to = st.text_input("Test alıcı (+90...)", key="wa_test_to")
        if st.button("Test mesajı gönder", key="wa_test_btn"):
            ok, info = send_whatsapp_message(test_to, "Test: Check-up WhatsApp bağlantısı çalışıyor.")
            st.success(f"OK: {info}") if ok else st.error(f"Hata: {info}")

    if st.button("🚪 Çıkış", key="btn_logout"):
        st.session_state.auth = {"logged_in": True, "is_admin": True, "username": "admin"}  # bugün admin kalalım
        st.experimental_rerun()

# ================== MAIN ==================
st.title("✅ Check-up Takip Sistemi")

tab_hasta, tab_tetkik, tab_ozet, tab_yedek = st.tabs(
    ["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti", "💾 Yedek"]
)

# -------- 🧑‍⚕️ Hastalar --------
with tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts = list_patients(visit_date_iso=sel_iso)
    st.dataframe([{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Alarm Saati":p[6] or "-"} for p in pts],
                 use_container_width=True)

    st.markdown("### ➕ Hasta Ekle")
    with st.form("frm_add_patient", clear_on_submit=True):
        c1,c2,c3 = st.columns([2,2,1])
        fn = c1.text_input("Ad")
        ln = c2.text_input("Soyad")
        age = c3.number_input("Yaş", 0, 120, 0, 1)
        gender = st.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        submitted = st.form_submit_button("Ekle")
    if submitted:
        if not fn.strip() or not ln.strip():
            st.warning("Ad ve Soyad zorunludur.")
        else:
            add_patient(fn, ln, int(age), gender, sel_iso)
            st.success(f"Eklendi: {fn} {ln}")
            st.rerun()

    if pts:
        st.markdown("### 🗑️ Hasta Sil")
        choice = st.selectbox("Silinecek", [(p[0], f"{p[1]} {p[2]}") for p in pts],
                              format_func=lambda x: x[1], key="sel_del_patient")
        if st.button("Sil", type="primary", key="btn_del_patient"):
            delete_patient(choice[0])
            st.success("Hasta ve tetkikleri silindi.")
            st.rerun()

# -------- 🧪 Tetkik Takibi --------
with tab_tetkik:
    pts_today = list_patients(visit_date_iso=sel_iso)
    if not pts_today:
        st.info("Bu tarih için hasta yok.")
    else:
        sel = st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts_today],
                           format_func=lambda x: x[1], key="sel_patient_tests")
        pid = sel[0]

        st.markdown("#### Tetkik Ekle")
        with st.form("frm_add_test", clear_on_submit=True):
            tname = st.text_input("Tetkik adı")
            alarm_check = st.checkbox("🔔 Alarm kurmak istiyorum", key="chk_alarm")
            alarm_hhmm = None
            if alarm_check:
                colh, colm = st.columns(2)
                hour = colh.selectbox("Saat", [f"{h:02d}" for h in range(0,24)], key="alarm_hour")
                minute = colm.selectbox("Dakika", [f"{m:02d}" for m in range(0,60,5)], key="alarm_minute")
                alarm_hhmm = f"{hour}:{minute}"
            addt = st.form_submit_button("Ekle")
        if addt:
            if not tname.strip():
                st.warning("Tetkik adı boş olamaz.")
            else:
                add_patient_test(pid, tname)
                if alarm_check and alarm_hhmm:
                    set_patient_alarm_time(pid, alarm_hhmm)
                st.success("Tetkik eklendi" + (f" ve alarm {alarm_hhmm} için kuruldu." if (alarm_check and alarm_hhmm) else "."))
                st.rerun()

        st.markdown("#### Tetkikler")
        trs = list_patient_tests(patient_id=pid)
        if not trs:
            st.info("Tetkik kaydı yok.")
        else:
            for t in trs:
                tid, _, fn, ln, tname, tstatus, updated = t
                icon = "✅" if tstatus=="tamamlandi" else "⏳"
                cols = st.columns([6,1,1,1])
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

# -------- 📊 Gün Özeti --------
with tab_ozet:
    st.subheader(f"{sel_disp} — Gün Özeti")
    pts = list_patients(visit_date_iso=sel_iso)
    if not pts:
        st.info("Bu tarihte hasta yok.")
    else:
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
        st.dataframe(rows, use_container_width=True)

# -------- 💾 Yedek --------
with tab_yedek:
    st.subheader("Yedek / Dışa Aktar (CSV)")
    col1, col2 = st.columns(2)

    def _csv(query:str):
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute(query)
            rows = c.fetchall()
            headers = [d[0] for d in c.description]
        buf = io.StringIO()
        w = csv.writer(buf); w.writerow(headers); w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    with col1:
        if st.button("Hastalar CSV", key="dl_pat"):
            st.download_button("İndir – patients.csv", _csv("SELECT * FROM patients"),
                               "patients.csv", "text/csv", key="dl_pat_btn")
        if st.button("Tetkikler CSV", key="dl_tests"):
            st.download_button("İndir – patient_tests.csv", _csv("SELECT * FROM patient_tests"),
                               "patient_tests.csv", "text/csv", key="dl_tests_btn")
    with col2:
        if st.button("Personel CSV", key="dl_staff"):
            st.download_button("İndir – personnel.csv", _csv("SELECT * FROM personnel"),
                               "personnel.csv", "text/csv", key="dl_staff_btn")
        if st.button("Mesaj Logları CSV", key="dl_logs"):
            st.download_button("İndir – msg_logs.csv", _csv("SELECT * FROM msg_logs"),
                               "msg_logs.csv", "text/csv", key="dl_logs_btn")
