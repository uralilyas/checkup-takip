# app.py — DB'siz geçici sürüm (sadece çalışırlık)
import os
from datetime import date
import streamlit as st
from twilio.rest import Client

# ---- Secrets / Config ----
def S(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)

ADMIN_USERNAME = S("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = S("ADMIN_PASSWORD", "changeme")
TWILIO_SID    = S("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN  = S("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM   = S("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
WEBHOOK_HOST  = S("WEBHOOK_HOST", "")

st.set_page_config(page_title="Check-up Takip (Geçici - DB Kapalı)", page_icon="✅", layout="wide")

# ---- Auth ----
def ensure_auth():
    if "ok" not in st.session_state: st.session_state.ok = False
    if st.session_state.ok:
        return True
    with st.sidebar:
        st.subheader("🔐 Giriş")
        u = st.text_input("Kullanıcı adı")
        p = st.text_input("Parola", type="password")
        if st.button("Giriş"):
            if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
                st.session_state.ok = True
                st.rerun()
            else:
                st.error("Hatalı bilgiler")
    return st.session_state.ok

if not ensure_auth():
    st.stop()

# ---- In-memory (oturumluk) veri yapısı ----
if "records" not in st.session_state:
    st.session_state.records = []  # [{name, phone, pkg, cdate, tasks:[{title,done}]}]

# ---- WhatsApp gönderimi ----
def send_whatsapp(to_phone: str, body: str):
    try:
        if not to_phone.startswith("whatsapp:"):
            to_phone = f"whatsapp:{to_phone}"
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(from_=TWILIO_FROM, to=to_phone, body=body)
        return True
    except Exception as e:
        st.error(f"Twilio gönderim hatası: {e}")
        return False

# ---- UI ----
st.title("✅ Check-up Takip (Geçici Sürüm – Veritabanı KAPALI)")
st.caption("Bu ekran DB'ye BAĞLANMADAN çalışır. Kayıtlar sadece bu oturum boyunca tutulur.")
with st.sidebar:
    st.markdown(f"**Webhook (Twilio):** `{WEBHOOK_HOST}/twilio/whatsapp`")

# Yeni kayıt formu
st.subheader("📝 Yeni Check-up Kaydı (oturumda saklanır)")
with st.form("new"):
    name  = st.text_input("Ad Soyad")
    phone = st.text_input("Telefon (+90...)")
    pkg   = st.text_input("Paket", value="Standart")
    cdate = st.date_input("Tarih", value=date.today())
    tasks_raw = st.text_area("Görevler (her satır bir görev)",
                             "Kan Tahlili\nEKG\nRadyoloji (Akciğer)\nVücut Analizi\nSon Doktor Değerlendirmesi")
    if st.form_submit_button("Kaydı Ekle"):
        if not (name and phone):
            st.warning("Ad ve telefon zorunlu.")
        else:
            tasks = [{"title": t.strip(), "done": False} for t in tasks_raw.splitlines() if t.strip()]
            st.session_state.records.append({
                "name": name, "phone": phone, "pkg": pkg, "cdate": cdate, "tasks": tasks
            })
            st.success(f"Kayıt eklendi: {name} • {pkg} • {cdate}")

# Bugünün listesi
st.subheader("📆 Bugünün Check-up Listesi (oturum)")
if not st.session_state.records:
    st.info("Henüz kayıt yok.")
else:
    for idx, rec in enumerate(st.session_state.records):
        pending = [t for t in rec["tasks"] if not t["done"]]
        done    = [t for t in rec["tasks"] if t["done"]]
        with st.expander(f"{rec['name']} • {rec['pkg']} • {rec['cdate']} • {rec['phone']}"):
            # görevler
            for j, t in enumerate(rec["tasks"]):
                col1, col2 = st.columns([6,2])
                with col1:
                    st.write(("✅ " if t["done"] else "⏳ ") + t["title"])
                with col2:
                    if not t["done"] and st.button("Tamamla", key=f"done_{idx}_{j}"):
                        t["done"] = True
                        st.rerun()

            # WhatsApp ile durum gönder
            if st.button("Durumu WhatsApp ile Gönder", key=f"msg_{idx}"):
                body = "Check-up Durumunuz:\n"
                body += "- Bekleyen: " + (", ".join([t['title'] for t in pending]) if pending else "Yok") + "\n"
                body += "- Tamamlanan: " + (", ".join([t['title'] for t in done   ]) if done    else "Yok")
                ok = send_whatsapp(rec["phone"], body)
                st.success("WhatsApp gönderildi.") if ok else st.error("Gönderilemedi.")

st.divider()
st.caption("Geçici sürüm: veriler kalıcı değildir. DB açıldığında otomatik olarak kalıcıya geçeceğiz.")
