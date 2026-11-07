"""Сервис чтения чеков и распознавания покупок.

Поддерживает:
1. Сканирование QR-кодов кассовых чеков РФ (ФНС 54-ФЗ: t=...&s=...&fn=...).
2. Оптическое распознавание текста (OCR) чеков через RapidOCR:
   - Поиск строчки «ИТОГО / К ОПЛАТЕ / СУММА».
   - Определение магазина/бренда.
   - Определение даты чека.
   - Извлечение списка товаров.
3. Разбор банковских пуш-уведомлений и СМС («Покупка 450р Пятерочка»).
4. Автоматическую классификацию чека по категориям через categorizer.
5. Опциональный AI Vision (OpenAI / Gemini), если в .env указан API ключ.
"""
import datetime
import io
import logging
import os
import re
import urllib.parse
from PIL import Image

logger = logging.getLogger("budget-bot")

# Попытка импорта QR и OCR библиотек
try:
    import zxingcpp
except ImportError:
    zxingcpp = None

try:
    from rapidocr_onnxruntime import RapidOCR
    _ocr_engine = None

    def get_ocr():
        global _ocr_engine
        if _ocr_engine is None:
            _ocr_engine = RapidOCR()
        return _ocr_engine
except ImportError:
    RapidOCR = None
    get_ocr = lambda: None

from kopilka.services.categorizer import classify_expense, CATEGORIES_CATALOG
from kopilka.utils.formatting import today_date

# Известные сети и магазины для сопоставления чека
KNOWN_STORES = [
    # Супермаркеты и продукты
    ("Пятёрочка", ["пятерочка", "пятёрочка", "агроторг", "pyaterochka"]),
    ("Магнит", ["магнит", "тендер", "магнит у дома", "магнит семейный"]),
    ("ВкусВилл", ["вкусвилл", "вкус вилл", "vkusvill"]),
    ("Перекрёсток", ["перекресток", "перекрёсток", "perekrestok"]),
    ("Лента", ["лента", "гиперлента", "суперлента"]),
    ("Ашан", ["ашан", "auchan"]),
    ("Дикси", ["дикси"]),
    ("Чижик", ["чижик"]),
    ("Самокат", ["самокат", "умный ритейл"]),
    ("Купер", ["купер", "сбермаркет"]),
    ("Spar", ["spar", "евроспар", "спар"]),
    ("Metro", ["metro cash", "метро кэш"]),
    ("Красное & Белое", ["красное и белое", "красное & белое", "к&б", "кб"]),
    ("Бристоль", ["бристоль"]),
    ("Винлаб", ["винлаб"]),
    ("Азбука Вкуса", ["азбука вкуса"]),
    ("Глобус", ["глобус", "globus"]),
    ("Да!", ["магазин да", "да!"]),
    ("Ярче!", ["ярче!?", "ярче"]),
    # Кафе и фастфуд
    ("Вкусно и Точка", ["вкусно и точка", "вкусно - и точка", "макдоналдс", "mcdonalds"]),
    ("KFC / Rostic's", ["kfc", "кфс", "ростикс", "ростик'с", "rostics"]),
    ("Бургер Кинг", ["бургер кинг", "burger king"]),
    ("Додо Пицца", ["додо пицца", "додо", "dodo pizza", "додопицца"]),
    ("Шоколадница", ["шоколадница"]),
    ("Кофемания", ["кофемания"]),
    ("Теремок", ["теремок"]),
    ("Тануки", ["тануки"]),
    ("Якитория", ["якитория"]),
    ("Папа Джонс", ["папа джонс"]),
    ("Surf Coffee", ["surf coffee", "серф кофе"]),
    ("Stars Coffee", ["stars coffee"]),
    ("Cofix", ["cofix", "кофикс"]),
    ("One Price Coffee", ["one price coffee"]),
    # Аптеки
    ("Аптека Апрель", ["аптека апрель", "аптечный клуб апрель"]),
    ("Планета Здоровья", ["планета здоровья"]),
    ("Горздрав", ["горздрав"]),
    ("Ригла", ["ригла"]),
    ("Аптека Вита", ["аптека вита", "вита экспресс"]),
    ("Еаптека", ["еаптека", "сбереаптека"]),
    ("Аптека 36.6", ["аптека 36.6", "36.6"]),
    ("Столички", ["столички", "неофарм"]),
    # АЗС
    ("Лукойл", ["лукойл", "lukoil", "ликард"]),
    ("Газпромнефть", ["газпромнефть", "гпн", "газпром нефть"]),
    ("Роснефть", ["роснефть", "рн-москва"]),
    ("Татнефть", ["татнефть"]),
    ("Тебойл", ["тебойл", "teboil"]),
    # Магазины для дома и маркетплейсы
    ("Fix Price", ["fix price", "фикс прайс", "бэст прайс"]),
    ("Леруа Мерлен", ["леруа мерлен", "леруа", "leroy merlin", "лемана про"]),
    ("Петрович", ["петрович", "стд петрович"]),
    ("Hoff", ["hoff", "хофф"]),
    ("Wildberries", ["вайлдберриз", "wildberries"]),
    ("Ozon", ["ozon", "озон"]),
    ("Золотое Яблоко", ["золотое яблоко"]),
    ("Лэтуаль", ["летуаль", "лэтуаль"]),
    ("Детский Мир", ["детский мир"]),
    ("Спортмастер", ["спортмастер"]),
    ("DNS", ["днс", "dns"]),
    ("М.Видео", ["м.видео", "мвидео"]),
]


