# Dockerfile — 2026-08, foydalanuvchi ANIQ so'ragan: production'da
# ffmpeg/ffprobe HAQIQATDA mavjud bo'lishi kerak (audio metadata +
# stereo-kanal ajratish uchun). `render.yaml` endi shu Dockerfile
# orqali `runtime: docker` bilan deploy qilinadi.
#
# MUHIM: `--workers 1` SAQLANGAN (o'zgartirilmagan) -- APScheduler
# (kunlik hisobot, soatlik audit, byudjet nazorati, AI-tahlil navbati)
# va Telegram bot holati BITTA jarayonda saqlanishi kerak; agar
# workers>1 bo'lsa, har bir worker ALOHIDA scheduler nusxasini ishga
# tushirib, vazifalar TAKRORLANIB bajarilardi (masalan bitta hisobot
# 2-4 marta yuborilishi mumkin edi).

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./

EXPOSE 10000

# Shell-shakl (array emas) -- Render beradigan $PORT o'zgaruvchisini
# ishlatish uchun ATAYLAB shunday yozilgan (agar $PORT berilmasa, 10000
# standart sifatida ishlatiladi).
CMD gunicorn wsgi:application --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-10000}
