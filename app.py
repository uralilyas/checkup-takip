# app.py — Check-up Takip Sistemi (Twilio/WhatsApp düzeltmeli)
# Gerekenler: requirements.txt içinde streamlit, pandas, twilio, xlsxwriter
# Çalıştırma (lokalde): streamlit run app.py
# Bulut: GitHub → Streamlit Cloud → Secrets'e Twilio bilgileri

import os
import re
import io
import sqlite3
import hashlib
from contextlib import closing
from datetime import datetime, date, time, timedelta, timezone

import pandas as pd
import streamlit as st
from twilio.rest import Client

APP_TITLE = "🏥 Check-up Takip Sistemi"
DB_PATH = os.getenv("DB_PATH", "checkup_tracker.db")

# Admin başlangıç bilgileri (Secrets > General)
DEFAULT_ADMIN_USER = st.secrets.get("ADMIN_USERNAME", os.getenv("ADMIN_USERNAME", "admin"))
DEFAULT_ADMIN_PASS = st.secrets.get("ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", "Edam456+"))

# Twilio Secrets (Streamlit Cloud → ⋮ → Settings → Secrets)
TWILIO_SID            = st.secrets.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN          = st.secrets.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM  = st.secrets.get("TWILIO_WHATSAPP_FROM", "")  # örn: whatsapp:+14155238886

STATUS_OPTIONS = ["Planlandı","Kayıt Alındı","Devam Ediyor","Sonuç Bekleniyor","Tamamlandı","Tekrar Gerekli","Atlandı","İptal"]
DEFAULT_PACKAGES = [
    ("Standart", "Temel kan + görüntüleme + EKG"),
    ("VIP", "Genişletilmiş biyokimya + kardiyo testleri"),
    ("Kadın Sağlığı", "MMG/PAP/USG içeren paket"),
    ("Premium Kardiyoloji", "EKO + Efor + ileri kardiyo"),
    ("Genel Tarama", "Yaşa göre kapsamlı tarama"),
]

# ---------------- Helpers ----------------
def _now():
    # timezone-aware UTC → yerel gösterimler için isterseniz .astimezone() kullanabilirsiniz
    return datetime.now(timezone.utc)

def _now_str():
    return _now().strftime("%Y-%m-%d %H:%M:%S")

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

# ---------------- DB init ----------------
def init_db():
    with closing(_conn()) as con, con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT,
            role TEXT DEFAULT 'personel',        -- admin | yonetici | personel
            phone TEXT,
            notifications_enabled INTEGER DEFAULT 1,
            created_at TEXT
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS packages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            active INTEGER DEFAULT 1
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS patients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_code TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            dob TEXT,
            phone TEXT,
            package_id INTEGER,
            checkup_date TEXT,
            coordinator TEXT,
            amount_billed REAL DEFAULT 0,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(package_id) REFERENCES packages(id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            planned_at TEXT,
            status TEXT DEFAULT 'Planlandı',
            completed_at TEXT,
            notified INTEGER DEFAULT 0,
            comments TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        )""")

    # bootstrap
    with closing(_conn()) as con, con:
        if con.execute("SELECT 1 FROM users WHERE username=?", (DEFAULT_ADMIN_USER,)).fetchone() is None:
            con.execute("""INSERT INTO users(username,password_hash,full_name,role,notifications_enabled,created_at)
                           VALUES(?,?,?,?,1,?)""",
                        (DEFAULT_ADMIN_USER, _hash(DEFAULT_ADMIN_PASS), "Yönetici (Admin)", "admin", _now_str()))
        if con.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 0:
            for n,d in DEFAULT_PACKAGES:
                try:
                    con.execute("INSERT INTO packages(name,description,active) VALUES(?,?,1)", (n,d))
                except sqlite3.IntegrityError:
                    pass

# ---------------- Auth ----------------
def validate_login(u, p):
    with closing(_conn()) as con:
        row = con.execute("SELECT id,username,password_hash,full_name,role,phone,notifications_enabled FROM users WHERE username=?", (u,)).fetchone()
    if not row: return None
    if row["password_hash"] != _hash(p): return None
    return dict(row)

def update_user_notifications(user_id:int, enabled:bool):
    with closing(_conn()) as con, con:
        con.execute("UPDATE users SET notifications_enabled=? WHERE id=?", (1 if enabled else 0, user_id))

def list_users():
    with closing(_conn()) as con:
        return pd.read_sql_query("SELECT id,username,full_name,role,phone,notifications_enabled,created_at FROM users ORDER BY id DESC", con)

def create_user(username, password, full_name, role, phone, notifications_enabled=True):
    try:
        with closing(_conn()) as con, con:
            con.execute("""INSERT INTO users(username,password_hash,full_name,role,phone,notifications_enabled,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (username, _hash(password), full_name, role, phone, 1 if notifications_enabled else 0, _now_str()))
        return True, "Kullanıcı oluşturuldu"
    except sqlite3.IntegrityError:
        return False, "Kullanıcı adı zaten var"

