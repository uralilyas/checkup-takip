import os
import sqlite3
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
        c.execute("""
        CREATE TABLE IF NOT EXISTS personnel(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            test_name TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS msg_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            personnel_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            result TEXT NOT NULL,
            info TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (personnel_id) REFERENCES personnel(id)
        )""")
init_db()

def normalize_phone(p: str) -> str:
    return p.replace(" ", "").replace("-", "")

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def add_personnel(name: str, phone: str):
    phone = normalize_phone(phone)
    if not phone.startswith("+"):
        raise ValueError("Telefon numarası + ile başlamalı (ör. +90...).")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO personnel(name, phone, created_at) VALUES(?,?,?)",
                  (name.strip(), phone.strip(), now()))

def delete_personnel(personnel_id: int):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("DELETE FROM personnel WHERE id=?", (personnel_id,))
        c.execute("DELETE FROM tests WHERE personnel_id=?", (personnel_id,))
        c.execute("DELETE FROM msg_logs WHERE personnel_id=?", (personnel_id,))

def list_personnel(active_only=True):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        if active_only:
            c.execute("SELECT id, name, phone, active FROM personnel WHERE active=1 ORDER BY name")
        else:
            c.execute("SELECT id, name, phone, active FROM personnel ORDER BY name")
        return c.fetchall()

def add_test(personnel_id: int, test_name: str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO tests(personnel_id, test_name, status, updated_at) VALUES(?,?,?,?)",
                  (personnel_id, test_name.strip(), "bekliyor", now()))

def get_tests(personnel_id: int | None = None, status: str | None = None):
    q = ("SELECT t.id, t.personnel_id, p.name, t.test_name, t.status, t.updated_at "
         "FROM tests t JOIN personnel p ON p.id=t.personnel_id")
    conds, params = [], []
    if personnel_id:
        conds.append("t.personnel_id=?"); params.append(personnel_id)
    if status:
        conds.append("t.status=?"); params.append(status)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY t.updated_at DESC"
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute(q, tuple(params))
        return c.fetchall()

def update_test_status(test_id: int, new_status: str):
    if new_status not in ("bekliyor", "tamamlandi"):
        raise ValueError("Geçersiz durum.")
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("UPDATE tests SET status=?, updated_at=? WHERE id=?",
                  (new_status, now(), test_id))

def send_whatsapp_message(to_phone: str, body: str) -> tuple[bool, str]:
    if not _twilio_ok:
        return False, "Twilio paketi yüklü değil."
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        return False, "Twilio ortam değişkenleri eksik."
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{normalize_phone(to_phone)}",
            body=body
        )
        return True, getattr(msg, "sid", "ok")
    except Exception as e:
        return False, str(e)

def log_message(personnel_id: int, body: str, ok: bool, info: str):
    with closing(get_conn()) as conn, conn, closing(conn.cursor()) as c:
        c.execute("INSERT INTO msg_logs(personnel_id, body, result, info, created_at) VALUES (?,?,?,?,?)",
                  (personnel_id, body, "ok" if ok else "hata", info[:500], now()))

def list_msg_logs(limit: int = 50):
    with closing(get_conn()) as conn, closing(conn.cursor()) as c:
        c.execute("""
        SELECT m.created_at, p.name, p.phone, m.result, m.info, m.body
        FROM msg_logs m JOIN personnel p ON p.id=m.personnel_id
        ORDER BY m.id DESC LIMIT ?""", (limit,))
        return c.fetchall()

def require_login():
    if "auth" not in st.session_state:
        st.session_state.auth = {"logged_in": False, "is_admin": False, "username": ""}
    if not st.session_state.auth["logged_in"]:
        with st.form("login_form"):
            st.subheader("🔐 Giriş")
            u = st.text_input("Kullanıcı Adı")
            p = st.text_input("Parola", type="password")
            sb = st.form_submit_button("Giriş Yap")
        if sb:
            if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                st.session_state.auth = {"logged_in": True, "is_admin": True, "username": u}
                st.success("Admin olarak giriş yapıldı.")
                st.rerun()
            else:
                st.error("Geçersiz kullanıcı adı/parola.")
        st.stop()

st.set_page_config(page_title="Check-up Takip Sistemi", page_icon="✅", layout="wide")
st.title("✅ Check-up Takip Sistemi")
st.caption("Sadece personele WhatsApp • Admin personel ekleme • Tetkik Tamamla / Geri Al")
require_login()

with st.sidebar:
    st.markdown(f"**Kullanıcı:** {st.session_state.auth['username']}")
    st.markdown("**Rol:** Admin")
    if st.button("🚪 Çıkış Yap"):
        st.session_state.auth = {"logged_in": False, "is_admin": False, "username": ""}
        st.rerun()
    st.divider()
    st.markdown("**Durum**")
    st.write("Twilio:", "✅" if _twilio_ok else "⚠️ Yüklü değil")
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        st.warning("Ortam değişkenlerini ayarlayın: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM")
    else:
        st.success("Twilio ayarları tamam.")

tab_personel, tab_tetkik, tab_mesaj, tab_kayit = st.tabs(["👥 Personel", "🧪 Tetkik", "📲 WhatsApp", "🧾 Kayıtlar"])

