# app.py — Check-up Takip (Bulut, Çok Kullanıcılı, WhatsApp uyarılı)
# GEREKSİNİMLER: requirements.txt dosyasına bakınız (aşağıda ayrıca verdim)
# NASIL ÇALIŞTIRILIR (özet):
# 1) Bilgisayarında çalıştırmak için: `pip install -r requirements.txt` ardından `streamlit run app.py`
# 2) Buluta almak için: Kodu bir GitHub deposuna koy → Streamlit Cloud'da bu depoyu seç → Secrets'a TWILIO bilgilerini ekle → Deploy.

import os
import sqlite3
from contextlib import closing
from datetime import datetime, date, timedelta
import hashlib
from typing import Optional, List, Tuple

import pandas as pd
import streamlit as st
from twilio.rest import Client

# ---------------------- Genel Ayarlar ----------------------
DB_PATH = os.getenv("DB_PATH", "checkup_tracker.db")
APP_TITLE = "🏥 Check-up Takip Sistemi"
AUTO_REFRESH_SEC = 60  # sayfa otomatik yenileme sıklığı (sn)

# Admin başlangıç bilgileri (Streamlit Cloud'da Secrets'dan da gelebilir)
DEFAULT_ADMIN_USER = st.secrets.get("ADMIN_USERNAME", os.getenv("ADMIN_USERNAME", "admin"))
DEFAULT_ADMIN_PASS = st.secrets.get("ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", "Edam456+"))

# Twilio / WhatsApp (isteğe bağlı; yoksa sistem uyarıları atlar)
TWILIO_SID = st.secrets.get("TWILIO_ACCOUNT_SID", os.getenv("TWILIO_ACCOUNT_SID", ""))
TWILIO_TOKEN = st.secrets.get("TWILIO_AUTH_TOKEN", os.getenv("TWILIO_AUTH_TOKEN", ""))
TWILIO_WHATSAPP_FROM = st.secrets.get("TWILIO_WHATSAPP_FROM", os.getenv("TWILIO_WHATSAPP_FROM", ""))  # örn: 'whatsapp:+14155238886'

# ---------------------- Yardımcılar ----------------------
STATUS_OPTIONS = [
    "Planlandı",
    "Kayıt Alındı",
    "Devam Ediyor",
    "Sonuç Bekleniyor",
    "Tamamlandı",
    "Tekrar Gerekli",
    "Atlandı",
    "İptal",
]

DEFAULT_PACKAGES = [
    ("Standart", "Temel kan + görüntüleme + EKG"),
    ("VIP", "Genişletilmiş biyokimya + kardiyo testleri"),
    ("Kadın Sağlığı", "MMG/PAP/USG içeren paket"),
    ("Premium Kardiyoloji", "EKO + Efor + ileri kardiyo"),
    ("Genel Tarama", "Yaşa göre kapsamlı tarama"),
]


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


