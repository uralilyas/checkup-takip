import os, sqlite3
from datetime import datetime, date
from contextlib import closing
import streamlit as st

# ---------------- Config ----------------
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

# ---------------- DB ----------------
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
            status TEXT NOT NULL,     -- bekliyor | tamamlandi
            updated_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,     -- ok | hata
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id))""")
init_db()

# ---------------- Utils ----------------
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def normalize_phone(p:str)->str: return p.replace(" ","").replace("-","")

# ---------------- Personnel ----------------
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
        raise ValueError("Numara + ile başlamalı (ör. +90...)")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO personnel(name,phone,created_at) VALUES(?,?,?)",
                  (name.strip(), phone.strip(), now_str()))

def delete_personnel(personnel_id:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM personnel WHERE id=?", (personnel_id,))
        c.execute("DELETE FROM msg_logs WHERE personnel_id=?", (personnel_id,))

# ---------------- Patients ----------------
def add_patient(first_name:str,last_name:str,age:int,gender:str,visit_date:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patients(first_name,last_name,age,gender,visit_date,created_at)
                     VALUES(?,?,?,?,?,?)""",
                  (first_name.strip(), last_name.strip(), age, gender, visit_date, now_str()))

def delete_patient(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE patient_id=?", (pid,))
        c.execute("DELETE FROM patients WHERE id=?", (pid,))

def list_patients(visit_date:str|None=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date:
            c.execute("""SELECT id,first_name,last_name,age,gender
                         FROM patients WHERE visit_date=? ORDER BY last_name, first_name""",
                      (visit_date,))
        else:
            c.execute("""SELECT id,first_name,last_name,age,gender
                         FROM patients ORDER BY visit_date DESC, last_name, first_name""")
        return c.fetchall()

# ---------------- Patient Tests ----------------
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

# ---------------- Messaging ----------------
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

def auto_message_for_patient(patient_id:int):
    trs = list_patient_tests(patient_id)
    done = [t[4] for t in trs if t[5] == "tamamlandi"]
    remain = [t[4] for t in trs if t[5] == "bekliyor"]
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT first_name,last_name FROM patients WHERE id=?", (patient_id,))
        fn, ln = c.fetchone()
    body = (f"{fn} {ln} için tetkik güncellemesi:\n"
            f"Tamamlananlar: {', '.join(done) if done else '-'}\n"
            f"Kalanlar: {', '.join(remain) if remain else '-'}")
    for staff in list_personnel(active_only=True):
        ok, info = send_whatsapp_message(staff[2], body)
        log_message(staff[0], body, ok, info)

# ---------------- Auth ----------------
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
                    st.success("Admin olarak giriş yapıldı.")
                    st.rerun()
                else:
                    st.error("Geçersiz kullanıcı adı/parola.")
        st.stop()

# ---------------- UI ----------------
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
st.title("✅ Check-up Takip Sistemi")
require_login()

# Tarih seçimi (yarının hastalarını bugünden girmek için)
selected_date: date = st.sidebar.date_input("📅 Tarih seç", value=date.today())
sel_date_str = selected_date.strftime("%Y-%m-%d")

# Sol panel: bağlantı & durum
with st.sidebar:
    st.divider()
    st.subheader("🔌 Bağlantı Durumu")
    st.write("• Twilio:", "✅" if _twilio_ok and TWILIO_ACCOUNT_SID else "⚠️ Ayarları kontrol edin")
    try:
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute("SELECT COUNT(*) FROM patients WHERE visit_date=?", (sel_date_str,))
            pc = c.fetchone()[0]
            c.execute("""SELECT COUNT(*) FROM patient_tests t
                         JOIN patients p ON p.id=t.patient_id
                         WHERE p.visit_date=? AND t.status='tamamlandi'""", (sel_date_str,))
            tc_done = c.fetchone()[0]
        st.write(f"• DB: ✅ (Günlük Hasta: {pc}, Tamamlanan Tetkik: {tc_done})")
    except Exception:
        st.write("• DB: ⚠️")
    if st.button("🚪 Çıkış", key="logout_btn"):
        st.session_state.auth["logged_in"] = False
        st.rerun()

tab_hasta, tab_tetkik, tab_ozet, tab_mesaj, tab_personel = st.tabs(
    ["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti", "📲 WhatsApp Mesaj", "👥 Personel"]
)

# ---------------- Hastalar ----------------
with tab_hasta:
    st.subheader(f"{sel_date_str} - Hasta Listesi")
    pts = list_patients(visit_date=sel_date_str)
    st.dataframe(
        [{"ID":p[0],"Ad":p[1],"Soyad":p[2],"Yaş":p[3],"Cinsiyet":p[4]} for p in pts],
        use_container_width=True
    )
    st.markdown("### ➕ Hasta Ekle (bu tarihe)")
    with st.form("hasta_add", clear_on_submit=True):
        col1, col2, col3, col4 = st.columns([2,2,1,1])
        with col1: fn = st.text_input("Ad")
        with col2: ln = st.text_input("Soyad")
        with col3: age = st.number_input("Yaş", 0, 120, 0, 1)
        with col4: gender = st.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        submitted = st.form_submit_button("Ekle")
    if submitted:
        if not fn.strip() or not ln.strip():
            st.warning("Ad ve Soyad zorunludur.")
        else:
            try:
                add_patient(fn, ln, int(age), gender, sel_date_str)
                st.success(f"Hasta eklendi: {fn} {ln}")
                st.rerun()
            except Exception as e:
                st.error(f"Hata: {e}")

    st.markdown("### 🗑️ Hasta Sil")
    if pts:
        sel = st.selectbox("Silinecek hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts],
                           format_func=lambda x: x[1], key="hasta_sil_select")
        if st.button("Sil", type="primary", key="hasta_sil_btn"):
            delete_patient(sel[0])
            st.success("Hasta ve tetkikleri silindi.")
            st.rerun()
    else:
        st.caption("Silinecek hasta yok.")

# ---------------- Tetkik ----------------
with tab_tetkik:
    pts_today = list_patients(visit_date=sel_date_str)
    if not pts_today:
        st.info("Bu tarih için hasta yok.")
    else:
        pid = st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts_today],
                           format_func=lambda x: x[1], key="tetkik_hasta_select")[0]
        st.markdown("#### Tetkik Ekle")
        with st.form("tetkik_add", clear_on_submit=True):
            tname = st.text_input("Tetkik adı (örn. MR, Kan Tahlili)")
            addt = st.form_submit_button("Ekle")
        if addt:
            if not tname.strip():
                st.warning("Tetkik adı boş olamaz.")
            else:
                add_patient_test(pid, tname)
                st.success("Tetkik eklendi.")
                st.rerun()

        st.markdown("#### Tetkikler")
        filt = st.selectbox("Durum", ["Tümü","Bekliyor","Tamamlandı"], key="tetkik_filter")
        status = {"Tümü":None, "Bekliyor":"bekliyor", "Tamamlandı":"tamamlandi"}[filt]
        trs = list_patient_tests(patient_id=pid, status=status)
        if not trs:
            st.info("Kayıt yok.")
        else:
            for t in trs:
                tid, _, fn, ln, tname, tstatus, updated = t
                c = st.columns([5,2,2,3])
                with c[0]:
                    st.write(f"**{tname}** — {fn} {ln}")
                    st.caption(f"Durum: {'✅ Tamamlandı' if tstatus=='tamamlandi' else '⏳ Bekliyor'} • Güncelleme: {updated}")
                with c[1]:
                    if tstatus == "bekliyor" and c[1].button("Tamamla", key=f"done_{tid}"):
                        update_patient_test_status(tid, "tamamlandi")
                        auto_message_for_patient(pid)
                        st.success("Tetkik tamamlandı ve mesaj gönderildi.")
                        st.rerun()
                with c[2]:
                    if tstatus == "tamamlandi" and c[2].button("Geri Al", key=f"undo_{tid}"):
                        update_patient_test_status(tid, "bekliyor")
                        auto_message_for_patient(pid)
                        st.info("Tetkik geri alındı ve mesaj gönderildi.")
                        st.rerun()
                with c[3]:
                    st.empty()

