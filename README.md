# Check-up Takip Sistemi

Streamlit tabanlı, çok kullanıcılı, WhatsApp uyarılı check-up takip sistemi.

## Özellikler
- Kullanıcı girişi (admin & personel)
- Hasta kaydı
- Check-up paket yönetimi
- Tetkik planlama ve durum güncelleme
- Tarih ve paket filtreli listeleme
- Excel’e aktarma
- WhatsApp bildirim entegrasyonu (Twilio Sandbox)
- 10 dk kala **otomatik** WhatsApp uyarısı (ENABLE_AUTO_NOTIF ile aç/kapa)

## Kurulum (Lokalde)
```bash
pip install -r requirements.txt
streamlit run app.py
```
> Admin girişi: kullanıcı adı `admin` / şifre `Edam456+` (Secrets ile değiştirilebilir)

## Secrets (lokal)
`./.streamlit/secrets.toml` dosyasını oluşturun:
```toml
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Edam456+"

TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_AUTH_TOKEN = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
ENABLE_AUTO_NOTIF = "true"
```

## Streamlit Cloud
- Uygulamayı GitHub'tan deploy edin.
- **Manage app → Secrets** alanına yukarıdaki TOML değerlerini girin.
- Gerekirse **ENABLE_AUTO_NOTIF="false"** yaparak otomatiği geçici kapatabilirsiniz.
