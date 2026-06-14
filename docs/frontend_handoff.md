# Хендофф фронтенду: Avito + наложение одежды на модель

Две новые фичи на бэкенде. Ниже — что нужно сделать на фронте и какие эндпоинты дёргать.

База API — тот же хост, что и сейчас. Авторизация маркетплейсов — через `Authorization: Bearer <access_token>`,
который возвращает соответствующий `/auth/.../login`.

---

## 1. Avito как маркетплейс

Avito теперь работает по той же схеме, что Ozon: логин по ключам → Bearer-сессия → запросы к API.

### 1.1. Логин

`POST /auth/avito/login`
```json
{ "avito_client_id": "...", "avito_client_secret": "..." }
```
Ответ:
```json
{ "access_token": "<bearer>", "token_type": "Bearer", "expires_in": 43200, "expires_at": "2026-06-14T..." }
```
Где взять ключи: продавец создаёт их в кабинете Avito → «Настройки» → «API» (client_id + client_secret, OAuth2 client_credentials). Бэкенд сам обменивает их на access_token Avito и обновляет его при истечении — фронту это знать не нужно.

> Сохраняйте `access_token` так же, как уже сохраняете токен Ozon. Дальше во все Avito-запросы шлите `Authorization: Bearer <access_token>`.

### 1.2. Логаут
`POST /auth/avito/logout` (с Bearer) → `{ "ok": true }`

### 1.3. Аккаунт продавца
`GET /avito/account` (Bearer) →
```json
{ "ok": true, "account": { ...сырой ответ Avito accounts/self... } }
```

### 1.4. Объявления продавца
`GET /avito/items?per_page=25&page=1&status=active` (Bearer) →
```json
{ "ok": true, "items": [ ... ], "meta": { ... } }
```

### 1.5. Генерация карточек под Avito

В существующий `POST /cards` добавлен параметр формы `marketplace`.

`POST /cards` (multipart/form-data):
| поле | тип | обязательное | описание |
|------|-----|--------------|----------|
| `image` | file | да | фото товара |
| `refinement_prompt` | string | нет | уточнение/стиль |
| `size` | string | нет | `1024x1024` \| `1024x1536` \| `1536x1024` \| `auto` |
| `marketplace` | string | нет | **`wb` (по умолчанию) \| `ozon` \| `avito`** |

→ `{ "job_id": "...", "status": "queued" }`, дальше как раньше поллинг `GET /crads/{job_id}`.

**Что сделать на фронте:** добавить переключатель маркетплейса (WB / Ozon / Avito) на экране генерации карточек и прокидывать его значение в поле `marketplace`. Под каждый маркетплейс бэкенд сам подстраивает формат карточки и текста листинга (у Avito — стиль «живого» объявления, короткий заголовок и т.д.).

---

## 2. Наложение одежды на модель (try-on)

Сценарий: пользователь выбирает **позу модели** из каталога (или загружает свою) + загружает **фото одежды** → бэкенд наносит одежду на модель.

### 2.1. Каталог поз моделей
`GET /tryon/models` →
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
**Что сделать:** показать сетку моделей по `preview_url`, выбор одной модели по `id`.

> Сами картинки поз кладутся в папку `models/` на бэкенде + запись в `models/catalog.json` (формат описан в `models/README.md`). Пока список может быть пустым — заполняется отдельно (нужны исходные фото моделей).

### 2.2. Запуск наложения
`POST /tryon` (multipart/form-data):
| поле | тип | обязательное | описание |
|------|-----|--------------|----------|
| `garment` | file | да | фото одежды |
| `model_id` | string | нет* | id выбранной модели из каталога |
| `model_image` | file | нет* | СВОЁ фото модели (альтернатива `model_id`) |
| `prompt` | string | нет | доп. пожелания (например «заправь рубашку») |

\* нужно передать **либо** `model_id`, **либо** `model_image`. Если не передано ничего — 400.

→ `{ "job_id": "...", "status": "queued" }`

### 2.3. Поллинг результата
`GET /tryon/{job_id}` → объект job:
```json
{
  "job_id": "...",
  "status": "queued | processing | done | failed",
  "kind": "tryon",
  "images": ["output/<job_id>_tryon.png"],   // появляется при status=done
  "error": "..."                              // при status=failed
}
```
Готовая картинка доступна по статике: `https://<host>/output/<job_id>_tryon.png`.

**Поведение фронта:**
1. Экран try-on: слева выбор/загрузка модели, справа загрузка фото одежды, кнопка «Примерить».
2. После `POST /tryon` поллить `GET /tryon/{job_id}` (раз в ~3–5 сек), пока `status` не `done`/`failed`.
3. При `done` — показать `images[0]` (через `/output/...`). Генерация занимает обычно десятки секунд (внешний AI-сервис).

---

## Сводка новых/изменённых эндпоинтов
| метод | путь | назначение |
|-------|------|-----------|
| POST | `/auth/avito/login` | логин Avito |
| POST | `/auth/avito/logout` | логаут Avito |
| GET | `/avito/account` | аккаунт продавца Avito |
| GET | `/avito/items` | объявления Avito |
| POST | `/cards` | **+ поле `marketplace` (wb/ozon/avito)** |
| GET | `/tryon/models` | каталог поз моделей |
| POST | `/tryon` | запустить наложение одежды |
| GET | `/tryon/{job_id}` | результат наложения |