# ---------------------- DB Kurulum ----------------------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                full_name TEXT,
                role TEXT DEFAULT 'personel', -- 'admin' | 'yonetici' | 'personel'
                phone TEXT,
                notifications_enabled INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                description TEXT,
                active INTEGER DEFAULT 1
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS patients (
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
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER,
                test_name TEXT,
                planned_at TEXT,
                status TEXT,
                completed_at TEXT,
                notified INTEGER DEFAULT 0, -- 10 dk kala WA bildirimi yapıldı mı
                comments TEXT,
                FOREIGN KEY(patient_id) REFERENCES patients(id)
            )
            """
        )

    # İlk admin ve paketleri yükle
    bootstrap_admin_and_packages()


def bootstrap_admin_and_packages():
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        # Admin var mı?
        cur = con.execute("SELECT id FROM users WHERE username=?", (DEFAULT_ADMIN_USER,))
        if cur.fetchone() is None:
            con.execute(
                "INSERT INTO users (username, password_hash, full_name, role, phone, notifications_enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    DEFAULT_ADMIN_USER,
                    _hash_password(DEFAULT_ADMIN_PASS),
                    "Yönetici (Admin)",
                    "admin",
                    "",
                    1,
                    _now_str(),
                ),
            )
        # Paketler var mı?
        cur = con.execute("SELECT COUNT(*) FROM packages")
        n = cur.fetchone()[0]
        if n == 0:
            for name, desc in DEFAULT_PACKAGES:
                con.execute(
                    "INSERT INTO packages (name, description, active) VALUES (?, ?, 1)",
                    (name, desc),
                )


# ---------------------- DB İşlevleri ----------------------

def validate_login(username: str, password: str) -> Optional[dict]:
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute("SELECT id, username, password_hash, full_name, role, phone, notifications_enabled FROM users WHERE username=?", (username,))
        row = cur.fetchone()
        if not row:
            return None
        ok = row[2] == _hash_password(password)
        if not ok:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "full_name": row[3],
            "role": row[4],
            "phone": row[5] or "",
            "notifications_enabled": bool(row[6]),
        }


def create_user(username: str, password: str, full_name: str, role: str, phone: str, notifications_enabled: bool=True) -> Tuple[bool, str]:
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute(
                "INSERT INTO users (username, password_hash, full_name, role, phone, notifications_enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, _hash_password(password), full_name, role, phone, 1 if notifications_enabled else 0, _now_str()),
            )
        return True, "Kullanıcı oluşturuldu"
    except sqlite3.IntegrityError:
        return False, "Kullanıcı adı zaten var"


def update_user_notifications(user_id: int, enabled: bool):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("UPDATE users SET notifications_enabled=? WHERE id=?", (1 if enabled else 0, user_id))


def list_users() -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as con:
        return pd.read_sql_query("SELECT id, username, full_name, role, phone, notifications_enabled FROM users ORDER BY id", con)


def list_packages(active_only=True) -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as con:
        if active_only:
            q = "SELECT id, name, description, active FROM packages WHERE active=1 ORDER BY name"
        else:
            q = "SELECT id, name, description, active FROM packages ORDER BY name"
        return pd.read_sql_query(q, con)


def upsert_package(name: str, description: str, active: bool=True, pkg_id: Optional[int]=None) -> Tuple[bool, str]:
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        try:
            if pkg_id:
                con.execute("UPDATE packages SET name=?, description=?, active=? WHERE id=?", (name, description, 1 if active else 0, pkg_id))
                return True, "Paket güncellendi"
            else:
                con.execute("INSERT INTO packages (name, description, active) VALUES (?, ?, ?)", (name, description, 1 if active else 0))
                return True, "Paket eklendi"
        except sqlite3.IntegrityError:
            return False, "Bu paket adı zaten var"


def create_patient(row: dict) -> Tuple[bool, str, Optional[int]]:
    try:
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute(
                """
                INSERT INTO patients (patient_code, full_name, dob, phone, package_id, checkup_date, coordinator, amount_billed, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("patient_code"),
                    row.get("full_name"),
                    row.get("dob"),
                    row.get("phone"),
                    row.get("package_id"),
                    row.get("checkup_date"),
                    row.get("coordinator"),
                    float(row.get("amount_billed") or 0),
                    row.get("notes"),
                    _now_str(),
                    _now_str(),
                ),
            )
            pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        return True, "Hasta kaydedildi", pid
    except sqlite3.IntegrityError as e:
        return False, f"Hata: {e}", None


def add_test(patient_id: int, test_name: str, planned_at: str):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute(
            "INSERT INTO tests (patient_id, test_name, planned_at, status) VALUES (?, ?, ?, ?)",
            (patient_id, test_name, planned_at, "Planlandı"),
        )


