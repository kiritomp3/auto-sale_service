from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher


MARKETPLACE_OUTPUT_IDS: dict[str, str] = {
    "wb": "wildberries",
    "ozon": "ozon",
    "avito": "avito",
    "yandex_market": "yandex_market",
    "megamarket": "megamarket",
}

MARKETPLACE_ALIASES: dict[str, str] = {
    "wb": "wb",
    "wildberries": "wb",
    "wild_berries": "wb",
    "ozon": "ozon",
    "авито": "avito",
    "avito": "avito",
    "yandex": "yandex_market",
    "yandexmarket": "yandex_market",
    "yandex_market": "yandex_market",
    "yandex_marketplace": "yandex_market",
    "ymarket": "yandex_market",
    "яндекс": "yandex_market",
    "яндекс_маркет": "yandex_market",
    "mega": "megamarket",
    "mega_market": "megamarket",
    "megamarket": "megamarket",
    "sber": "megamarket",
    "sbermarket": "megamarket",
    "sbermegamarket": "megamarket",
    "sber_megamarket": "megamarket",
    "sber_mega_market": "megamarket",
    "мегамаркет": "megamarket",
}

ALL_MARKETPLACE_TARGETS: tuple[str, ...] = ("wb", "ozon", "avito", "yandex_market", "megamarket")

MARKETPLACE_CATEGORY_OPTIONS: dict[str, tuple[str, ...]] = {
    "wb": (
        "Женщинам",
        "Мужчинам",
        "Детям",
        "Обувь",
        "Аксессуары",
        "Электроника",
        "Бытовая техника",
        "Дом",
        "Красота",
        "Спорт",
        "Игрушки",
        "Книги",
        "Канцтовары",
        "Автотовары",
        "Продукты",
        "Зоотовары",
        "Сад и дача",
        "Строительство и ремонт",
        "Мебель",
        "Ювелирные изделия",
        "Товары для взрослых",
    ),
    "ozon": (
        "Электроника",
        "Одежда, обувь и аксессуары",
        "Дом и сад",
        "Детские товары",
        "Красота и здоровье",
        "Спорт и отдых",
        "Автотовары",
        "Бытовая техника",
        "Книги",
        "Зоотовары",
        "Продукты питания",
        "Мебель",
        "Строительство и ремонт",
        "Аптека",
        "Хобби и творчество",
        "Канцтовары",
        "Ювелирные изделия",
        "Товары для взрослых",
    ),
    "avito": (
        "Транспорт",
        "Недвижимость",
        "Работа",
        "Услуги",
        "Личные вещи",
        "Для дома и дачи",
        "Запчасти и аксессуары",
        "Электроника",
        "Хобби и отдых",
        "Животные",
        "Для бизнеса",
    ),
    "yandex_market": (
        "Электроника",
        "Бытовая техника",
        "Компьютеры",
        "Одежда, обувь и аксессуары",
        "Красота",
        "Детские товары",
        "Дом",
        "Дача и сад",
        "Спорт и отдых",
        "Авто",
        "Зоотовары",
        "Продукты",
        "Книги",
        "Аптека",
        "Строительство и ремонт",
        "Мебель",
        "Хобби и творчество",
        "Канцтовары",
        "Товары для взрослых",
    ),
    "megamarket": (
        "Электроника",
        "Бытовая техника",
        "Одежда, обувь и аксессуары",
        "Дом и дача",
        "Детские товары",
        "Красота и здоровье",
        "Спорт и отдых",
        "Авто",
        "Зоотовары",
        "Продукты",
        "Книги",
        "Мебель",
        "Строительство и ремонт",
        "Хобби и творчество",
        "Канцелярия",
        "Товары для взрослых",
    ),
}

