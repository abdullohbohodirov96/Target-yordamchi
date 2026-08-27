# Dockerfile — IXTIYORIY, hozircha render.yaml'da ISHLATILMAYDI.
#
# Nega qo'shildi: 2026-08 audio-tahlil audit so'rovida foydalanuvchi
# ffmpeg/ffprobe orqali audio metadata aniqlash va stereo (2-kanalli)
# yozuvlarni kanal bo'yicha ajratishni so'radi. Bu funksiyalar
# `call_analysis.py`da ALLAQACHON qo'shilgan VA to'liq himoyalangan
# (`shutil.which()` orqali) -- ffmpeg/ffprobe mavjud bo'lmasa, ular
# JIM o'tkazib yuboriladi, butun tahlil (transkripsiya+baholash)
# baribir ishlayveradi.
#
# LEKIN: joriy `render.yaml` `runtime: python` (native buildpack)
# ishlatadi -- bu muhitda apt-get/tizim paketi o'rnatish IMKONSIZ,
# demak ffmpeg/ffprobe PRODUCTION'da odatda MAVJUD EMAS. Agar
# haqiqatda audio-metadata va stereo-kanal ajratishni PRODUCTION'da
# ISHLATMOQCHI bo'lsangiz -- shu Dockerfile'dan foydalanib
# `render.yaml`da `runtime: python`ni `runtime: docker`ga
# o'zgartirish kerak.
#
# BU O'ZGARISH ATAYLAB AVTOMATIK QILINMADI (faqat shu fayl tayyorlab
# qo'yildi) -- bu deploy runtime'ini o'zgartiruvchi infratuzilma
# qarori, va uni faqat siz aniq tasdiqlaganingizdan keyin qo'llash
# kerak deb topildi. Agar xohlasangiz -- render.yaml'dagi runtime
# qatorini o'zgartirib qo'yaman, buni faqat siz so'raganingizda
# qilaman.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./

EXPOSE 10000

CMD ["gunicorn", "wsgi:application", "--workers", "1", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:10000"]
