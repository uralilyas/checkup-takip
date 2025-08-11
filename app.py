# app.py
import os, sqlite3, csv, io, zipfile
from datetime import datetime, date, timedelta
from contextlib import closing
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
import streamlit as st

# =============== CONFIG & TIME (TR) ===============
st.set_page_config(page_title="Check-up Takip", page_icon="✅", layout="wide")
DB_PATH = "checkup.db"
TR_TZ = ZoneInfo("Europe/Istanbul")
AUTH_ENABLED = False  # True yaparsan giriş ekranı açılır (admin/admin)

def now_tr(): return datetime.now(TR_TZ)
def today_tr_date():
    n = now_tr(); return date(n.year, n.month, n.day)
def to_iso(d:date)->str: return d.strftime("%Y-%m-%d")
def to_display(d:date)->str: return d.strftime("%d/%m/%Y")
def now_str()->str: return now_tr().strftime("%Y-%m-%d %H:%M:%S")

def normalize_phone(p:str)->str:
    p = (p or "").strip().replace(" ","").replace("-","")
    if p and not p.startswith("+"): p = "+" + p
    return p

# =============== DB ===============
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def column_exists(conn, table, column)->bool:
    with closing(conn.cursor()) as c:
        c.execute(f"PRAGMA table_info({table})")
        return any(r[1]==column for r in c.fetchall())

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
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'bekliyor',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT, action TEXT, detail TEXT, created_at TEXT NOT NULL)""")
init_db()

def log_action(actor:str, action:str, detail:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO audit_log(actor,action,detail,created_at) VALUES(?,?,?,?)",
                  (actor, action, detail, now_str()))

# =============== SETTINGS HELPERS ===============
def get_setting(key:str, default:str=""):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT val FROM app_settings WHERE key=?", (key,))
        r = c.fetchone()
    return r[0] if r else default

def set_setting(key:str, val:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO app_settings(key,val) VALUES(?,?)
                     ON CONFLICT(key) DO UPDATE SET val=excluded.val""", (key,val))

# =============== PERSONNEL ===============
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q = "SELECT id,name,phone,active FROM personnel"
        if active_only: q += " WHERE active=1"
        q += " ORDER BY name"
        c.execute(q); return c.fetchall()

def upsert_personnel(name:str, phone:str, active:int)->int:
    phone = normalize_phone(phone)
    if not phone.startswith("+"): raise ValueError("Telefon + ile başlamalı (örn. +90...)")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("SELECT id FROM personnel WHERE phone=?", (phone,))
        r = c.fetchone()
        if r:
            pid = r[0]
            c.execute("UPDATE personnel SET name=?, active=? WHERE id=?",(name.strip(),active,pid))
            return pid
        c.execute("INSERT INTO personnel(name,phone,active) VALUES(?,?,?)",(name.strip(),phone,active))
        return c.lastrowid