MARKETPLACE_FIXED_VALUE_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "ozon": {
        "vat": ("0", "0.05", "0.07", "0.1", "0.2"),
        "currency_code": ("RUB",),
        "dimension_unit": ("mm", "cm", "in"),
        "weight_unit": ("g", "kg", "lb"),
    },
    "avito": {
        "condition": ("Новое", "Б/у"),
    },
    "yandex_market": {
        "currencyId": ("RUR",),
    },
    "megamarket": {
        "currency": ("RUR",),
    },
}

FIXED_VALUE_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "condition": ("condition", "item_condition", "status", "состояние"),
    "vat": ("vat", "nds", "ндс"),
    "currency": ("currency", "currency_id", "currencyId", "currency_code", "currencyCode", "валюта"),
    "currencyId": ("currencyId", "currency_id", "currency", "валюта"),
    "currency_code": ("currency_code", "currencyCode", "currency", "валюта"),
    "dimension_unit": ("dimension_unit", "dimensionUnit", "unit_dimension", "единица_габаритов"),
    "weight_unit": ("weight_unit", "weightUnit", "unit_weight", "единица_веса"),
}

FIXED_VALUE_HINTS: dict[str, tuple[str, ...]] = {
    "Новое": ("новое", "новый", "new", "не использовался", "без эксплуатации"),
    "Б/у": ("б/у", "бу", "used", "подержанный", "с пробегом", "после использования"),
    "0": ("0", "без ндс", "ндс 0", "не облагается", "none"),
    "0.05": ("5%", "5", "0.05", "ндс 5"),
    "0.07": ("7%", "7", "0.07", "ндс 7"),
    "0.1": ("10%", "10", "0.1", "ндс 10"),
    "0.2": ("20%", "20", "0.2", "ндс 20"),
    "RUB": ("rub", "rur", "руб", "рубль", "рубли", "₽"),
    "RUR": ("rub", "rur", "руб", "рубль", "рубли", "₽"),
    "mm": ("mm", "мм", "миллиметр", "миллиметры"),
    "cm": ("cm", "см", "сантиметр", "сантиметры"),
    "in": ("in", "inch", "дюйм", "дюймы"),
    "g": ("g", "гр", "грамм", "граммы"),
    "kg": ("kg", "кг", "килограмм", "килограммы"),
    "lb": ("lb", "lbs", "фунт", "фунты"),
}

