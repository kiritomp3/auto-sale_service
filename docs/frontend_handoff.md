# Хендофф фронтенду: наложение одежды на модель

База API — тот же хост, что и сейчас. Авторизация маркетплейсов остаётся через `Authorization: Bearer <access_token>`, который возвращает соответствующий `/auth/.../login`.

## Генерация карточек

`POST /cards` принимает `multipart/form-data`:

| поле | тип | обязательно | описание |
|------|-----|--------------|----------|
| `image` | file | да | фото товара |
| `refinement_prompt` | string | нет | уточнение или стиль |
| `size` | string | нет | `1024x1024` \| `1024x1536` \| `1536x1024` \| `auto` |
| `marketplace` | string | нет | `wb` по умолчанию \| `ozon` |

Ответ: `{ "job_id": "...", "status": "queued" }`, дальше поллинг `GET /crads/{job_id}`.

## Наложение одежды на модель (try-on)

Сценарий: пользователь выбирает позу модели из каталога или загружает свою, добавляет фото одежды, а бэкенд переносит одежду на модель.

### Каталог моделей

`GET /tryon/models`:

```json
{
  "ok": true,
  "models": [
    {
      "id": "female_front_standing",
      "name": "Девушка, фронтально, стоя",
      "gender": "female",
      "pose": "стоя, фронтальный ракурс",
      "preview_url": "https://<host>/models/female_front_standing.jpg"
    }
  ]
}
```

Картинки поз лежат в `models/`, список описан в `models/catalog.json`.

### Запуск try-on

`POST /tryon` принимает `multipart/form-data`:

| поле | тип | обязательно | описание |
|------|-----|--------------|----------|
| `garment` | file | да | фото одежды |
| `model_id` | string | нет* | id выбранной модели из каталога |
| `model_image` | file | нет* | своё фото модели, альтернатива `model_id` |
| `prompt` | string | нет | дополнительные пожелания |
| `n_cards` | number | нет | количество изображений, 1-6 |
| `marketplace` | string | нет | `wb` по умолчанию \| `ozon` |

\* Нужно передать либо `model_id`, либо `model_image`.

Ответ: `{ "job_id": "...", "status": "queued" }`.

### Поллинг результата

`GET /tryon/{job_id}` возвращает объект job:

```json
{
  "job_id": "...",
  "status": "queued | processing | done | failed",
  "kind": "tryon",
  "images": ["output/<job_id>_tryon.png"],
  "error": "..."
}
```

Готовая картинка доступна по статике: `https://<host>/output/<job_id>_tryon.png`.

## Сводка эндпоинтов

| метод | путь | назначение |
|-------|------|------------|
| POST | `/cards` | генерация карточек |
| GET | `/crads/{job_id}` | результат генерации |
| GET | `/tryon/models` | каталог поз моделей |
| POST | `/tryon` | запустить наложение одежды |
| GET | `/tryon/{job_id}` | результат наложения |