def set_personnel_active(pid:int, active:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE personnel SET active=? WHERE id=?", (active, pid))

def delete_personnel(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM personnel WHERE id=?", (pid,))

# =============== PATIENTS / TESTS ===============
def add_patient(fn:str, ln:str, age:int, gender:str, visit_date_iso:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO patients(first_name,last_name,age,gender,visit_date,department,visit_time)
                     VALUES(?,?,?,?,?,?,?)""", (fn.strip(),ln.strip(),age,gender,visit_date_iso,"Genel",None))

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
                     VALUES(?,?, 'bekliyor')""", (patient_id, test_name.strip()))

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

# =============== ICS & CALENDAR LINKS (TR) ===============
def build_ics(patient_name:str, visit_date_iso:str, hhmm:str,
              title_prefix:str="Check-up Randevu",
              duration_min:int=30, remind_min:int=10,
              location:str="Klinik")->bytes:
    dt_local = datetime.strptime(f"{visit_date_iso} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TR_TZ)
    dt_end_local = dt_local + timedelta(minutes=duration_min)
    dtstamp = now_tr().strftime("%Y%m%dT%H%M%S")
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

def google_calendar_link(patient_name:str, visit_date_iso:str, hhmm:str, duration_min:int=30, location:str="Klinik"):
    # Google linki UTC ister; yaklaşık dönüşüm
    start_local = datetime.strptime(f"{visit_date_iso} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TR_TZ)
    end_local = start_local + timedelta(minutes=duration_min)
    fmt = "%Y%m%dT%H%M%SZ"
    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime(fmt)
    end_utc   = end_local.astimezone(ZoneInfo("UTC")).strftime(fmt)
    text = quote_plus(f"Check-up Randevu – {patient_name}")
    details = quote_plus("Check-up randevusu")
    loc = quote_plus(location)
    return f"https://www.google.com/calendar/render?action=TEMPLATE&text={text}&dates={start_utc}/{end_utc}&details={details}&location={loc}"

# =============== WhatsApp (deeplink) ===============
def make_whatsapp_link(phone:str, text:str)->str:
    return f"https://wa.me/{normalize_phone(phone).replace('+','')}?text={quote_plus(text)}"

# =============== THEMES (incl. System Match) & FX ===============
def apply_theme(theme_name:str):
    THEMES = {
        "Sistemle Uyumlu": """
        <style>
        :root{ --accent:#22c55e; }
        @media (prefers-color-scheme: light){
          body,.stApp{ background:#f8fafc!important; color:#0f172a!important;}
          .stButton>button,.stDownloadButton>button,.stLinkButton>button{
            background:linear-gradient(135deg,#0ea5e9,#22c55e)!important; color:#fff!important; border:none!important; border-radius:12px!important;
          }
        }
        @media (prefers-color-scheme: dark){
          body,.stApp{ background:#0b1220!important; color:#e5e7eb!important;}
          .stButton>button,.stDownloadButton>button,.stLinkButton>button{
            background:#1f2937!important; color:#e5e7eb!important; border:1px solid #334155!important; border-radius:12px!important;
          }
        }
        </style>""",
        "Klinik Açık": """
        <style>
        body,.stApp{ background:#f7fbff!important; color:#0f172a!important;}
        .stButton>button,.stDownloadButton>button,.stLinkButton>button{
          background:linear-gradient(135deg,#0ea5e9,#22c55e)!important; color:#fff!important; border:none!important; border-radius:12px!important;
          transition:transform .15s ease, filter .15s ease;
        }
        .stButton>button:hover,.stDownloadButton>button:hover,.stLinkButton>button:hover{ transform:translateY(-1px); filter:saturate(1.08);}
        </style>""",
        "Gece Koyu": """
        <style>
        body,.stApp{ background:#0b1220!important; color:#e5e7eb!important;}
        .stButton>button,.stDownloadButton>button,.stLinkButton>button{
          background:#1f2937!important; color:#e5e7eb!important; border:1px solid #334155!important; border-radius:12px!important;
          transition:transform .15s ease, box-shadow .2s ease;
        }
        .stButton>button:hover,.stDownloadButton>button:hover,.stLinkButton>button:hover{ transform:translateY(-1px); box-shadow:0 8px 22px rgba(0,0,0,.35);}
        </style>""",
        "Pastel Mint": """
        <style>
        body,.stApp{ background:#f5fffb!important; color:#0f172a!important;}
        .stButton>button,.stDownloadButton>button,.stLinkButton>button{
          background:#10b981!important; color:#fff!important; border:none!important; border-radius:14px!important; transition:transform .15s ease,opacity .2s ease;
        }
        .stButton>button:hover,.stDownloadButton>button:hover,.stLinkButton>button:hover{ transform:scale(1.01); opacity:.96;}
        </style>"""
    }
    css = THEMES.get(theme_name, "")
    if css: st.markdown(css, unsafe_allow_html=True)

# global yumuşak geçiş
st.markdown("""
<style>
section.main > div { animation: fadeIn .35s ease-in-out; }
@keyframes fadeIn { from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;} }
button, .stDownloadButton>button, .stLinkButton>button{ padding:.7rem 1rem!important; font-weight:600!important; }
</style>
""", unsafe_allow_html=True)

# =============== AUTH (opsiyonel) ===============
def do_login_ui():
    st.title("✅ Check-up Takip Sistemi")
    with st.form("login_form"):
        u = st.text_input("Kullanıcı adı")
        p = st.text_input("Şifre", type="password")
        ok = st.form_submit_button("Giriş")
    if ok:
        if u=="admin" and p=="admin":
            st.session_state.auth={"logged_in":True,"user":"admin"}
            st.experimental_rerun()
        else: st.error("Geçersiz bilgiler.")

if "auth" not in st.session_state:
    st.session_state.auth = {"logged_in": (not AUTH_ENABLED), "user":"admin"}

if AUTH_ENABLED and not st.session_state.auth["logged_in"]:
    do_login_ui(); st.stop()

# =============== APPLY THEME ===============
apply_theme(get_setting("theme", "Sistemle Uyumlu"))

# =============== SIDEBAR ===============
picked_date = st.sidebar.date_input("📅 Tarih seç", value=today_tr_date())
sel_iso = to_iso(picked_date); sel_disp = to_display(picked_date)

with st.sidebar:
    st.divider()
    with st.expander("⚙️ Ayarlar", expanded=False):
        st.markdown("#### 🎨 Tema")
        themes = ["Sistemle Uyumlu","Klinik Açık","Gece Koyu","Pastel Mint"]
        cur = get_setting("theme","Sistemle Uyumlu")
        new_t = st.selectbox("Tema seç", themes, index=themes.index(cur))
        if st.button("Temayı Uygula"):
            set_setting("theme", new_t); st.rerun()

        st.markdown("#### 💬 Mesaj şablonu")
        default_tpl = ("📌 Tetkik Güncellemesi\n"
                       "Hasta: {patient} ({date})\n"
                       "Tamamlanan: {done}\n"
                       "Kalan: {remaining}")
        tpl = st.text_area("Şablon", value=get_setting("wa_template", default_tpl), height=140,
                           help="{patient}, {date}, {done}, {remaining} alanlarını kullanabilirsiniz.")
        if st.button("Şablonu Kaydet"):
            set_setting("wa_template", tpl); st.success("Kaydedildi.")

        st.divider()
        st.markdown("#### 👥 Kişiler (WhatsApp)")
        people = list_personnel(active_only=False)
        if people:
            for pid,name,phone,active in people:
                c1,c2,c3 = st.columns([3,1,1])
                c1.caption(f"**{name}** — {phone}")
                tg = c2.toggle("Aktif", value=bool(active), key=f"act_{pid}")
                if tg != bool(active):
                    set_personnel_active(pid, int(tg)); st.rerun()
                if c3.button("Sil", key=f"del_{pid}"):
                    delete_personnel(pid); st.rerun()
        else:
            st.info("Kayıtlı kişi yok.")

        with st.form("frm_add_staff", clear_on_submit=True):
            nm = st.text_input("Ad / Not")
            ph = st.text_input("Telefon (+90...)")
            act = st.checkbox("Aktif", True)
            if st.form_submit_button("Kişi Ekle"):
                try:
                    upsert_personnel(nm, ph, 1 if act else 0); st.success("Eklendi."); st.rerun()
                except Exception as e: st.error(f"Hata: {e}")

        st.markdown("#### ✅ Varsayılan alıcı")
        active_ppl = list_personnel(active_only=True)
        options = [(p[2], f"{p[1]} — {p[2]}") for p in active_ppl] or [("", "Aktif kişi yok")]
        default_phone = get_setting("default_recipient", options[0][0] if options and options[0][0] else "")
        sel_idx = 0
        for i,o in enumerate(options):
            if o[0]==default_phone: sel_idx=i; break
        sel_def = st.selectbox("Kişi seç", options, index=sel_idx)
        if st.button("Varsayılanı Kaydet"):
            set_setting("default_recipient", sel_def[0] if sel_def[0] else ""); st.success("Kaydedildi.")

# =============== MAIN TABS ===============
st.title("✅ Check-up Takip Sistemi")
tab_hasta, tab_tetkik, tab_ozet, tab_yedek = st.tabs(["🧑‍⚕️ Hastalar","🧪 Tetkik Takibi","📊 Gün Özeti","💾 Yedek"])

# ---- Hastalar (sabit tablo + hızlı ekleme + paketler)
with tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts = list_patients(sel_iso)
    st.table([{"ID":p[0],"Ad":p[1],"Soyad":p[2],"Cinsiyet":p[4] or "","Yaş":p[3] or "","Alarm":p[6] or "-"} for p in pts])

    st.markdown("### ➕ Hızlı Ekle")
    with st.form("frm_quick_add", clear_on_submit=True):
        c1,c2,c3,c4 = st.columns([2,2,2,1])
        fullname = c1.text_input("Ad Soyad")
        testname = c2.text_input("Tetkik (isteğe bağlı)")
        hour = c3.selectbox("Saat (isteğe bağlı)", ["-"]+[f"{h:02d}:{m:02d}" for h in range(24) for m in (0,15,30,45)])
        gender = c4.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        age = c4.number_input("Yaş",0,120,0, key="q_age")
        ok = st.form_submit_button("Ekle")
    if ok:
        if not fullname.strip():
            st.warning("Ad Soyad gerekli.")
        else:
            parts = fullname.split()
            fn = " ".join(parts[:-1]) if len(parts)>1 else parts[0]
            ln = parts[-1] if len(parts)>1 else "-"
            add_patient(fn,ln,int(age),gender,sel_iso)
            # son eklenen hastayı bul
            pts2 = list_patients(sel_iso)
            new_id = max([p[0] for p in pts2]) if pts2 else None
            if new_id and testname.strip(): add_patient_test(new_id, testname.strip())
            if new_id and hour!="-" : set_patient_alarm_time(new_id, hour)
            st.success("Eklendi."); st.rerun()

    st.markdown("### 📦 Hazır Paketler")
    PACKS = {
        "Temel": ["Kan Tahlili","Görüntüleme","EKG"],
        "Kardiyoloji": ["EKG","Efor","Ekokardiyografi"],
        "Göz": ["Görme Keskinliği","Göz Tansiyonu","Biyomikroskopi"]
    }
    if pts:
        pick = st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts], format_func=lambda x:x[1], key="pkg_pt")
        p_id = pick[0]
        colp1,colp2 = st.columns([2,1])
        pack = colp1.selectbox("Paket", list(PACKS.keys()))
        if colp2.button("Paketi Ekle"):
            for t in PACKS[pack]: add_patient_test(p_id, t)
            st.success("Paket eklendi."); st.rerun()

    if pts:
        st.markdown("### 🗑️ Hasta Sil")
        choice = st.selectbox("Silinecek", [(p[0], f"{p[1]} {p[2]}") for p in pts], format_func=lambda x:x[1], key="del_pt_sel")
        if st.button("Sil"):
            delete_patient(choice[0]); st.success("Silindi."); st.rerun()

# ---- Tetkik Takibi
with tab_tetkik:
    pts_today = list_patients(sel_iso)
    if not pts_today:
        st.info("Bu tarih için hasta yok.")
    else:
        sel = st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts_today], format_func=lambda x:x[1], key="sel_pt_for_tests")
        pid = sel[0]

        st.markdown("#### Tetkik Ekle")
        with st.form("frm_add_test", clear_on_submit=True):
            tname = st.text_input("Tetkik adı")
            alarm = st.checkbox("🔔 Alarm kur (isteğe bağlı)")
            hhmm=None
            if alarm:
                colh,colm = st.columns(2)
                hour = colh.selectbox("Saat", [f"{h:02d}" for h in range(24)])
                minute = colm.selectbox("Dakika", [f"{m:02d}" for m in range(0,60,5)])
                hhmm=f"{hour}:{minute}"
            addt = st.form_submit_button("Ekle")
        if addt:
            if not tname.strip(): st.warning("Tetkik adı zorunlu.")
            else:
                add_patient_test(pid, tname)
                if alarm and hhmm: set_patient_alarm_time(pid, hhmm); st.success(f"Tetkik + alarm {hhmm}")
                else: st.success("Tetkik eklendi.")
                st.rerun()

        st.markdown("#### Tetkikler")
        trs = list_patient_tests(pid)
        if not trs: st.info("Tetkik yok.")
        else:
            prow = [p for p in pts_today if p[0]==pid][0]
            patient_name = f"{prow[1]} {prow[2]}"
            visit_hhmm = prow[6]

            done = [t[2] for t in trs if t[3]=="tamamlandi"]
            rem  = [t[2] for t in trs if t[3]=="bekliyor"]
            tpl = get_setting("wa_template", "Hasta: {patient} ({date})\nTamamlanan: {done}\nKalan: {remaining}")
            msg = tpl.format(patient=patient_name, date=sel_disp,
                             done=", ".join(done) if done else "-",
                             remaining=", ".join(rem) if rem else "-")

            active_people = list_personnel(active_only=True)
            receivers = [(p[2], f"{p[1]} — {p[2]}") for p in active_people]
            default_phone = get_setting("default_recipient", receivers[0][0] if receivers else "")

            cwa,cics = st.columns([2,2])
            with cwa:
                st.markdown("**💬 WhatsApp**")
                if default_phone:
                    st.link_button("Gönder (varsayılan)", make_whatsapp_link(default_phone, msg), use_container_width=True)
                    st.caption(f"Varsayılan: {default_phone}")
                if receivers:
                    recv = st.selectbox("Başka alıcı", receivers, format_func=lambda x:x[1], key="wa_alt")
                    st.link_button("Bu kişiye gönder", make_whatsapp_link(recv[0], msg), use_container_width=True)
                    multi = st.multiselect("Çoklu alıcı", receivers, format_func=lambda x:x[1], key="wa_multi")
                    for ph,label in multi:
                        st.link_button(f"{label}’a gönder", make_whatsapp_link(ph, msg))
                with st.popover("Mesajı kopyala"):
                    st.code(msg, language=None)

            with cics:
                if visit_hhmm:
                    rem_min = st.selectbox("Alarm süresi", [5,10,15,30], index=1)
                    ics_bytes = build_ics(patient_name, sel_iso, visit_hhmm, remind_min=rem_min)
                    st.download_button("🔔 Takvime ekle (.ics)", data=ics_bytes,
                                       file_name=f"checkup_{patient_name.replace(' ','_')}_{sel_iso}_{visit_hhmm}.ics",
                                       mime="text/calendar")
                    st.link_button("🗓️ Google Calendar’a ekle", google_calendar_link(patient_name, sel_iso, visit_hhmm),
                                   use_container_width=False)
                else:
                    st.info("Alarm saati yok. Tetkik eklerken 'Alarm kur' ile belirleyebilirsin.")

            st.divider()
            for tid,_pid,name,status,upd in trs:
                icon = "✅" if status=="tamamlandi" else "⏳"
                cols = st.columns([6,1,1,1])
                cols[0].markdown(f"{icon} **{name}** — {upd}")
                if status=="bekliyor":
                    if cols[1].button("Tamamla", key=f"done_{tid}"): update_patient_test_status(tid,"tamamlandi"); st.rerun()
                else:
                    if cols[2].button("Geri Al", key=f"undo_{tid}"): update_patient_test_status(tid,"bekliyor"); st.rerun()
                if cols[3].button("Sil", key=f"del_{tid}"): delete_patient_test(tid); st.rerun()