_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "Электроника": (
        "электроника",
        "компьютер",
        "ноутбук",
        "смартфон",
        "телефон",
        "планшет",
        "гаджет",
        "мышь",
        "клавиатура",
        "монитор",
        "наушники",
        "колонка",
        "камера",
        "телевизор",
        "приставка",
    ),
    "Компьютеры": ("компьютер", "ноутбук", "пк", "монитор", "клавиатура", "мышь", "процессор", "ssd"),
    "Бытовая техника": ("бытовая техника", "пылесос", "чайник", "кофеварка", "холодильник", "стиральная", "утюг", "миксер"),
    "Одежда, обувь и аксессуары": (
        "одежда",
        "обувь",
        "аксессуар",
        "футболка",
        "платье",
        "куртка",
        "брюки",
        "сумка",
        "рюкзак",
        "ремень",
    ),
    "Женщинам": ("женский", "женщинам", "платье", "юбка", "блузка", "топ", "женская одежда"),
    "Мужчинам": ("мужской", "мужчинам", "рубашка", "брюки", "мужская одежда"),
    "Детям": ("детский", "детям", "ребенок", "малыш", "школьный"),
    "Детские товары": ("детский", "детям", "ребенок", "малыш", "игрушка", "коляска", "школьный"),
    "Обувь": ("обувь", "кроссовки", "ботинки", "сапоги", "туфли", "сандалии"),
    "Аксессуары": ("аксессуар", "сумка", "рюкзак", "ремень", "кошелек", "часы", "очки"),
    "Дом": ("дом", "кухня", "посуда", "текстиль", "интерьер", "светильник", "декор", "хранение"),
    "Дом и сад": ("дом", "сад", "кухня", "посуда", "текстиль", "интерьер", "дача", "растение"),
    "Дом и дача": ("дом", "дача", "сад", "кухня", "посуда", "текстиль", "интерьер"),
    "Для дома и дачи": ("дом", "дача", "сад", "мебель", "посуда", "инструмент", "текстиль"),
    "Дача и сад": ("дача", "сад", "растение", "огород", "полив", "семена"),
    "Красота": ("красота", "косметика", "макияж", "уход", "парфюм", "маникюр"),
    "Красота и здоровье": ("красота", "здоровье", "косметика", "уход", "парфюм", "медицинский", "витамины"),
    "Спорт": ("спорт", "фитнес", "тренировка", "йога", "велосипед", "мяч"),
    "Спорт и отдых": ("спорт", "отдых", "фитнес", "туризм", "тренировка", "велосипед", "рыбалка", "палатка"),
    "Хобби и отдых": ("хобби", "отдых", "коллекция", "музыка", "рыбалка", "туризм", "настольная игра"),
    "Хобби и творчество": ("хобби", "творчество", "рукоделие", "рисование", "набор", "музыка"),
    "Игрушки": ("игрушка", "игра", "конструктор", "кукла", "робот", "пазл"),
    "Книги": ("книга", "учебник", "литература", "журнал", "комикс"),
    "Канцтовары": ("канцтовары", "канцелярия", "ручка", "тетрадь", "бумага", "маркер", "офис"),
    "Канцелярия": ("канцелярия", "канцтовары", "ручка", "тетрадь", "бумага", "маркер", "офис"),
    "Автотовары": ("авто", "автомобиль", "машина", "запчасть", "шина", "масло", "аксессуар авто"),
    "Авто": ("авто", "автомобиль", "машина", "запчасть", "шина", "масло"),
    "Запчасти и аксессуары": ("запчасть", "авто", "машина", "аксессуар авто", "шина", "диск"),
    "Продукты": ("продукт", "еда", "напиток", "бакалея", "чай", "кофе", "сладости"),
    "Продукты питания": ("продукт", "питание", "еда", "напиток", "бакалея", "чай", "кофе"),
    "Зоотовары": ("зоо", "питомец", "кошка", "собака", "корм", "наполнитель", "аквариум"),
    "Животные": ("животное", "питомец", "кошка", "собака", "корм", "аквариум"),
    "Сад и дача": ("сад", "дача", "растение", "огород", "полив", "семена"),
    "Строительство и ремонт": ("строительство", "ремонт", "инструмент", "краска", "сантехника", "крепеж"),
    "Мебель": ("мебель", "стол", "стул", "диван", "шкаф", "кровать", "полка"),
    "Ювелирные изделия": ("ювелир", "кольцо", "серьги", "цепочка", "браслет", "серебро", "золото"),
    "Аптека": ("аптека", "лекарство", "медицинский", "витамин", "бад", "здоровье"),
    "Товары для взрослых": ("для взрослых", "18+", "интим"),
    "Транспорт": ("транспорт", "автомобиль", "мотоцикл", "велосипед", "самокат", "лодка"),
    "Недвижимость": ("недвижимость", "квартира", "дом", "участок", "комната", "аренда"),
    "Работа": ("работа", "вакансия", "резюме", "сотрудник"),
    "Услуги": ("услуга", "ремонт", "мастер", "доставка", "обучение"),
    "Личные вещи": ("личные вещи", "одежда", "обувь", "аксессуар", "красота", "детский"),
    "Для бизнеса": ("бизнес", "оборудование", "торговля", "касса", "станок", "витрина"),
}


def normalize_marketplace_id(value: str | None) -> str:
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return MARKETPLACE_ALIASES.get(raw, raw)


def marketplace_output_id(marketplace: str) -> str:
    normalized = normalize_marketplace_id(marketplace)
    return MARKETPLACE_OUTPUT_IDS.get(normalized, normalized)