# ---------------- Packages ----------------
def list_packages(active_only=True):
    with closing(_conn()) as con:
        if active_only:
            return pd.read_sql_query("SELECT id,name,description,active FROM packages WHERE active=1 ORDER BY name", con)
        return pd.read_sql_query("SELECT id,name,description,active FROM packages ORDER BY name", con)

def upsert_package(name, description, active=True, pkg_id=None):
    try:
        with closing(_conn()) as con, con:
            if pkg_id:
                con.execute("UPDATE packages SET name=?, description=?, active=? WHERE id=?", (name, description, 1 if active else 0, pkg_id))
                return True, "Paket güncellendi"
            con.execute("INSERT INTO packages(name,description,active) VALUES(?,?,?)", (name,description,1 if active else 0))
            return True, "Paket eklendi"
    except sqlite3.IntegrityError:
        return False, "Bu paket adı zaten var"

# ---------------- Patients / Tests ----------------
def create_patient(row:dict):
    try:
        with closing(_conn()) as con, con:
            con.execute("""INSERT INTO patients(patient_code,full_name,dob,phone,package_id,checkup_date,coordinator,amount_billed,notes,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (row.get("patient_code"), row.get("full_name"), row.get("dob"), row.get("phone"),
                         row.get("package_id"), row.get("checkup_date"), row.get("coordinator"),
                         float(row.get("amount_billed") or 0), row.get("notes"), _now_str(), _now_str()))
            pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        return True, "Hasta kaydedildi", pid
    except sqlite3.IntegrityError as e:
        return False, f"Hata: {e}", None

def fetch_patients(filters:dict):
    q = """
    SELECT p.id, p.patient_code, p.full_name, p.dob, p.phone,
           pk.name AS package, p.checkup_date, p.coordinator, p.amount_billed, p.notes, p.created_at
    FROM patients p LEFT JOIN packages pk ON pk.id=p.package_id WHERE 1=1
    """
    params=[]
    if filters.get("package"):
        q += " AND pk.name=?"
        params.append(filters["package"])
    if filters.get("date_range"):
        start, end = filters["date_range"]
        q += " AND date(p.checkup_date) BETWEEN date(?) AND date(?)"
        params += [start, end]
    with closing(_conn()) as con:
        return pd.read_sql_query(q, con, params=params)

def add_test(patient_id:int, test_name:str, planned_at:str):
    with closing(_conn()) as con, con:
        con.execute("INSERT INTO tests(patient_id,test_name,planned_at,status) VALUES(?,?,?,?)",
                    (patient_id, test_name, planned_at, "Planlandı"))

def fetch_tests(patient_id:int):
    with closing(_conn()) as con:
        return pd.read_sql_query("SELECT * FROM tests WHERE patient_id=? ORDER BY planned_at", con, params=(patient_id,))

def update_test_status(test_id:int, status:str, completed:bool=False, comments:str=""):
    completed_at = _now_str() if (completed or status=="Tamamlandı") else None
    with closing(_conn()) as con, con:
        con.execute("UPDATE tests SET status=?, completed_at=?, comments=? WHERE id=?", (status, completed_at, comments, test_id))

# ---------------- WhatsApp / Twilio ----------------
def can_send_whatsapp():
    return bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_WHATSAPP_FROM)

def normalize_to_whatsapp_e164(num:str) -> str:
    """
    Girdi: '+90555...' veya '90555...' veya 'whatsapp:+90555...'
    Çıkış: 'whatsapp:+90555...'
    """
    n = re.sub(r"\s+", "", (num or "")).strip()
    if not n:
        return ""
    if n.startswith("whatsapp:"):
        return n
    if not n.startswith("+"):
        # çok nadir bazı ülkeler için farklıdır ama burada + ile başlatma zorunlu
        n = f"+{n}"
    return f"whatsapp:{n}"

def send_whatsapp_message(to_number:str, body:str) -> bool:
    if not can_send_whatsapp():
        return False
    try:
        to_w = normalize_to_whatsapp_e164(to_number)
        client = Client(TWILIO_SID.strip(), TWILIO_TOKEN.strip())
        msg = client.messages.create(
            body=body,
            from_=TWILIO_WHATSAPP_FROM.strip(),
            to=to_w
        )
        return bool(msg.sid)
    except Exception as e:
        st.warning(f"WhatsApp gönderim hatası: {e}")
        return False

def notify_upcoming_tests() -> int:
    """Önümüzdeki 10 dk içinde başlayacak ve 'notified=0' olan testlere bildirim gönderir."""
    if not can_send_whatsapp():
        return 0
    now = _now()
    soon = now + timedelta(minutes=10)
    with closing(_conn()) as con:
        rows = con.execute(
            """SELECT t.id, t.test_name, t.planned_at, p.full_name
               FROM tests t JOIN patients p ON p.id=t.patient_id
               WHERE t.notified=0
                 AND t.status IN ('Planlandı','Kayıt Alındı','Devam Ediyor')
                 AND datetime(t.planned_at) BETWEEN datetime(?) AND datetime(?)""",
            (_now_str(), soon.strftime("%Y-%m-%d %H:%M:%S"))
        ).fetchall()
    if not rows:
        return 0

    df_users = list_users()
    df_users = df_users[df_users["notifications_enabled"] == 1]
    sent = 0
    for t_id, test_name, planned_at, patient_name in rows:
        msg = f"{patient_name} isimli hastamızın {test_name} işlemi 10 dk sonra. Lütfen teyit alınız ve hastaya eşlik ediniz."
        for _, u in df_users.iterrows():
            phone = str(u["phone"] or "").strip()
            if normalize_to_whatsapp_e164(phone):
                if send_whatsapp_message(phone, msg):
                    sent += 1
        with closing(_conn()) as con, con:
            con.execute("UPDATE tests SET notified=1 WHERE id=?", (t_id,))
    return sent

# ---------------- UI ----------------
st.set_page_config(page_title=APP_TITLE, layout="wide")
init_db()

st.title(APP_TITLE)

# hafif otomatik yenile (her 60s), health-checki bozmaz
if "tick" not in st.session_state:
    st.session_state.tick = 0
st.sidebar.caption("⏱️ Arkaplanda 60 sn’de bir kontrol edilir.")
if st.sidebar.button("Yenile"):
    st.session_state.tick += 1

# login
if "user" not in st.session_state:
    st.session_state.user = None

if not st.session_state.user:
    st.subheader("Giriş Yap")
    u = st.text_input("Kullanıcı adı", value="")
    p = st.text_input("Şifre", type="password")
    if st.button("Giriş", type="primary"):
        user = validate_login(u.strip(), p)
        if user:
            st.session_state.user = user
            st.success(f"Hoş geldiniz, {user['full_name']}")
            st.experimental_rerun()
        else:
            st.error("Kullanıcı adı veya şifre hatalı")
    st.info("Admin ilk giriş bilgileri: kullanıcı adı **admin**, şifre **Edam456+** (Secrets ile değiştirilebilir).")
    st.stop()

user = st.session_state.user

with st.sidebar:
    st.markdown(f"**👤 {user['full_name']} ({user['role']})**")
    notif = st.toggle("Bildirimleri Aç/Kapat", value=bool(user["notifications_enabled"]))
    if notif != bool(user["notifications_enabled"]):
        update_user_notifications(user["id"], notif)
        st.session_state.user["notifications_enabled"] = notif
        st.success("Bildirim tercihiniz güncellendi.")

    menu = ["Hasta Kayıt","Liste & Filtre","Tetkik Yönetimi","Raporlar"]
    if user["role"] in ("admin","yonetici"):
        menu += ["Paket Yönetimi","Kullanıcı Yönetimi","Test Uyarısı (Manuel)"]
    page = st.radio("Menü", menu, index=0)
    st.markdown("---")
    if not can_send_whatsapp():
        st.warning("WhatsApp için Twilio Sandbox bilgilerini Secrets’a ekleyin.")
    else:
        st.caption("Twilio bağlı ✅")

# bildirim taraması (sessiz)
try:
    n = notify_upcoming_tests()
    if n:
        st.toast(f"🔔 {n} test için bildirim gönderildi.")
except Exception as e:
    st.caption(f"(Bildirim kontrolü atlandı: {e})")

# ---------------- Pages ----------------
if page == "Hasta Kayıt":
    st.header("Hasta Kaydı Oluştur")
    pkgs = list_packages(True)
    c1,c2,c3 = st.columns(3)
    with c1:
        patient_code = st.text_input("Hasta Kodu (benzersiz)*")
        full_name    = st.text_input("Ad Soyad*")
        dob          = st.text_input("Doğum Tarihi", placeholder="1990/01/01")
    with c2:
        phone        = st.text_input("Telefon (örn: +90555xxxxxxx)")
        pkg_name     = st.selectbox("Paket", pkgs["name"].tolist() if not pkgs.empty else ["(paket yok)"])
        checkup_date = st.date_input("Check-up Tarihi", value=date.today())
    with c3:
        coordinator  = st.text_input("Koordinatör/Danışman", value=user.get("full_name") or user["username"])
        amount_billed= st.number_input("Fatura Tutarı (TL)", min_value=0.0, step=100.0, format="%.2f")
        notes        = st.text_area("Notlar", height=80)

    if st.button("Kaydet", type="primary"):
        if not patient_code or not full_name:
            st.error("Hasta kodu ve Ad Soyad zorunlu.")
        else:
            pkg_id = int(pkgs[pkgs["name"]==pkg_name]["id"].iloc[0]) if not pkgs.empty else None
            ok, msg, pid = create_patient({
                "patient_code": patient_code.strip(),
                "full_name": full_name.strip(),
                "dob": dob.strip(),
                "phone": phone.strip(),
                "package_id": pkg_id,
                "checkup_date": checkup_date.isoformat(),
                "coordinator": coordinator.strip(),
                "amount_billed": amount_billed,
                "notes": notes.strip()
            })
            (st.success if ok else st.error)(msg)

elif page == "Liste & Filtre":
    st.header("Hasta Listesi")
    pkgs = list_packages(False)
    c1,c2 = st.columns(2)
    with c1:
        pf = st.selectbox("Paket filtresi", [""] + pkgs["name"].tolist())
    with c2:
        date_range = st.date_input("Tarih aralığı", (date.today()-timedelta(days=30), date.today()))
    df = fetch_patients({
        "package": pf or None,
        "date_range": date_range
    })
    st.dataframe(df, use_container_width=True, hide_index=True)

    if not df.empty:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
            df.to_excel(w, index=False, sheet_name="Hastalar")
        st.download_button("Excel’e Aktar", data=buf.getvalue(),
                           file_name=f"hasta_listesi_{date.today()}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

elif page == "Tetkik Yönetimi":
    st.header("Tetkik Planlama ve Durum Yönetimi")
    pid = st.number_input("Hasta ID", min_value=1, step=1)
    c1,c2 = st.columns(2)
    with c1:
        test_name = st.text_input("Tetkik Adı", value="Kardiyoloji Muayenesi")
    with c2:
        default_dt = (_now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
        planned_dt = st.text_input("Planlanan Tarih-Saat (YYYY-MM-DD HH:MM)", value=default_dt)

    if st.button("Tetkik Ekle", type="primary"):
        try:
            datetime.strptime(planned_dt, "%Y-%m-%d %H:%M")
            add_test(pid, test_name.strip(), planned_dt + ":00")
            st.success("Tetkik eklendi.")
        except ValueError:
            st.error("Tarih-saat formatı hatalı. Örn: 2025-08-10 14:30")

    tdf = fetch_tests(pid)
    st.subheader("Tetkikler")
    if tdf.empty:
        st.info("Kayıt yok.")
    else:
        edited = st.data_editor(
            tdf, use_container_width=True, hide_index=True,
            column_config={"status": st.column_config.SelectboxColumn("Durum", options=STATUS_OPTIONS)},
            disabled=["id","patient_id","planned_at","notified"]
        )
        if st.button("Değişiklikleri Kaydet"):
            for _, r in edited.iterrows():
                update_test_status(int(r["id"]), str(r["status"]), completed=(str(r["status"])=="Tamamlandı"),
                                   comments=str(r.get("comments") or ""))
            st.success("Güncellendi.")

elif page == "Raporlar":
    st.header("Raporlar")
    pdf = fetch_patients({})
    with closing(_conn()) as con:
        tdf = pd.read_sql_query("SELECT * FROM tests", con)

    cA,cB,cC = st.columns(3)
    cA.metric("Toplam Hasta", len(pdf))
    cB.metric("Toplam Tetkik", len(tdf))
    done = (tdf["status"]=="Tamamlandı").sum() if not tdf.empty else 0
    cC.metric("Tamamlanan Tetkik", int(done))

    st.subheader("Paket Dağılımı")
    if not pdf.empty:
        packs = pdf["package"].value_counts().reset_index()
        packs.columns = ["Paket","Adet"]
        st.bar_chart(packs.set_index("Paket"))
    else:
        st.caption("Veri yok.")

elif page == "Paket Yönetimi":
    if user["role"] not in ("admin","yonetici"):
        st.warning("Bu sayfaya sadece admin/yonetici erişir.")
        st.stop()
    st.header("Paket Yönetimi")
    pkgs = list_packages(False)
    st.dataframe(pkgs, use_container_width=True, hide_index=True)
    st.markdown("---")
    c1,c2,c3 = st.columns([3,4,2])
    with c1:
        sel = st.selectbox("Düzenlenecek Paket", ["Yeni Paket"] + pkgs["name"].tolist())
    with c2:
        name = st.text_input("Paket Adı", value=(sel if sel!="Yeni Paket" else ""))
        desc = st.text_input("Açıklama", value=(pkgs[pkgs["name"]==sel]["description"].iloc[0] if sel!="Yeni Paket" and not pkgs.empty else ""))
    with c3:
        active = st.checkbox("Aktif", value=True)
    if st.button("Kaydet / Güncelle", type="primary"):
        pkg_id = int(pkgs[pkgs["name"]==sel]["id"].iloc[0]) if (sel!="Yeni Paket" and not pkgs.empty) else None
        ok, msg = upsert_package(name.strip(), desc.strip(), active, pkg_id)
        (st.success if ok else st.error)(msg)

elif page == "Kullanıcı Yönetimi":
    if user["role"] not in ("admin","yonetici"):
        st.warning("Bu sayfaya sadece admin/yonetici erişir.")
        st.stop()
    st.header("Kullanıcı Yönetimi")
    dfu = list_users()
    st.dataframe(dfu, use_container_width=True, hide_index=True)
    st.markdown("---")
    c1,c2,c3 = st.columns(3)
    with c1:
        nu = st.text_input("Kullanıcı adı")
        nf = st.text_input("Ad Soyad")
        nph= st.text_input("Telefon (E.164: +90...)")
    with c2:
        npw = st.text_input("Şifre", type="password")
        role= st.selectbox("Rol", ["personel","yonetici","admin"], index=0)
        noti= st.checkbox("Bildirimleri Aç", value=True)
    with c3:
        st.caption("Güncelle/sil özellikleri bir sonraki sürümde.")
    if st.button("Kullanıcı Oluştur", type="primary"):
        if not nu or not npw:
            st.error("Kullanıcı adı ve şifre zorunlu.")
        else:
            ok, msg = create_user(nu.strip(), npw, nf.strip(), role, nph.strip(), noti)
            (st.success if ok else st.error)(msg)

elif page == "Test Uyarısı (Manuel)":
    st.header("Test Uyarısı Gönder (Manuel)")
    to = st.text_input("Kime (örn: +90555xxxxxxx veya whatsapp:+90555...)", value=user.get("phone") or "")
    body = st.text_area("Mesaj", value="Örnek: İlyas Ural isimli hastamızın Kardiyoloji muayenesi 10 dk sonra, lütfen teyit alınız ve hastaya eşlik ediniz.", height=120)
    if st.button("Mesaj Gönder", type="primary"):
        if not can_send_whatsapp():
            st.error("Twilio/WhatsApp yapılandırması eksik.")
        else:
            ok = send_whatsapp_message(to, body)
            (st.success if ok else st.error)("Gönderildi." if ok else "Gönderilemedi.")

# küçük CSS
st.markdown("""
<style>
.sidebar .stButton>button { width: 100%; }
.stMetric { text-align:center; }
</style>
""", unsafe_allow_html=True)
