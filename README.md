# 📚 AI Light Novel & Web Novel Translator (Bahasa Indonesia)

Aplikasi web berbasis Python (Flask) yang dirancang khusus untuk menerjemahkan **Light Novel (Jepang)** dan **Web Novel (Inggris/Mandarin)** ke dalam Bahasa Indonesia yang alami, luwes, dan emosional layaknya novel terjemahan resmi.

## ✨ Fitur Utama

- **Multi-AI Provider & Auto-Fallback:** Mendukung **Google Gemini**, **DeepSeek**, **Groq**, dan **OpenRouter**. Jika salah satu API mengalami limit kuota (`429`) atau server sibuk (`503`), sistem otomatis beralih ke provider cadangan yang tersedia.
- **Web Scraper Bab Otomatis:** Cukup masukkan URL bab novel dari situs baca online, aplikasi akan mengekstrak teks bersih (membuang iklan/navigasi) sekaligus mendeteksi tautan *Next Chapter* secara otomatis.
- **Dukungan File EPUB & TXT:** Unggah file `.epub` atau `.txt` utuh untuk diterjemahkan per bab secara paralel di latar belakang (*background job*) dan unduh hasilnya kembali dalam format `.epub` atau `.txt`.
- **Manajemen Glosarium (JSON):** Pertahankan konsistensi nama karakter, jurus, lokasi, dan aturan honorifik (`-san`, `-kun`, `Senpai`, dll.) menggunakan sistem referensi glosarium yang dapat diedit, diunduh, dan diunggah.
- **Keamanan & Proteksi:** Dilengkapi fitur proteksi kata sandi opsional (`APP_PASSWORD`) serta validasi keamanan URL (anti-SSRF).

---

## 🛠️ Teknologi yang Digunakan

- **Backend:** Python, Flask
- **AI SDK & API:** Google GenAI SDK, REST API (DeepSeek, Groq, OpenRouter)
- **Web Scraping & Parsing:** BeautifulSoup4, `readability-lxml`, `ebooklib`, `requests`

---

## 🚀 Cara Instalasi & Menjalankan di Lokal

1. **Clone repository ini:**
   ```bash
   git clone [https://github.com/username-anda/nama-repo-anda.git](https://github.com/username-anda/nama-repo-anda.git)
   cd nama-repo-anda
