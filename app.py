import os, sqlite3
from datetime import datetime
from contextlib import closing
import streamlit as st

DB_PATH = "checkup.db"
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

_twilio_ok = True
try:
    from twilio.rest import Client
except Exception:
    _twilio_ok = False

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS patients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            age INTEGER,
            gender TEXT CHECK(gender IN ('Kadın','Erkek','Diğer')) DEFAULT 'Diğer',
            created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS patient_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,        -- bekliyor | tamamlandi
            updated_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,        -- ok | hata
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id))""")
init_db()

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def normalize_phone(p:str)->str: return p.replace(" ","").replace("-","")

# --- Personnel (for WhatsApp to staff only) ---
def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        q = "SELECT id,name,phone,active FROM personnel"
        if active_only: q += " WHERE active=1"
        q += " ORDER BY name"
        c.execute(q)
        return c.fetchall()

def add_personnel(name:str, phone:str):
    phone = normalize_phone(phone)
    if not phone.startswith("+"):
        raise ValueError("Telefon numarası + ile başlamalı (ör. +90...)")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO personnel(name,phone,created_at) VALUES(?,?,?)",
                  (name.strip(), phone.strip(), now()))

def delete_personnel(personnel_id:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM personnel WHERE id=?", (personnel_id,))
        c.execute("DELETE FROM msg_logs WHERE personnel_id=?", (personnel_id,))

# --- Patients ---
def add_patient(first_name:str,last_name:str,age:int|None,gender:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO patients(first_name,last_name,age,gender,created_at) VALUES(?,?,?,?,?)",
                  (first_name.strip(), last_name.strip(), age, gender, now()))

def delete_patient(patient_id:int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM patient_tests WHERE patient_id=?", (patient_id,))
        c.execute("DELETE FROM patients WHERE id=?", (patient_id,))

def list_patients():
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("SELECT id, first_name, last_name, age, gender FROM patients ORDER BY last_name, first_name")
        return c.fetchall()

# --- Patient Tests ---
def add_patient_test(patient_id:int, test_name:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO patient_tests(patient_id,test_name,status,updated_at) VALUES(?,?,?,?)",
                  (patient_id, test_name.strip(), "bekliyor", now()))

def list_patient_tests(patient_id:int|None=None, status:str|None=None):
    q = ("SELECT t.id, t.patient_id, p.first_name, p.last_name, t.test_name, t.status, t.updated_at "
         "FROM patient_tests t JOIN patients p ON p.id=t.patient_id")
    conds, params = [], []
    if patient_id: conds.append("t.patient_id=?"); params.append(patient_id)
    if status: conds.append("t.status=?"); params.append(status)
    if conds: q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY t.updated_at DESC"
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute(q, tuple(params)); return c.fetchall()

def update_patient_test_status(test_id:int, new_status:str):
    if new_status not in ("bekliyor","tamamlandi"):
        raise ValueError("Geçersiz durum.")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE patient_tests SET status=?, updated_at=? WHERE id=?",
                  (new_status, now(), test_id))

# --- WhatsApp (to staff only) ---
def send_whatsapp_message(to_phone:str, body:str)->tuple[bool,str]:
    if not _twilio_ok: return False, "Twilio paketi yüklü değil."
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        return False, "Twilio ortam değişkenleri eksik."
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{normalize_phone(to_phone)}",
            body=body
        )
        return True, getattr(msg,"sid","ok")
    except Exception as e:
        return False, str(e)

def log_message(personnel_id:int, body:str, ok:bool, info:str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO msg_logs(personnel_id,body,result,info,created_at) VALUES(?,?,?,?,?)",
                  (personnel_id, body, "ok" if ok else "hata", info[:500], now()))

def list_msg_logs(limit:int=100):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("""SELECT m.created_at, p.name, p.phone, m.result, m.info, m.body
                     FROM msg_logs m JOIN personnel p ON p.id=m.personnel_id
                     ORDER BY m.id DESC LIMIT ?""", (limit,))
        return c.fetchall()

# --- Auth ---
def require_login():
    if "auth" not in st.session_state:
        st.session_state.auth = {"logged_in": False, "is_admin": False, "username": ""}
    if not st.session_state.auth["logged_in"]:
        with st.form("login_form"):
            st.subheader("🔐 Giriş")
            u = st.text_input("Kullanıcı Adı")
            p = st.text_input("Parola", type="password")
            if st.form_submit_button("Giriş Yap"):
                if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                    st.session_state.auth = {"logged_in": True, "is_admin": True, "username": u}
                    st.success("Admin olarak giriş yapıldı.")
                    st.rerun()
                else:
                    st.error("Geçersiz kullanıcı adı/parola.")
        st.stop()

# --- UI ---
st.set_page_config(page_title="Check-up Takip Sistemi", page_icon="✅", layout="wide")
st.title("✅ Check-up Takip Sistemi")
st.caption("Hasta check-up takibi • Tetkik Tamamla/Geri Al • WhatsApp yalnızca personele")
require_login()

with st.sidebar:
    st.markdown(f"**Kullanıcı:** {st.session_state.auth['username']}")
    st.markdown("**Rol:** Admin")
    if st.button("🚪 Çıkış Yap"):
        st.session_state.auth = {"logged_in": False, "is_admin": False, "username": ""}
        st.rerun()
    st.divider()
    st.markdown("**Sistem**")
    st.write("Twilio:", "✅" if _twilio_ok else "⚠️ Yüklü değil")
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        st.warning("Ortam değişkenleri: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM")
    else:
        st.success("Twilio ayarları tamam.")

tab_hasta, tab_tetkik, tab_mesaj, tab_kayit, tab_personel = st.tabs([
    "🧑‍⚕️ Hastalar", "🧪 Tetkik Takibi", "📲 WhatsApp Mesaj (Personel)", "🧾 Mesaj Kayıtları", "👥 Personel"
])

# --- HASTALAR ---
with tab_hasta:
    st.subheader("🧑‍⚕️ Hasta Listesi")
    pts = list_patients()
    if pts:
        st.dataframe(
            [{"ID":p[0], "Ad":p[1], "Soyad":p[2], "Yaş":p[3], "Cinsiyet":p[4]} for p in pts],
            use_container_width=True
        )
    else:
        st.info("Kayıtlı hasta yok.")

    st.divider()
    st.markdown("### ➕ Hasta Ekle (minimal veri)")
    with st.form("hasta_ekle", clear_on_submit=True):
        col1, col2, col3, col4 = st.columns([2,2,1,1])
        with col1: fn = st.text_input("Ad")
        with col2: ln = st.text_input("Soyad")
        with col3: age = st.number_input("Yaş", min_value=0, max_value=120, value=0, step=1)
        with col4: gender = st.selectbox("Cinsiyet", ["Kadın","Erkek","Diğer"])
        add_ok = st.form_submit_button("Ekle")
    if add_ok:
        try:
            if not fn.strip() or not ln.strip():
                st.warning("Ad ve Soyad zorunludur.")
            else:
                add_patient(fn, ln, int(age) if age else None, gender)
                st.success(f"Hasta eklendi: {fn} {ln}")
                st.rerun()
        except Exception as e:
            st.error(f"Hata: {e}")

    st.markdown("### 🗑️ Hasta Sil")
    if pts:
        sel = st.selectbox("Silinecek hasta", options=[(p[0], f"{p[1]} {p[2]} (#{p[0]})") for p in pts],
                           format_func=lambda x: x[1] if isinstance(x, tuple) else x)
        if st.button("Sil", type="primary"):
            try:
                delete_patient(sel[0])
                st.success("Hasta ve tetkikleri silindi.")
                st.rerun()
            except Exception as e:
                st.error(f"Silme hatası: {e}")
    else:
        st.caption("Silinecek hasta yok.")

# --- TETKİK (HASTAYA BAĞLI) ---
with tab_tetkik:
    st.subheader("🧪 Tetkik Takibi (Hasta Bazlı)")
    pts = list_patients()
    if not pts:
        st.warning("Önce hasta ekleyin.")
    else:
        pmap = {f"{p[1]} {p[2]} — (#{p[0]})": p[0] for p in pts}
        sel_name = st.selectbox("Hasta seç", list(pmap.keys()))
        pid = pmap[sel_name]

        st.markdown("#### Tetkik Ekle")
        with st.form("tetkik_ekle", clear_on_submit=True):
            tname = st.text_input("Tetkik adı (örn. MR, Kan Tahlili)")
            sb = st.form_submit_button("Ekle")
        if sb:
            if not tname.strip():
                st.warning("Tetkik adı boş olamaz.")
            else:
                try:
                    add_patient_test(pid, tname)
                    st.success("Tetkik eklendi.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Hata: {e}")

        st.markdown("#### Tetkikler")
        filt = st.selectbox("Durum", ["Tümü","Bekliyor","Tamamlandı"])
        status = {"Tümü":None,"Bekliyor":"bekliyor","Tamamlandı":"tamamlandi"}[filt]
        trs = list_patient_tests(patient_id=pid, status=status)
        if not trs:
            st.info("Kayıt yok.")
        else:
            for (tid, _pid, fn, ln, test_name, status, updated_at) in trs:
                c = st.columns([5,2,2,3])
                with c[0]:
                    st.write(f"**{test_name}** — {fn} {ln}")
                    st.caption(f"Durum: {'✅ Tamamlandı' if status=='tamamlandi' else '⏳ Bekliyor'} • Güncelleme: {updated_at}")
                with c[1]:
                    if status == "bekliyor" and st.button("Tamamla", key=f"done_{tid}"):
                        try:
                            update_patient_test_status(tid, "tamamlandi")
                            st.success("Tetkik tamamlandı.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Hata: {e}")
                with c[2]:
                    if status == "tamamlandi" and st.button("Geri Al", key=f"undo_{tid}"):
                        try:
                            update_patient_test_status(tid, "bekliyor")
                            st.info("Geri alındı (Bekliyor).")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Hata: {e}")
                with c[3]:
                    st.empty()

# --- WHATSAPP (PERSONEL) ---
with tab_mesaj:
    st.subheader("📲 WhatsApp Mesaj Gönder (Sadece Personel)")
    if not _twilio_ok:
        st.warning("Twilio paketi yüklü değil. Terminal: pip install twilio")
    staff = list_personnel(active_only=True)
    if not staff:
        st.info("Önce personel ekleyin (👥 Personel sekmesi).")
    else:
        multi = st.multiselect(
            "Mesaj gönderilecek personel(ler)",
            options=[(p[0], f"{p[1]} — {p[2]}") for p in staff],
            format_func=lambda x: x[1] if isinstance(x, tuple) else x
        )
        st.caption("Mesajda {ad} değişkenini kullanabilirsiniz (ör. 'Merhaba {ad}').")
        default_msg = "Merhaba {ad}, Check-up süreç bilgilendirmesidir."
        msg = st.text_area("Mesaj", value=default_msg, height=120)
        if st.button("Gönder", type="primary", disabled=len(multi)==0):
            okc, errc = 0, 0
            for (pid, _) in multi:
                person = [p for p in staff if p[0]==pid][0]
                _, name, phone, _ = person
                body = msg.replace("{ad}", name)
                ok, info = send_whatsapp_message(phone, body)
                log_message(pid, body, ok, info)
                okc += 1 if ok else 0
                errc += 0 if ok else 1
            if okc and not errc: st.success(f"{okc} kişiye gönderildi.")
            elif okc and errc:   st.warning(f"{okc} başarılı, {errc} hatalı.")
            else:                st.error("Gönderim başarısız. Ayarları kontrol edin.")

# --- MESAJ KAYITLARI ---
with tab_kayit:
    st.subheader("🧾 Son Mesaj Kayıtları (Personel)")
    logs = list_msg_logs(limit=100)
    if not logs:
        st.info("Henüz kayıt yok.")
    else:
        st.dataframe(
            [{"Zaman":r[0],"Ad Soyad":r[1],"Telefon":r[2],
              "Sonuç":"✅" if r[3]=='ok' else "❌","Bilgi":r[4],"İleti":r[5]} for r in logs],
            use_container_width=True
        )

# --- PERSONEL (YÖNETİM) ---
with tab_personel:
    st.subheader("👥 Personel Listesi")
    rows = list_personnel(active_only=False)
    if rows:
        st.dataframe(
            [{"ID": r[0], "Ad Soyad": r[1], "Telefon": r[2], "Aktif": "Evet" if r[3] else "Hayır"} for r in rows],
            use_container_width=True
        )
    else:
        st.info("Kayıtlı personel bulunamadı.")

    st.divider()
    st.markdown("### ➕ Personel Ekle")
    with st.form("personel_ekle", clear_on_submit=True):
        ad = st.text_input("Ad Soyad")
        tel = st.text_input("Telefon (+90...)")
        submitted = st.form_submit_button("Ekle")
    if submitted:
        try:
            if not ad.strip() or not tel.strip():
                st.warning("Ad ve telefon zorunludur.")
            else:
                add_personnel(ad, tel)
                st.success(f"Personel eklendi: {ad}")
                st.rerun()
        except Exception as e:
            st.error(f"Personel eklenemedi: {e}")

    st.markdown("### 🗑️ Personel Sil")
    all_people = list_personnel(active_only=False)
    if all_people:
        choice = st.selectbox("Silinecek personel",
                              options=[(r[0], f"{r[1]} ({r[2]})") for r in all_people],
                              format_func=lambda x: x[1] if isinstance(x, tuple) else x)
        if st.button("Sil", type="primary"):
            try:
                delete_personnel(choice[0])
                st.success("Personel ve ilişkili mesaj kayıtları silindi.")
                st.rerun()
            except Exception as e:
                st.error(f"Silme hatası: {e}")
    else:
        st.caption("Silinecek personel yok.")
