# app.py — Check‑up Takip Sistemi (tam sürüm)
# Özellikler:
# - Giriş (admin: admin / Edam456+)  [Secrets ile değiştirilebilir]
# - Hasta kaydı, liste/filtre + Excel indirme
# - Tetkik planlama, durum güncelleme
# - Paket yönetimi, kullanıcı yönetimi
# - Raporlar (metrikler, grafikler)
# - WhatsApp (Twilio Sandbox) manual uyarı + yaklaşan tetkikler için 10 dk kala bildirim taraması
#
# Gereksinimler: requirements.txt
# Secrets (Streamlit Cloud > Edit secrets):
# ADMIN_USERNAME="admin"
# ADMIN_PASSWORD="Edam456+"
# TWILIO_ACCOUNT_SID="ACxxxxxxxx..."
# TWILIO_AUTH_TOKEN="yyyyyyyy..."
# TWILIO_WHATSAPP_FROM="whatsapp:+14155238886"

import os
import io
import sqlite3
import hashlib
from contextlib import closing
from datetime import datetime, date, timedelta

import pandas as pd
import streamlit as st

# Twilio opsiyonel; yoksa mesaj fonksiyonu sadece False döner
try:
    from twilio.rest import Client  # type: ignore
    _TWILIO_AVAILABLE = True
except Exception:
    _TWILIO_AVAILABLE = False

# ---------------------- Genel Ayarlar ----------------------
DB_PATH = os.getenv("DB_PATH", "checkup_tracker.db")
APP_TITLE = "Check-up Takip Sistemi"

# Admin başlangıç bilgileri (Secrets > ENV sırası)
def _get_secret(key: str, default: str = "") -> str:
    # st.secrets güvenle yoklanır; Streamlit Local'de yoksa KeyError atmaz
    try:
        return str(st.secrets.get(key, os.getenv(key, default)))
    except Exception:
        return os.getenv(key, default)