def parse_fns_qr(text: str) -> dict | None:
    """Разбирает строку QR-кода кассового чека по стандарту 54-ФЗ.

    Пример: 't=20261107T1435&s=350.00&fn=9960440300999999&i=12345&fp=1234567890&n=1'
    """
    if not text or not isinstance(text, str):
        return None
    raw = text.strip()
    qs = raw.partition("?")[2] if "?" in raw else raw
    params = urllib.parse.parse_qs(qs)

    s_val = params.get("s", [None])[0]
    t_val = params.get("t", [None])[0]

    # Резервный поиск через regex
    if not s_val:
        m_s = re.search(r"(?:^|[?&])s=([0-9]+(?:\.[0-9]+)?)", raw)
        if m_s:
            s_val = m_s.group(1)
    if not t_val:
        m_t = re.search(r"(?:^|[?&])t=([0-9T]+)", raw)
        if m_t:
            t_val = m_t.group(1)

    if not s_val:
        return None

    try:
        amount = float(s_val)
    except (ValueError, TypeError):
        return None

    date_val = None
    if t_val:
        # Форматы: 20261107T1435, 20261107T143500, 20261107
        m_d = re.match(r"^(\d{4})(\d{2})(\d{2})", t_val)
        if m_d:
            try:
                date_val = datetime.date(int(m_d.group(1)), int(m_d.group(2)), int(m_d.group(3)))
            except ValueError:
                date_val = None

    return {
        "amount": amount,
        "spent_date": date_val or today_date(),
        "fn": params.get("fn", [None])[0],
        "i": params.get("i", [None])[0],
        "fp": params.get("fp", [None])[0],
        "method": "qr",
        "raw": raw,
    }