with tab_personel:
    st.subheader("👥 Personel Listesi")
    rows = list_personnel(active_only=False)
    if rows:
        st.dataframe(
            [{"ID": r[0], "Ad Soyad": r[1], "Telefon": r[2], "Aktif": "Evet" if r[3] else "Hayır"} for r in rows],
            use_container_width=True
        )
    else:
        st.info("Kayıtlı personel yok.")

    st.divider()
    st.markdown("### ➕ Admin: Personel Ekle")
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
                st.success(f"Eklendi: {ad}")
                st.rerun()
        except Exception as e:
            st.error(f"Hata: {e}")

    st.markdown("### 🗑️ Admin: Personel Sil")
    all_people = list_personnel(active_only=False)
    if all_people:
        choice = st.selectbox(
            "Silinecek personel",
            options=[(r[0], f"{r[1]} ({r[2]})") for r in all_people],
            format_func=lambda x: x[1] if isinstance(x, tuple) else x
        )
        if st.button("Sil", type="primary"):
            try:
                delete_personnel(choice[0])
                st.success("Personel ve ilişkili kayıtlar silindi.")
                st.rerun()
            except Exception as e:
                st.error(f"Silme hatası: {e}")
    else:
        st.caption("Silinecek personel yok.")

with tab_tetkik:
    st.subheader("🧪 Tetkik Takibi (Tamamla / Geri Al)")
    plist = list_personnel(active_only=True)
    if not plist:
        st.warning("Önce personel ekleyin.")
    else:
        pid_map = {f"{p[1]} ({p[2]})": p[0] for p in plist}
        sec_txt = st.selectbox("Personel seç", list(pid_map.keys()))
        selected_pid = pid_map.get(sec_txt)

        st.markdown("#### Tetkik Ekle")
        with st.form("tetkik_ekle", clear_on_submit=True):
            tname = st.text_input("Tetkik adı (örn. Kan Tahlili)")
            add_ok = st.form_submit_button("Ekle")
        if add_ok:
            if not tname.strip():
                st.warning("Tetkik adı boş olamaz.")
            else:
                try:
                    add_test(selected_pid, tname)
                    st.success("Tetkik eklendi.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Hata: {e}")

        st.markdown("#### Tetkikler")
        filt = st.selectbox("Duruma göre filtrele", ["Tümü", "Bekliyor", "Tamamlandı"])
        status_filter = None
        if filt == "Bekliyor":
            status_filter = "bekliyor"
        elif filt == "Tamamlandı":
            status_filter = "tamamlandi"
        trs = get_tests(personnel_id=selected_pid, status=status_filter)

        if not trs:
            st.info("Kayıt yok.")
        else:
            for (tid, _pid, pname, test_name, status, updated_at) in trs:
                cols = st.columns([5,2,2,3])
                with cols[0]:
                    st.write(f"**{test_name}** — {pname}")
                    st.caption(f"Durum: {'✅ Tamamlandı' if status=='tamamlandi' else '⏳ Bekliyor'} • Güncelleme: {updated_at}")
                with cols[1]:
                    if status == "bekliyor" and st.button("Tamamla", key=f"done_{tid}"):
                        try:
                            update_test_status(tid, "tamamlandi")
                            st.success("Tetkik tamamlandı.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Hata: {e}")
                with cols[2]:
                    if status == "tamamlandi" and st.button("Geri Al", key=f"undo_{tid}"):
                        try:
                            update_test_status(tid, "bekliyor")
                            st.info("Geri alındı (Bekliyor).")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Hata: {e}")
                with cols[3]:
                    st.empty()

with tab_mesaj:
    st.subheader("📲 WhatsApp Mesaj Gönder (Sadece Personel)")
    if not _twilio_ok:
        st.warning("Twilio paketi yüklü değil. Terminal: pip install twilio")
    active_personnel = list_personnel(active_only=True)
    if not active_personnel:
        st.info("Önce personel ekleyin.")
    else:
        multi = st.multiselect(
            "Mesaj gönderilecek personel(ler)",
            options=[(p[0], f"{p[1]} — {p[2]}") for p in active_personnel],
            format_func=lambda x: x[1] if isinstance(x, tuple) else x
        )
        st.caption("Mesajda {ad} değişkenini kullanabilirsiniz.")
        default_msg = "Merhaba {ad}, Check-up süreç bilgilendirmesidir."
        msg = st.text_area("Mesaj", value=default_msg, height=120)

        if st.button("Gönder", type="primary", disabled=len(multi)==0):
            sent_ok, sent_err = 0, 0
            for (pid, _) in multi:
                person = [p for p in active_personnel if p[0] == pid][0]
                _, name, phone, _ = person
                body = msg.replace("{ad}", name)
                ok, info = send_whatsapp_message(phone, body)
                log_message(pid, body, ok, info)
                sent_ok += 1 if ok else 0
                sent_err += 0 if ok else 1
            if sent_ok and not sent_err:
                st.success(f"{sent_ok} kişiye gönderildi.")
            elif sent_ok and sent_err:
                st.warning(f"{sent_ok} başarılı, {sent_err} hatalı.")
            else:
                st.error("Gönderim başarısız. Ayarları kontrol edin.")

with tab_kayit:
    st.subheader("🧾 Son Mesaj Kayıtları")
    logs = list_msg_logs(limit=100)
    if not logs:
        st.info("Henüz kayıt yok.")
    else:
        st.dataframe(
            [{"Zaman": r[0], "Ad Soyad": r[1], "Telefon": r[2], "Sonuç": "✅" if r[3]=='ok' else "❌", "Bilgi": r[4], "İleti": r[5]} for r in logs],
            use_container_width=True
        )
