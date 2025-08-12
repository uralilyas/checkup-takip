# app.py
# Check-up Takip Sistemi — tek dosya / Streamlit
import sqlite3, csv, io, zipfile
from datetime import datetime, date, timedelta
from contextlib import closing
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
import streamlit as st

# ================== CONFIG ==================
st.set_page_config(page_title="Check-up Takip", page_icon="🩺", layout="wide")
DB_PATH = "checkup.db"
TR_TZ = ZoneInfo("Europe/Istanbul")
AUTH_ENABLED = False  # True yaparsan login: admin/admin olur

def now_tr(): return datetime.now(TR_TZ)
def today_tr_date(): n=now_tr(); return date(n.year, n.month, n.day)
def to_iso(d:date)->str: return d.strftime("%Y-%m-%d")
def to_display(d:date)->str: return d.strftime("%d/%m/%Y")
def now_str()->str: return now_tr().strftime("%Y-%m-%d %H:%M:%S")
def normalize_phone(p:str)->str:
    p=(p or "").strip().replace(" ","").replace("-","")
    if p and not p.startswith("+"): p="+"+p
    return p

# ================== DB ==================
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn

def column_exists(conn,t,c)->bool:
    with closing(conn.cursor()) as cur:
        cur.execute(f"PRAGMA table_info({t})")
        return any(r[1]==c for r in cur.fetchall())

def init_db():
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY, val TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS patients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL, last_name TEXT NOT NULL,
            age INTEGER, gender TEXT,
            visit_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        # Şema yükseltmeleri
        if not column_exists(conn,"patients","department"):
            c.execute("ALTER TABLE patients ADD COLUMN department TEXT")
            c.execute("UPDATE patients SET department='Genel' WHERE department IS NULL")
        if not column_exists(conn,"patients","visit_time"):
            c.execute("ALTER TABLE patients ADD COLUMN visit_time TEXT")
        try:
            c.execute("UPDATE patients SET created_at = COALESCE(created_at, datetime('now'))")
        except sqlite3.OperationalError:
            pass
        # patient_tests
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'bekliyor',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(patient_id) REFERENCES patients(id))""")
        if not column_exists(conn,"patient_tests","status"):
            c.execute("ALTER TABLE patient_tests ADD COLUMN status TEXT NOT NULL DEFAULT 'bekliyor'")
        if not column_exists(conn,"patient_tests","updated_at"):
            c.execute("ALTER TABLE patient_tests ADD COLUMN updated_at TEXT")
            c.execute("UPDATE patient_tests SET updated_at = COALESCE(updated_at, datetime('now'))")
        c.execute("""CREATE TABLE IF NOT EXISTS packages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        c.execute("""CREATE TABLE IF NOT EXISTS package_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            ord INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(package_id) REFERENCES packages(id))""")

def cleanup_old_patients():
    """Dünkü ve öncesi hastaları (tetkikleriyle) sil — paketler kalır."""
    today_iso = to_iso(today_tr_date())
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("SELECT id FROM patients WHERE visit_date < ?", (today_iso,))
        ids=[r[0] for r in c.fetchall()]
        if ids:
            c.executemany("DELETE FROM patient_tests WHERE patient_id=?", [(i,) for i in ids])
            c.executemany("DELETE FROM patients WHERE id=?", [(i,) for i in ids])

init_db()
cleanup_old_patients()

# ================== SETTINGS HELPERS ==================
def get_setting(key, default=""):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT val FROM app_settings WHERE key=?", (key,))
        r=c.fetchone()
        return r[0] if r else default
