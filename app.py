import os, sqlite3, csv, io, threading, time, zipfile
from datetime import datetime, date, timedelta
from contextlib import closing
import streamlit as st

# ================== CONFIG ==================
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")

DB_PATH = "checkup.db"
TZ = "Europe/Istanbul"  # bilgilendirme amaçlı; sistem saatini kullanıyoruz

# ---- Ortam değişkenleri ----
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # +14155238886 veya whatsapp:+14155238886 (sandbox/prod)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

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
        # Personel (mesaj alıcıları)
        c.execute("""CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )""")

        # Hastalar
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

        # Tetkikler
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,   -- bekliyor | tamamlandi
            updated_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )""")

        # Mesaj logları
        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,   -- ok | hata
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")

        # Basit ayarlar (tema, özet saati, şablonlar...)
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

# Twilio from

def _wa_from() -> str:
    f = (os.environ.get("TWILIO_WHATSAPP_FROM", TWILIO_WHATSAPP_FROM) or "").strip()
    if f.startswith("whatsapp:"):
        return f
    f = normalize_phone(f) if f else ""
    return f"whatsapp:{f}" if f else ""

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

# ---- Şablonlar ----
DEFAULT_TEMPLATES = {
    "tmpl_auto_update": (
        "📌 Tetkik Güncellemesi\n"
        "Hasta: {hasta_ad} {hasta_soyad}\n"
        "Tamamlananlar: {tamamlanan}\n"
        "Kalanlar: {kalan}"
    ),
    "tmpl_alarm_reminder": (
        "📅 Hatırlatma:\n"
        "{hasta_ad} {hasta_soyad}'ın 10 dakika sonra {bolum} randevusu var.\n"
        "Lütfen bölüm ile teyit sağlayın ve hastaya eşlik edin."
    ),
    "tmpl_daily_summary": (
        "📝 Günlük Özet – {tarih}\n"
        "Toplam Hasta: {toplam_hasta}\n"
        "Tamamlanan Tetkik: {toplam_tamam}\n"
        "Bekleyen Tetkik: {toplam_bekleyen}"
    )
}

for k, v in DEFAULT_TEMPLATES.items():
    if get_setting(k, "") == "":
        set_setting(k, v)

# ---- Yardımcı: toplu alıcıya gönder ----
def broadcast(body:str):
    for staff in list_personnel(active_only=True):
        ok, info = send_whatsapp_message(staff[2], body)
        log_message(staff[0], body, ok, info)
        time.sleep(0.2)  # hız limiti

# ---- Otomatik tetkik güncellemesi ----
def auto_message_for_patient(pid:int):
    trs = list_patient_tests(pid)
    done = [t[4] for t in trs if t[5] == "tamamlandi"]
    remain = [t[4] for t in trs if t[5] == "bekliyor"]
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT first_name,last_name,department FROM patients WHERE id=?", (pid,))
        row = c.fetchone()
    if not row: return
    fn, ln, dept = row
    tpl = get_setting("tmpl_auto_update", DEFAULT_TEMPLATES["tmpl_auto_update"])
    body = tpl.format(
        hasta_ad=fn,
        hasta_soyad=ln,
        tamamlanan=", ".join(done) if done else "-",
        kalan=", ".join(remain) if remain else "-",
        bolum=dept or "İlgili bölüm"
    )
    broadcast(body)

# ================== ALARM / GÜNLÜK ÖZET ZAMANLAYICILAR ==================

def _hhmm_now():
    return datetime.now().strftime("%H:%M")

# 10 dk önce alarm (1 dakikalık pencere)
def check_alarms_once():
    today_iso = datetime.now().strftime("%Y-%m-%d")
    target = (datetime.now() + timedelta(minutes=10)).strftime("%H:%M")
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("""SELECT first_name,last_name,department,visit_time
                     FROM patients
                     WHERE visit_date=? AND visit_time=?""", (today_iso, target))
        matches = c.fetchall()
    if matches:
        tpl = get_setting("tmpl_alarm_reminder", DEFAULT_TEMPLATES["tmpl_alarm_reminder"])
        for (fn, ln, dept, _vtime) in matches:
            body = tpl.format(hasta_ad=fn, hasta_soyad=ln, bolum=dept or "İlgili bölüm")
            broadcast(body)

# Günlük özet derleme/gönderme

def compute_daily_summary(date_iso:str):
    # metrikleri hesapla
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT COUNT(*) FROM patients WHERE visit_date=?", (date_iso,))
        toplam_hasta = c.fetchone()[0]
        c.execute("""SELECT COUNT(*) FROM patient_tests t
                     JOIN patients p ON p.id=t.patient_id
                     WHERE p.visit_date=? AND t.status='tamamlandi'""", (date_iso,))
        toplam_tamam = c.fetchone()[0]
        c.execute("""SELECT COUNT(*) FROM patient_tests t
                     JOIN patients p ON p.id=t.patient_id
                     WHERE p.visit_date=? AND t.status='bekliyor'""", (date_iso,))
        toplam_bekleyen = c.fetchone()[0]
    return {
        "toplam_hasta": toplam_hasta,
        "toplam_tamam": toplam_tamam,
        "toplam_bekleyen": toplam_bekleyen,
    }

def build_daily_summary_text(date_iso:str):
    metrik = compute_daily_summary(date_iso)
    tpl = get_setting("tmpl_daily_summary", DEFAULT_TEMPLATES["tmpl_daily_summary"])
    tarih = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    return tpl.format(tarih=tarih, **metrik)

def send_daily_summary_now(date_iso:str):
    body = build_daily_summary_text(date_iso)
    broadcast(body)

# Zamanlayıcı döngüsü

def scheduler_loop():
    last_alarm_tick = ""
    while True:
        hhmm = _hhmm_now()
        # 1) Alarm kontrolü (her dakika bir kez)
        if hhmm != last_alarm_tick:
            check_alarms_once()
            last_alarm_tick = hhmm

        # 2) Günlük özet planlı gönderim
        enabled = get_setting("daily_summary_enabled", "1") == "1"
        target_hhmm = get_setting("daily_summary_time", "18:00")
        last_sent = get_setting("daily_summary_last_sent", "")
        today_iso = datetime.now().strftime("%Y-%m-%d")
        if enabled and hhmm == target_hhmm and last_sent != today_iso:
            send_daily_summary_now(today_iso)
            set_setting("daily_summary_last_sent", today_iso)

        time.sleep(5)  # hafif ve sürekli döngü

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

# mini animasyon/hover + Safari küçük düzeltmeler
st.markdown("""
<style>
.main > div { animation: fadeIn .35s ease-in-out; }
@keyframes fadeIn { from{opacity:0; transform: translateY(6px);} to{opacity:1; transform:none;} }
button[kind="primary"]:hover { transform: scale(1.03); transition: .15s; }
/* Safari için: select/dropdown hizalama */
section[data-testid="stSidebar"] * { -webkit-font-smoothing: antialiased; }
</style>
""", unsafe_allow_html=True)

# ================== AUTH ==================
if "auth" not in st.session_state:
    st.session_state.auth = {"logged_in": False, "is_admin": False, "username": None}

# Login ekranı
if not st.session_state.auth["logged_in"]:
    st.title("✅ Check-up Takip Sistemi")
    st.caption("Giriş yapın – bugün için yalnızca admin kullanıcı gereklidir.")
    with st.form("login_form"):
        u = st.text_input("Kullanıcı adı", value="")
        p = st.text_input("Şifre", type="password", value="")
        submit = st.form_submit_button("Giriş")
    if submit:
        if u == ADMIN_USER and p == ADMIN_PASS:
            st.session_state.auth = {"logged_in": True, "is_admin": True, "username": u}
            st.success("Giriş başarılı.")
            st.rerun()
        else:
            st.error("Hatalı kullanıcı adı veya şifre.")
    st.stop()

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
    if st.button("Çıkış Yap"):
        st.session_state.auth = {"logged_in": False, "is_admin": False, "username": None}
        st.rerun()

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
        # --- Günlük Özet ---
        st.markdown("#### 🕕 Günlük Özet Planı")
        en = st.toggle("Günlük özet otomatik gönderilsin", value=(get_setting("daily_summary_enabled", "1") == "1"))
        set_setting("daily_summary_enabled", "1" if en else "0")
        hh = st.selectbox("Saat", [f"{h:02d}" for h in range(0,24)], index=int(get_setting("daily_summary_time","18:00").split(":")[0]))
        mm = st.selectbox("Dakika", [f"{m:02d}" for m in range(0,60,5)], index=int(get_setting("daily_summary_time","18:00").split(":")[1])//5)
        set_setting("daily_summary_time", f"{hh}:{mm}")
        st.caption(f"Son gönderim: {get_setting('daily_summary_last_sent','-')}")

        st.divider()
        # --- Mesaj Şablonları ---
        st.markdown("#### ✉️ Mesaj Şablonları")
        for key, title in [
            ("tmpl_auto_update", "Tetkik Güncellemesi"),
            ("tmpl_alarm_reminder", "Randevu Hatırlatma (10 dk önce)"),
            ("tmpl_daily_summary", "Günlük Özet")
        ]:
            val = st.text_area(title, value=get_setting(key, DEFAULT_TEMPLATES[key]), height=120, key=f"ta_{key}")
            if st.button(f"Kaydet – {title}", key=f"save_{key}"):
                set_setting(key, val)
                st.success("Kaydedildi.")

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

        st.markdown("#### ⬆️ CSV'den Toplu Personel Ekle")
        st.caption("Sütunlar: name, phone, active (1/0). Başlık zorunlu.")
        f = st.file_uploader("personnel.csv yükle", type=["csv"], key="up_staff")
        if f is not None:
            try:
                txt = f.read().decode("utf-8")
                rdr = csv.DictReader(io.StringIO(txt))
                cnt_ok, cnt_err = 0, 0
                for row in rdr:
                    try:
                        nm = row.get("name", "")
                        ph = row.get("phone", "")
                        ac = 1 if str(row.get("active", "1")).strip() in ("1", "true", "True") else 0
                        upsert_personnel(nm, ph, ac)
                        cnt_ok += 1
                    except Exception:
                        cnt_err += 1
                st.success(f"Yüklendi: {cnt_ok} kayıt. Hatalı: {cnt_err}.")
                st.rerun()
            except Exception as e:
                st.error(f"CSV okunamadı: {e}")

        st.divider()
        st.markdown("#### 🧪 WhatsApp Testi")
        test_to = st.text_input("Test alıcı (+90...)", key="wa_test_to")
        if st.button("Test mesajı gönder", key="wa_test_btn"):
            ok, info = send_whatsapp_message(test_to, "Test: Check-up WhatsApp bağlantısı çalışıyor.")
            if ok:
                st.success(f"OK: {info}")
            else:
                st.error(f"Hata: {info}")

# =============== ZAMANLAYICI BAŞLAT ===============
if "bg_threads_started" not in st.session_state:
    threading.Thread(target=scheduler_loop, daemon=True).start()
    st.session_state.bg_threads_started = True

# ================== MAIN ==================
st.title("✅ Check-up Takip Sistemi")

# Sekmeler
_tab_hasta, _tab_tetkik, _tab_ozet, _tab_yedek = st.tabs(
    ["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti", "💾 Yedek"]
)

# -------- 🧑‍⚕️ Hastalar --------
with _tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts = list_patients(visit_date_iso=sel_iso)
    st.dataframe([{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Alarm Saati":p[6] or "-"} for p in pts],
                 use_container_width=True, height=360)

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
with _tab_tetkik:
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
with _tab_ozet:
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
        st.dataframe(rows, use_container_width=True, height=380)

        st.markdown("### ✉️ Mesaj Olarak Gönder")
        colx, coly = st.columns([1,3])
        if colx.button("Bugünün özetini gönder", type="primary"):
            send_daily_summary_now(sel_iso)
            st.success("Günlük özet WhatsApp'tan gönderildi.")
        preview = build_daily_summary_text(sel_iso)
        coly.text_area("Önizleme", value=preview, height=140)

# -------- 💾 Yedek --------
with _tab_yedek:
    st.subheader("Yedek / Dışa Aktar (CSV & ZIP)")

    def _csv(query:str):
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute(query)
            rows = c.fetchall()
            headers = [d[0] for d in c.description]
        buf = io.StringIO()
        w = csv.writer(buf); w.writerow(headers); w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    c1, c2 = st.columns(2)
    with c1:
        st.download_button("İndir – patients.csv", _csv("SELECT * FROM patients"),
                           "patients.csv", "text/csv", key="dl_pat_btn")
        st.download_button("İndir – patient_tests.csv", _csv("SELECT * FROM patient_tests"),
                           "patient_tests.csv", "text/csv", key="dl_tests_btn")
    with c2:
        st.download_button("İndir – personnel.csv", _csv("SELECT * FROM personnel"),
                           "personnel.csv", "text/csv", key="dl_staff_btn")
        st.download_button("İndir – msg_logs.csv", _csv("SELECT * FROM msg_logs"),
                           "msg_logs.csv", "text/csv", key="dl_logs_btn")

    st.markdown("#### 📦 Tüm tabloları tek tıkla (ZIP)")
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("patients.csv", _csv("SELECT * FROM patients"))
        zf.writestr("patient_tests.csv", _csv("SELECT * FROM patient_tests"))
        zf.writestr("personnel.csv", _csv("SELECT * FROM personnel"))
        zf.writestr("msg_logs.csv", _csv("SELECT * FROM msg_logs"))
    zip_bytes.seek(0)
    st.download_button("İndir – checkup_backup.zip", zip_bytes, file_name="checkup_backup.zip", mime="application/zip")
