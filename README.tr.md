# Instagram Unfollower

[English](README.md) | **Türkçe**

Takip ettiğin hesapları takipçi sayısına göre sıralar, bir web sayfasında seçmeni sağlar
ve seçtiklerini yavaşça (insan hızında) takipten çıkarır.

## Kurulum ve çalıştırma (macOS)

```bash
git clone https://github.com/ferhatsanli/unfollower.git
cd unfollower
./run.sh --demo   # try the UI with fake data first (no Instagram calls)
./run.sh          # real run
```

İlk çalıştırmada `run.sh` bir `.venv` sanal ortamı oluşturur ve `requirements.txt` içindeki
kütüphaneleri kurar. Sonraki çalıştırmalarda kurulum atlanır. Python 3.10+ gerekir
(`brew install python`).

## Seçenekler

- `--demo`: arayüzü sahte veriyle açar, hiçbir şey göndermez.
- `--refresh`: kayıtlı takipçi sayılarını silip baştan sorgular.

## Oluşan dosyalar

- `session.json`: Instagram oturumu (şifre değil). Kimseyle paylaşma.
- `following_cache.json`: takipçi sayıları önbelleği.
- `following_sorted.csv`: takipçi sayısına göre azalan liste.
- `to_unfollow.txt`: sayfada seçip kaydettiğin hesaplar.
- `unfollowed_log.txt`: takipten çıkılanlar; tekrar çalıştırınca kaldığı yerden devam eder.

Bunların hepsi `.gitignore` içinde.

Ctrl+C ile istediğin an durdurabilirsin. Bir çalıştırmada en fazla 150 hesaptan çıkılır;
fazlasını ertesi gün tekrar çalıştırarak tamamla.

## Uyarı

Bu araç Instagram'ın resmî olmayan private API'sini
[instagrapi](https://github.com/subzeroid/instagrapi) üzerinden kullanır. Hesap otomasyonu
Instagram kurallarına aykırıdır; rastgele beklemeler engellenme riskini azaltır ama
sıfırlamaz. Kullanım sorumluluğu sana aittir.