def set_setting(key,val):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""INSERT INTO app_settings(key,val) VALUES(?,?)
                     ON CONFLICT(key) DO UPDATE SET val=excluded.val""",(key,val))

# ================== PERSONNEL ==================
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q="SELECT id,name,phone,active FROM personnel"
        if active_only: q+=" WHERE active=1"
        q+=" ORDER BY name"
        c.execute(q); return c.fetchall()
def upsert_personnel(name,phone,active:int):
    phone=normalize_phone(phone)
    if phone and not phone.startswith("+"): raise ValueError("Telefon +90… formatında olmalı")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("SELECT id FROM personnel WHERE phone=?", (phone,))
        r=c.fetchone()
        if r:
            c.execute("UPDATE personnel SET name=?,active=? WHERE id=?",(name.strip(),active,r[0]))
            return r[0]
        c.execute("INSERT INTO personnel(name,phone,active) VALUES(?,?,?)",(name.strip(),phone,active))
        return c.lastrowid
def set_personnel_active(pid,active:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE personnel SET active=? WHERE id=?", (active,pid))
def delete_personnel(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM personnel WHERE id=?", (pid,))

# ================== PATIENTS / TESTS ==================
def add_patient(fn, ln, age, gender, visit_date_iso):
    """created_at’i de doldurarak ekler (IntegrityError fix)."""
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""
            INSERT INTO patients(
                first_name,last_name,age,gender,visit_date,
                department,visit_time,created_at
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (fn.strip(), ln.strip(), age, gender,
              visit_date_iso, "Genel", None, now_str()))
def delete_patient(pid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE patient_id=?", (pid,))
        c.execute("DELETE FROM patients WHERE id=?", (pid,))
def set_patient_alarm_time(pid:int, hhmm:str|None):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patients SET visit_time=? WHERE id=?", (hhmm,pid))
def list_patients(visit_date_iso:str|None=None):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if visit_date_iso:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients WHERE visit_date=? ORDER BY last_name,first_name""",(visit_date_iso,))
        else:
            c.execute("""SELECT id,first_name,last_name,age,gender,department,visit_time
                         FROM patients ORDER BY visit_date DESC,last_name""")
        return c.fetchall()

def _migrate_patient_tests_if_needed(conn, cur):
    if not column_exists(conn,"patient_tests","status"):
        cur.execute("ALTER TABLE patient_tests ADD COLUMN status TEXT NOT NULL DEFAULT 'bekliyor'")
    if not column_exists(conn,"patient_tests","updated_at"):
        cur.execute("ALTER TABLE patient_tests ADD COLUMN updated_at TEXT")
        cur.execute("UPDATE patient_tests SET updated_at = COALESCE(updated_at, datetime('now'))")

def add_patient_test(pid:int, test_name:str):
    test_name = (test_name or "").strip()
    if not test_name:
        raise ValueError("Tetkik adı boş olamaz.")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        try:
            # En güncel şema ile deneriz
            c.execute("""INSERT INTO patient_tests(patient_id,test_name,status,updated_at)
                         VALUES(?,?,?,?)""", (pid, test_name, 'bekliyor', now_str()))
        except sqlite3.IntegrityError:
            # Eski tabloda NOT NULL/constraint vb. sorun: şemayı onarıp tekrar dene
            _migrate_patient_tests_if_needed(conn, c)
            c.execute("""INSERT INTO patient_tests(patient_id,test_name,status,updated_at)
                         VALUES(?,?,?,?)""", (pid, test_name, 'bekliyor', now_str()))
        except sqlite3.OperationalError:
            # Çok eski kurulumlarda sütun farklı olabilir
            _migrate_patient_tests_if_needed(conn, c)
            c.execute("""INSERT INTO patient_tests(patient_id,test_name,status,updated_at)
                         VALUES(?,?,?,?)""", (pid, test_name, 'bekliyor', now_str()))

def list_patient_tests(pid:int):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("""SELECT id,patient_id,test_name,status,updated_at
                     FROM patient_tests WHERE patient_id=? ORDER BY updated_at DESC, id DESC""",(pid,))
        return c.fetchall()