# ---- Gün Özeti (sabit tablo, alarm sütunu yok) + mini rapor
with tab_ozet:
    st.subheader(f"{sel_disp} — Gün Özeti")
    pts = list_patients(sel_iso)
    if not pts: st.info("Bu tarihte hasta yok.")
    else:
        rows = []
        total_done = total_rem = 0
        for p in pts:
            tests = list_patient_tests(p[0])
            done = [f"✅ {t[2]}" for t in tests if t[3]=="tamamlandi"]
            rem  = [f"⏳ {t[2]}" for t in tests if t[3]=="bekliyor"]
            total_done += len([1 for t in tests if t[3]=="tamamlandi"])
            total_rem  += len([1 for t in tests if t[3]=="bekliyor"])
            rows.append({"Hasta": f"{p[1]} {p[2]}",
                         "Tamamlanan": ", ".join(done) if done else "-",
                         "Kalan": ", ".join(rem) if rem else "-"})
        st.table(rows)
        st.caption(f"Toplam tamamlanan: {total_done} • Kalan: {total_rem}")

        # Toplu .ics (yalnızca kalan tetkik olanlar)
        need_time = [p for p in pts if p[6] and any(t[3]=="bekliyor" for t in list_patient_tests(p[0]))]
        if need_time:
            mem = io.BytesIO()
            with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
                for p in need_time:
                    pname = f"{p[1]} {p[2]}"
                    ics = build_ics(pname, sel_iso, p[6])
                    z.writestr(f"{pname.replace(' ','_')}_{sel_iso}_{p[6]}.ics", ics)
            mem.seek(0)
            st.download_button("📦 Kalan tetkiki olanların randevuları (.zip)", mem,
                               file_name=f"{sel_iso}_randevular_kalan.zip", mime="application/zip")

