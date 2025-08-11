# app.py
import os, sqlite3, csv, io, zipfile
from datetime import datetime, date, time as dtime, timedelta
from contextlib import closing
from urllib.parse import quote_plus

import streamlit as st

# ================== CONFIG ==================
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
DB_PATH = "checkup.db"

# ================== DB ==================
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def column_exists(conn, table, column) -> bool:
    with closing(conn.cursor()) as c:
        c.execute(f"PRAGMA table_info({table})")
        return any(r[1] == column for r in c.fetchall())

def init_db():
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY, val TEXT)""")

        c.execute("""CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS patients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name  TEXT NOT NULL,
            age INTEGER, gender TEXT,
            visit_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        if not column_exists(conn, "patients", "department"):
            c.execute("ALTER TABLE patients ADD COLUMN department TEXT")
            c.execute("UPDATE patients SET department='Genel' WHERE department IS NULL")
        if not column_exists(conn, "patients", "visit_time"):
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")  # 'HH:MM' veya NULL

        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'bekliyor', -- bekliyor|tamamlandi
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )""")
init_db()

# ================== UTILS ==================
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def to_iso(d:date) -> str: return d.strftime("%Y-%m-%d")
def to_display(d:date) -> str: return d.strftime("%d/%m/%Y")

def normalize_phone(p:str)->str:
    p = (p or "").strip().replace(" ", "").replace("-", "")
    if p and not p.startswith("+"):
        p = "+" + p
    return p

# ---- Settings helpers ----
def get_setting(key:str, default:str=""):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT val FROM app_settings WHERE key=?", (key,))
        r = c.fetchone()
    return r[0] if r else default
def set_setting(key:str, val:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO app_settings(key,val) VALUES(?,?)
                     ON CONFLICT(key) DO UPDATE SET val=excluded.val""", (key,val))

# ================== PERSONNEL ==================
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q = "SELECT id,name,phone,active FROM personnel"
        if active_only: q += " WHERE active=1"
        q += " ORDER BY name"
        c.execute(q); return c.fetchall()

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
        c.execute("INSERT INTO personnel(name,phone,active) VALUES(?,?,?)",
                  (name.strip(), phone, active))
        return c.lastrowid

