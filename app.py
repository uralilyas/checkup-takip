# app.py — Check-up Takip Sistemi (düzeltilmiş sade sürüm)
# Özellikler:
# - Giriş (admin: admin / Edam456+), kullanıcı/rol/telefon, bildirim açık/kapalı
# - Hasta kaydı + liste/filtre + Excel’e aktar
# - Paket yönetimi
# - Tetkik planlama: "Hasta Seç" dropdown, tarih/saat seçici, +10dk/+30dk/+1saat
# - Raporlar: tarih aralığına duyarlı metrikler
# - WhatsApp entegrasyonuna hazır (Twilio secrets varsa çalışır; yoksa sessiz geçer)

import os, io, sqlite3, hashlib
from contextlib import closing
from datetime import datetime, date, time, timedelta

import streamlit as st
import pandas as pd

APP_TITLE = "Check-up Takip Sistemi"
DB_PATH = "checkup_tracker.db"

# ---------------------- Yardımcılar ----------------------
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def conn_open():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with closing(conn_open()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pass_hash TEXT NOT NULL,
            full_name TEXT,
            phone TEXT,
            role TEXT DEFAULT 'staff',         -- admin | manager | staff
            notify_enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS packages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS patients(
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
        conn.execute("""CREATE TABLE IF NOT EXISTS tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            planned_at TEXT,
            status TEXT DEFAULT 'Planlandı',   -- Planlandı | Devam | Sonuç | Tamamlandı | İptal
            completed_at TEXT,
            notified INTEGER DEFAULT 0,
            comments TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        )""")

        # Admin oluştur
        admin_u = os.getenv("ADMIN_USERNAME", st.secrets.get("ADMIN_USERNAME", "admin"))
        admin_p = os.getenv("ADMIN_PASSWORD", st.secrets.get("ADMIN_PASSWORD", "admin"))
        if conn.execute("SELECT 1 FROM users WHERE username=?", (admin_u,)).fetchone() is None:
            conn.execute("INSERT INTO users(username,pass_hash,full_name,role,notify_enabled) VALUES(?,?,?,?,1)",
                         (admin_u, sha256(admin_p), "Yönetici (Admin)", "admin"))

        # Örnek paketler
        packs = [("Genel Tarama","Temel laboratuvar + USG"),
                 ("VIP","Geniş kapsamlı VIP paket"),
                 ("Kadın Sağlığı","Kadın sağlığı odaklı"),
                 ("Premium Kardiyoloji","EKO/ETT/EKG + muayene"),
                 ("Standart","Temel check-up")]
        for n,d in packs:
            try:
                conn.execute("INSERT INTO packages(name,description) VALUES(?,?)",(n,d))
            except sqlite3.IntegrityError:
                pass

db_init()

# ---------------------- Giriş / Oturum ----------------------
st.set_page_config(page_title=APP_TITLE, page_icon="🏥", layout="wide")

if "user" not in st.session_state:
    st.session_state.user = None

def login_view():
    st.title(APP_TITLE)
    st.subheader("Giriş Yap")
    u = st.text_input("Kullanıcı adı", value=os.getenv("ADMIN_USERNAME", st.secrets.get("ADMIN_USERNAME","admin")))
    p = st.text_input("Şifre", type="password")
    if st.button("Giriş", type="primary"):
        with closing(conn_open()) as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row and row["pass_hash"] == sha256(p):
            st.session_state.user = dict(row)
            st.success("Giriş başarılı.")
            st.rerun()
        else:
            st.error("Kullanıcı adı veya şifre hatalı.")

def require_login():
    if not st.session_state.user:
        login_view()
        st.stop()

require_login()
user = st.session_state.user

# ---------------------- Sidebar ----------------------
st.sidebar.markdown(f"**{APP_TITLE}**")
st.sidebar.caption(f"Giriş yapan: **{user['username']}** ({user['role']})")

# Bildirim anahtarı (kayıt)
notify_toggle = st.sidebar.toggle("Bildirimleri Aç/Kapat", value=bool(user.get("notify_enabled",1)))
with closing(conn_open()) as conn, conn:
    conn.execute("UPDATE users SET notify_enabled=? WHERE id=?",(1 if notify_toggle else 0, user["id"]))
user["notify_enabled"] = 1 if notify_toggle else 0
st.session_state.user = user

menu = st.sidebar.radio("Menü", [
    "Hasta Kayıt", "Liste & Filtre", "Tetkik Yönetimi",
    "Raporlar", "Paket Yönetimi", "Kullanıcı Yönetimi", "Test Uyarısı (Manuel)"
])

st.sidebar.info("WhatsApp için Twilio Sandbox bilgilerini Secrets’a ekleyin.")

# ---------------------- Ortak yardımcılar ----------------------
@st.cache_data(ttl=30)
def patients_for_select():
    with closing(conn_open()) as conn:
        rows = conn.execute("SELECT id, full_name, patient_code FROM patients ORDER BY created_at DESC").fetchall()
    return {f"{r['full_name']}  (Kod: {r['patient_code']})": r["id"] for r in rows}

def patient_select(label="Hasta Seç"):
    opts = patients_for_select()
    if not opts:
        st.warning("Önce hasta ekleyin.")
        return None
    key = st.selectbox(label, list(opts.keys()))
    return opts[key]

def send_whatsapp(to_number: str, body: str) -> bool:
    sid = st.secrets.get("TWILIO_ACCOUNT_SID","") if "TWILIO_ACCOUNT_SID" in st.secrets else ""
    token = st.secrets.get("TWILIO_AUTH_TOKEN","") if "TWILIO_AUTH_TOKEN" in st.secrets else ""
    wfrom = st.secrets.get("TWILIO_WHATSAPP_FROM","") if "TWILIO_WHATSAPP_FROM" in st.secrets else ""
    if not (sid and token and wfrom and to_number):
        return False
    try:
        from twilio.rest import Client  # type: ignore
        client = Client(sid, token)
        msg = client.messages.create(body=body, from_=wfrom, to=f"whatsapp:{to_number}")
        return bool(msg.sid)
    except Exception:
        return False

# ---------------------- Sayfalar ----------------------
if menu == "Hasta Kayıt":
    st.title(APP_TITLE)
    st.header("Hasta Kaydı Oluştur")

    col1, col2, col3 = st.columns(3)
    with col1:
        patient_code = st.text_input("Hasta Kodu (benzersiz)*")
        full_name    = st.text_input("Ad Soyad*")
        dob          = st.text_input("Doğum Tarihi", placeholder="1990/01/01")
    with col2:
        phone = st.text_input("Telefon (örn: +90555xxxxxxx)")
        with closing(conn_open()) as conn:
            packs = [r["name"] for r in conn.execute("SELECT name FROM packages ORDER BY name").fetchall()]
        package     = st.selectbox("Paket", packs or ["Genel Tarama"])
        coordinator = st.text_input("Koordinatör/Danışman",
                                    value="Yönetici (Admin)" if user["role"]=="admin" else (user.get("full_name") or user["username"]))
    with col3:
        checkup_date = st.date_input("Check-up Tarihi", value=date.today())
        amount_billed = st.number_input("Fatura Tutarı (TL)", min_value=0.0, step=100.0, value=0.0, format="%.2f")
        notes = st.text_area("Notlar")

    if st.button("Kaydet", type="primary"):
        if not patient_code or not full_name:
            st.error("Hasta kodu ve ad soyad zorunlu.")
        else:
            try:
                with closing(conn_open()) as conn, conn:
                    conn.execute("""INSERT INTO patients
                        (patient_code, full_name, dob, phone, package, coordinator, checkup_date, amount_billed, notes)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        (patient_code, full_name, dob, phone, package, coordinator, checkup_date.isoformat(), amount_billed, notes))
                st.success("Hasta kaydedildi.")
                st.cache_data.clear()
            except sqlite3.IntegrityError:
                st.error("Bu hasta kodu zaten mevcut.")

elif menu == "Liste & Filtre":
    st.title(APP_TITLE)
    st.header("Hasta Listesi")

    with closing(conn_open()) as conn:
        packs = [r["name"] for r in conn.execute("SELECT name FROM packages ORDER BY name").fetchall()]
    f1, f2 = st.columns([1,1])
    with f1:
        pack_f = st.selectbox("Paket filtresi", ["(hepsi)"] + packs)
    with f2:
        dr = st.date_input("Tarih aralığı", value=(date.today()-timedelta(days=30), date.today()))
        if isinstance(dr, tuple):
            start_date, end_date = dr
        else:
            start_date, end_date = date.today()-timedelta(days=30), date.today()

    q = "SELECT * FROM patients WHERE date(checkup_date) BETWEEN ? AND ?"
    params = [start_date.isoformat(), end_date.isoformat()]
    if pack_f != "(hepsi)":
        q += " AND package=?"
        params.append(pack_f)

    with closing(conn_open()) as conn:
        df = pd.read_sql_query(q, conn, params=params)

    st.dataframe(df, use_container_width=True, hide_index=True)

    if not df.empty:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
            df.to_excel(w, index=False, sheet_name="Hastalar")
        st.download_button("Excel’e Aktar", buf.getvalue(),
                           file_name=f"hasta_listesi_{date.today()}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

elif menu == "Tetkik Yönetimi":
    st.title(APP_TITLE)
    st.header("Tetkik Planlama ve Durum Yönetimi")

    pid = patient_select("Hasta Seç")
    if not pid:
        st.stop()

    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        test_name = st.text_input("Tetkik Adı", value="Kardiyoloji Muayenesi")
    with c2:
        d = st.date_input("Planlanan Tarih", value=date.today())
    with c3:
        default_t = (datetime.now()+timedelta(minutes=15)).time().replace(second=0, microsecond=0)
        t = st.time_input("Planlanan Saat", value=default_t, step=900)

    # kısayollar
    k1, k2, k3 = st.columns(3)
    if k1.button("+10 dk"):
        t = (datetime.combine(date.today(), t) + timedelta(minutes=10)).time()
    if k2.button("+30 dk"):
        t = (datetime.combine(date.today(), t) + timedelta(minutes=30)).time()
    if k3.button("+1 saat"):
        t = (datetime.combine(date.today(), t) + timedelta(hours=1)).time()

    planned_dt = datetime.combine(d, t).strftime("%Y-%m-%d %H:%M")

    add, _ = st.columns([1,6])
    if add.button("Tetkik Ekle", type="primary"):
        with closing(conn_open()) as conn, conn:
            conn.execute("""INSERT INTO tests (patient_id, test_name, planned_at, status, notified)
                            VALUES (?,?,?,?,0)""", (pid, test_name, planned_dt, "Planlandı"))
        st.success("Tetkik eklendi.")

    # liste
    with closing(conn_open()) as conn:
        tdf = pd.read_sql_query("""SELECT id, patient_id, test_name, planned_at, status, completed_at, notified
                                   FROM tests WHERE patient_id=? ORDER BY planned_at""", conn, params=[pid])
    st.subheader("Tetkikler")
    st.dataframe(tdf, use_container_width=True, hide_index=True)

    # durum güncelle
    st.markdown("**Durum Güncelle / Tamamla**")
    if not tdf.empty:
        row_id = st.selectbox("Tetkik ID", list(tdf["id"]))
        new_status = st.selectbox("Yeni Durum", ["Planlandı","Devam","Sonuç","Tamamlandı","İptal"])
        if st.button("Değişiklikleri Kaydet"):
            completed = datetime.now().strftime("%Y-%m-%d %H:%M") if new_status=="Tamamlandı" else None
            with closing(conn_open()) as conn, conn:
                conn.execute("UPDATE tests SET status=?, completed_at=? WHERE id=?", (new_status, completed, row_id))
            st.success("Güncellendi.")

elif menu == "Raporlar":
    st.title(APP_TITLE)
    st.header("Raporlar")

    dr = st.date_input("Tarih aralığı", value=(date.today()-timedelta(days=30), date.today()))
    if isinstance(dr, tuple):
        sdate, edate = dr
    else:
        sdate, edate = date.today()-timedelta(days=30), date.today()

    with closing(conn_open()) as conn:
        pdf = pd.read_sql_query("SELECT * FROM patients WHERE date(checkup_date) BETWEEN ? AND ?",
                                conn, params=[sdate.isoformat(), edate.isoformat()])
        tdf = pd.read_sql_query("""SELECT * FROM tests
                                   WHERE datetime(planned_at) BETWEEN ? AND ?""",
                                conn, params=[f"{sdate} 00:00", f"{edate} 23:59"])

    colA, colB, colC = st.columns(3)
    colA.metric("Seçili Aralıkta Hasta", len(pdf))
    colB.metric("Toplam Fatura (TL)", f"{pdf['amount_billed'].fillna(0).sum():,.2f}".replace(",", "."))

    def to_dt(x):
        try: return datetime.fromisoformat(x)
        except: return None
    tdf["planned_dt"]   = tdf["planned_at"].apply(to_dt)
    tdf["completed_dt"] = tdf["completed_at"].apply(to_dt)
    done = tdf.dropna(subset=["planned_dt","completed_dt"])
    if not done.empty:
        avg = (done["completed_dt"] - done["planned_dt"]).mean()
        colC.metric("Ortalama Tamamlama", f"{avg.total_seconds()/3600:.1f} saat")
    else:
        colC.metric("Ortalama Tamamlama", "veri yok")

    st.subheader("Paket Kullanım Dağılımı")
    if not pdf.empty:
        pack_counts = pdf["package"].value_counts().reset_index()
        pack_counts.columns = ["Paket", "Adet"]
        st.dataframe(pack_counts, use_container_width=True, hide_index=True)
    else:
        st.info("Seçili aralıkta veri yok.")

elif menu == "Paket Yönetimi":
    st.title(APP_TITLE)
    if user["role"] != "admin":
        st.warning("Bu sayfaya sadece admin erişir.")
        st.stop()

    st.header("Paket Ekle / Düzenle")
    name = st.text_input("Paket adı")
    desc = st.text_area("Açıklama")
    if st.button("Ekle", type="primary"):
        if not name:
            st.error("Paket adı boş olamaz.")
        else:
            with closing(conn_open()) as conn, conn:
                try:
                    conn.execute("INSERT INTO packages(name,description) VALUES(?,?)",(name,desc))
                    st.success("Paket eklendi.")
                except sqlite3.IntegrityError:
                    st.error("Bu paket zaten var.")

    with closing(conn_open()) as conn:
        pdf = pd.read_sql_query("SELECT * FROM packages ORDER BY name", conn)
    st.subheader("Paketler")
    st.dataframe(pdf, use_container_width=True, hide_index=True)

elif menu == "Kullanıcı Yönetimi":
    st.title(APP_TITLE)
    if user["role"] != "admin":
        st.warning("Bu sayfaya sadece admin erişir.")
        st.stop()

    st.header("Yeni Kullanıcı Ekle")
    c1, c2, c3 = st.columns(3)
    with c1:
        u = st.text_input("Kullanıcı adı")
        f = st.text_input("Ad Soyad")
    with c2:
        p = st.text_input("Şifre", type="password")
        phone = st.text_input("Telefon (örn: +90555xxxxxxx)")
    with c3:
        role = st.selectbox("Rol", ["manager","staff"])
        notify = st.toggle("Bildirimler açık", value=True)

    if st.button("Kullanıcıyı Kaydet", type="primary"):
        if not u or not p:
            st.error("Kullanıcı adı ve şifre zorunlu.")
        else:
            with closing(conn_open()) as conn, conn:
                try:
                    conn.execute("""INSERT INTO users(username, pass_hash, full_name, phone, role, notify_enabled)
                                    VALUES (?,?,?,?,?,?)""",
                                 (u, sha256(p), f, phone, role, 1 if notify else 0))
                    st.success("Kullanıcı eklendi.")
                except sqlite3.IntegrityError:
                    st.error("Bu kullanıcı adı zaten var.")

    with closing(conn_open()) as conn:
        udf = pd.read_sql_query("SELECT id,username,full_name,phone,role,notify_enabled,created_at FROM users ORDER BY id DESC", conn)
    st.subheader("Kullanıcılar")
    st.dataframe(udf, use_container_width=True, hide_index=True)

elif menu == "Test Uyarısı (Manuel)":
    st.title(APP_TITLE)
    st.header("Deneme WhatsApp Uyarısı")
    st.caption("Twilio Sandbox kurulumu yaptıysan buradan deneme mesajı gönderebilirsin.")

    to = st.text_input("Kime (örn: +90555xxxxxxx)", value=(user.get("phone") or ""))
    body = st.text_area("Mesaj", value="İlyas Ural isimli hastamızın Kardiyoloji muayenesi 10 dk sonra. Lütfen bölümü arayarak teyit alınız ve hastaya eşlik ediniz.")
    if st.button("Mesaj Gönder"):
        if send_whatsapp(to, body):
            st.success("Gönderildi.")
        else:
            st.warning("Gönderilemedi. Twilio bilgilerini Secrets’a eklediğinden ve numaranın Sandbox’a kayıtlı olduğundan emin ol.")
