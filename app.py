# app.py
import os, sqlite3, csv, io, threading, time, random, hashlib
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
        # staff who receive WhatsApp
        c.execute("""CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )""")
        # patients
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
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")
        # tests
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,   -- bekliyor | tamamlandi
            updated_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )""")
        # message logs
        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,   -- ok | hata
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")
        # users (for login/registration)
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

init_db()

# ================== UTILS ==================
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def to_iso(d:date) -> str: return d.strftime("%Y-%m-%d")
def to_display(d:date) -> str: return d.strftime("%d/%m/%Y")
def normalize_phone(p:str)->str: return p.replace(" ","").replace("-","")
def hash_pw(pw:str)->str: return hashlib.sha256(pw.encode("utf-8")).hexdigest()

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
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        # try find existing by phone
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

# Start alarm thread once
if "alarm_thread_started" not in st.session_state:
    threading.Thread(target=check_alarms_loop, daemon=True).start()
    st.session_state.alarm_thread_started = True

# ================== AUTH & REGISTRATION ==================
def hide_streamlit_chrome(hide: bool):
    if not hide:  # admin için açık kalsın
        return
    css = """
    <style>
    header [data-testid="stToolbarActions"] {display:none !important;} /* Share/GitHub */
    footer {visibility:hidden;}             /* Manage app */
    [data-testid="stStatusWidget"]{display:none !important;} /* sağ alt simgeler */
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)

def send_otp(phone:str, code:str):
    body = f"Check-up doğrulama kodunuz: {code}"
    # OTP için logda personel_id olmayabilir; 0 yazıyoruz
    ok, info = send_whatsapp_message(phone, body)
    try:
        log_message(0, f"OTP:{body}", ok, info)
    except Exception:
        pass
    return ok, info

def create_user(username:str, password:str, phone:str, want_msgs:bool):
    username = username.strip().lower()
    phone = normalize_phone(phone)
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        # create / update personnel (aktiflik doğrulamadan sonra ayarlanacak)
        pid = upsert_personnel(name=username, phone=phone, active=0)
        # generate OTP
        code = f"{random.randint(100000,999999)}"
        expire = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""INSERT INTO users(username,password_hash,phone,is_admin,receive_msgs,verified,personnel_id,created_at,otp_code,otp_expires)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (username, hash_pw(password), phone, 0, 1 if want_msgs else 0, 0, pid, now_str(), code, expire))
        return pid, code

def verify_user(username:str, code:str)->bool:
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("SELECT id,receive_msgs,personnel_id,otp_code,otp_expires FROM users WHERE username=?", (username,))
        row = c.fetchone()
    if not row: return False
    uid, recv, pid, otp, expires = row
    if otp != code: return False
    if datetime.now() > datetime.strptime(expires, "%Y-%m-%d %H:%M:%S"):
        return False
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE users SET verified=1, otp_code=NULL, otp_expires=NULL WHERE id=?", (uid,))
    set_personnel_active(pid, 1 if recv else 0)
    return True

def user_login(username:str, password:str):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("""SELECT id,username,phone,is_admin,receive_msgs,verified,personnel_id,password_hash
                     FROM users WHERE username=?""", (username.strip().lower(),))
        row = c.fetchone()
    if not row: return None
    uid, uname, phone, is_admin, recv, verified, pid, pwh = row
    if pwh != hash_pw(password): return None
    return {"uid":uid, "username":uname, "phone":phone, "is_admin":bool(is_admin),
            "receive_msgs":bool(recv), "verified":bool(verified), "personnel_id":pid}

def require_login():
    if "auth" not in st.session_state:
        st.session_state.auth = {"logged_in": False, "is_admin": False}

    if not st.session_state.auth["logged_in"]:
        st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
        st.title("✅ Check-up Takip Sistemi")

        tab_giris, tab_kayit = st.tabs(["🔐 Giriş", "📝 Kayıt"])

        # --- GİRİŞ ---
        with tab_giris:
            st.subheader("Sistem Girişi")
            with st.form("frm_login"):
                u = st.text_input("Kullanıcı Adı")
                p = st.text_input("Parola", type="password")
                submitted = st.form_submit_button("Giriş Yap")
            if submitted:
                # Admin kısa yol
                if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                    st.session_state.auth = {"logged_in": True, "is_admin": True,
                                             "username": "admin", "personnel_id": None,
                                             "receive_msgs": True}
                    st.success("Admin olarak giriş yapıldı.")
                    st.rerun()
                else:
                    info = user_login(u, p)
                    if info and info["verified"]:
                        st.session_state.auth = {"logged_in": True,
                                                 "is_admin": info["is_admin"],
                                                 "username": info["username"],
                                                 "personnel_id": info["personnel_id"],
                                                 "receive_msgs": info["receive_msgs"]}
                        st.success("Giriş başarılı.")
                        st.rerun()
                    else:
                        st.error("Giriş başarısız veya telefon doğrulanmamış.")

        # --- KAYIT ---
        with tab_kayit:
            st.subheader("Yeni Kayıt")
            if "pending_user" not in st.session_state:
                with st.form("frm_register"):
                    uname = st.text_input("Kullanıcı Adı (sade harf/rakam)")
                    pw = st.text_input("Parola", type="password")
                    phone = st.text_input("Telefon (+90...)")
                    want_msgs = st.checkbox("WhatsApp mesajları almak istiyorum", value=True)
                    ok = st.form_submit_button("Kayıt Ol")
                if ok:
                    try:
                        pid, code = create_user(uname, pw, phone, want_msgs)
                        ok2, _info = send_otp(phone, code)
                        st.session_state.pending_user = {"username": uname, "phone": phone}
                        st.success("Kayıt alındı. Telefonunuza gelen doğrulama kodunu girin.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Hata: {e}")
            else:
                st.info(f"{st.session_state.pending_user['phone']} numarasına gönderilen kodu girin.")
                with st.form("frm_otp"):
                    code_in = st.text_input("Doğrulama Kodu (6 hane)")
                    okv = st.form_submit_button("Doğrula")
                if okv:
                    if verify_user(st.session_state.pending_user["username"], code_in.strip()):
                        st.success("Telefon doğrulandı. Artık giriş yapabilirsiniz.")
                        del st.session_state["pending_user"]
                    else:
                        st.error("Kod hatalı veya süresi doldu.")
        st.stop()