def set_personnel_active(pid:int, active:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE personnel SET active=? WHERE id=?", (active, pid))
def delete_personnel(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM personnel WHERE id=?", (pid,))

# ================== PATIENTS / TESTS ==================
def add_patient(fn:str, ln:str, age:int, gender:str, visit_date_iso:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patients(first_name,last_name,age,gender,visit_date,department,visit_time)
                     VALUES(?,?,?,?,?,?,?)""",
                  (fn.strip(), ln.strip(), age, gender, visit_date_iso, "Genel", None))
def delete_patient(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE patient_id=?", (pid,))
        c.execute("DELETE FROM patients WHERE id=?", (pid,))
def set_patient_alarm_time(pid:int, hhmm:str|None):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patients SET visit_time=? WHERE id=?", (hhmm, pid))
def list_patients(visit_date_iso:str|None=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date_iso:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients WHERE visit_date=? ORDER BY last_name,first_name""",(visit_date_iso,))
        else:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients ORDER BY visit_date DESC,last_name""")
        return c.fetchall()

def add_patient_test(patient_id:int, test_name:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patient_tests(patient_id,test_name,status)
                     VALUES(?,?, 'bekliyor')""",(patient_id,test_name.strip()))
def list_patient_tests(patient_id:int):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("""SELECT id,patient_id,test_name,status,updated_at
                     FROM patient_tests WHERE patient_id=? ORDER BY updated_at DESC""",(patient_id,))
        return c.fetchall()
def update_patient_test_status(test_id:int, new_status:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patient_tests SET status=?, updated_at=? WHERE id=?",
                  (new_status, now_str(), test_id))
def delete_patient_test(test_id:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE id=?", (test_id,))

# ================== ICS & WHATSAPP DEEPLINK ==================
def build_ics(patient_name:str, visit_date_iso:str, hhmm:str,
              title_prefix:str="Check-up Randevu", duration_min:int=30,
              remind_min:int=10, location:str="Klinik")->bytes:
    """Saat verilmişse 10 dk önce uyarılı .ics üretir."""
    dt = datetime.strptime(f"{visit_date_iso} {hhmm}", "%Y-%m-%d %H:%M")
    dt_end = dt + timedelta(minutes=duration_min)
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dtstart = dt.strftime("%Y%m%dT%H%M%S")
    dtend   = dt_end.strftime("%Y%m%dT%H%M%S")

    uid = f"{abs(hash((patient_name, visit_date_iso, hhmm, dtstamp)))}@checkup"
    summary = f"{title_prefix} – {patient_name}"
    desc = f"{patient_name} randevusu. Hatırlatıcı: {remind_min} dk önce."
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//checkup//streamlit//TR
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
DTSTART:{dtstart}
DTEND:{dtend}
SUMMARY:{summary}
LOCATION:{location}
DESCRIPTION:{desc}
BEGIN:VALARM
TRIGGER:-PT{remind_min}M
ACTION:DISPLAY
DESCRIPTION:Hatırlatma
END:VALARM
END:VEVENT
END:VCALENDAR
"""
    return ics.encode("utf-8")

def make_whatsapp_link(phone:str, text:str)->str:
    """Ücretsiz manuel gönderim: WhatsApp’ı hazır metinle açar."""
    digits = normalize_phone(phone).replace("+","")
    return f"https://wa.me/{digits}?text={quote_plus(text)}"

# ================== THEME / POLISH ==================
def apply_theme(theme_name: str):
    THEMES = {
        "Sistem (varsayılan)": "",
        "Açık": """
        <style>
        body, .stApp { background:#f7f7f9!important; }
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
    if css: st.markdown(css, unsafe_allow_html=True)

st.markdown("""
<style>
.main > div { animation: fadeIn .35s ease-in-out; }
@keyframes fadeIn { from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;} }
</style>
""", unsafe_allow_html=True)

# ================== AUTH (bugün herkes admin) ==================
if "auth" not in st.session_state:
    st.session_state.auth = {"logged_in": True, "is_admin": True, "username": "admin"}

# ================== THEME APPLY ==================
apply_theme(get_setting("theme", "Sistem (varsayılan)"))

# ================== SIDEBAR ==================
picked_date = st.sidebar.date_input("📅 Tarih seç", value=date.today(), key="dt_pick")
sel_iso = to_iso(picked_date); sel_disp = to_display(picked_date)

with st.sidebar:
    st.divider()
    with st.expander("⚙️ Ayarlar", expanded=False):
        st.markdown("#### 🎨 Tema")
        themes = ["Sistem (varsayılan)", "Açık", "Klinik (mint)", "Yüksek Kontrast"]
        cur = get_setting("theme", "Sistem (varsayılan)")
        new_t = st.selectbox("Tema seç", themes, index=themes.index(cur))
        if st.button("Temayı Uygula"):
            set_setting("theme", new_t); st.rerun()

        st.divider()
        st.markdown("#### 👥 Kişiler (WhatsApp için)")
        people = list_personnel(active_only=False)
        if people:
            for pid, name, phone, active in people:
                c1, c2, c3 = st.columns([3,1,1])
                c1.caption(f"**{name}** — {phone}")
                tg = c2.toggle("Aktif", value=bool(active), key=f"act_{pid}")
                if tg != bool(active):
                    set_personnel_active(pid, int(tg)); st.rerun()
                if c3.button("Sil", key=f"del_{pid}"):
                    delete_personnel(pid); st.rerun()
        else:
            st.info("Kayıtlı kişi yok.")

        st.markdown("#### ➕ Kişi Ekle")
        with st.form("frm_add_staff", clear_on_submit=True):
            nm = st.text_input("Ad / Not")
            ph = st.text_input("Telefon (+90...)")
            act = st.checkbox("Aktif", True)
            if st.form_submit_button("Ekle"):
                try:
                    upsert_personnel(nm, ph, 1 if act else 0)
                    st.success("Eklendi."); st.rerun()
                except Exception as e:
                    st.error(f"Hata: {e}")

# ================== MAIN ==================
st.title("✅ Check-up Takip Sistemi")
tab_hasta, tab_tetkik, tab_ozet, tab_yedek = st.tabs(
    ["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti", "💾 Yedek"]
)

# ---- Hastalar
with tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts = list_patients(sel_iso)
    st.dataframe([{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Alarm Saati":p[6] or "-"} for p in pts],
                 use_container_width=True)

    st.markdown("### ➕ Hasta Ekle")
    with st.form("frm_add_patient", clear_on_submit=True):
        c1,c2,c3 = st.columns([2,2,1])
        fn = c1.text_input("Ad")
        ln = c2.text_input("Soyad")
        age = c3.number_input("Yaş", 0, 120, 0, 1)
        gender = st.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        if st.form_submit_button("Ekle"):
            if not fn.strip() or not ln.strip():
                st.warning("Ad ve Soyad zorunludur.")
            else:
                add_patient(fn, ln, int(age), gender, sel_iso)
                st.success(f"Eklendi: {fn} {ln}"); st.rerun()

    if pts:
        st.markdown("### 🗑️ Hasta Sil")
        choice = st.selectbox("Silinecek", [(p[0], f"{p[1]} {p[2]}") for p in pts],
                              format_func=lambda x:x[1], key="del_pt_sel")
        if st.button("Sil", key="btn_del_pt"):
            delete_patient(choice[0]); st.success("Silindi."); st.rerun()

# ---- Tetkik Takibi
with tab_tetkik:
    pts_today = list_patients(sel_iso)
    if not pts_today:
        st.info("Bu tarih için hasta yok.")
    else:
        sel = st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts_today],
                           format_func=lambda x:x[1], key="sel_pt_for_tests")
        pid = sel[0]
        # Tetkik ekle + isteğe bağlı alarm
        st.markdown("#### Tetkik Ekle")
        with st.form("frm_add_test", clear_on_submit=True):
            tname = st.text_input("Tetkik adı")
            alarm = st.checkbox("🔔 Alarm kurmak istiyorum (isteğe bağlı)")
            hhmm = None
            if alarm:
                colh, colm = st.columns(2)
                hour = colh.selectbox("Saat", [f"{h:02d}" for h in range(24)], key="alarm_hour")
                minute = colm.selectbox("Dakika", [f"{m:02d}" for m in range(0,60,5)], key="alarm_min")
                hhmm = f"{hour}:{minute}"
            addt = st.form_submit_button("Ekle")
        if addt:
            if not tname.strip():
                st.warning("Tetkik adı zorunlu.")
            else:
                add_patient_test(pid, tname)
                if alarm and hhmm:
                    set_patient_alarm_time(pid, hhmm)
                    st.success(f"Tetkik eklendi ve alarm {hhmm} için kaydedildi.")
                else:
                    st.success("Tetkik eklendi.")
                st.rerun()

        # Tetkikler listesi
        st.markdown("#### Tetkikler")
        trs = list_patient_tests(pid)
        if not trs:
            st.info("Tetkik yok.")
        else:
            # Hastanın saatini çek
            p_row = [p for p in pts_today if p[0]==pid][0]
            patient_name = f"{p_row[1]} {p_row[2]}"
            visit_hhmm = p_row[6]

            # WhatsApp metni
            done = [t[2] for t in trs if t[3]=="tamamlandi"]
            rem  = [t[2] for t in trs if t[3]=="bekliyor"]
            wa_text = (f"📌 Tetkik Güncellemesi\n"
                       f"Hasta: {patient_name} ({sel_disp})\n"
                       f"Tamamlanan: {', '.join(done) if done else '-'}\n"
                       f"Kalan: {', '.join(rem) if rem else '-'}")
            # Hedef kişi seçimi (ayarlar kişilerinden)
            active_people = list_personnel(active_only=True)
            receivers = [(p[2], f"{p[1]} — {p[2]}") for p in active_people] or [("+900000000000","Numara ekleyin (Ayarlar)")]

            cwa, cics = st.columns([2,2])
            with cwa:
                recv = st.selectbox("WhatsApp alıcı", receivers, format_func=lambda x:x[1], key="wa_recv")
                st.link_button("💬 WhatsApp’tan gönder", make_whatsapp_link(recv[0], wa_text))
                st.caption("Mesaj hazır açılır; gönderme kararı sizde.")
            with cics:
                if visit_hhmm:
                    ics_bytes = build_ics(patient_name, sel_iso, visit_hhmm)
                    st.download_button("🔔 Takvime ekle (.ics, 10 dk önce uyar)", data=ics_bytes,
                                       file_name=f"checkup_{patient_name.replace(' ','_')}_{sel_iso}_{visit_hhmm}.ics",
                                       mime="text/calendar")
                else:
                    st.info("Alarm için saat kaydı yok. Tetkik eklerken 'Alarm kur' ile saat seçebilirsin.")

            st.divider()
            for tid, _pid, name, status, upd in trs:
                icon = "✅" if status=="tamamlandi" else "⏳"
                cols = st.columns([6,1,1,1])
                cols[0].markdown(f"{icon} **{name}** — {upd}")
                if status=="bekliyor":
                    if cols[1].button("Tamamla", key=f"done_{tid}"):
                        update_patient_test_status(tid,"tamamlandi"); st.rerun()
                else:
                    if cols[2].button("Geri Al", key=f"undo_{tid}"):
                        update_patient_test_status(tid,"bekliyor"); st.rerun()
                if cols[3].button("Sil", key=f"del_{tid}"):
                    delete_patient_test(tid); st.rerun()

# ---- Gün Özeti
with tab_ozet:
    st.subheader(f"{sel_disp} — Gün Özeti")
    pts = list_patients(sel_iso)
    if not pts:
        st.info("Bu tarihte hasta yok.")
    else:
        rows = []
        for p in pts:
            tests = list_patient_tests(p[0])
            done = [f"✅ {t[2]}" for t in tests if t[3]=="tamamlandi"]
            rem  = [f"⏳ {t[2]}" for t in tests if t[3]=="bekliyor"]
            rows.append({"Hasta": f"{p[1]} {p[2]}", "Alarm Saati": p[6] or "-",
                         "Tamamlanan": ", ".join(done) if done else "-",
                         "Kalan": ", ".join(rem) if rem else "-"})
        st.dataframe(rows, use_container_width=True)

        # Toplu .ics (saat kaydı olan hastalar için)
        pts_with_time = [p for p in pts if p[6]]
        if pts_with_time:
            mem = io.BytesIO()
            with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
                for p in pts_with_time:
                    pname = f"{p[1]} {p[2]}"
                    ics = build_ics(pname, sel_iso, p[6])
                    z.writestr(f"{pname.replace(' ','_')}_{sel_iso}_{p[6]}.ics", ics)
            mem.seek(0)
            st.download_button("📦 Tüm randevuları .zip (10 dk önce uyarı)", mem,
                               file_name=f"{sel_iso}_randevular.zip", mime="application/zip")

# ---- Yedek
with tab_yedek:
    st.subheader("Yedek / Dışa Aktar (CSV)")
    def _csv(query:str):
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute(query); rows = c.fetchall(); headers = [d[0] for d in c.description]
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(headers); w.writerows(rows)
        return buf.getvalue().encode("utf-8")
    c1,c2 = st.columns(2)
    with c1:
        st.download_button("Hastalar CSV", _csv("SELECT * FROM patients"), "patients.csv", "text/csv")
        st.download_button("Tetkikler CSV", _csv("SELECT * FROM patient_tests"), "patient_tests.csv", "text/csv")
    with c2:
        st.download_button("Kişiler CSV", _csv("SELECT * FROM personnel"), "personnel.csv", "text/csv")