def marketplace_category_options(marketplace: str) -> tuple[str, ...]:
    return MARKETPLACE_CATEGORY_OPTIONS.get(normalize_marketplace_id(marketplace), ())


def marketplace_category_prompt(marketplace: str) -> str:
    categories = marketplace_category_options(marketplace)
    if not categories:
        return ""

    label = marketplace_output_id(marketplace)
    values = "\n".join(f"- {category}" for category in categories)
    return (
        f"ДОПУСТИМЫЕ КАТЕГОРИИ {label}:\n"
        f"{values}\n"
        "Поле category обязано быть ровно одним значением из этого списка, без новых категорий и без подкатегорий."
    )


def marketplace_fixed_value_options(marketplace: str) -> dict[str, tuple[str, ...]]:
    return MARKETPLACE_FIXED_VALUE_OPTIONS.get(normalize_marketplace_id(marketplace), {})


def marketplace_fixed_values_prompt(marketplace: str) -> str:
    fixed_values = marketplace_fixed_value_options(marketplace)
    if not fixed_values:
        return ""

    lines = []
    for field, values in fixed_values.items():
        rendered_values = ", ".join(values)
        lines.append(f"- {field}: только {rendered_values}")
    values_text = "\n".join(lines)
    return (
        f"ФИКСИРОВАННЫЕ ЗНАЧЕНИЯ {marketplace_output_id(marketplace)}:\n"
        f"{values_text}\n"
        "Если возвращаешь эти поля или одноимённые характеристики, используй только значения из списка."
    )


def all_marketplace_category_prompt(marketplaces: Iterable[str] = ALL_MARKETPLACE_TARGETS) -> str:
    blocks = []
    for marketplace in marketplaces:
        prompt = marketplace_category_prompt(marketplace)
        if prompt:
            blocks.append(prompt)
    return "\n\n".join(blocks)


def all_marketplace_fixed_values_prompt(marketplaces: Iterable[str] = ALL_MARKETPLACE_TARGETS) -> str:
    blocks = []
    for marketplace in marketplaces:
        prompt = marketplace_fixed_values_prompt(marketplace)
        if prompt:
            blocks.append(prompt)
    return "\n\n".join(blocks)


def normalize_category_text(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("ё", "е").casefold()).strip()


def _candidate_texts(values: Iterable[object]) -> list[str]:
    texts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                texts.append(value.strip())
            continue
        if isinstance(value, (int, float, bool)):
            texts.append(str(value))
            continue
        if isinstance(value, dict):
            texts.extend(_candidate_texts(value.values()))
            continue
        if isinstance(value, (list, tuple, set)):
            texts.extend(_candidate_texts(value))
    return texts


def _field_matches(field: str, candidate: object) -> bool:
    normalized_candidate = normalize_category_text(candidate)
    if not normalized_candidate:
        return False
    aliases = FIXED_VALUE_FIELD_ALIASES.get(field, (field,))
    return any(normalize_category_text(alias) == normalized_candidate for alias in aliases)


def _score_category(category: str, normalized_candidate: str) -> float:
    normalized_category = normalize_category_text(category)
    if not normalized_candidate:
        return 0.0
    if normalized_candidate == normalized_category:
        return 200.0
    if normalized_category in normalized_candidate:
        return 140.0 + len(normalized_category)

    score = SequenceMatcher(None, normalized_candidate, normalized_category).ratio() * 70.0
    candidate_tokens = set(re.findall(r"[a-zа-я0-9]+", normalized_candidate))
    category_tokens = set(re.findall(r"[a-zа-я0-9]+", normalized_category))
    score += len(candidate_tokens & category_tokens) * 18.0

    for hint in _CATEGORY_HINTS.get(category, ()):
        normalized_hint = normalize_category_text(hint)
        if normalized_hint and normalized_hint in normalized_candidate:
            score += 35.0

    return score