def update_patient_test_status(tid:int, status:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patient_tests SET status=?,updated_at=? WHERE id=?",(status,now_str(),tid))
def delete_patient_test(tid:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE id=?", (tid,))

# ================== PACKAGES ==================
def list_packages():
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT id,name FROM packages ORDER BY name"); return c.fetchall()
def get_package_tests(pkg_id:int):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("""SELECT id,test_name,ord FROM package_tests
                     WHERE package_id=? ORDER BY ord ASC, id ASC""",(pkg_id,))
        return c.fetchall()
def create_package(name:str, tests:list[str]):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO packages(name) VALUES(?)",(name.strip(),))
        pid=c.lastrowid
        for i,t in enumerate(tests):
            if t.strip():
                c.execute("INSERT INTO package_tests(package_id,test_name,ord) VALUES(?,?,?)",(pid,t.strip(),i))
        return pid
def rename_package(pkg_id:int, new_name:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE packages SET name=? WHERE id=?", (new_name.strip(), pkg_id))
def add_test_to_package(pkg_id:int, test_name:str, ord_hint:int|None=None):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        if ord_hint is None:
            c.execute("SELECT COALESCE(MAX(ord),-1)+1 FROM package_tests WHERE package_id=?", (pkg_id,))
            ord_hint=c.fetchone()[0]
        c.execute("INSERT INTO package_tests(package_id,test_name,ord) VALUES(?,?,?)",(pkg_id,test_name.strip(),ord_hint))
def delete_test_from_package(pt_id:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM package_tests WHERE id=?", (pt_id,))
def delete_package(pkg_id:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM package_tests WHERE package_id=?", (pkg_id,))
        c.execute("DELETE FROM packages WHERE id=?", (pkg_id,))
def apply_package_to_patient(pkg_id:int, patient_id:int):
    for _id, name, _ord in get_package_tests(pkg_id):
        add_patient_test(patient_id, name)

# ================== CALENDAR / WHATSAPP LINKS ==================
def build_ics(patient_name:str, visit_date_iso:str, hhmm:str,
              duration_min:int=30, remind_min:int=10, location:str="Klinik")->bytes:
    dt_local=datetime.strptime(f"{visit_date_iso} {hhmm}","%Y-%m-%d %H:%M").replace(tzinfo=TR_TZ)
    dt_end=dt_local+timedelta(minutes=duration_min)
    dtstamp=now_tr().strftime("%Y%m%dT%H%M%S")
    s=dt_local.strftime("%Y%m%dT%H%M%S"); e=dt_end.strftime("%Y%m%dT%H%M%S")
    uid=f"{abs(hash((patient_name,visit_date_iso,hhmm,dtstamp)))}@checkup"
    ics=f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//checkup//streamlit//TR
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:{uid}
DTSTAMP;TZID=Europe/Istanbul:{dtstamp}
DTSTART;TZID=Europe/Istanbul:{s}
DTEND;TZID=Europe/Istanbul:{e}
SUMMARY:Check-up Randevu – {patient_name}
LOCATION:{location}
DESCRIPTION:{patient_name} randevusu.
BEGIN:VALARM
TRIGGER:-PT{remind_min}M
ACTION:DISPLAY
DESCRIPTION:Hatırlatma
END:VALARM
END:VEVENT
END:VCALENDAR
"""
    return ics.encode("utf-8")
def google_calendar_link(patient_name, visit_date_iso, hhmm, duration_min:int=30, location:str="Klinik"):
    start_local=datetime.strptime(f"{visit_date_iso} {hhmm}","%Y-%m-%d %H:%M").replace(tzinfo=TR_TZ)
    end_local=start_local+timedelta(minutes=duration_min)
    fmt="%Y%m%dT%H%M%SZ"
    s=start_local.astimezone(ZoneInfo("UTC")).strftime(fmt)
    e=end_local.astimezone(ZoneInfo("UTC")).strftime(fmt)
    return ("https://www.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote_plus('Check-up – '+patient_name)}"
            f"&dates={s}/{e}&location={quote_plus(location)}&details={quote_plus('Check-up randevusu')}")

def make_whatsapp_link(phone,text)->str:
    return f"https://wa.me/{normalize_phone(phone).replace('+','')}?text={quote_plus(text)}"

# ================== THEME & EFFECTS ==================
def apply_theme(theme_name:str):
    THEMES={
        "Sistemle Uyumlu": """
        <style>
        :root{ --chip-bg: #e2f6ff; --chip-tx: #0b4a6f; --card:#ffffff; --border:#e5e7eb; }
        @media (prefers-color-scheme: light){
          body,.stApp{ background:#f8fafc!important; color:#0f172a!important;}
        }
        @media (prefers-color-scheme: dark){
          body,.stApp{ background:#0b1220!important; color:#e5e7eb!important;}
          :root{ --chip-bg:#143041; --chip-tx:#d3efff; --card:#0f172a; --border:#233042; }
        }
        </style>""",
        "Klinik Açık": """
        <style>
        :root{ --chip-bg:#e6fff3; --chip-tx:#064e3b; --card:#ffffff; --border:#e5e7eb; }
        body,.stApp{ background:#f7fbff!important; color:#0f172a!important;}
        </style>""",
        "Gece Koyu": """
        <style>
        :root{ --chip-bg:#1f2937; --chip-tx:#d1d5db; --card:#0f172a; --border:#233042; }
        body,.stApp{ background:#0b1220!important; color:#e5e7eb!important;}
        </style>""",
        "Pastel Mint": """
        <style>
        :root{ --chip-bg:#eafff7; --chip-tx:#065f46; --card:#ffffff; --border:#e5e7eb; }
        body,.stApp{ background:#f5fffb!important; color:#0f172a!important;}
        </style>"""
    }
    css=THEMES.get(theme_name,"")
    if css: st.markdown(css, unsafe_allow_html=True)

st.markdown("""
<style>
section.main > div { animation: fadeIn .35s ease-in-out; }
@keyframes fadeIn { from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;} }
.stButton>button,.stDownloadButton>button,.stLinkButton>button{
  padding:.72rem 1rem!important; font-weight:600!important; border-radius:12px!important;
  transition:transform .15s ease, filter .15s ease;}
.stButton>button:hover,.stDownloadButton>button:hover,.stLinkButton>button:hover{
  transform:translateY(-1px); filter:saturate(1.05);}

/* Paket kart/chip */
.pkg-card{ border:1px solid var(--border); background:var(--card); border-radius:14px; padding:12px 14px; margin:8px 0; }
.pkg-chip{ display:inline-block; padding:.35rem .6rem; border-radius:999px; background:var(--chip-bg); color:var(--chip-tx);
  margin:.22rem .28rem .22rem 0; font-size:.92rem; }
</style>
""", unsafe_allow_html=True)

# ================== AUTH (opsiyonel) ==================
def do_login_ui():
    st.title("🩺 Check-up Takip")
    with st.form("login_form"):
        u=st.text_input("Kullanıcı adı")
        p=st.text_input("Şifre", type="password")
        ok=st.form_submit_button("Giriş")
    if ok:
        if u=="admin" and p=="admin":
            st.session_state.auth={"logged_in":True,"user":"admin"}
            st.experimental_rerun()
        else: st.error("Geçersiz bilgiler.")
if "auth" not in st.session_state:
    st.session_state.auth={"logged_in": (not AUTH_ENABLED), "user":"admin"}
if AUTH_ENABLED and not st.session_state.auth["logged_in"]:
    do_login_ui(); st.stop()

# ================== THEME APPLY ==================
apply_theme(get_setting("theme","Sistemle Uyumlu"))

# ================== SIDEBAR ==================
picked_date=st.sidebar.date_input("📅 Tarih", value=today_tr_date())
sel_iso=to_iso(picked_date); sel_disp=to_display(picked_date)

with st.sidebar:
    st.divider()
    with st.expander("⚙️ Ayarlar", expanded=False):
        st.markdown("#### 🎨 Tema")
        themes=["Sistemle Uyumlu","Klinik Açık","Gece Koyu","Pastel Mint"]
        cur=get_setting("theme","Sistemle Uyumlu")
        new_t=st.selectbox("Tema seç", themes, index=themes.index(cur))
        if st.button("Temayı Uygula"): set_setting("theme",new_t); st.rerun()

        st.markdown("#### 💬 Mesaj şablonu")
        default_tpl=("📌 Tetkik Güncellemesi\nHasta: {patient} ({date})\nTamamlanan: {done}\nKalan: {remaining}")
        tpl=st.text_area("Şablon", value=get_setting("wa_template", default_tpl), height=120,
                         help="{patient}, {date}, {done}, {remaining}")
        if st.button("Şablonu Kaydet"): set_setting("wa_template",tpl); st.success("Kaydedildi")

        st.markdown("#### 👥 Kişiler (WhatsApp)")
        ppl=list_personnel(active_only=False)
        if ppl:
            for pid,name,phone,active in ppl:
                c1,c2,c3=st.columns([3,1,1]); c1.caption(f"**{name}** — {phone}")
                tg=c2.toggle("Aktif", value=bool(active), key=f"act_{pid}")
                if tg!=bool(active): set_personnel_active(pid,int(tg)); st.rerun()
                if c3.button("Sil", key=f"del_{pid}"): delete_personnel(pid); st.rerun()
        with st.form("staff_add", clear_on_submit=True):
            nm=st.text_input("Ad/Not"); ph=st.text_input("Telefon (+90...)"); act=st.checkbox("Aktif",True)
            if st.form_submit_button("Kişi Ekle"):
                try: upsert_personnel(nm,ph,1 if act else 0); st.success("Eklendi"); st.rerun()
                except Exception as e: st.error(f"Hata: {e}")
        st.markdown("#### ✅ Varsayılan alıcı")
        act=list_personnel(active_only=True)
        opts=[(p[2], f"{p[1]} — {p[2]}") for p in act] or [("", "Aktif kişi yok")]
        default = get_setting("default_recipient", opts[0][0] if opts and opts[0][0] else "")
        idx=0
        for i,o in enumerate(opts):
            if o[0]==default: idx=i; break
        sel=st.selectbox("Kişi seç", opts, index=idx)
        if st.button("Kaydet"): set_setting("default_recipient", sel[0] if sel[0] else ""); st.success("Kaydedildi")

# ================== MAIN ==================
st.title("🩺 Check-up Takip Sistemi")
tab_hasta, tab_tetkik, tab_paket, tab_ozet, tab_yedek = st.tabs(
    ["🧑‍⚕️ Hastalar","🧪 Tetkik Takibi","📦 Paketler","📊 Gün Özeti","💾 Yedek"]
)

# -------- Hastalar --------
with tab_hasta:
    st.subheader(f"{sel_disp} — Hasta Listesi")
    pts=list_patients(sel_iso)
    st.table([{"ID":p[0],"Ad":p[1],"Soyad":p[2],"Cinsiyet":p[4] or "","Yaş":p[3] or "","Alarm":p[6] or "-"} for p in pts])

    st.markdown("### ➕ Hızlı Ekle")
    pkgs_all = list_packages()
    pkg_opts = [(k,n) for k,n in pkgs_all]
    with st.form("quick_add", clear_on_submit=True):
        c1,c2,c3 = st.columns([3,2,2])
        fullname=c1.text_input("Ad Soyad")
        gender=c2.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        age=c3.number_input("Yaş",0,120,0)
        st.markdown("**📦 Paket seç (isteğe bağlı)**")
        sel_pkgs = st.multiselect("Hazır paketlerden seçebilirsiniz", pkg_opts, format_func=lambda x:x[1])
        ok=st.form_submit_button("Ekle")
    if ok:
        if not fullname.strip():
            st.warning("Ad Soyad gerekli.")
        else:
            parts=fullname.split()
            fn=" ".join(parts[:-1]) if len(parts)>1 else parts[0]
            ln=parts[-1] if len(parts)>1 else "-"
            try:
                add_patient(fn, ln, int(age), gender, sel_iso)
            except sqlite3.IntegrityError:
                init_db()
                add_patient(fn, ln, int(age), gender, sel_iso)
            pts2=list_patients(sel_iso)
            new_id=max([p[0] for p in pts2]) if pts2 else None
            if new_id and sel_pkgs:
                for pid_pkg,_ in sel_pkgs: apply_package_to_patient(pid_pkg, new_id)
            st.success("Hasta eklendi" + (f" (+ {len(sel_pkgs)} paket)" if sel_pkgs else ""))
            st.rerun()

    if pts:
        st.markdown("### 🗑️ Hasta Sil")
        choice=st.selectbox("Silinecek", [(p[0], f"{p[1]} {p[2]}") for p in pts], format_func=lambda x:x[1], key="del_pt_sel")
        if st.button("Sil"): delete_patient(choice[0]); st.success("Silindi"); st.rerun()

# -------- Tetkik Takibi --------
with tab_tetkik:
    pts_today=list_patients(sel_iso)
    if not pts_today: st.info("Bu tarihte hasta yok.")
    else:
        sel=st.selectbox("Hasta", [(p[0], f"{p[1]} {p[2]}") for p in pts_today], format_func=lambda x:x[1], key="pt_for_tests")
        pid=sel[0]; prow=[p for p in pts_today if p[0]==pid][0]
        patient_name=f"{prow[1]} {prow[2]}"; visit_hhmm=prow[6]

        st.markdown("#### Tetkik Ekle")
        with st.form("add_test", clear_on_submit=True):
            tname=st.text_input("Tetkik adı")
            alarm=st.checkbox("🔔 Alarm kur (isteğe bağlı)")
            hhmm=None
            if alarm:
                colh,colm=st.columns(2)
                hour=colh.selectbox("Saat",[f"{h:02d}" for h in range(24)])
                minute=colm.selectbox("Dakika",[f"{m:02d}" for m in range(0,60,5)])
                hhmm=f"{hour}:{minute}"
            addt=st.form_submit_button("Ekle")
        if addt:
            try:
                add_patient_test(pid,tname)
                if alarm and hhmm:
                    set_patient_alarm_time(pid,hhmm)
                    st.success(f"Tetkik + alarm {hhmm}")
                else:
                    st.success("Tetkik eklendi.")
                st.rerun()
            except ValueError as e:
                st.warning(str(e))

        st.markdown("#### Paket Ata")
        pkgs=list_packages()
        if pkgs:
            cpa, cbtn = st.columns([3,1])
            pkg_sel=cpa.selectbox("Paket seç", [(k, n) for k,n in pkgs], format_func=lambda x:x[1])
            if cbtn.button("Paketi uygula"): apply_package_to_patient(pkg_sel[0], pid); st.success(f"'{pkg_sel[1]}' paketi eklendi."); st.rerun()
            with st.expander("Paket içeriği"):
                items=get_package_tests(pkg_sel[0])
                if not items: st.info("Paket boş.")
                else:
                    st.markdown('<div class="pkg-card">', unsafe_allow_html=True)
                    cols = st.columns(2)
                    half = (len(items)+1)//2
                    def _render(col, arr):
                        with col:
                            for idx,(_ptid, tname, _o) in enumerate(arr, start=1):
                                st.markdown(f'<span class="pkg-chip">{idx}. {tname}</span>', unsafe_allow_html=True)
                    _render(cols[0], items[:half]); _render(cols[1], items[half:])
                    st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("#### Tetkikler")
        trs=list_patient_tests(pid)
        if not trs: st.info("Tetkik yok.")
        else:
            done=[t[2] for t in trs if t[3]=="tamamlandi"]
            rem=[t[2] for t in trs if t[3]=="bekliyor"]
            tpl=get_setting("wa_template","Hasta: {patient} ({date})\nTamamlanan: {done}\nKalan: {remaining}")
            msg=tpl.format(patient=patient_name, date=sel_disp,
                           done=", ".join(done) if done else "-", remaining=", ".join(rem) if rem else "-")
            active_people=list_personnel(active_only=True)
            receivers=[(p[2], f"{p[1]} — {p[2]}") for p in active_people]
            default_phone=get_setting("default_recipient", receivers[0][0] if receivers else "")
            cwa,cics=st.columns([2,2])
            with cwa:
                st.markdown("**💬 WhatsApp**")
                if default_phone:
                    st.link_button("Gönder (varsayılan)", make_whatsapp_link(default_phone, msg), use_container_width=True)
                if receivers:
                    recv=st.selectbox("Başka alıcı", receivers, format_func=lambda x:x[1], key="wa_alt")
                    st.link_button("Bu kişiye gönder", make_whatsapp_link(recv[0], msg), use_container_width=True)
                    multi=st.multiselect("Çoklu alıcı", receivers, format_func=lambda x:x[1], key="wa_multi")
                    for ph,label in multi: st.link_button(f"{label}’a gönder", make_whatsapp_link(ph, msg))
                with st.popover("Mesajı kopyala"): st.code(msg, language=None)
            with cics:
                if visit_hhmm:
                    rem_min=st.selectbox("Alarm süresi", [5,10,15,30], index=1)
                    ics=build_ics(patient_name, sel_iso, visit_hhmm, remind_min=rem_min)
                    st.download_button("🔔 Takvime ekle (.ics)", data=ics,
                                       file_name=f"checkup_{patient_name.replace(' ','_')}_{sel_iso}_{visit_hhmm}.ics",
                                       mime="text/calendar")
                    st.link_button("🗓️ Google Calendar", google_calendar_link(patient_name, sel_iso, visit_hhmm))
                else:
                    st.info("Alarm saati yok. Tetkik eklerken 'Alarm kur' ile belirleyebilirsin.")
            st.divider()
            for tid,_pid,name,status,upd in trs:
                icon="✅" if status=="tamamlandi" else "⏳"
                c1,c2,c3,c4=st.columns([6,1,1,1])
                c1.markdown(f"{icon} **{name}** — {upd}")
                if status=="bekliyor":
                    if c2.button("Tamamla", key=f"done_{tid}"): update_patient_test_status(tid,"tamamlandi"); st.rerun()
                else:
                    if c3.button("Geri Al", key=f"undo_{tid}"): update_patient_test_status(tid,"bekliyor"); st.rerun()
                if c4.button("Sil", key=f"del_{tid}"): delete_patient_test(tid); st.rerun()

# -------- Paketler --------
with tab_paket:
    st.subheader("📦 Check-up Paketleri")
    pkgs=list_packages()
    col_a, col_b = st.columns([2,2])
    with col_a:
        st.markdown("### ➕ Yeni Paket")
        with st.form("pkg_new", clear_on_submit=True):
            name=st.text_input("Paket adı")
            tests_area=st.text_area("Tetkikler (her satır bir tetkik)", height=140,
                                    placeholder="Kan Tahlili\nGörüntüleme\nEKG")
            ok=st.form_submit_button("Oluştur")
        if ok:
            tests=[t.strip() for t in tests_area.splitlines() if t.strip()]
            if not name.strip() or not tests: st.warning("Paket adı ve en az 1 tetkik gerekli.")
            else:
                try: create_package(name, tests); st.success("Paket oluşturuldu"); st.rerun()
                except Exception as e: st.error(f"Hata: {e}")
        st.markdown("### ✏️ Paket Düzenle")
        if pkgs:
            sel_pkg=st.selectbox("Paket seç", [(k,n) for k,n in pkgs], format_func=lambda x:x[1], key="pkg_edit_sel")
            new_name=st.text_input("Yeni ad", value=sel_pkg[1], key="pkg_new_name")
            if st.button("Adı Güncelle"): rename_package(sel_pkg[0], new_name); st.success("Güncellendi"); st.rerun()
            st.markdown("**Paket İçeriği**")
            items=get_package_tests(sel_pkg[0])
            if items:
                st.markdown('<div class="pkg-card">', unsafe_allow_html=True)
                for idx,(pt_id,test_name,_ord) in enumerate(items, start=1):
                    cols=st.columns([6,1])
                    with cols[0]:
                        st.markdown(f'<span class="pkg-chip">{idx}. {test_name}</span>', unsafe_allow_html=True)
                    with cols[1]:
                        if st.button("Sil", key=f"pt_del_{pt_id}"): delete_test_from_package(pt_id); st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            else:
                st.info("Bu paket henüz boş.")
            st.markdown("**Tetkik Ekle (çok satırlı)**")
            with st.form("pkg_add_tests_multi", clear_on_submit=True):
                bulk=st.text_area("Her satır bir tetkik olacak şekilde giriniz", height=120,
                                  placeholder="Hemogram\nEkokardiyografi\nEfor")
                addm=st.form_submit_button("Ekle")
            if addm:
                lines=[l.strip() for l in bulk.splitlines() if l.strip()]
                if not lines: st.warning("En az bir satır girin.")
                else:
                    base_ord=len(items)
                    for i,name in enumerate(lines): add_test_to_package(sel_pkg[0], name, ord_hint=base_ord+i)
                    st.success(f"{len(lines)} tetkik eklendi."); st.rerun()
        else:
            st.info("Henüz paket yok.")
    with col_b:
        st.markdown("### 🗑️ Paket Sil")
        if pkgs:
            del_sel=st.selectbox("Silinecek paket", [(k,n) for k,n in pkgs], format_func=lambda x:x[1], key="pkg_del_sel")
            if st.button("Paketi Sil"): delete_package(del_sel[0]); st.success("Silindi"); st.rerun()
        st.markdown("### ↕️ Paket Dışa/İçe Aktar (CSV)")
        def _csv_packages():
            with closing(get_conn()) as conn, closing(conn.cursor()) as c:
                c.execute("SELECT id,name FROM packages ORDER BY id"); pk=c.fetchall()
                c.execute("SELECT package_id,test_name,ord FROM package_tests ORDER BY package_id,ord"); pt=c.fetchall()
            mem=io.StringIO(); w=csv.writer(mem)
            w.writerow(["type","id_or_package_id","name_or_test","ord"])
            for i,n in pk: w.writerow(["package", i, n, ""])
            for pid,t,o in pt: w.writerow(["item", pid, t, o])
            return mem.getvalue().encode("utf-8")
        st.download_button("Paketleri CSV İndir", _csv_packages(), "packages.csv", "text/csv")
        up=st.file_uploader("CSV Yükle (type,id/name, name/test, ord)", type=["csv"])
        if up and st.button("CSV'den Yükle"):
            txt=up.read().decode("utf-8").splitlines(); rd=csv.DictReader(txt)
            created={}
            for r in rd:
                if r["type"]=="package":
                    created[r["name_or_test"]]=create_package(r["name_or_test"], [])
                elif r["type"]=="item":
                    try: pid=int(r["id_or_package_id"])
                    except: pid=created.get(r["id_or_package_id"])
                    if pid: add_test_to_package(pid, r["name_or_test"])
            st.success("İçe aktarıldı"); st.rerun()

# -------- Gün Özeti --------
with tab_ozet:
    st.subheader(f"{sel_disp} — Gün Özeti")
    pts=list_patients(sel_iso)
    if not pts: st.info("Bu tarihte hasta yok.")
    else:
        rows=[]; total_done=total_rem=0
        for p in pts:
            tests=list_patient_tests(p[0])
            done=[f"✅ {t[2]}" for t in tests if t[3]=="tamamlandi"]
            rem=[f"⏳ {t[2]}" for t in tests if t[3]=="bekliyor"]
            total_done += len(done); total_rem += len(rem)
            rows.append({"Hasta": f"{p[1]} {p[2]}", "Tamamlanan": ", ".join(done) if done else "-", "Kalan": ", ".join(rem) if rem else "-"})
        st.table(rows)
        st.caption(f"Toplam tamamlanan: {total_done} • Kalan: {total_rem}")
        with_time=[p for p in pts if p[6] and any(t[3]=="bekliyor" for t in list_patient_tests(p[0]))]
        if with_time:
            mem=io.BytesIO()
            with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
                for p in with_time:
                    pname=f"{p[1]} {p[2]}"; ics=build_ics(pname, sel_iso, p[6])
                    z.writestr(f"{pname.replace(' ','_')}_{sel_iso}_{p[6]}.ics", ics)
            mem.seek(0)
            st.download_button("📦 Kalan tetkiki olanların randevuları (.zip)", mem,
                               file_name=f"{sel_iso}_randevular_kalan.zip", mime="application/zip")

# -------- Yedek --------
with tab_yedek:
    st.subheader("Dışa Aktar (CSV)")
    def _csv(q):
        with closing(get_conn()) as conn, closing(conn.cursor()) as c:
            c.execute(q); rows=c.fetchall(); headers=[d[0] for d in c.description]
        buf=io.StringIO(); w=csv.writer(buf); w.writerow(headers); w.writerows(rows)
        return buf.getvalue().encode("utf-8")
    c1,c2=st.columns(2)
    with c1:
        st.download_button("Hastalar CSV", _csv("SELECT * FROM patients"), "patients.csv","text/csv")
        st.download_button("Tetkikler CSV", _csv("SELECT * FROM patient_tests"), "patient_tests.csv","text/csv")
    with c2:
        st.download_button("Kişiler CSV", _csv("SELECT * FROM personnel"), "personnel.csv","text/csv")
        st.download_button("Paketler CSV (yalnızca başlıklar)", _csv("SELECT * FROM packages"), "packages_only.csv","text/csv")