def fetch_patients(filters: dict) -> pd.DataFrame:
    query = """
    SELECT p.id, p.patient_code, p.full_name, p.dob, p.phone, pk.name AS package, p.checkup_date, p.coordinator, p.amount_billed, p.notes, p.created_at
    FROM patients p LEFT JOIN packages pk ON pk.id = p.package_id
    WHERE 1=1
    """
    params: List = []
    if pkg := filters.get("package"):
        query += " AND pk.name=?"
        params.append(pkg)
    if dr := filters.get("date_range"):
        start, end = dr
        query += " AND date(p.checkup_date) BETWEEN date(?) AND date(?)"
        params.extend([start, end])
    with closing(sqlite3.connect(DB_PATH)) as con:
        return pd.read_sql_query(query, con, params=params)


def fetch_tests(patient_id: int) -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as con:
        return pd.read_sql_query("SELECT * FROM tests WHERE patient_id=? ORDER BY planned_at", con, params=(patient_id,))


def update_test_status(test_id: int, status: str, completed: bool=False, comments: str=""):
    completed_at = _now_str() if completed or status == "Tamamlandı" else None
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute(
            "UPDATE tests SET status=?, completed_at=?, comments=? WHERE id=?",
            (status, completed_at, comments, test_id),
        )


# ---------------------- WhatsApp Gönderimi ----------------------

def can_send_whatsapp() -> bool:
    return bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_WHATSAPP_FROM)


def send_whatsapp_message(to_phone_e164: str, body: str) -> bool:
    """to_phone_e164 örn: '+90555XXXXXXX'. Kullanıcı numarası WhatsApp Sandbox'a join etmiş olmalı."""
    if not can_send_whatsapp():
        return False
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{to_phone_e164}",
            body=body,
        )
        return True
    except Exception as e:
        st.warning(f"WhatsApp gönderim hatası: {e}")
        return False


