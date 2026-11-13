"""Сервис чтения чеков, банковских переводов, скриншотов приложений и фото записей.

Поддерживает:
1. Сканирование QR-кодов кассовых чеков РФ (ФНС 54-ФЗ: t=...&s=...&fn=...).
2. Оптическое распознавание текста (OCR) чеков через RapidOCR:
   - Поиск строчки «ИТОГО / К ОПЛАТЕ / СУММА».
   - Определение магазина/бренда.
   - Определение даты чека (числовые и текстовые форматы РФ).
   - Извлечение списка товаров.
3. Распознавание скриншотов банковских переводов (СберБанк, Т-Банк, Альфа, ВТБ, СБП):
   - Сумма перевода (с отсевом комиссии 0 ₽ и балансов карт).
   - Получатель перевода и банк.
   - Назначение / комментарий к переводу.
4. Распознавание истории покупок из приложений магазинов (Магнит, ВкусВилл, Самокат, Ozon и др.).
5. Распознавание записей от руки или на бумаге («Продукты 1200», «Такси 450», «280 помело»).
6. Разбор банковских пуш-уведомлений и СМС («Покупка 450р Пятерочка», «Перевод 1500р Иван И.»).
7. Автоматическую классификацию по категориям через categorizer.
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

# Месяцы на русском для разбора дат («08 ноября 2026», «8 ноя», «5 мая»)
RUSSIAN_MONTHS = {
    "янв": 1, "января": 1, "январь": 1,
    "фев": 2, "февраля": 2, "февраль": 2,
    "мар": 3, "марта": 3, "март": 3,
    "апр": 4, "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июн": 6, "июня": 6, "июнь": 6,
    "июл": 7, "июля": 7, "июль": 7,
    "авг": 8, "августа": 8, "август": 8,
    "сен": 9, "сентября": 9, "сентябрь": 9, "сент": 9,
    "окт": 10, "октября": 10, "октябрь": 10,
    "ноя": 11, "ноября": 11, "ноябрь": 11,
    "дек": 12, "декабря": 12, "декабрь": 12,
}

# Известные сети, магазины, банки и сервисы для сопоставления чека
KNOWN_STORES = [
    # Супермаркеты и продукты
    ("Пятёрочка", ["пятерочка", "пятёрочка", "агроторг", "pyaterochka"]),
    ("Магнит", ["магнит", "тендер", "магнит у дома", "магнит семейный", "магнит аптека"]),
    ("ВкусВилл", ["вкусвилл", "вкус вилл", "vkusvill"]),
    ("Перекрёсток", ["перекресток", "перекрёсток", "perekrestok"]),
    ("Лента", ["лента", "гиперлента", "суперлента"]),
    ("Ашан", ["ашан", "auchan"]),
    ("Дикси", ["дикси"]),
    ("Чижик", ["чижик"]),
    ("Светофор", ["светофор"]),
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
    ("Бахетле", ["бахетле"]),
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
    ("Subway", ["subway", "сабвей"]),
    ("Цех 85", ["цех 85", "цех85"]),
    ("Буше", ["буше"]),
    # Аптеки
    ("Аптека Апрель", ["аптека апрель", "аптечный клуб апрель"]),
    ("Планета Здоровья", ["планета здоровья"]),
    ("Горздрав", ["горздрав"]),
    ("Ригла", ["ригла"]),
    ("Аптека Вита", ["аптека вита", "вита экспресс"]),
    ("Еаптека", ["еаптека", "сбереаптека"]),
    ("Аптека 36.6", ["аптека 36.6", "36.6"]),
    ("Столички", ["столички", "неофарм"]),
    ("Здравсити", ["здравсити"]),
    # АЗС
    ("Лукойл", ["лукойл", "lukoil", "ликард"]),
    ("Газпромнефть", ["газпромнефть", "гпн", "газпром нефть"]),
    ("Роснефть", ["роснефть", "рн-москва"]),
    ("Татнефть", ["татнефть"]),
    ("Тебойл", ["тебойл", "teboil"]),
    # Магазины для дома и маркетплейсы
    ("Fix Price", ["fix price", "фикс прайс", "бэст прайс"]),
    ("Леруа Мерлен", ["леруа мерлен", "леруа", "leroy merlin", "лемана про", "лемана"]),
    ("Петрович", ["петрович", "стд петрович"]),
    ("Hoff", ["hoff", "хофф"]),
    ("Wildberries", ["вайлдберриз", "wildberries", "вайлдбериз"]),
    ("Ozon", ["ozon", "озон"]),
    ("Золотое Яблоко", ["золотое яблоко"]),
    ("Лэтуаль", ["летуаль", "лэтуаль"]),
    ("Детский Мир", ["детский мир"]),
    ("Спортмастер", ["спортмастер"]),
    ("DNS", ["днс", "dns"]),
    ("М.Видео", ["м.видео", "мвидео"]),
    ("Эльдорадо", ["эльдорадо"]),
    ("Ситилинк", ["ситилинк"]),
    # Транспорт, такси и доставка
    ("Яндекс Go", ["яндекс go", "яндекс такси", "яндекс.такси", "yandex go", "яндекс go такси"]),
    ("Яндекс Драйв", ["яндекс драйв", "яндекс.драйв", "yandex drive"]),
    ("Делимобиль", ["делимобиль", "delimobil"]),
    ("Ситимобил", ["ситимобил"]),
    ("Яндекс Еда", ["яндекс еда", "яндекс.еда"]),
    ("Маркет Деливери", ["маркет деливери", "delivery club", "деливери клаб", "деливери"]),
    # Банки и сервисы денежных переводов
    ("СберБанк", ["сбербанк", "сбер", "sberbank", "sber", "клиенту сбербанка", "клиент сбербанка"]),
    ("Т-Банк", ["т-банк", "тбанк", "тинькофф", "тинькоф", "t-bank", "tinkoff"]),
    ("Альфа-Банк", ["альфа-банк", "альфа банк", "альфа", "alfa"]),
    ("ВТБ", ["втб", "vtb"]),
    ("СБП", ["сбп", "система быстрых платежей", "перевод по сбп", "sbp"]),
    ("Райффайзен", ["райффайзенбанк", "райффайзен", "raiffeisen"]),
    ("Газпромбанк", ["газпромбанк", "гпб"]),
    ("Совкомбанк", ["совкомбанк", "халва банк"]),
    ("Яндекс Банк", ["яндекс банк", "яндекс пей", "yandex pay"]),
    ("Ozon Банк", ["ozon банк", "озон банк"]),
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
    - «Перевод 1500р Иван И.»
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
        re.compile(
            r"(?:покупка|оплата|списание|чек|перевод)\s+([0-9\s]+(?:[.,][0-9]{1,2})?)\s*(?:р|руб|₽|rub)?\s+(?:в\s+|на\s+|кому\s+)?(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r".*?(?:оплата|покупка|списание|перевод)\s+([0-9\s]+(?:[.,][0-9]{1,2})?)\s*(?:р|руб|₽|rub)[,\s]+(?:в\s+|на\s+|кому\s+)?(.+)",
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
    """Ищет дату покупки в тексте (07.11.2026, 07/11/2026, 8 ноября 2026, вчера, сегодня)."""
    now = today_date()

    # 1. Цифровые форматы: 08.11.2026, 08/11/2026, 08.11.26
    matches = re.findall(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", text)
    for d_str, m_str, y_str in matches:
        try:
            d, m, y = int(d_str), int(m_str), int(y_str)
            if y < 100:
                y += 2000
            dt = datetime.date(y, m, d)
            if 0 <= (now - dt).days <= 365:
                return dt
        except ValueError:
            continue

    # 2. Текстовые даты на русском («8 ноября 2026», «08 ноября», «5 ноя», «12 мая»)
    text_matches = re.finditer(r"\b(\d{1,2})\s+([а-яёА-ЯЁ]{3,8})(?:\s+(\d{4}))?\b", text, re.IGNORECASE)
    for tm in text_matches:
        d_str, m_word, y_str = tm.group(1), tm.group(2).lower(), tm.group(3)
        month = None
        for prefix, m_num in RUSSIAN_MONTHS.items():
            if m_word.startswith(prefix):
                month = m_num
                break
        if month:
            y = int(y_str) if y_str else now.year
            try:
                dt = datetime.date(y, month, int(d_str))
                if dt > now and not y_str:
                    dt = datetime.date(y - 1, month, int(d_str))
                if 0 <= (now - dt).days <= 365:
                    return dt
            except ValueError:
                continue

    # 3. Относительные даты
    if re.search(r"\bвчера\b", text, re.IGNORECASE):
        return now - datetime.timedelta(days=1)
    if re.search(r"\bсегодня\b", text, re.IGNORECASE):
        return now

    return None


def _find_transfer_details(lines: list[str], full_text: str) -> dict | None:
    """Определяет, является ли изображение скриншотом банковского перевода, и извлекает детали."""
    low = full_text.lower()
    is_transfer = bool(re.search(
        r"\b(перевод|сбп|клиенту\s+сбер|получател|комисси|чек\s+по\s+операции|исходящий\s+перевод|перевод\s+выполнен|перевод\s+отправлен)\b",
        low,
    ))
    if not is_transfer:
        return None

    recipient = None
    for i, l in enumerate(lines):
        m = re.match(r"^(?:получатель|получателю|кому|фио\s*получателя)[:\s]*(.*)$", l, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val and len(val) >= 2:
                recipient = val
            elif i + 1 < len(lines):
                next_val = lines[i + 1].strip()
                if next_val and len(next_val) >= 2 and not any(k in next_val.lower() for k in ["счёт", "банк", "сумма", "номер", "комиссия", "карта"]):
                    recipient = next_val
            break

    comment = None
    for i, l in enumerate(lines):
        m = re.match(r"^(?:сообщение|сообщение\s*получателю|назначение|комментарий)[:\s]*(.*)$", l, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val:
                comment = val
            elif i + 1 < len(lines):
                comment = lines[i + 1].strip()
            break

    return {
        "is_transfer": True,
        "recipient": recipient,
        "comment": comment,
    }


def _extract_amount_and_notes(lines: list[str]) -> tuple[float | None, list[str]]:
    """Гибкий поиск суммы: чеки, переводы, история покупок в приложениях, записи на бумаге.

    Возвращает (сумма, список_заметок).
    """
    # Приоритет 1: Строка со словом ИТОГ / ИТОГО / К ОПЛАТЕ / СУММА / СУММА ПЕРЕВОДА / ОПЛАЧЕНО
    total_re = re.compile(
        r"(?:ИТОГ[А-Я]*|ИТОГО|СУММА\s*ПЕРЕВОДА|СУММА|К\s*ОПЛАТЕ|ВСЕГО|ОПЛАЧЕНО|СПИСАНО|ИТОГ\s*К\s*ОПЛАТЕ|ЭЛЕКТРОННЫМИ|НАЛИЧНЫМИ)[\s:=]*[+-]?\s*([0-9\s]+(?:[.,][0-9]{1,2})?)\s*(?:₽|руб|р\b)?",
        re.IGNORECASE,
    )
    for l in lines:
        low = l.lower()
        if any(skip in low for skip in ["комиссия", "баланс", "бонус", "кэшбэк", "остаток"]):
            continue
        m = total_re.search(l)
        if m:
            s = m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
            try:
                amt = float(s)
                if 0.5 <= amt <= 10_000_000:
                    return amt, []
            except ValueError:
                pass

    # Приоритет 2: Ключевое слово на одной строке, а число на следующей
    for i, l in enumerate(lines[:-1]):
        if re.search(r"\b(ИТОГ|ИТОГО|К\s*ОПЛАТЕ|СУММА|СУММА\s*ПЕРЕВОДА|СПИСАНИЕ|ОПЛАЧЕНО|УСПЕШНО|ИСПОЛНЕН)\b", l, re.IGNORECASE):
            next_line = lines[i + 1]
            low_next = next_line.lower()
            if any(skip in low_next for skip in ["комиссия", "баланс", "бонус", "кэшбэк", "остаток"]):
                continue
            m = re.search(r"^[+-]?\s*([0-9\s]+(?:[.,][0-9]{1,2})?)\s*(?:₽|руб|р\b)?$", next_line.strip())
            if not m:
                m = re.search(r"[+-]?\s*([0-9\s]+(?:[.,][0-9]{2}))", next_line)
            if m:
                s = m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
                try:
                    amt = float(s)
                    if 0.5 <= amt <= 10_000_000:
                        return amt, []
                except ValueError:
                    pass

    # Приоритет 3: Явное число с символом валюты (например '1 500 ₽', '-850 ₽', '1 240,50 ₽')
    # Отсекаем строки с комиссией, остатком, бонусами, картой и номерами счетов
    curr_re = re.compile(r"^[+-]?\s*([0-9\s]+(?:[.,][0-9]{1,2})?)\s*(?:₽|руб\.?|р\b)", re.IGNORECASE)
    curr_candidates = []
    for l in lines:
        low = l.lower()
        if any(skip in low for skip in ["комиссия", "баланс", "остаток", "бонус", "начислен", "кэшбэк", "скидк", "карта", "счет", "счёт"]):
            continue
        m = curr_re.search(l.strip())
        if not m:
            m = re.search(r"(?:^|\s)[+-]?([0-9\s]{1,10}(?:[.,][0-9]{1,2})?)\s*(?:₽|руб\b)", l, re.IGNORECASE)
        if m:
            s = m.group(1).replace(" ", "").replace("\xa0", "").replace(",", ".")
            try:
                amt = float(s)
                if 0.5 <= amt <= 10_000_000:
                    curr_candidates.append(amt)
            except ValueError:
                pass
    if curr_candidates:
        return max(curr_candidates), []

    # Приоритет 4: Записи от руки или на бумаге («Продукты 1200», «Такси 450», «Помело 280», «450 такси»)
    note_candidates = []
    for l in lines:
        cleaned = l.strip()
        m1 = re.match(r"^([А-Яа-яЁёA-Za-z\s]{2,40})\s+([0-9\s]+(?:[.,][0-9]{1,2})?)$", cleaned)
        m2 = re.match(r"^([0-9\s]+(?:[.,][0-9]{1,2})?)\s+([А-Яа-яЁёA-Za-z\s]{2,40})$", cleaned)
        for m, num_grp, text_grp in [(m1, 2, 1), (m2, 1, 2)]:
            if m:
                word = m.group(text_grp).strip()
                # Пропускаем даты («2026 год», «8 ноября»)
                if any(w in word.lower() for w in ["год", "года", "нояб", "дек", "янв", "фев", "мар", "апр", "май", "мая", "июн", "июл", "авг", "сен", "окт"]):
                    continue
                s = m.group(num_grp).replace(" ", "").replace("\xa0", "").replace(",", ".")
                try:
                    amt = float(s)
                    if 1 <= amt <= 1_000_000:
                        note_candidates.append((amt, word))
                except ValueError:
                    pass
    if note_candidates:
        tot = sum(a for a, _ in note_candidates)
        items = [f"{w} {int(a) if a.is_integer() else a}₽" for a, w in note_candidates]
        return tot, items

    return None, []


def _extract_amount_from_ocr_lines(lines: list[str]) -> float | None:
    """Ищет итоговую сумму покупки в строках чека или изображения."""
    amt, _ = _extract_amount_and_notes(lines)
    return amt


def scan_receipt_image(
    image_bytes: bytes,
    user_id: int = 0,
    caption: str = "",
    user_existing_categories: list[str] | None = None,
) -> dict:
    """Полный цикл распознавания чека, перевода или фото трат с изображения.

    1. Сканирование QR-кода чека (ФНС 54-ФЗ).
    2. Оптическое распознавание текста (RapidOCR).
    3. Распознавание типа документа:
       - Кассовый чек (QR или строка «ИТОГО»)
       - Банковский перевод (СберБанк, Т-Банк, СБП, Альфа и др.)
       - Скриншот истории покупок из приложения магазина (Магнит, ВкусВилл, Самокат)
       - Записи на бумаге («Продукты 1200», «Такси 450»)
    4. Автоматическая классификация по категориям трат.
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

        category = store_cat
        if not category and merchant:
            category, _ = classify_expense(user_id, merchant, user_existing_categories)
        if not category and caption:
            category, _ = classify_expense(user_id, caption, user_existing_categories)
        if not category:
            category = "Продукты"

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

    # Если QR-кода нет — определяем тип и извлекаем данные из OCR
    amount, note_items = _extract_amount_and_notes(ocr_lines)
    spent_d = doc_date or today_date()

    if amount is not None:
        transfer_info = _find_transfer_details(ocr_lines, full_text)

        # 3a. Банковский перевод
        if transfer_info:
            recipient = transfer_info.get("recipient")
            transfer_comment = transfer_info.get("comment")
            bank_name = merchant if (merchant and ("банк" in merchant.lower() or merchant == "СБП")) else None

            if recipient and bank_name:
                merch_title = f"Перевод ({bank_name}): {recipient}"
            elif recipient:
                merch_title = f"Перевод: {recipient}"
            elif bank_name:
                merch_title = f"Перевод ({bank_name})"
            else:
                merch_title = "Перевод"

            if caption:
                category, _ = classify_expense(user_id, caption, user_existing_categories)
            elif transfer_comment:
                category, _ = classify_expense(user_id, transfer_comment, user_existing_categories)
            elif recipient:
                category, _ = classify_expense(user_id, recipient, user_existing_categories)
            else:
                category = "Разное"

            items = []
            if recipient:
                items.append(f"Кому: {recipient}")
            if bank_name:
                items.append(f"Банк: {bank_name}")
            if transfer_comment:
                items.append(f"Сообщение: {transfer_comment}")

            return {
                "success": True,
                "amount": amount,
                "spent_date": spent_d,
                "merchant": merch_title,
                "category": category or "Разное",
                "items": items,
                "method": "transfer",
            }

        # 3b. Записи от руки или на бумаге
        if note_items:
            if caption:
                category, _ = classify_expense(user_id, caption, user_existing_categories)
            else:
                category, _ = classify_expense(user_id, " ".join(note_items), user_existing_categories)

            merch_label = note_items[0].split()[0] if len(note_items) == 1 else "Запись на бумаге"

            return {
                "success": True,
                "amount": amount,
                "spent_date": spent_d,
                "merchant": merch_label,
                "category": category or "Разное",
                "items": note_items,
                "method": "note",
            }

        # 3c. История покупок в приложении магазина
        if merchant:
            category = store_cat
            if not category:
                category, _ = classify_expense(user_id, merchant, user_existing_categories)
            if not category and caption:
                category, _ = classify_expense(user_id, caption, user_existing_categories)

            items = [l for l in ocr_lines if len(l) > 3 and not re.search(r"(ИТОГ|ИТОГО|КАССОВЫЙ|ФН|ФД|ФП|НДС|СПАСИБО)", l, re.IGNORECASE)]

            return {
                "success": True,
                "amount": amount,
                "spent_date": spent_d,
                "merchant": merchant,
                "category": category or "Продукты",
                "items": items[:5],
                "method": "store_app",
            }

        # 3d. Обычный чек (OCR)
        category = store_cat
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
        "error": "Не удалось найти сумму на изображении чека, перевода или заметки.",
        "merchant": merchant,
        "ocr_lines_found": len(ocr_lines),
    }