# ---- Yedek / İçe-Dışa Aktarma / Audit
with tab_yedek:
    st.subheader("Dışa Aktar (CSV)")
    def _csv(query:str):
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute(query); rows = c.fetchall(); headers=[d[0] for d in c.description]
        buf = io.StringIO(); w=csv.writer(buf); w.writerow(headers); w.writerows(rows)
        return buf.getvalue().encode("utf-8")
    c1,c2 = st.columns(2)
    with c1:
        st.download_button("Hastalar CSV", _csv("SELECT * FROM patients"), "patients.csv", "text/csv")
        st.download_button("Tetkikler CSV", _csv("SELECT * FROM patient_tests"), "patient_tests.csv", "text/csv")
    with c2:
        st.download_button("Kişiler CSV", _csv("SELECT * FROM personnel"), "personnel.csv", "text/csv")
        st.download_button("Audit Log CSV", _csv("SELECT * FROM audit_log ORDER BY id DESC"), "audit_log.csv", "text/csv")

    st.divider(); st.subheader("İçe Aktar (CSV)")
    st.caption("Hastalar CSV başlıkları: first_name,last_name,age,gender,visit_date(YYYY-MM-DD),visit_time(HH:MM veya boş)")
    up1 = st.file_uploader("Hastalar CSV yükle", type=["csv"])
    if up1 and st.button("Hastaları içe aktar"):
        txt = up1.read().decode("utf-8").splitlines()
        rd = csv.DictReader(txt)
        cnt=0
        for r in rd:
            add_patient(r["first_name"], r["last_name"], int(r.get("age") or 0), r.get("gender") or "", r["visit_date"])
            # son id
            pid = list_patients(r["visit_date"])[-1][0]
            if r.get("visit_time"): set_patient_alarm_time(pid, r["visit_time"])
            cnt+=1
        st.success(f"{cnt} hasta aktarıldı."); log_action("admin","import_patients",f"{cnt} kayıt")

    st.caption("Tetkikler CSV başlıkları: patient_id,test_name,status(bekliyor|tamamlandi)")
    up2 = st.file_uploader("Tetkikler CSV yükle", type=["csv"])
    if up2 and st.button("Tetkikleri içe aktar"):
        txt = up2.read().decode("utf-8").splitlines()
        rd = csv.DictReader(txt); cnt=0
        for r in rd:
            add_patient_test(int(r["patient_id"]), r["test_name"])
            if r.get("status") in ("bekliyor","tamamlandi"):
                last = list_patient_tests(int(r["patient_id"]))[0]
                update_patient_test_status(last[0], r["status"])
            cnt+=1
        st.success(f"{cnt} tetkik aktarıldı."); log_action("admin","import_tests",f"{cnt} kayıt")

    st.divider(); st.subheader("Denetim Kaydı (son 50)")
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT created_at,actor,action,detail FROM audit_log ORDER BY id DESC LIMIT 50")
        logs = [{"Zaman":r[0],"Kullanıcı":r[1],"İşlem":r[2],"Detay":r[3]} for r in c.fetchall()]
    st.table(logs)
