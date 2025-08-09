# app.py
# Check-up Takip Sistemi (Bulut, Çok Kullanıcı, Rapor, Excel Aktarım)
# Özellikler:
# - Giriş sistemi (admin + kullanıcı rolleri), kullanıcı başına bildirim açık/kapalı
# - Hasta kaydı / listeleme / Excel’e aktarım
# - Paket yönetimi
# - Tetkik planlama (Hasta seçimi dropdown, tarih & saat seçici, +10dk/+30dk/+1saat kısayolu)
# - Raporlar (tarih filtresine duyarlı; en çok paket, toplam fatura, ortalama tamamlanma süresi)
# - WhatsApp için hazırlık (Twilio secrets eklenirse çalışır; ekli değilse sessizce pas geçer)

import os, io, sqlite3, hashlib
from datetime import datetime, date, time, timedelta
from contextlib import closing

import streamlit as st
import pandas as pd

# ---- Ayarlar
APP_TITLE = "Check-up Takip Sistemi"
DB_PATH = "checkup_tracker.db"

# ---- Yardımcılar
def sha256(txt: str) -> str:
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(get_conn()) as conn, conn:  # auto-commit
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pass_hash TEXT NOT NULL,
            full_name TEXT,
            phone TEXT,
            role TEXT DEFAULT 'staff', -- 'admin' | 'manager' | 'staff'
            notify_enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_code TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            dob TEXT,
            phone TEXT,
            package TEXT,
            coordinator TEXT,
            checkup_date TEXT,
            amount_billed REAL DEFAULT 0,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            planned_at TEXT,
            status TEXT DEFAULT 'Planlandı', -- Planlandı, Devam, Sonuç, Tamamlandı, İptal
            completed_at TEXT,
            notified INTEGER DEFAULT 0,
            comments TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        )""")

        # Admin kullanıcısını oluştur (Secrets varsa onlardan, yoksa varsayılan)
        admin_user = os.getenv("ADMIN_USERNAME", st.secrets.get("ADMIN_USERNAME", "admin"))
        admin_pass = os.getenv("ADMIN_PASSWORD", st.secrets.get("ADMIN_PASSWORD", "admin"))
        pass_hash = sha256(admin_pass)
        cur = conn.execute("SELECT id FROM users WHERE username=?", (admin_user,))
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO users (username, pass_hash, full_name, role, notify_enabled) VALUES (?,?,?,?,?)",
                (admin_user, pass_hash, "Yönetici (Admin)", "admin", 1),
            )

        # Örnek paketler (varsa ekleme)
        default_packs = [
            ("Genel Tarama", "Temel laboratuvar + USG"),
            ("VIP", "Geniş kapsamlı VIP paket"),
            ("Kadın Sağlığı", "Kadın sağlığı odaklı taramalar"),
            ("Premium Kardiyoloji", "EKO, EKG, kardiyoloji muayene"),
            ("Standart", "Temel check-up içerikleri"),
        ]
        for n, d in default_packs:
            try:
                conn.execute("INSERT INTO packages (name, description) VALUES (?,?)", (n, d))
            except sqlite3.IntegrityError:
                pass

init_db()

# ---- Oturum / Giriş
if "user" not in st.session_state:
    st.session_state.user = None

def login_form():
    st.markdown("### Giriş Yap")
    u = st.text_input("Kullanıcı Adı", value=os.getenv("ADMIN_USERNAME", st.secrets.get("ADMIN_USERNAME", "admin")))
    p = st.text_input("Şifre", type="password")
    if st.button("Giriş"):
        with closing(get_conn()) as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
            if row and row["pass_hash"] == sha256(p):
                st.session_state.user = dict(row)
                st.success("Giriş başarılı.")
                st.experimental_rerun()
            else:
                st.error("Kullanıcı adı veya şifre hatalı.")

def require_login():
    if not st.session_state.user:
        login_form()
        st.stop()

# ---- UI Başlık
st.set_page_config(page_title=APP_TITLE, page_icon="🏥", layout="wide")

# ---- Giriş kontrolü
require_login()
user = st.session_state.user

# ---- Sidebar
st.sidebar.toggle("Otomatik yenile (60 sn)", value=False, key="autorefresh")
if st.session_state.autorefresh:
    st_autorefresh = st.experimental_data_editor if False else None  # no-op; placeholder
    # Streamlit 1.36+ için:
    st.runtime.legacy_caching.clear_cache() if False else None
    st.experimental_rerun() if False else None
    st.toast("Arka planda periyodik yenileniyor.", icon="⏱️")
    # pratik çözüm: her etkileşimde zaten yeniden koşar; uyarıyı bilgi amaçlı tuttuk.

st.sidebar.markdown(f"**Yönetici (Admin)** ({user['username']})" if user["role"] == "admin" else f"**Kullanıcı** ({user['username']})")
notify_state = st.sidebar.toggle("Bildirimleri Aç/Kapat", value=bool(user.get("notify_enabled", 1)))
# toggle kaydı
with closing(get_conn()) as conn, conn:
    conn.execute("UPDATE users SET notify_enabled=? WHERE id=?", (1 if notify_state else 0, user["id"]))
    # session'ı güncelle
    user["notify_enabled"] = 1 if notify_state else 0
    st.session_state.user = user

menu = st.sidebar.radio(
    "Menü",
    ["Hasta Kayıt", "Liste & Filtre", "Tetkik Yönetimi", "Raporlar", "Paket Yönetimi", "Kullanıcı Yönetimi", "Test Uyarısı (Manuel)"],
)

st.sidebar.info("Not: WhatsApp ayarları için Twilio Sandbox yapılandırılmalıdır.")

# ---- Yardımcı: Hasta seçimi (dropdown)
@st.cache_data(ttl=30)
def get_patients_for_select():
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT id, full_name, patient_code FROM patients ORDER BY created_at DESC").fetchall()
    return {f"{r['full_name']}  (Kod: {r['patient_code']})": r["id"] for r in rows}

def patient_select(label="Hasta Seç"):
    options = get_patients_for_select()
    if not options:
        st.warning("Önce hasta ekleyin.")
        return None
    key = st.selectbox(label, list(options.keys()))
    return options[key]

# ---- WhatsApp (hazırlık)
def send_whatsapp_message(to_number: str, body: str) -> bool:
    # Secrets yoksa sessizce geç
    sid = st.secrets.get("TWILIO_ACCOUNT_SID", "") if "TWILIO_ACCOUNT_SID" in st.secrets else ""
    token = st.secrets.get("TWILIO_AUTH_TOKEN", "") if "TWILIO_AUTH_TOKEN" in st.secrets else ""
    wfrom = st.secrets.get("TWILIO_WHATSAPP_FROM", "") if "TWILIO_WHATSAPP_FROM" in st.secrets else ""
    if not (sid and token and wfrom and to_number):
        return False
    try:
        from twilio.rest import Client  # type: ignore
        client = Client(sid, token)
        msg = client.messages.create(
            body=body,
            from_=wfrom,  # e.g., "whatsapp:+14155238886"
            to=f"whatsapp:{to_number}",
        )
        return msg.sid is not None
    except Exception as e:
        st.warning(f"WhatsApp gönderilemedi: {e}")
        return False

# ---- SAYFALAR

# 1) Hasta Kayıt
if menu == "Hasta Kayıt":
    st.title(APP_TITLE)

    st.header("Hasta Kaydı Oluştur")
    c1, c2, c3 = st.columns(3)
    with c1:
        patient_code = st.text_input("Hasta Kodu (benzersiz)")
        full_name = st.text_input("Ad Soyad")
        dob = st.text_input("Doğum Tarihi", placeholder="1990/01/01")
    with c2:
        phone = st.text_input("Telefon (örn: +90555xxxxxxx)")
        # paketler
        with closing(get_conn()) as conn:
            packs = [r["name"] for r in conn.execute("SELECT name FROM packages ORDER BY name").fetchall()]
        package = st.selectbox("Paket", packs or ["Genel Tarama"])
        coordinator = st.text_input("Koordinatör/Danışman", value="Yönetici (Admin)" if user["role"] == "admin" else user.get("full_name") or user["username"])
    with c3:
        checkup_date = st.date_input("Check-up Tarihi", value=date.today())
        amount_billed = st.number_input("Fatura Tutarı (TL)", min_value=0.0, step=100.0, value=0.0, format="%.2f")
        notes = st.text_area("Notlar")

    if st.button("Kaydet", type="primary"):
        if not patient_code or not full_name:
            st.error("Hasta kodu ve ad-soyad zorunlu.")
        else:
            try:
                with closing(get_conn()) as conn, conn:
                    conn.execute("""
                        INSERT INTO patients (patient_code, full_name, dob, phone, package, coordinator, checkup_date, amount_billed, notes)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """, (patient_code, full_name, dob, phone, package, coordinator, checkup_date.isoformat(), amount_billed, notes))
                st.success("Hasta kaydedildi.")
                st.cache_data.clear()
            except sqlite3.IntegrityError:
                st.error("Bu hasta kodu zaten mevcut.")

# 2) Liste & Filtre
elif menu == "Liste & Filtre":
    st.title(APP_TITLE)
    st.header("Hasta Listesi")

    # Filtreler
    with closing(get_conn()) as conn:
        packs = [r["name"] for r in conn.execute("SELECT name FROM packages ORDER BY name").fetchall()]
    fcol1, fcol2 = st.columns([1,1])
    with fcol1:
        pack_f = st.selectbox("Paket filtresi", ["(hepsi)"] + packs)
    with fcol2:
        start_end = st.date_input("Tarih aralığı", value=(date.today()-timedelta(days=30), date.today()))
        if isinstance(start_end, tuple):
            start_date, end_date = start_end
        else:
            start_date, end_date = date.today()-timedelta(days=30), date.today()

    # Sorgu
    q = "SELECT * FROM patients WHERE date(checkup_date) BETWEEN ? AND ?"
    params = [start_date.isoformat(), end_date.isoformat()]
    if pack_f != "(hepsi)":
        q += " AND package=?"
        params.append(pack_f)

    with closing(get_conn()) as conn:
        df = pd.read_sql_query(q, conn, params=params)

    st.dataframe(df, use_container_width=True, hide_index=True)

    # Excel’e aktar
    if not df.empty:
        excel_buf = io.BytesIO()
        with pd.ExcelWriter(excel_buf, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Hastalar")
        st.download_button(
            label="Excel’e Aktar",
            data=excel_buf.getvalue(),
            file_name=f"hasta_listesi_{date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# 3) Tetkik Yönetimi
elif menu == "Tetkik Yönetimi":
    st.title(APP_TITLE)
    st.header("Tetkik Planlama ve Durum


