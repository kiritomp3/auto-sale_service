# Deploy WB Cards Service

## 1. Подготовка сервера

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
```

## 2. Клонирование проекта

```bash
git clone <YOUR_REPO_URL> wb-cards
cd wb-cards
```

## 3. Настройка переменных окружения

Создайте файл `.env`:

```env
OPENAI_API_KEY=sk-...
```

## 4. Запуск через Docker Compose

```bash
docker compose up -d --build
```

Проверка:

```bash
docker compose ps
docker compose logs -f wb-cards-api
```

Сервис будет доступен на `http://<SERVER_IP>:8000`.

## 5. Обновление версии

```bash
git pull
docker compose up -d --build
```

## 6. Smoke test

```bash
curl -X POST "http://127.0.0.1:8000/cards" \
  -F "image=@input/product.jpg" \
  -F "refinement_prompt=премиальный стиль"
```

Ответ вернет `job_id`, затем запросите результат:

```bash
curl "http://127.0.0.1:8000/crads/<job_id>"
```

