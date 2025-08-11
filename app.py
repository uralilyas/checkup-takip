# app.py
import os, sqlite3, csv, io, zipfile
from datetime import datetime, date, timedelta
from contextlib import closing
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
import streamlit as st

# ================== CONFIG ==================
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
DB_PATH = "checkup.db"
TR_TZ = ZoneInfo("Europe/Istanbul")
AUTH_ENABLED = False  # True yaparsan giriş ekranı açılır (admin/admin)

# ================== ZAMAN/YARDIMCI ==================
def now_tr():
    return datetime.now(TR_TZ)

def today_tr_date():
    n = now_tr()
    return date(n.year, n.month, n.day)

def to_iso(d:date) -> str: return d.strftime("%Y-%m-%d")
def to_display(d:date) -> str: return d.strftime("%d/%m/%Y")
def now_str(): return now_tr().strftime("%Y-%m-%d %H:%M:%S")

def normalize_phone(p:str)->str:
    p = (p or "").strip().replace(" ", "").replace("-", "")
    if p and not p.startswith("+"):
        p = "+" + p
    return p

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

# ================== SETTINGS HELPERS ==================
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

# ================== ICS (TR Zamanlı) & WHATSAPP DEEPLINK ==================
def build_ics(patient_name:str, visit_date_iso:str, hhmm:str,
              title_prefix:str="Check-up Randevu", duration_min:int=30,
              remind_min:int=10, location:str="Klinik")->bytes:
    """TR saatine göre 10 dk önce uyarılı .ics üretir."""
    dt_local = datetime.strptime(f"{visit_date_iso} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TR_TZ)
    dt_end_local = dt_local + timedelta(minutes=duration_min)
    dtstamp = now_tr().astimezone(TR_TZ).strftime("%Y%m%dT%H%M%S")
    dtstart = dt_local.strftime("%Y%m%dT%H%M%S")
    dtend   = dt_end_local.strftime("%Y%m%dT%H%M%S")
    uid = f"{abs(hash((patient_name, visit_date_iso, hhmm, dtstamp)))}@checkup"
    summary = f"{title_prefix} – {patient_name}"
    desc = f"{patient_name} randevusu. Hatırlatıcı: {remind_min} dk önce."
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//checkup//streamlit//TR
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:{uid}
DTSTAMP;TZID=Europe/Istanbul:{dtstamp}
DTSTART;TZID=Europe/Istanbul:{dtstart}
DTEND;TZID=Europe/Istanbul:{dtend}
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
    digits = normalize_phone(phone).replace("+","")
    return f"https://wa.me/{digits}?text={quote_plus(text)}"

# ================== TEMA / GEÇİŞLER ==================
def apply_theme(theme_name: str):
    THEMES = {
        "Klinik Açık": """
        <style>
        :root{ --brand:#0ea5e9; --brand2:#22c55e; --bg:#f8fafc; --text:#0f172a; }
        body, .stApp { background:var(--bg)!important; color:var(--text)!important; }
        .stButton>button, .stDownloadButton>button, .stLinkButton>button{
            background:linear-gradient(135deg,var(--brand),var(--brand2))!important;
            color:white!important; border:none!important; border-radius:10px!important;
            transition:transform .15s ease, filter .2s ease;
        }
        .stButton>button:hover, .stDownloadButton>button:hover, .stLinkButton>button:hover{
            transform:translateY(-1px); filter:saturate(1.1);
        }
        .stTabs [data-baseweb="tab"]{ font-weight:600; }
        </style>""",
        "Gece Koyu": """
        <style>
        :root{ --brand:#60a5fa; --bg:#0b1220; --text:#e5e7eb; }
        body, .stApp { background:var(--bg)!important; color:var(--text)!important; }
        .stButton>button, .stDownloadButton>button, .stLinkButton>button{
            background:#1f2937!important; color:#e5e7eb!important; border:1px solid #334155!important; border-radius:10px!important;
            transition:transform .15s ease, box-shadow .2s ease;
        }
        .stButton>button:hover, .stDownloadButton>button:hover, .stLinkButton>button:hover{
            transform:translateY(-1px); box-shadow:0 6px 20px rgba(0,0,0,.35);
        }
        .stTabs [data-baseweb="tab"]{ color:#cbd5e1!important; font-weight:600; }
        </style>""",
        "Pastel Mint": """
        <style>
        :root{ --brand:#10b981; --bg:#f5fffb; --text:#0f172a; }
        body, .stApp { background:var(--bg)!important; color:var(--text)!important; }
        .stButton>button, .stDownloadButton>button, .stLinkButton>button{
            background:#10b981!important; color:white!important; border:none!important; border-radius:12px!important;
            transition: transform .15s ease, opacity .2s ease;
        }
        .stButton>button:hover, .stDownloadButton>button:hover, .stLinkButton>button:hover{ transform:scale(1.01); opacity:.95; }
        </style>""",
    }
    css = THEMES.get(theme_name, "")
    if css: st.markdown(css, unsafe_allow_html=True)

# global geçişler (Safari dostu)
st.markdown("""
<style>
section.main > div { animation: fadeIn .35s ease-in-out; }
@keyframes fadeIn { from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;} }
</style>
""", unsafe_allow_html=True)

# ================== AUTH ==================
def do_login_ui() -> bool:
    st.title("✅ Check-up Takip Sistemi")
    st.subheader("Giriş")
    with st.form("login_form"):
        u = st.text_input("Kullanıcı adı", value="", autocomplete="username")
        p = st.text_input("Şifre", value="", type="password", autocomplete="current-password")
        ok = st.form_submit_button("Giriş")
    if ok:
        if u == "admin" and p == "admin":
            st.session_state.auth = {"logged_in": True, "is_admin": True, "username": "admin"}
            st.experimental_rerun()
        else:
            st.error("Geçersiz bilgiler.")
    return False

if "auth" not in st.session_state:
    if AUTH_ENABLED:
        st.session_state.auth = {"logged_in": False}
    else:
        st.session_state.auth = {"logged_in": True, "is_admin": True, "username": "admin"}

if AUTH_ENABLED and not st.session_state.auth.get("logged_in"):
    do_login_ui()
    st.stop()

# ================== TEMA UYGULA ==================
apply_theme(get_setting("theme", "Klinik Açık"))

# ================== SIDEBAR ==================
picked_date = st.sidebar.date_input("📅 Tarih seç", value=today_tr_date(), key="dt_pick")
sel_iso = to_iso(picked_date); sel_disp = to_display(picked_date)

with st.sidebar:
    st.divider()
    with st.expander("⚙️ Ayarlar", expanded=False):
        st.markdown("#### 🎨 Tema")
        themes = ["Klinik Açık", "Gece Koyu", "Pastel Mint"]
        cur = get_setting("theme", "Klinik Açık")
        new_t = st.selectbox("Tema seç", themes, index=themes.index(cur), key="sel_theme")
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

        st.divider()
        st.markdown("#### ✅ Varsayılan WhatsApp alıcı")
        ppl_active = list_personnel(active_only=True)
        options = [(p[2], f"{p[1]} — {p[2]}") for p in ppl_active] or [("", "Aktif kişi yok")]
        default_phone = get_setting("default_recipient", options[0][0] if options and options[0][0] else "")
        sel_def_idx = 0
        for i, o in enumerate(options):
            if o[0] == default_phone:
                sel_def_idx = i; break
        sel_def = st.selectbox("Kişi seç", options, index=sel_def_idx)
        if st.button("Varsayılanı Kaydet"):
            set_setting("default_recipient", sel_def[0] if sel_def[0] else "")
            st.success("Varsayılan alıcı kaydedildi.")

# ================== MAIN ==================
st.title("✅ Check-up Takip Sistemi")
tab_hasta, tab_tetkik, tab_ozet, tab_yedek = st.tabs(
    ["🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📊 Gün Özeti", "💾 Yedek"]
)

# ---- Hastalar
with tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts = list_patients(sel_iso)
    st.dataframe([{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Alarm":p[6] or "-"} for p in pts],
                 use_container_width=True, hide_index=True)

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

        st.markdown("#### Tetkik Ekle")
        with st.form("frm_add_test", clear_on_submit=True):
            tname = st.text_input("Tetkik adı")
            alarm = st.checkbox("🔔 Alarm kurmak istiyorum (isteğe bağlı)")
            hhmm = None
            if alarm:
                colh, colm = st.columns(2)
                hour = colh.selectbox("Saat", [f"{h:02d}" for h in range(24)])
                minute = colm.selectbox("Dakika", [f"{m:02d}" for m in range(0,60,5)])
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
            p_row = [p for p in pts_today if p[0]==pid][0]
            patient_name = f"{p_row[1]} {p_row[2]}"
            visit_hhmm = p_row[6]

            done = [t[2] for t in trs if t[3]=="tamamlandi"]
            rem  = [t[2] for t in trs if t[3]=="bekliyor"]
            wa_text = (f"📌 Tetkik Güncellemesi\n"
                       f"Hasta: {patient_name} ({sel_disp})\n"
                       f"Tamamlanan: {', '.join(done) if done else '-'}\n"
                       f"Kalan: {', '.join(rem) if rem else '-'}")

            active_people = list_personnel(active_only=True)
            receivers = [(p[2], f"{p[1]} — {p[2]}") for p in active_people]
            default_phone = get_setting("default_recipient", receivers[0][0] if receivers else "")

            cwa, cics = st.columns([2,2])
            with cwa:
                st.markdown("**💬 WhatsApp**")
                if default_phone:
                    st.link_button("Gönder (varsayılan)", make_whatsapp_link(default_phone, wa_text),
                                   use_container_width=True)
                    st.caption(f"Varsayılan: {default_phone}")
                else:
                    st.info("Ayarlar > 'Varsayılan alıcı'yı belirleyin.")

                if receivers:
                    recv = st.selectbox("Başka alıcı", receivers, format_func=lambda x:x[1], key="wa_recv_alt")
                    st.link_button("Bu kişiye gönder", make_whatsapp_link(recv[0], wa_text),
                                   use_container_width=True)
                else:
                    st.info("Ayarlar > Kişiler bölümüne en az bir aktif kişi ekleyin.")

                with st.popover("Mesajı kopyala"):
                    st.code(wa_text, language=None)

                if receivers:
                    with st.expander("Aktif herkese bağlantıları göster"):
                        for ph, label in receivers:
                            st.markdown(f"- [{label}]({make_whatsapp_link(ph, wa_text)})")

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

# ---- Gün Özeti (Safari sabit tablo, Alarm kolonu kaldırıldı)
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
            rows.append({"Hasta": f"{p[1]} {p[2]}",
                         "Tamamlanan": ", ".join(done) if done else "-",
                         "Kalan": ", ".join(rem) if rem else "-"})
        st.table(rows)  # interaktif değil → sütun kayması yok

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
