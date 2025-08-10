import streamlit as st

# Sayfa ayarı
st.set_page_config(page_title="Check-up Takip - Test", layout="wide")

# Başlık
st.title("✅ Check-up Takip Uygulaması")
st.write("Bu bir test sürümüdür. Uygulama çalışıyor ve Streamlit sayfası yükleniyor.")

# Test butonu
if st.button("Test Mesajı Gönder"):
    st.success("Butona tıklandı! Streamlit doğru çalışıyor.")

# Alt bilgi
st.caption("Versiyon: Test-1.0 | Bu sayfa sadece çalışırlığı test etmek için hazırlandı.")
