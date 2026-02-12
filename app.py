import re

# "L, M, XS(на мне)" / "38FR" / "50,52" / "50 52" / "50-52"
SIZE_WORDS_RE = re.compile(r"(?i)\b(XXS|XS|S|M|L|XL|XXL)\b")
SIZE_NUM_RE = re.compile(r"^\s*\d{1,3}(\s*[-,/]\s*\d{1,3})+(\s*(FR|IT|EU|US))?\s*$", re.IGNORECASE)
SIZE_SINGLE_NUM_WITH_SYS_RE = re.compile(r"^\s*\d{1,3}\s*(FR|IT|EU|US)\s*$", re.IGNORECASE)

# "Chloé", "DIOR", "entirestudios" (из #entirestudios)
HASHTAG_BRAND_RE = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,30})")

def is_service_line(line: str) -> bool:
    t = line.strip()
    if not t:
        return True
    low = t.lower()
    if low.startswith("по вопросам"):
        return True
    if low.startswith("вопрос"):
        return True
    if t.startswith("@"):
        return True
    return False

def looks_like_price_line(line: str) -> bool:
    """
    Нам нужна строка, где видно скидку.
    Обычно содержит '€' и '%' (или хотя бы '=€' / '=%' но чаще %).
    """
    t = line.strip()
    if "€" not in t:
        return False
    # скидка почти всегда содержит %
    if "%" in t:
        return True
    # иногда бывают форматы без %, но с "=": "1990€=€796,00"
    if "=" in t:
        return True
    return False

def find_price_line(lines: list[str]) -> str:
    # 1) приоритет — строка со скидкой (€, %)
    for ln in lines:
        if looks_like_price_line(ln):
            return ln.strip()

    # 2) запасной вариант — первая строка с €
    for ln in lines:
        if "€" in ln:
            return ln.strip()

    return ""

def is_size_line(line: str) -> bool:
    t = line.strip()
    if not t or t.startswith("#") or is_service_line(t):
        return False

    if SIZE_WORDS_RE.search(t):
        return True

    # чисто числовые размеры: "50,52" / "50-52" / "50/52"
    if SIZE_NUM_RE.match(t):
        return True

    # "38FR"
    if SIZE_SINGLE_NUM_WITH_SYS_RE.match(t):
        return True

    return False

def find_size_line(lines: list[str]) -> str | None:
    # Сначала ищем отдельной строкой
    for ln in lines:
        if is_size_line(ln):
            return ln.strip()

    # Иногда размер приписан к строке с ценой: "… €796,00 38FR"
    for ln in lines:
        if "€" in ln:
            tail = ln.strip()
            # берем все после последней цены "€число"
            m = re.search(r"€\s*[0-9\.\,]+\s*(.+)$", tail)
            if m:
                maybe = m.group(1).strip()
                if maybe and (SIZE_WORDS_RE.search(maybe) or SIZE_NUM_RE.match(maybe) or SIZE_SINGLE_NUM_WITH_SYS_RE.match(maybe)):
                    return maybe
    return None

def extract_brand_from_lines(lines: list[str]) -> str:
    """
    Бренд есть всегда, просто может быть не в первой строке.
    Правила:
    1) Если есть хэштег #brand — берем его (UPPER).
    2) Иначе берем первую "нормальную" строку, которая НЕ цена, НЕ размер, НЕ сервисная.
       (и не голая цифра)
    3) Если не нашли — вернем пустую строку (но ты говоришь, что бренд всегда есть).
    """
    # 1) бренд из хэштега
    for ln in lines:
        m = HASHTAG_BRAND_RE.search(ln)
        if m:
            return m.group(1).upper()

    # 2) бренд как строка текста
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        if is_service_line(t):
            continue
        if t.startswith("#"):
            continue
        if looks_like_price_line(t):
            continue
        if is_size_line(t):
            continue
        # отсекаем "8" / "50,52" и прочее числовое
        if re.fullmatch(r"[\d\s,\.]+", t):
            continue

        # Это и будет бренд (эмодзи сохраняются)
        return t

    return ""

def parse_caption_to_card(text: str) -> dict:
    """
    Возвращает 3 строки для каталога:
    1) бренд
    2) цена (полная строка со скидкой)
    3) размеры (если есть)
    """
    raw_lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines = [ln.strip() for ln in raw_lines if ln.strip()]

    brand_line = extract_brand_from_lines(lines)
    price_line = find_price_line(lines)
    size_line = find_size_line(lines)

    return {
        "brand_line": brand_line,          # всегда стараемся найти
        "price_line": price_line,          # полная строка со скидкой
        "size_line": size_line,            # может быть None
    }