# ================== UI (MAIN APP) ==================
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
require_login()

# hide chrome for non-admin
hide_streamlit_chrome(hide=not st.session_state.auth.get("is_admin", False))

st.title("✅ Check-up Takip Sistemi")

# Tarih
picked_date = st.sidebar.date_input("📅 Tarih seç", value=date.today(), key="dt_pick")
sel_iso = to_iso(picked_date)
sel_disp = to_display(picked_date)

with st.sidebar:
    st.divider()
    st.subheader("🔌 Sistem")
    st.write("• Twilio:", "✅" if (_twilio_ok and TWILIO_ACCOUNT_SID) else "⚠️ Ayarları kontrol edin")
    st.write("• Tarih:", sel_disp)
    # Profil ayarları (mesaj alma tercihi) — kullanıcılar için
    if not st.session_state.auth.get("is_admin", False):
        st.markdown("### 👤 Profil")
        recv = st.checkbox("WhatsApp mesajları almak istiyorum", value=st.session_state.auth.get("receive_msgs", True), key="profile_recv")
        if st.button("Kaydet", key="btn_profile_save"):
            with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
                c.execute("UPDATE users SET receive_msgs=? WHERE username=?",
                          (1 if recv else 0, st.session_state.auth["username"]))
            st.session_state.auth["receive_msgs"] = recv
            pid = st.session_state.auth.get("personnel_id")
            if pid:
                set_personnel_active(pid, 1 if recv else 0)
            st.success("Tercih güncellendi.")
    if st.button("🚪 Çıkış", key="btn_logout"):
        st.session_state.auth = {"logged_in": False, "is_admin": False}
        st.rerun()

# Sekmeler: admin her şeyi görür; kullanıcı sade (Hastalar, Tetkik, Özeti)
if st.session_state.auth.get("is_admin", False):
    tabs = st.tabs(["🧑‍⚕️ Hastalar", "🧪 Tetkik", "📊 Gün Özeti", "📲 Mesaj (Personel)", "👥 Personel", "💾 Yedek"])
else:
    tabs = st.tabs(["🧑‍⚕️ Hastalar", "🧪 Tetkik", "📊 Gün Özeti"])

# -------- 🧑‍⚕️ Hastalar --------
with tabs[0]:
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

# -------- 🧪 Tetkik --------
with tabs[1]:
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
            alarm_check = st.checkbox("🔔 Alarm kurmak istiyorum")
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
with tabs[2]:
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

# -------- Admin’e özel ek sekmeler --------
if st.session_state.auth.get("is_admin", False):
    # 📲 Mesaj
    with tabs[3]:
        st.subheader("WhatsApp Mesaj (Personel)")
        staff = list_personnel(active_only=True)
        if not staff:
            st.info("Aktif personel yok.")
        else:
            sel_staff = st.multiselect("Alıcılar", [(s[0], f"{s[1]} — {s[2]}") for s in staff],
                                       format_func=lambda x: x[1], key="ms_sel_staff")
            msg = st.text_area("Mesaj", height=120, key="ms_body")
            if st.button("Gönder", key="ms_send"):
                okc, errc = 0, 0
                for sid, _ in sel_staff:
                    phone = [x[2] for x in staff if x[0]==sid][0]
                    ok, info = send_whatsapp_message(phone, msg)
                    log_message(sid, msg, ok, info)
                    okc += 1 if ok else 0
                    errc += 0 if ok else 1
                if okc and not errc: st.success(f"{okc} kişiye gönderildi.")
                elif okc and errc:   st.warning(f"{okc} başarılı, {errc} hatalı.")
                else:                st.error("Gönderim başarısız.")

    # 👥 Personel
    with tabs[4]:
        st.subheader("Personel")
        people = list_personnel(active_only=False)
        st.dataframe(
            [{"ID":p[0], "Ad/Username":p[1], "Telefon":p[2], "Aktif":"Evet" if p[3] else "Hayır"} for p in people],
            use_container_width=True
        )

    # 💾 Yedek
    with tabs[5]:
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