def parse_bank_notification(text: str) -> dict | None:
    """Разбирает текст банковской СМС / пуш-уведомления или электронной квитанции.

    Примеры:
    - «Покупка 450р Пятерочка»
    - «СберБанк: Оплата 1 250.50 ₽ в ВкусВилл. Баланс 15000р»
    - «Тинькофф: Покупка 300 ₽, Додо Пицца»
    """
    if not text:
        return None
    cleaned = text.strip()

    # Проверка на QR-строку в тексте
    if "fn=" in cleaned or "s=" in cleaned and "t=" in cleaned:
        qr_parsed = parse_fns_qr(cleaned)
        if qr_parsed:
            return qr_parsed

    patterns = [
        # «Покупка 450р Пятерочка», «Оплата 1250.50 руб Магнит»
        re.compile(
            r"(?:покупка|оплата|списание|чек)\s+([0-9\s]+(?:[.,][0-9]{1,2})?)\s*(?:р|руб|₽|rub)?\s+(?:в\s+)?(.+)",
            re.IGNORECASE,
        ),
        # «СберБанк: Оплата 450 ₽, Додо Пицца»
        re.compile(
            r".*?(?:оплата|покупка|списание)\s+([0-9\s]+(?:[.,][0-9]{1,2})?)\s*(?:р|руб|₽|rub)[,\s]+(?:в\s+)?(.+)",
            re.IGNORECASE,
        ),
    ]

    for p in patterns:
        m = p.match(cleaned)
        if m:
            amt_str = m.group(1).replace(" ", "").replace(",", ".")
            rest = m.group(2).strip()
            # Отрезаем хвост «Баланс ...» или «Карта ...»
            rest = re.split(r"(?:карта|\*|\bбаланс\b|\bостаток\b)", rest, flags=re.IGNORECASE)[0].strip(" .,-")
            try:
                amt = float(amt_str)
                return {
                    "amount": amt,
                    "spent_date": today_date(),
                    "merchant": rest[:40] if rest else None,
                    "method": "text_notification",
                    "raw": cleaned,
                }
            except ValueError:
                pass

    return None


def _find_merchant_in_text(text: str) -> tuple[str | None, str | None]:
    """Ищет известный магазин или бренд в распознанном тексте чека.

    Возвращает (название_магазина, категория).
    """
    low = text.lower()
    for store_name, aliases in KNOWN_STORES:
        if any(a in low for a in aliases):
            cat, _ = classify_expense(0, store_name, [])
            return store_name, cat
    return None, None


