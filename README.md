# MEXC MA Cross Notifier

Script sederhana untuk memantau pasangan futures di MEXC dengan timeframe
5 menit. Bot akan mengirim pesan Telegram ketika MA5 memotong ke bawah MA10.
Tidak ada eksekusi order apa pun.

## Penggunaan

1. Salin `.env.example` menjadi `.env` lalu isi `TELEGRAM_BOT_TOKEN` dan
   `TELEGRAM_CHAT_ID`.
2. Jalankan bot:

```bash
python mexc.py --symbol BTC_USDT
```

Parameter tambahan:

- `--poll-seconds` : interval polling data (default `10`).

Jalankan instansi terpisah untuk setiap pasangan yang ingin dipantau.

