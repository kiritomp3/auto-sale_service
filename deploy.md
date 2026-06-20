# Deploy WB Cards Service on `api.auto-sell.site`

## 1. Подготовка сервера (Ubuntu)

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin nginx certbot python3-certbot-nginx git
sudo systemctl enable --now docker
sudo systemctl enable --now nginx
```

Проверьте, что в firewall открыты `80` и `443`.

## 2. DNS для поддомена

У регистратора домена `auto-sell.site` создайте запись:

- Тип: `A`
- Имя: `api`
- Значение: `IP_ВАШЕГО_СЕРВЕРА`

Проверка (должен вернуться IP сервера):

```bash
dig +short api.auto-sell.site
```

## 3. Клонирование и настройка проекта

```bash
git clone <YOUR_REPO_URL> wbcards
cd wbcards
```

Создайте `.env`:

```env
OPENAI_API_KEY=sk-...
GENERATION_IMAGE_SIZE=1024x1536
```

## 4. Запуск API через Docker Compose

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f wb-cards-api
```

В проекте порт API проброшен только на localhost:

- `127.0.0.1:18000:8000`

Это нужно, чтобы внешние запросы шли только через Nginx.

## 5. Настройка Nginx для `api.auto-sell.site`

В репозитории уже есть готовый конфиг:

- `deploy/nginx/api.auto-sell.site.conf`

Примените его на сервере:

```bash
sudo cp deploy/nginx/api.auto-sell.site.conf /etc/nginx/sites-available/api.auto-sell.site.conf
sudo ln -sf /etc/nginx/sites-available/api.auto-sell.site.conf /etc/nginx/sites-enabled/api.auto-sell.site.conf
sudo nginx -t
sudo systemctl reload nginx
```

## 6. Выпуск SSL-сертификата (Let's Encrypt)

```bash
sudo certbot --nginx -d api.auto-sell.site
```

Проверьте автообновление:

```bash
sudo systemctl status certbot.timer
```

## 7. Проверка после деплоя

```bash
curl -I https://api.auto-sell.site/docs
```

Smoke test создания задачи:

```bash
curl -X POST "https://api.auto-sell.site/cards" \
  -F "image=@input/product.jpg" \
  -F "refinement_prompt=премиальный стиль"
```

Запрос результата по `job_id`:

```bash
curl "https://api.auto-sell.site/crads/<job_id>"
```

## 8. Обновление сервиса ()()()

```bash
git pull
docker compose up -d --build
```