def _extract_date_from_text(text: str) -> datetime.date | None:
    """Ищет дату покупки в тексте чека (07.11.2026 или 07.11.26 или 07/11/2026)."""
    matches = re.findall(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", text)
    for d_str, m_str, y_str in matches:
        try:
            d, m, y = int(d_str), int(m_str), int(y_str)
            if y < 100:
                y += 2000
            dt = datetime.date(y, m, d)
            # Дата не должна быть из далёкого будущего или старше 1 года
            now = today_date()
            if 0 <= (now - dt).days <= 365:
                return dt
        except ValueError:
            continue
    return None


def _extract_amount_from_ocr_lines(lines: list[str]) -> float | None:
    """Ищет итоговую сумму покупки в строках чека."""
    # Приоритет 1: Строка со словом ИТОГ / ИТОГО / К ОПЛАТЕ / СУММА
    total_re = re.compile(
        r"(?:ИТОГ[А-Я]*|ИТОГО|СУММА|К\s*ОПЛАТЕ|ВСЕГО|ИТОГ\s*К\s*ОПЛАТЕ|ЭЛЕКТРОННЫМИ|НАЛИЧНЫМИ)[\s:=]*([0-9\s]+[.,][0-9]{2})",
        re.IGNORECASE,
    )
    for l in lines:
        m = total_re.search(l)
        if m:
            s = m.group(1).replace(" ", "").replace(",", ".")
            try:
                amt = float(s)
                if 0.5 <= amt <= 10_000_000:
                    return amt
            except ValueError:
                pass

    # Приоритет 2: Слово «ИТОГ» на одной строке, а число на следующей
    for i, l in enumerate(lines[:-1]):
        if re.search(r"\b(ИТОГ|ИТОГО|К\s*ОПЛАТЕ|СУММА)\b", l, re.IGNORECASE):
            next_line = lines[i + 1]
            m = re.search(r"([0-9\s]+[.,][0-9]{2})", next_line)
            if m:
                s = m.group(1).replace(" ", "").replace(",", ".")
                try:
                    amt = float(s)
                    if 0.5 <= amt <= 10_000_000:
                        return amt
                except ValueError:
                    pass

    return None


def scan_receipt_image(
    image_bytes: bytes,
    user_id: int = 0,
    caption: str = "",
    user_existing_categories: list[str] | None = None,
) -> dict:
    """Полный цикл распознавания чека с изображения (фото или файл).

    1. Сканирование QR-кода чека (ФНС).
    2. Если QR-код есть — сумма и дата берутся гарантированно точно,
       а OCR дополнительно ищет название магазина.
    3. Если QR-кода нет — OCR ищет «ИТОГО», дату и магазин.
    4. Результат классифицируется по категориям.
    """
    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        logger.warning("Не удалось открыть изображение: %s", e)
        return {"success": False, "error": "Не удалось открыть изображение."}

    qr_res = None
    # 1. Попытка чтения QR-кода через zxingcpp
    if zxingcpp:
        try:
            barcodes = zxingcpp.read_barcodes(pil_img, try_rotate=True, try_downscale=True)
            for bc in barcodes:
                if bc.text and ("s=" in bc.text or "fn=" in bc.text):
                    parsed_qr = parse_fns_qr(bc.text)
                    if parsed_qr:
                        qr_res = parsed_qr
                        break
        except Exception as e:
            logger.debug("Ошибка при чтении QR через zxingcpp: %s", e)

    ocr_lines = []
    # 2. Оптическое распознавание текста (RapidOCR)
    ocr_engine = get_ocr()
    if ocr_engine:
        try:
            # RapidOCR принимает bytes или numpy array
            ocr_out, _ = ocr_engine(image_bytes)
            if ocr_out:
                ocr_lines = [item[1].strip() for item in ocr_out if item and len(item) > 1]
        except Exception as e:
            logger.debug("Ошибка при выполнении OCR: %s", e)

    full_text = " ".join(ocr_lines)
    if caption:
        full_text += " " + caption

    merchant, store_cat = _find_merchant_in_text(full_text)
    doc_date = _extract_date_from_text(full_text)

    # Если QR-код найден — берём его точные данные
    if qr_res:
        amount = qr_res["amount"]
        spent_d = qr_res.get("spent_date") or doc_date or today_date()

        # Определяем категорию
        category = store_cat
        if not category and merchant:
            category, _ = classify_expense(user_id, merchant, user_existing_categories)
        if not category and caption:
            category, _ = classify_expense(user_id, caption, user_existing_categories)
        if not category:
            category = "Продукты"  # Самый частый кассовый чек — продукты

        # Собираем превью товаров из OCR
        items = [l for l in ocr_lines if len(l) > 3 and not re.search(r"(ИТОГ|ИТОГО|КАССОВЫЙ|ФН|ФД|ФП|НДС|СПАСИБО)", l, re.IGNORECASE)]

        return {
            "success": True,
            "amount": amount,
            "spent_date": spent_d,
            "merchant": merchant,
            "category": category,
            "items": items[:5],
            "method": "qr",
        }

    # Если QR-код не найден — парсим из OCR текста
    amount = _extract_amount_from_ocr_lines(ocr_lines)
    if amount is not None:
        spent_d = doc_date or today_date()

        category = store_cat
        if not category and merchant:
            category, _ = classify_expense(user_id, merchant, user_existing_categories)
        if not category and caption:
            category, _ = classify_expense(user_id, caption, user_existing_categories)
        if not category:
            category = "Разное"

        items = [l for l in ocr_lines if len(l) > 3 and not re.search(r"(ИТОГ|ИТОГО|КАССОВЫЙ|ФН|ФД|ФП|НДС|СПАСИБО)", l, re.IGNORECASE)]

        return {
            "success": True,
            "amount": amount,
            "spent_date": spent_d,
            "merchant": merchant,
            "category": category,
            "items": items[:5],
            "method": "ocr",
        }

    return {
        "success": False,
        "error": "Не удалось найти сумму «ИТОГО» или QR-код на чеке.",
        "merchant": merchant,
        "ocr_lines_found": len(ocr_lines),
    }