# ---------------- Gün Özeti ----------------
with tab_ozet:
    st.subheader(f"📊 {sel_date_str} Gün Özeti")
    pts = list_patients(visit_date=sel_date_str)
    if not pts:
        st.info("Bu tarih için hasta yok.")
    else:
        rows = []
        for p in pts:
            tests = list_patient_tests(p[0])
            done = [t[4] for t in tests if t[5]=="tamamlandi"]
            remain = [t[4] for t in tests if t[5]=="bekliyor"]
            rows.append({
                "Hasta": f"{p[1]} {p[2]}",
                "Tamamlanan": ", ".join(done) if done else "-",
                "Kalan": ", ".join(remain) if remain else "-"
            })
        st.dataframe(rows, use_container_width=True)

# ---------------- Mesaj (manuel) ----------------
with tab_mesaj:
    st.subheader("📲 WhatsApp Mesaj (Personel)")
    staff = list_personnel(active_only=True)
    if not staff:
        st.info("Önce personel ekleyin (👥 Personel sekmesi).")
    else:
        sel_staff = st.multiselect("Alıcılar", [(s[0], f"{s[1]} — {s[2]}") for s in staff],
                                   format_func=lambda x: x[1], key="msg_staff_multi")
        msg = st.text_area("Mesaj", height=120, key="msg_body")
        if st.button("Gönder", key="msg_send_btn"):
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

# ---------------- Personel ----------------
with tab_personel:
    st.subheader("👥 Personel")
    people = list_personnel(active_only=False)
    st.dataframe([{"ID":p[0],"Ad Soyad":p[1],"Telefon":p[2],"Aktif":"Evet" if p[3] else "Hayır"} for p in people],
                 use_container_width=True)
    st.markdown("### ➕ Personel Ekle")
    with st.form("personel_add", clear_on_submit=True):
        name = st.text_input("Ad Soyad")
        phone = st.text_input("Tel (+90...)")
        addp = st.form_submit_button("Ekle")
    if addp:
        try:
            add_personnel(name, phone)
            st.success("Personel eklendi.")
            st.rerun()
        except Exception as e:
            st.error(f"Hata: {e}")

    if people:
        choice = st.selectbox("Silinecek personel", [(p[0], f"{p[1]} ({p[2]})") for p in people],
                              format_func=lambda x: x[1], key="personel_sil_select")
        if st.button("Sil", type="primary", key="personel_sil_btn"):
            delete_personnel(choice[0])
            st.success("Personel ve ilgili mesaj kayıtları silindi.")
            st.rerun()
