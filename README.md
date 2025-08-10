
# Check-up Takip (Streamlit + Twilio WhatsApp)

Tek dosyalık Streamlit uygulaması ve arka planda FastAPI webhook ile Twilio WhatsApp entegrasyonu.

## Hızlı Başlangıç

```bash
python -m venv .venv
# Windows
.\.venv\Scriptsctivate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

### Secrets oluştur
`.streamlit/secrets.toml` dosyasını OLUSTUR ve aşağıdaki şablona göre doldur:

```toml
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Edam456*"

TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_AUTH_TOKEN = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
WEBHOOK_HOST = "http://127.0.0.1:8000"
```

### Çalıştır
```bash
streamlit run app.py
```

Ayrı bir terminalde ngrok ile webhook'u aç:
```bash
ngrok http 8000
```

Twilio Console → WhatsApp → "When a message comes in" URL:
```
https://<ngrok-https>/twilio/whatsapp
```

### WhatsApp Komutları
- `KAYIT Ad Soyad; +905xx...; Paket; YYYY-MM-DD`
- `DURUM`
- `YAPILDI Görev`

> **Not:** Sandbox kullanıyorsanız alıcı numaranızın sandbox’a *join* atmış olması gerekir.