def notify_upcoming_tests():
    """
    Önümüzdeki 10 dakika içinde başlayacak ve bildirimi gitmemiş testleri bulur.
    Bildirimleri kullanıcı tercihlerine göre (notifications_enabled) gönderir.
    """
    if not can_send_whatsapp():
        return 0
    now = datetime.now()
    soon = now + timedelta(minutes=10)
    count = 0
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute(
            """
            SELECT t.id, t.test_name, t.planned_at, p.full_name as patient_name
            FROM tests t
            JOIN patients p ON p.id = t.patient_id
            WHERE t.notified=0 AND t.status IN ('Planlandı','Kayıt Alındı','Devam Ediyor')
                  AND datetime(t.planned_at) BETWEEN datetime(?) AND datetime(?)
            """,
            (_now_str(), soon.strftime("%Y-%m-%d %H:%M:%S")),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    # Bildirim almak isteyen kullanıcıları çek
    users_df = list_users()
    users_df = users_df[users_df["notifications_enabled"] == 1]
    for t_id, test_name, planned_at, patient_name in rows:
        msg = f"{patient_name} isimli hastamızın {test_name} işlemi 10 dk sonra. Lütfen bölümü arayarak teyit alınız ve hastaya eşlik ediniz."
        for _, u in users_df.iterrows():
            phone = str(u["phone"]).strip()
            if phone.startswith("+") and len(phone) >= 8:
                send_whatsapp_message(phone, msg)
        # notified işaretle
        with closing(sqlite3.connect(DB_PATH)) as con, con:
            con.execute("UPDATE tests SET notified=1 WHERE id=?", (t_id,))
        count += 1
    return count


# ---------------------- UI ----------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)

init_db()

# Otomatik yenileme (arka planda tetiklenmiş bildirim taraması için)
st_autorefresh = st.sidebar.toggle("Otomatik yenile (60 sn)", value=True)
if st_autorefresh:
    st.experimental_rerun  # placeholder (Streamlit 1.36+ için st_autorefresh kullanımı)
    try:
        from streamlit.runtime.scriptrunner import add_script_run_ctx  # noqa
        # Not: st_autorefresh API'si versiyona göre değişebilir, aşağıdaki yedek çözüm:
        st.session_state.setdefault("_tick", 0)
        st.session_state["_tick"] = (st.session_state["_tick"] + 1) % 1_000_000
        st.caption("⏱️ Sayfa arka planda periyodik olarak yenileniyor.")
    except Exception:
        pass

# Giriş / Oturum
if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.subheader("Giriş Yap")
    colA, colB = st.columns(2)
    with colA:
        username = st.text_input("Kullanıcı Adı", value="")
        password = st.text_input("Şifre", type="password")
        if st.button("Giriş"):
            user = validate_login(username.strip(), password)
            if user:
                st.session_state.user = user
                st.success(f"Hoş geldiniz, {user['full_name']}")
                st.experimental_rerun()
            else:
                st.error("Kullanıcı adı veya şifre hatalı")
    with colB:
        st.info("Admin ilk giriş bilgileri: kullanıcı adı 'admin', şifre 'Edam456+' (Secrets ile değiştirilebilir).")
    st.stop()

user = st.session_state.user

with st.sidebar:
    st.markdown(f"**👤 {user['full_name']} ({user['role']})**")
    notif_toggle = st.toggle("Bildirimleri Aç/Kapat", value=user["notifications_enabled"])
    if notif_toggle != user["notifications_enabled"]:
        update_user_notifications(user_id=user["id"], enabled=notif_toggle)
        st.session_state.user["notifications_enabled"] = notif_toggle
        st.success("Bildirim tercihiniz güncellendi")

    menu = ["Hasta Kayıt", "Liste & Filtre", "Tetkik Yönetimi", "Raporlar"]
    if user["role"] in ("admin", "yonetici"):
        menu += ["Paket Yönetimi", "Kullanıcı Yönetimi", "Test Uyarısı (Manuel)"]
    page = st.radio("Menü", menu, index=0)
    st.markdown("---")
    st.caption("Not: WhatsApp uyarıları için Twilio Sandbox yapılandırılmalıdır.")

# Arkaplanda yaklaşan testler için bildirim taraması
try:
    sent = notify_upcoming_tests()
    if sent:
        st.toast(f"🔔 {sent} tetkik için bildirim gönderildi.")
except Exception as e:
    st.warning(f"Bildirim kontrolü çalıştırılamadı: {e}")

# ---------------------- Sayfalar ----------------------
if page == "Hasta Kayıt":
    st.subheader("Hasta Kaydı Oluştur")
    pkgs = list_packages(active_only=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        patient_code = st.text_input("Hasta Kodu (benzersiz)")
        full_name = st.text_input("Ad Soyad")
        dob = st.date_input("Doğum Tarihi", value=date(1990,1,1))
    with c2:
        phone = st.text_input("Telefon (örn: +90555XXXXXXX)")
        pkg_name = st.selectbox("Paket", pkgs["name"].tolist() if not pkgs.empty else ["(paket yok)"])
        checkup_date = st.date_input("Check-up Tarihi", value=date.today())
    with c3:
        coordinator = st.text_input("Koordinatör/Danışman", value=user["full_name"]) 
        amount_billed = st.number_input("Fatura Tutarı (TL)", min_value=0.0, step=50.0)
        notes = st.text_area("Notlar", height=80)

    if st.button("Kaydet"):
        if not patient_code or not full_name:
            st.error("Hasta kodu ve Ad Soyad zorunlu")
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
            if ok:
                st.success(f"{msg} (ID: {pid})")
            else:
                st.error(msg)

elif page == "Liste & Filtre":
    st.subheader("Hasta Listesi")
    pkgs = list_packages(active_only=False)
    colf1, colf2 = st.columns(2)
    with colf1:
        pkg_filter = st.selectbox("Paket filtresi", [""] + pkgs["name"].tolist())
    with colf2:
        dr = st.date_input("Tarih aralığı", value=(date.today()-timedelta(days=30), date.today()))

    df = fetch_patients({
        "package": pkg_filter if pkg_filter else None,
        "date_range": dr,
    })
    st.dataframe(df, use_container_width=True)

elif page == "Tetkik Yönetimi":
    st.subheader("Tetkik Planlama ve Durum Yönetimi")
    pid = st.number_input("Hasta ID", min_value=1, step=1)
    c1, c2 = st.columns(2)
    with c1:
        test_name = st.text_input("Tetkik Adı", placeholder="Kardiyoloji Muayenesi / MR / EKO ...")
    with c2:
        planned_dt = st.text_input("Planlanan Tarih-Saat (YYYY-MM-DD HH:MM)", value=datetime.now().strftime("%Y-%m-%d %H:00"))
    if st.button("Tetkik Ekle"):
        try:
            # format doğrulama
            datetime.strptime(planned_dt, "%Y-%m-%d %H:%M")
            add_test(pid, test_name.strip(), planned_dt + ":00")
            st.success("Tetkik eklendi")
        except ValueError:
            st.error("Tarih-saat formatı hatalı. Örn: 2025-08-09 14:30")

    tests_df = fetch_tests(pid)
    if tests_df.empty:
        st.info("Bu hastaya ait tetkik listesi boş")
    else:
        edited = st.data_editor(
            tests_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "status": st.column_config.SelectboxColumn("Durum", options=STATUS_OPTIONS),
            },
            disabled=["id", "patient_id", "planned_at", "notified"],
        )
        if st.button("Değişiklikleri Kaydet"):
            for _, r in edited.iterrows():
                update_test_status(int(r["id"]), str(r["status"]), completed=str(r["status"])=="Tamamlandı", comments=str(r.get("comments") or ""))
            st.success("Güncellendi")

elif page == "Raporlar":
    st.subheader("Raporlar ve Göstergeler")
    df = fetch_patients({})
    if df.empty:
        st.info("Rapor için veri bulunamadı")
    else:
        # Göstergeler
        with closing(sqlite3.connect(DB_PATH)) as con:
            tests_all = pd.read_sql_query("SELECT * FROM tests", con)
        cA, cB, cC, cD = st.columns(4)
        cA.metric("Toplam Hasta", len(df))
        cB.metric("Toplam Tetkik", len(tests_all))
        done = (tests_all["status"] == "Tamamlandı").sum() if not tests_all.empty else 0
        waiting = (tests_all["status"] == "Sonuç Bekleniyor").sum() if not tests_all.empty else 0
        cC.metric("Tamamlanan Tetkik", int(done))
        cD.metric("Sonuç Bekleyen", int(waiting))

        # Paket dağılımı
        st.markdown("### Paket Dağılımı")
        pkg_counts = df["package"].value_counts().reset_index()
        if not pkg_counts.empty:
            pkg_counts.columns = ["Paket", "Hasta Sayısı"]
            st.bar_chart(pkg_counts.set_index("Paket"))

        # Aylık hasta sayısı
        st.markdown("### Aylık Hasta Sayısı")
        tmp = df.copy()
        tmp["Ay"] = pd.to_datetime(tmp["checkup_date"]).dt.to_period("M").astype(str)
        monthly = tmp.groupby("Ay").size().reset_index(name="Hasta Sayısı")
        if not monthly.empty:
            st.bar_chart(monthly.set_index("Ay"))

        # Ortalama bitiş süresi (planlanan → tamamlanan)
        st.markdown("### Ortalama Check-up Bitiş Süresi (saat)")
        if not tests_all.empty:
            tt = tests_all.dropna(subset=["planned_at", "completed_at"]).copy()
            if not tt.empty:
                tt["planned_at"] = pd.to_datetime(tt["planned_at"])
                tt["completed_at"] = pd.to_datetime(tt["completed_at"])
                tt["delta_h"] = (tt["completed_at"] - tt["planned_at"]).dt.total_seconds() / 3600
                st.metric("Ortalama", round(tt["delta_h"].mean(), 2))
            else:
                st.caption("Tamamlanan tetkik bulunamadı")

        # Toplam fatura
        st.markdown("### Fatura Özeti")
        total_bill = float(df["amount_billed"].fillna(0).sum())
        st.metric("Toplam Fatura (TL)", f"{total_bill:,.2f}")

elif page == "Paket Yönetimi":
    st.subheader("Paket Yönetimi (Admin/Yönetici)")
    pkgs = list_packages(active_only=False)
    st.dataframe(pkgs, use_container_width=True)
    st.markdown("---")
    c1, c2, c3 = st.columns([3,4,2])
    with c1:
        sel = st.selectbox("Düzenlenecek Paket", ["Yeni Paket"] + pkgs["name"].tolist())
    with c2:
        name = st.text_input("Paket Adı", value=(sel if sel != "Yeni Paket" else ""))
        desc = st.text_input("Açıklama", value=(pkgs[pkgs["name"]==sel]["description"].iloc[0] if sel!="Yeni Paket" and not pkgs.empty else ""))
    with c3:
        active = st.checkbox("Aktif", value=True)
    if st.button("Kaydet / Güncelle"):
        pkg_id = int(pkgs[pkgs["name"]==sel]["id"].iloc[0]) if (sel != "Yeni Paket" and not pkgs.empty) else None
        ok, msg = upsert_package(name.strip(), desc.strip(), active, pkg_id)
        (st.success if ok else st.error)(msg)

elif page == "Kullanıcı Yönetimi":
    st.subheader("Kullanıcı Yönetimi (Admin/Yönetici)")
    dfu = list_users()
    st.dataframe(dfu, use_container_width=True)
    st.markdown("---")
    c1, c2, c3 = st.columns(3)
    with c1:
        new_username = st.text_input("Yeni Kullanıcı Adı")
        new_fullname = st.text_input("Ad Soyad")
        new_phone = st.text_input("Telefon (E.164: +90...)")
    with c2:
        new_password = st.text_input("Şifre", type="password")
        role = st.selectbox("Rol", ["personel", "yonetici", "admin"], index=0)
        notif_on = st.checkbox("Bildirimleri Aç", value=True)
    with c3:
        st.caption("Admin kullanıcı silme/devre dışı bırakma 2. sürümde eklenecek.")
    if st.button("Kullanıcı Oluştur"):
        if not new_username or not new_password:
            st.error("Kullanıcı adı ve şifre zorunlu")
        else:
            ok, msg = create_user(new_username.strip(), new_password, new_fullname.strip(), role, new_phone.strip(), notif_on)
            (st.success if ok else st.error)(msg)

elif page == "Test Uyarısı (Manuel)":
    st.subheader("Test Uyarısı Gönder (Manuel)")
    test_msg = st.text_area("Mesaj", value="Örnek: İlyas Ural isimli hastamızın Kardiyoloji muayenesi 10 dk sonra, lütfen teyit alınız ve hastaya eşlik ediniz.", height=120)
    if st.button("Tüm kullanıcılara gönder"):
        if not can_send_whatsapp():
            st.error("Twilio/WhatsApp yapılandırmasını yapmadığınız için gönderilemedi.")
        else:
            dfu = list_users()
            dfu = dfu[dfu["notifications_enabled"] == 1]
            sent = 0
            for _, u in dfu.iterrows():
                phone = str(u["phone"]).strip()
                if phone.startswith("+"):
                    if send_whatsapp_message(phone, test_msg):
                        sent += 1
            st.success(f"Gönderildi: {sent} kullanıcı")

# Görsel iyileştirme
st.markdown(
    """
    <style>
    .stMetric { text-align:center; }
    .sidebar .stButton>button { width: 100%; }
    </style>
    """,
    unsafe_allow_html=True,
)
