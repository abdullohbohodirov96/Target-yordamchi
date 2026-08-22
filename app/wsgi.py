"""wsgi.py — gunicorn shu faylni ishga tushiradi: `gunicorn wsgi:application`.
`create_app()` bazani (jadvallarni) tayyorlaydi va fon scheduler'ni ishga
tushiradi -- ilova birinchi so'rovni kutmasdan ham ishlashi kerak."""

from app import create_app

application = create_app()
