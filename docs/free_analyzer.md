# Private free analyzer gateway

`POST /internal/analyze` принимает multipart `image` и `X-Analyzer-Key`.
Одинаковый случайный `ANALYZER_INTERNAL_KEY` длиной 32+ символов должен быть задан в auth gateway сайта и здесь. Без него API закрыт. Пользовательские запросы проходят только через сайт, который хранит приватные результаты, применяет квоты, связывает OAuth и события воронки.

Используется существующий `container.text_client.analyze_product` (Kie Gemini vision): никаких новых AI-провайдеров. JSON ответа проверяется `Analysis` (Pydantic, strict 0–100 scores, SEO nullable, 3–7 problems/recommendations, bounded actions). Только PNG/JPEG/WebP, 10 МБ, 25 Мп. MIME соответствует декодированному формату; имя пользователя игнорируется. TemporaryDirectory удаляется при success/error. Vision concurrency 2, вызов вынесен в worker thread. Marketplace URL никогда не загружается.

Новых таблиц/Redis ключей/миграций здесь нет. Endpoint не генерирует изображения и не списывает токены; квоты находятся в существующем web auth backend. Приватные файлы не попадают в `/output`.

Проверка: `python -m unittest discover -s tests -v` (6 analyzer/bootstrap tests + 20 existing: key, decoding/MIME/size/dimensions, schema, cleanup, startup wiring, phone MPO JPEG). Реальный provider call требует `KIE_AI_API_KEY` и staging smoke-test. Парный web PR содержит endpoint inventory, все 10 events, funnel KPI, screenshots и ручной сценарий. Развернуть API перед web; автоматического deploy из feature branch нет. Revert этого PR и предыдущий образ откатывают endpoint без изменения существующих job данных.

Фотографии JPEG с дополнительным MPO кадром принимаются как JPEG: в vision отправляется первый кадр с учётом EXIF orientation, исходник сохраняется для генератора. Ошибка upstream логируется только по типу исключения, без ключей, изображения или AI-ответа.

Staging 6 октября 2026: Docker API/auth/frontend собраны и запущены в изолированной сети; upload в Kie успешен. Старый Gemini 2.5 Flash возвращал HTTP-200 JSON code=422, msg=The channel is not supported. 7 октября клиент переключён на рабочий Gemini 3.8 Flash (`/gemini-3-8-flash-openai/v1/chat/completions`) с явным model. Реальный vision на фото 12 Мп прошёл за 23 с, генерация — за 108 с; проверены сохранение результата, исходник/prompt в генераторе, списание и готовое изображение. Production не изменён.


HTTPS staging: https://staging.auto-sell.site/analyze. Сертификат Let's Encrypt и автообновление, отдельные Redis/auth/output volumes, noindex. Настоящие Google/Яндекс callback URL добавлены к существующим OAuth клиентам; production callbacks сохранены. Fixture OAuth выключен, /stage-oauth/ закрыт. Production приложение не обновлялось.

Документация канала: https://docs.kie.ai/market/gemini/gemini-3-8-flash-openai .