ADMIN_USERNAME = _get_secret("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _get_secret("ADMIN_PASSWORD", "Edam456+")

# Twilio / WhatsApp
TWILIO_SID = _get_secret("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = _get_secret("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = _get_secret("TWILIO_WHATSAPP_FROM", "")  # "whatsapp:+14155238886"

STATUS_OPTIONS = [
    "Planlandı", "Kayıt Alındı", "Devam Ediyor",
    "Sonuç Bekleniyor", "Tamamlandı", "Tekrar Gerekli",
    "Atlandı", "İptal",
]

DEFAULT_PACKAGES = [
    ("Standart", "Temel kan + görüntüleme + EKG"),
    ("VIP", "Genişletilmiş biyokimya + kardiyo testleri"),
    ("Kadın Sağlığı", "MMG/PAP/USG içeren paket"),
    ("Premium Kardiyoloji", "EKO + Efor + ileri kardiyo"),
    ("Genel Tarama", "Yaşa göre kapsamlı tarama"),
]

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# ---------------------- DB Kurulum ----------------------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            pass_hash TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'personel', -- admin | yonetici | personel
            phone TEXT,
            notifications_enabled INTEGER DEFAULT 1,
            created_at TEXT
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS packages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            description TEXT,
            active INTEGER DEFAULT 1
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS patients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_code TEXT UNIQUE,
            full_name TEXT,
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
            patient_id INTEGER,
            test_name TEXT,
            planned_at TEXT,
            status TEXT,
            completed_at TEXT,
            notified INTEGER DEFAULT 0,
            comments TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        )""")

    # Bootstrap admin & paketler
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        # Admin
        cur = con.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,))
        if cur.fetchone() is None:
            con.execute(
                "INSERT INTO users(username,pass_hash,full_name,role,phone,notifications_enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                (ADMIN_USERNAME, sha256(ADMIN_PASSWORD), "Yönetici (Admin)", "admin", "", 1, now_str())
            )
        # Paketler
        cur = con.execute("SELECT COUNT(*) FROM packages")
        if (cur.fetchone() or [0])[0] == 0:
            for n, d in DEFAULT_PACKAGES:
                try:
                    con.execute("INSERT INTO packages(name,description,active) VALUES(?,?,1)", (n, d))
                except sqlite3.IntegrityError:
                    pass

# ---------------------- DB İşlevleri ----------------------
def validate_login(u: str, p: str):
    with closing(sqlite3.connect(DB_PATH)) as con:
        r = con.execute("SELECT id, username, pass_hash, full_name, role, phone, notifications_enabled FROM users WHERE username=?", (u,)).fetchone()
        if not r: return None
        if r[2] != sha256(p): return None
        return {
            "id": r[0], "username": r[1], "full_name": r[3] or r[1],
            "role": r[4] or "personel", "phone": r[5] or "",
            "notifications_enabled": bool(r[6]),
        }

def update_user_notifications(uid: int, on: bool):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("UPDATE users SET notifications_enabled=? WHERE id=?", (1 if on else 0, uid))

def list_users() -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as con:
        return pd.read_sql_query(
            "SELECT id,username,full_name,role,phone,notifications_enabled,created_at FROM users ORDER BY id DESC", con
        )

def create_user(username, password, full_name, role, phone, notifications_enabled=True):
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute("""INSERT INTO users(username,pass_hash,full_name,role,phone,notifications_enabled,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (username, sha256(password), full_name, role, phone, 1 if notifications_enabled else 0, now_str()))
        return True, "Kullanıcı oluşturuldu"
    except sqlite3.IntegrityError:
        return False, "Bu kullanıcı adı zaten var"

def list_packages(active_only=True) -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as con:
        q = "SELECT id,name,description,active FROM packages" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return pd.read_sql_query(q, con)

def upsert_package(name, description, active=True, pkg_id=None):
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            if pkg_id:
                con.execute("UPDATE packages SET name=?, description=?, active=? WHERE id=?",
                            (name, description, 1 if active else 0, pkg_id))
                return True, "Paket güncellendi"
            else:
                con.execute("INSERT INTO packages(name,description,active) VALUES(?,?,?)",
                            (name, description, 1 if active else 0))
                return True, "Paket eklendi"
    except sqlite3.IntegrityError:
        return False, "Bu paket adı zaten var"

def create_patient(row: dict):
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute("""
                INSERT INTO patients(patient_code,full_name,dob,phone,package_id,checkup_date,coordinator,amount_billed,notes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["patient_code"], row["full_name"], row["dob"], row["phone"],
                 row["package_id"], row["checkup_date"], row["coordinator"],
                 float(row.get("amount_billed") or 0), row["notes"], now_str(), now_str()))
            pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        return True, "Hasta kaydedildi", pid
    except sqlite3.IntegrityError as e:
        return False, f"Hata: {e}", None

def fetch_patients(filters: dict) -> pd.DataFrame:
    q = """
    SELECT p.id, p.patient_code, p.full_name, p.dob, p.phone,
           pk.name AS package, p.checkup_date, p.coordinator,
           p.amount_billed, p.notes, p.created_at
    FROM patients p LEFT JOIN packages pk ON pk.id = p.package_id
    WHERE 1=1
    """
    params = []
    if filters.get("package"):
        q += " AND pk.name=?"
        params.append(filters["package"])
    if filters.get("date_range"):
        s, e = filters["date_range"]
        q += " AND date(p.checkup_date) BETWEEN date(?) AND date(?)"
        params += [s, e]
    with closing(sqlite3.connect(DB_PATH)) as con:
        return pd.read_sql_query(q, con, params=params)

def add_test(patient_id: int, test_name: str, planned_at: str):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("INSERT INTO tests(patient_id,test_name,planned_at,status) VALUES(?,?,?,?)",
                    (patient_id, test_name, planned_at, "Planlandı"))

def fetch_tests(pid: int) -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as con:
        return pd.read_sql_query("SELECT * FROM tests WHERE patient_id=? ORDER BY planned_at", con, params=(pid,))

def update_test_status(test_id: int, status: str, completed=False, comments: str = ""):
    completed_at = now_str() if completed or status == "Tamamlandı" else None
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("UPDATE tests SET status=?, completed_at=?, comments=? WHERE id=?",
                    (status, completed_at, comments, test_id))

# ---------------------- WhatsApp (Twilio) ----------------------
def can_send_whatsapp() -> bool:
    return _TWILIO_AVAILABLE and bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_WHATSAPP_FROM)

def normalize_to_whatsapp(raw: str) -> str:
    # "whatsapp:+90..." formatına çevir; + işaretini ve rakamları koru
    digits = "".join(ch for ch in raw.strip() if ch.isdigit() or ch == "+")
    if not digits.startswith("+"):
        # Türkiye varsayımı; istersen ülkeni değiştir
        digits = "+90" + digits
    return f"whatsapp:{digits}"

def send_whatsapp_message(to_number: str, body: str) -> bool:
    """ WhatsApp sandbox mesajı. to_number: '+90555..' veya 'whatsapp:+905..' kabul eder """
    if not can_send_whatsapp():
        st.warning("Twilio yapılandırması eksik (Secrets/ENV).")
        return False
    try:
        sid = TWILIO_SID.strip()
        token = TWILIO_TOKEN.strip()
        wfrom = TWILIO_WHATSAPP_FROM.strip()
        wto = to_number.strip()
        if not wto.startswith("whatsapp:"):
            wto = normalize_to_whatsapp(wto)

        # Teşhis (son 4 hane)
        st.caption(f"Twilio check → SID ..{sid[-4:]}, FROM={wfrom}, TO={wto}")

        client = Client(sid, token)
        msg = client.messages.create(from_=wfrom, to=wto, body=body)
        st.success(f"Twilio OK (sid ..{msg.sid[-6:]})")
        return True
    except Exception as e:
        st.error(f"WhatsApp gönderim hatası: {e}")
        return False

def notify_upcoming_tests() -> int:
    """Önümüzdeki 10 dk içinde başlayacak ve bildirimi gitmemiş testleri kullanıcı(lar)a iletir."""
    if not can_send_whatsapp():
        return 0
    now_dt = datetime.now()
    soon = now_dt + timedelta(minutes=10)
    with closing(sqlite3.connect(DB_PATH)) as con:
        rows = con.execute("""
            SELECT t.id, t.test_name, t.planned_at, p.full_name
            FROM tests t
            JOIN patients p ON p.id=t.patient_id
            WHERE t.notified=0
              AND t.status IN ('Planlandı','Kayıt Alındı','Devam Ediyor')
              AND datetime(t.planned_at) BETWEEN datetime(?) AND datetime(?)
        """, (now_str(), soon.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    if not rows: return 0

    users = list_users()
    users = users[users["notifications_enabled"] == 1]
    sent = 0
    for t_id, test_name, planned_at, patient_name in rows:
        msg = f"{patient_name} isimli hastamızın {test_name} işlemi 10 dk sonra. Lütfen teyit ve refakat sağlayınız."
        for _, u in users.iterrows():
            phone = str(u["phone"]).strip()
            if phone.startswith("+") and len(phone) >= 8:
                ok = send_whatsapp_message(phone, msg)
                if ok: sent += 1
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute("UPDATE tests SET notified=1 WHERE id=?", (t_id,))
    return sent

# ---------------------- UI ----------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)

init_db()

# Oturum
if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.subheader("Giriş Yap")
    c1, c2 = st.columns([2,1])
    with c1:
        u = st.text_input("Kullanıcı adı", value=ADMIN_USERNAME if ADMIN_USERNAME!="admin" else "")
        p = st.text_input("Şifre", type="password")
        if st.button("Giriş", type="primary"):
            user = validate_login(u.strip(), p)
            if user:
                st.session_state.user = user
                st.success(f"Hoş geldiniz, {user['full_name']}")
                st.rerun()
            else:
                st.error("Kullanıcı adı veya şifre hatalı.")
    with c2:
        st.info("Varsayılan: admin / Edam456+  (Secrets ile değiştirilebilir)")
    st.stop()

user = st.session_state.user

with st.sidebar:
    st.markdown(f"**👤 {user['full_name']} ({user['role']})**")
    notif = st.toggle("Bildirimleri Aç/Kapat", value=user["notifications_enabled"])
    if notif != user["notifications_enabled"]:
        update_user_notifications(user["id"], notif)
        st.session_state.user["notifications_enabled"] = notif
        st.toast("Bildirim tercihiniz güncellendi.")

    menu = ["Hasta Kayıt", "Liste & Filtre", "Tetkik Yönetimi", "Raporlar"]
    if user["role"] in ("admin", "yonetici"):
        menu += ["Paket Yönetimi", "Kullanıcı Yönetimi", "Test Uyarısı (Manuel)"]
    page = st.radio("Menü", menu)

# Arkaplanda yaklaşan testler için *hafif* tarama düğmesi (manuel)
with st.sidebar.expander("🔔 Yaklaşan tetkikleri kontrol et"):
    if st.button("Şimdi kontrol et"):
        try:
            n = notify_upcoming_tests()
            if n: st.success(f"{n} bildirim gönderildi.")
            else: st.info("Şu anlık gönderilecek bildirim yok.")
        except Exception as e:
            st.warning(f"Kontrol çalışamadı: {e}")

# ---------------------- Sayfalar ----------------------
if page == "Hasta Kayıt":
    st.subheader("Hasta Kaydı Oluştur")
    pkgs = list_packages(True)
    c1, c2, c3 = st.columns(3)
    with c1:
        patient_code = st.text_input("Hasta Kodu (benzersiz)")
        full_name = st.text_input("Ad Soyad")
        dob = st.date_input("Doğum Tarihi", value=date(1990,1,1))
    with c2:
        phone = st.text_input("Telefon (örn: +90555xxxxxxx)")
        pkg_name = st.selectbox("Paket", pkgs["name"].tolist() if not pkgs.empty else ["(paket yok)"])
        checkup_date = st.date_input("Check‑up Tarihi", value=date.today())
    with c3:
        coordinator = st.text_input("Koordinatör/Danışman", value=user["full_name"])
        amount_billed = st.number_input("Fatura Tutarı (TL)", min_value=0.0, step=50.0)
        notes = st.text_area("Notlar", height=80)

    if st.button("Kaydet", type="primary"):
        if not patient_code or not full_name:
            st.error("Hasta kodu ve Ad Soyad zorunlu.")
        else:
            pkg_id = int(pkgs[pkgs["name"] == pkg_name]["id"].iloc[0]) if not pkgs.empty else None
            ok, msg, pid = create_patient({
                "patient_code": patient_code.strip(),
                "full_name": full_name.strip(),
                "dob": str(dob),
                "phone": phone.strip(),
                "package_id": pkg_id,
                "checkup_date": str(checkup_date),
                "coordinator": coordinator.strip(),
                "amount_billed": amount_billed,
                "notes": notes.strip(),
            })
            (st.success if ok else st.error)(msg)

elif page == "Liste & Filtre":
    st.subheader("Hasta Listesi")
    pkgs = list_packages(False)
    f1, f2 = st.columns(2)
    with f1:
        pf = st.selectbox("Paket filtresi", [""] + pkgs["name"].tolist())
    with f2:
        dr = st.date_input("Tarih aralığı", value=(date.today()-timedelta(days=30), date.today()))
    df = fetch_patients({"package": pf or None, "date_range": dr})
    st.dataframe(df, use_container_width=True, hide_index=True)
    if not df.empty:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
            df.to_excel(w, index=False, sheet_name="Hastalar")
        st.download_button("Excel’e Aktar", buf.getvalue(),
                           f"hasta_listesi_{date.today()}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

elif page == "Tetkik Yönetimi":
    st.subheader("Tetkik Planlama ve Durum Yönetimi")
    pid = st.number_input("Hasta ID", min_value=1, step=1)
    c1, c2 = st.columns(2)
    with c1:
        test_name = st.text_input("Tetkik Adı", value="Kardiyoloji Muayenesi")
    with c2:
        planned_dt = st.text_input("Planlanan Tarih-Saat (YYYY-MM-DD HH:MM)",
                                   value=datetime.now().strftime("%Y-%m-%d %H:00"))
    if st.button("Tetkik Ekle", type="primary"):
        try:
            datetime.strptime(planned_dt, "%Y-%m-%d %H:%M")
            add_test(pid, test_name.strip(), planned_dt + ":00")
            st.success("Tetkik eklendi.")
        except ValueError:
            st.error("Tarih-saat formatı hatalı. Örn: 2025-08-09 14:30")

    tdf = fetch_tests(pid)
    if tdf.empty:
        st.info("Bu hastaya ait tetkik listesi boş.")
    else:
        edited = st.data_editor(
            tdf, use_container_width=True, hide_index=True,
            column_config={"status": st.column_config.SelectboxColumn("Durum", options=STATUS_OPTIONS)},
            disabled=["id","patient_id","planned_at","notified"],
        )
        if st.button("Değişiklikleri Kaydet"):
            for _, r in edited.iterrows():
                update_test_status(int(r["id"]), str(r["status"]),
                                   completed=(str(r["status"])=="Tamamlandı"),
                                   comments=str(r.get("comments") or ""))
            st.success("Güncellendi.")

elif page == "Raporlar":
    st.subheader("Raporlar ve Göstergeler")
    df = fetch_patients({})
    with closing(sqlite3.connect(DB_PATH)) as con:
        tests_all = pd.read_sql_query("SELECT * FROM tests", con)

    cA, cB, cC, cD = st.columns(4)
    cA.metric("Toplam Hasta", len(df))
    cB.metric("Toplam Tetkik", 0 if tests_all is None else len(tests_all))
    done = int((tests_all["status"]=="Tamamlandı").sum()) if not tests_all.empty else 0
    waiting = int((tests_all["status"]=="Sonuç Bekleniyor").sum()) if not tests_all.empty else 0
    cC.metric("Tamamlanan Tetkik", done)
    cD.metric("Sonuç Bekleyen", waiting)

    st.markdown("### Paket Dağılımı")
    if not df.empty:
        pkg_counts = df["package"].value_counts().reset_index()
        pkg_counts.columns = ["Paket", "Hasta Sayısı"]
        st.bar_chart(pkg_counts.set_index("Paket"))
    else:
        st.caption("Veri yok.")

    st.markdown("### Aylık Hasta Sayısı")
    if not df.empty:
        tmp = df.copy()
        tmp["Ay"] = pd.to_datetime(tmp["checkup_date"], errors="coerce").dt.to_period("M").astype(str)
        monthly = tmp.groupby("Ay").size().reset_index(name="Hasta Sayısı")
        st.bar_chart(monthly.set_index("Ay"))

    st.markdown("### Fatura Özeti")
    total_bill = float(df["amount_billed"].fillna(0).sum()) if not df.empty else 0.0
    st.metric("Toplam Fatura (TL)", f"{total_bill:,.2f}")

elif page == "Paket Yönetimi":
    if user["role"] not in ("admin", "yonetici"):
        st.warning("Bu sayfaya sadece admin/yonetici erişebilir.")
        st.stop()
    st.subheader("Paket Yönetimi")
    pkgs = list_packages(False)
    st.dataframe(pkgs, use_container_width=True, hide_index=True)
    st.markdown("---")
    c1, c2, c3 = st.columns([3,4,2])
    with c1:
        sel = st.selectbox("Düzenlenecek Paket", ["Yeni Paket"] + pkgs["name"].tolist())
    with c2:
        name = st.text_input("Paket Adı", value=(sel if sel!="Yeni Paket" else ""))
        desc = st.text_input("Açıklama", value=(pkgs[pkgs["name"]==sel]["description"].iloc[0]
                                                if sel!="Yeni Paket" and not pkgs.empty else ""))
    with c3:
        active = st.checkbox("Aktif", value=True)
    if st.button("Kaydet / Güncelle", type="primary"):
        pkg_id = int(pkgs[pkgs["name"]==sel]["id"].iloc[0]) if (sel!="Yeni Paket" and not pkgs.empty) else None
        ok, msg = upsert_package(name.strip(), desc.strip(), active, pkg_id)
        (st.success if ok else st.error)(msg)

elif page == "Kullanıcı Yönetimi":
    if user["role"] not in ("admin", "yonetici"):
        st.warning("Bu sayfaya sadece admin/yonetici erişebilir.")
        st.stop()
    st.subheader("Kullanıcı Yönetimi")
    dfu = list_users()
    st.dataframe(dfu, use_container_width=True, hide_index=True)
    st.markdown("---")
    c1, c2, c3 = st.columns(3)
    with c1:
        nu = st.text_input("Yeni Kullanıcı Adı")
        nf = st.text_input("Ad Soyad")
        np = st.text_input("Telefon (E.164: +90...)")
    with c2:
        pw = st.text_input("Şifre", type="password")
        role = st.selectbox("Rol", ["personel","yonetici","admin"], index=0)
        on = st.checkbox("Bildirimleri Aç", value=True)
    with c3:
        st.caption("Kullanıcı silme/devre dışı bırakma sonraki sürüm.")
    if st.button("Kullanıcı Oluştur", type="primary"):
        if not nu or not pw:
            st.error("Kullanıcı adı ve şifre zorunlu.")
        else:
            ok, msg = create_user(nu.strip(), pw, nf.strip(), role, np.strip(), on)
            (st.success if ok else st.error)(msg)

elif page == "Test Uyarısı (Manuel)":
    if user["role"] not in ("admin", "yonetici"):
        st.warning("Bu sayfaya sadece admin/yonetici erişebilir.")
        st.stop()
    st.subheader("Test Uyarısı Gönder (Manuel)")
    to = st.text_input("Kime (örn: +90555xxxxxxx)", value=(user.get("phone") or ""))
    body = st.text_area("Mesaj",
        value="Örnek: İlyas Ural isimli hastamızın Kardiyoloji muayenesi 10 dk sonra. Lütfen teyit alınız ve refakat ediniz.")
    if st.button("Mesaj Gönder", type="primary"):
        ok = send_whatsapp_message(to, body)
        if not ok:
            st.warning("Gönderilemedi. (Secrets değerlerini ve telefonun Sandbox'a kayıtlı olduğunu kontrol edin.)")

# Görsel iyileştirme
st.markdown("""
<style>
.stMetric { text-align:center; }
.sidebar .stButton>button { width:100%; }
</style>
""", unsafe_allow_html=True)
