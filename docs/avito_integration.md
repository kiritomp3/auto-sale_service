# Avito Integration

Avito is a separate marketplace. Use `marketplace=avito` in `POST /cards` or `POST /tryon` to generate Avito-oriented content.

Required UI warning:

> Авито ограничивает публикацию через API: 1 объявление в час. Мы поставим объявления в очередь и будем публиковать их автоматически по расписанию.

## Account

`POST /auth/avito/login`

```json
{
  "avito_account_id": "account-1",
  "avito_access_token": "official-avito-access-token",
  "avito_refresh_token": "",
  "account_name": "Основной аккаунт"
}
```

The response returns a local Bearer token. Queue operations use this token and operate only on that connected Avito account.

## Validate

`POST /avito/listings/validate`

```json
{
  "title": "Куртка зимняя",
  "description": "Теплая зимняя куртка, новая, размер M.",
  "price": 5900,
  "category": "Одежда",
  "location": "Москва",
  "images": ["https://example.com/photo.jpg"],
  "attributes": {"condition": "new"},
  "listing_content": {}
}
```

The response includes `ok`, `errors`, `warnings`, and `normalized_item`.

## Queue

`POST /avito/queue`

```json
{
  "items": [
    {
      "title": "Куртка зимняя",
      "description": "Теплая зимняя куртка, новая, размер M.",
      "price": 5900,
      "category": "Одежда",
      "location": "Москва",
      "images": ["https://example.com/photo.jpg"]
    }
  ]
}
```

The response returns the account schedule with `status`, `position`, `estimated_publish_at`, `attempts`, and `error`.

Statuses: `queued`, `scheduled`, `publishing`, `published`, `failed`, `cancelled`.

## Schedule And Actions

| method | path | purpose |
|---|---|---|
| GET | `/avito/account` | Current connected Avito account |
| GET | `/avito/schedule` | Account publication schedule |
| GET | `/avito/queue/{listing_id}` | One listing status |
| POST | `/avito/queue/reorder` | Manual order change; body: `{ "listing_ids": ["..."] }` |
| POST | `/avito/queue/{listing_id}/cancel` | Cancel if not published or publishing |
| POST | `/avito/queue/{listing_id}/retry` | Restart a `failed` listing |
| GET | `/avito/export?format=csv` | CSV export |
| GET | `/avito/export?format=xlsx` | XLSX export |
| GET | `/avito/export?format=xml` | XML export |

The service does not distribute listings across accounts, use proxies, emulate manual actions, or provide any other bypass mechanics. The scheduler waits at least 3700 seconds between API publication attempts for each connected Avito account, giving a small safety buffer above Avito's one-publication-per-hour limit.