def _score_fixed_value(option: str, normalized_candidate: str) -> float:
    normalized_option = normalize_category_text(option)
    if not normalized_candidate:
        return 0.0
    if normalized_candidate == normalized_option:
        return 200.0
    if normalized_option in normalized_candidate:
        return 150.0 + len(normalized_option)

    score = SequenceMatcher(None, normalized_candidate, normalized_option).ratio() * 70.0
    for hint in FIXED_VALUE_HINTS.get(option, ()):
        normalized_hint = normalize_category_text(hint)
        if normalized_hint == normalized_candidate:
            score += 120.0
        elif normalized_hint and normalized_hint in normalized_candidate:
            score += 55.0
    return score


def coerce_marketplace_category(marketplace: str, values: Iterable[object]) -> str:
    categories = marketplace_category_options(marketplace)
    if not categories:
        return ""

    best_category = categories[0]
    best_score = -1.0
    for text in _candidate_texts(values):
        normalized_candidate = normalize_category_text(text)
        for category in categories:
            score = _score_category(category, normalized_candidate)
            if score > best_score:
                best_category = category
                best_score = score

    return best_category


def coerce_fixed_value(field: str, options: Iterable[str], values: Iterable[object]) -> str:
    option_list = tuple(options)
    if not option_list:
        return ""

    best_option = option_list[0]
    best_score = -1.0
    has_candidate = False
    for text in _candidate_texts(values):
        has_candidate = True
        normalized_candidate = normalize_category_text(text)
        for option in option_list:
            score = _score_fixed_value(option, normalized_candidate)
            if score > best_score:
                best_option = option
                best_score = score
    return best_option if has_candidate else ""


def _normalize_fixed_value_container(value: object, field_options: dict[str, tuple[str, ...]]) -> object:
    if isinstance(value, dict):
        normalized: dict[object, object] = {}
        for key, nested_value in value.items():
            normalized_field = next(
                (field for field in field_options if _field_matches(field, key)),
                "",
            )
            if normalized_field:
                normalized_value = coerce_fixed_value(normalized_field, field_options[normalized_field], [nested_value])
                normalized[key] = normalized_value or nested_value
            else:
                normalized[key] = _normalize_fixed_value_container(nested_value, field_options)
        return normalized

    if isinstance(value, list):
        normalized_items = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("key") or item.get("attribute") or item.get("id")
                normalized_field = next(
                    (field for field in field_options if _field_matches(field, name)),
                    "",
                )
                if normalized_field and ("value" in item or "values" in item):
                    next_item = dict(item)
                    if "value" in next_item:
                        normalized_value = coerce_fixed_value(
                            normalized_field,
                            field_options[normalized_field],
                            [next_item.get("value")],
                        )
                        if normalized_value:
                            next_item["value"] = normalized_value
                    if isinstance(next_item.get("values"), list):
                        next_item["values"] = [
                            coerce_fixed_value(normalized_field, field_options[normalized_field], [raw]) or raw
                            for raw in next_item["values"]
                        ]
                    normalized_items.append(next_item)
                    continue
            normalized_items.append(_normalize_fixed_value_container(item, field_options))
        return normalized_items

    return value


def coerce_marketplace_fixed_values(marketplace: str, record: dict[str, object]) -> dict[str, object]:
    field_options = marketplace_fixed_value_options(marketplace)
    if not field_options:
        return dict(record)

    normalized = _normalize_fixed_value_container(dict(record), field_options)
    if not isinstance(normalized, dict):
        return dict(record)

    for field, options in field_options.items():
        matched_key = next((key for key in normalized if _field_matches(field, key)), None)
        if matched_key is None:
            if len(options) == 1:
                normalized[field] = options[0]
            continue

        coerced = coerce_fixed_value(field, options, [normalized[matched_key]])
        if coerced:
            normalized[matched_key] = coerced
            normalized.setdefault(field, coerced)

    return normalized
