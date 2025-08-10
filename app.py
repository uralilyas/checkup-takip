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
        # MIGRATIONS: add department + visit_time if missing (eski verileri koru)
        if not column_exists(conn, "patients", "department"):
            c.execute("ALTER TABLE patients ADD COLUMN department TEXT")
            c.execute("UPDATE patients SET department='Genel' WHERE department IS NULL")
        if not column_exists(conn, "patients", "visit_time"):
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")  # "HH:MM"
            c.execute("UPDATE patients SET visit_time='00:00' WHERE visit_time IS NULL")

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

init_db()  # her şey hazır olmadan thread başlatma

# ================== UTILS ==================
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def to_iso(d:date) -> str: return d.strftime("%Y-%m-%d")
def to_display(d:date) -> str: return d.strftime("%d/%m/%Y")  # Gün/Ay/Yıl
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
                  (fn.strip(), ln.strip(), age, gender, visit_date_iso, now_str(), dept.strip(), visit_time_hhmm))

def delete_patient(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE patient_id=?", (pid,))
        c.execute("DELETE FROM patients WHERE id=?", (pid,))

def list_patients(visit_date_iso:str|None=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date_iso:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients WHERE visit_date=?
                         ORDER BY visit_time, last_name, first_name""", (visit_date_iso,))
        else:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients ORDER BY visit_date DESC, visit_time""")
        return c.fetchall()

# ================== TESTS (sade takip) ==================
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

# ================== ALARM (10 dk önce) ==================
def check_alarms_loop():
    """Her dakika bugüne ait randevuları kontrol eder, 10 dk kala personele WhatsApp atar."""
    while True:
        today_iso = datetime.now().strftime("%Y-%m-%d")
        now_plus_10 = (datetime.now() + timedelta(minutes=10)).strftime("%H:%M")
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute("""SELECT first_name,last_name,department,visit_time
                         FROM patients
                         WHERE visit_date=? AND visit_time=?""", (today_iso, now_plus_10))
            matches = c.fetchall()
        if matches:
            for (fn, ln, dept, vtime) in matches:
                body = (f"📅 Hatırlatma:\n"
                        f"{fn} {ln}'ın 10 dakika sonra {dept} randevusu bulunmaktadır.\n"
                        f"Lütfen bölüm ile teyit sağlayarak hastaya eşlik ediniz.")
                for staff in list_personnel(active_only=True):
                    ok, info = send_whatsapp_message(staff[2], body)
                    log_message(staff[0], body, ok, info)
        time.sleep(60)

# Thread'i yalnızca bir kez başlat
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

# Tarih (ekranda DD/MM/YYYY; DB'de ISO)
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

# Sekmeler (sade içerik)
tab_hasta, tab_tetkik, tab_ozet, tab_mesaj, tab_personel, tab_yedek = st.tabs(
    ["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti", "📲 WhatsApp Mesaj", "👥 Personel", "💾 Yedek"]
)

# -------- 🧑‍⚕️ Hastalar --------
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
        dept = c5.text_input("Bölüm (manuel)", placeholder="Kardiyoloji, Dahiliye, ...")
        vtime = st.time_input("Randevu Saati", key="time_pick")
        submitted = st.form_submit_button("Ekle")
    if submitted:
        if not fn.strip() or not ln.strip():
            st.warning("Ad ve Soyad zorunludur.")
        else:
            try:
                add_patient(fn, ln, int(age), gender, dept or "Genel", sel_iso, vtime.strftime("%H:%M"))
                st.success(f"Eklendi: {fn} {ln} • {dept or 'Genel'} • {vtime.strftime('%H:%M')}")
                st.rerun()
            except Exception as e:
                st.error(f"Hata: {e}")

    if pts:
        st.markdown("### 🗑️ Hasta Sil")
        choice = st.selectbox("Silinecek", [(p[0], f"{p[1]} {p[2]} — {p[5]} {p[6]}") for p in pts],
                              format_func=lambda x: x[1], key="sel_del_patient")
        if st.button("Sil", type="primary", key="btn_del_patient"):
            delete_patient(choice[0])
            st.success("Hasta ve tetkikleri silindi.")
            st.rerun()
    else:
        st.caption("Bu tarihte kayıtlı hasta yok.")

# -------- 🧪 Tetkik Takibi --------
with tab_tetkik:
    pts_today = list_patients(visit_date_iso=sel_iso)
    if not pts_today:
        st.info("Bu tarih için hasta yok.")
    else:
        pid, label = st.selectbox(
            "Hasta",
            [(p[0], f"{p[1]} {p[2]} — {p[5]} {p[6]}") for p in pts_today],
            format_func=lambda x: x[1],
            key="sel_patient_tests"
        )
        with st.form("frm_add_test", clear_on_submit=True):
            tname = st.text_input("Tetkik adı (örn. MR, Kan, EKG)")
            addt = st.form_submit_button("Ekle")
        if addt and tname.strip():
            add_patient_test(pid, tname)
            st.success("Tetkik eklendi.")
            st.rerun()

        filt = st.selectbox("Durum filtresi", ["Tümü","Bekliyor","Tamamlandı"], key="flt_tests")
        status = {"Tümü":None, "Bekliyor":"bekliyor", "Tamamlandı":"tamamlandi"}[filt]
        trs = list_patient_tests(patient_id=pid, status=status)
        if not trs:
            st.info("Kayıt yok.")
        else:
            for t in trs:
                tid, _, fn, ln, tname, tstatus, updated = t
                c = st.columns([6,2,2])
                c[0].write(f"**{tname}** — {fn} {ln} • {updated}")
                if tstatus == "bekliyor":
                    if c[1].button("Tamamla", key=f"done_{tid}"):
                        update_patient_test_status(tid, "tamamlandi")
                        auto_message_for_patient(pid)
                        st.success("Tamamlandı ve mesaj gönderildi.")
                        st.rerun()
                else:
                    if c[2].button("Geri Al", key=f"undo_{tid}"):
                        update_patient_test_status(tid, "bekliyor")
                        auto_message_for_patient(pid)
                        st.info("Geri alındı ve mesaj gönderildi.")
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
            done = [t[4] for t in tests if t[5]=="tamamlandi"]
            remain = [t[4] for t in tests if t[5]=="bekliyor"]
            rows.append({"Hasta": f"{p[1]} {p[2]}", "Bölüm": p[5] or "-", "Saat": p[6] or "-",
                         "Tamamlanan": ", ".join(done) if done else "-",
                         "Kalan": ", ".join(remain) if remain else "-"})
        st.dataframe(rows, use_container_width=True)

# -------- 📲 WhatsApp Mesaj (manuel) --------
with tab_mesaj:
    st.subheader("WhatsApp Mesaj (Personel)")
    staff = list_personnel(active_only=True)
    if not staff:
        st.info("Önce personel ekleyin.")
    else:
        sel_staff = st.multiselect("Alıcılar", [(s[0], f"{s[1]} — {s[2]}") for s in staff],
                                   format_func=lambda x: x[1], key="ms_sel_staff")
        msg = st.text_area("Mesaj", height=120, key="ms_body")
        if st.button("Gönder", key="ms_send"):
            okc, errc = 0, 0
            for sid, _label in sel_staff:
                phone = [x[2] for x in staff if x[0]==sid][0]
                ok, info = send_whatsapp_message(phone, msg)
                log_message(sid, msg, ok, info)
                okc += 1 if ok else 0
                errc += 0 if ok else 1
            if okc and not errc: st.success(f"{okc} kişiye gönderildi.")
            elif okc and errc:   st.warning(f"{okc} başarılı, {errc} hatalı.")
            else:                st.error("Gönderim başarısız.")

# -------- 👥 Personel --------
with tab_personel:
    st.subheader("Personel")
    people = list_personnel(active_only=False)
    st.dataframe(
        [{"ID":p[0], "Ad Soyad":p[1], "Telefon":p[2], "Aktif":"Evet" if p[3] else "Hayır"} for p in people],
        use_container_width=True
    )
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
                                 format_func=lambda x: x[1], key="ps_del_sel")
        if st.button("Sil", type="primary", key="ps_del_btn"):
            delete_personnel(sel_staff[0])
            st.success("Personel silindi.")
            st.rerun()

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
        import csv as _csvmod
        w = _csvmod.writer(buf); w.writerow(headers); w.writerows(rows)
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
