import os, json, time, re, requests, unicodedata, hashlib
from html import escape
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("MONTERRA_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("MONTERRA_CHAT_ID")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")

SEEN_FILE       = "data/seen_listings.json"
FINGERPRINT_FILE = "data/fingerprints.json"
MAX_MILEAGE     = 135000
MAX_PHOTOS      = 8
TOP_N           = 5
MIN_SCORE       = 7
MAX_RETRIES     = 3
RETRY_DELAY     = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
}

DESIRABLE = ["bmw", "audi", "mercedes", "škoda", "skoda", "porsche", "volkswagen", "volvo", "lexus"]
DESIRABLE_NORMALIZED = {
    unicodedata.normalize("NFKD", brand).encode("ascii", "ignore").decode("ascii").lower()
    for brand in DESIRABLE
}

DEALER_SIGNALS = [
    "s.r.o", "a.s.", "motors", "group", "bazar", "bazár", "trade",
    "cars s", "auto s", "dealer", "predajca", "komercni", "obchodne", "inc", "ltd", "gmbh",
]

LAZY_DESC_PATTERNS = [
    r"^(see|pozri|vid).{0,20}photo",
    r"^(only|len|iba).{0,20}photo",
    r"^vin.{0,20}\d{17}",
    r"^([a-z0-9]{17})$",
]

# ── Utilities ──────────────────────────────────────────────────────────────────
def require_config():
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("MONTERRA_TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("MONTERRA_CHAT_ID")
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

def normalize_text(value):
    value = "" if value is None else str(value)
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower().strip()

def request_with_retry(url, timeout=12):
    """Fetch URL with exponential backoff retry on network errors."""
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (2 ** attempt)
                print(f"    Retry {attempt + 1}/{MAX_RETRIES} after {wait}s: {e}")
                time.sleep(wait)
            else:
                raise
    return None

# ── Deduplication ──────────────────────────────────────────────────────────────
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    os.makedirs("data", exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def load_fingerprints():
    if os.path.exists(FINGERPRINT_FILE):
        try:
            with open(FINGERPRINT_FILE) as f:
                return set(json.load(f))
        except:
            return set()
    return set()

def save_fingerprints(fingerprints):
    os.makedirs("data", exist_ok=True)
    with open(FINGERPRINT_FILE, "w") as f:
        json.dump(list(fingerprints), f)

def compute_fingerprint(title, price, mileage):
    """Create a hash from normalized title+price+mileage to detect duplicates locally."""
    key = f"{normalize_text(title)}|{normalize_text(str(price))}|{mileage or 'none'}"
    return hashlib.md5(key.encode()).hexdigest()

def is_duplicate(listing, fingerprints):
    """Check if listing is likely duplicate based on fingerprint."""
    fp = compute_fingerprint(listing["title"], listing["price"], listing["mileage"])
    return fp in fingerprints

# ── Parsing ────────────────────────────────────────────────────────────────────
def parse_km(text):
    text = re.sub(r"[\s\xa0]", "", text)
    m = re.search(r"(\d{4,6})km", text, re.IGNORECASE)
    if m:
        v = int(m.group(1))
        if 500 < v < 600000:
            return v
    return None

def parse_price(text):
    """Parse price with validation; returns None if unparseable or implausible."""
    if not text:
        return None
    normalized = normalize_text(text)
    digits = re.sub(r"[^\d]", "", normalized)
    if len(digits) < 4:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    if 500 <= value <= 500000:
        return value
    return None

def is_lazy_description(desc):
    """Detect empty/lazy listings with only VINs or photo refs."""
    if not desc or len(desc) < 15:
        return True
    normalized = normalize_text(desc)
    return any(re.search(pattern, normalized) for pattern in LAZY_DESC_PATTERNS)

# ── Bazos ──────────────────────────────────────────────────────────────────────
def scrape_bazos(pages=4):
    base = "https://auto.bazos.sk"
    seen_urls = set()
    urls = []

    for page in range(pages):
        url = f"{base}/" if page == 0 else f"{base}/?od={page * 20}"
        try:
            r = request_with_retry(url, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            found = 0
            for a in soup.select(".inzeratynadpis a"):
                href = a.get("href", "")
                if href.startswith("/inzerat/"):
                    full = base + href
                    if full not in seen_urls:
                        seen_urls.add(full)
                        urls.append(full)
                        found += 1
            print(f"  Bazos page {page}: {found} new URLs")
            time.sleep(1)
        except Exception as e:
            print(f"  Bazos index error p{page}: {e}")

    print(f"  Bazos: {len(urls)} unique URLs total")

    listings = []
    for url in urls:
        l = scrape_bazos_detail(url)
        if l:
            listings.append(l)
        time.sleep(1)
    return listings

def scrape_bazos_detail(url):
    try:
        r = request_with_retry(url, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Title
        title = ""
        for sel in ["h1.nadpis", "h1"]:
            el = soup.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                break

        # Price
        price = ""
        for el in soup.find_all(["b", "strong", "span", "div"]):
            t = el.get_text(strip=True)
            if ("€" in t or "EUR" in t) and any(c.isdigit() for c in t) and len(t) < 30:
                price = t
                break

        # Mileage and description
        detail_el = soup.select_one("div.popisdetail")
        detail_text = detail_el.get_text(" ", strip=True) if detail_el else ""
        mileage = parse_km(detail_text)

        # Photos
        photos = soup.select("img.carousel-cell-image")
        image_count = len(photos)

        # Seller
        seller = ""
        seller_link = soup.select_one("a[href*='hodnotenie.php']")
        if seller_link:
            href = seller_link.get("href", "")
            m = re.search(r"jmeno=([^&]+)", href)
            if m:
                seller = requests.utils.unquote(m.group(1)).replace("+", " ").strip()

        seller_lower = normalize_text(seller)
        is_dealer = any(d in seller_lower for d in DEALER_SIGNALS)
        seller_type = "dealer" if is_dealer else "private"

        # Location
        location = ""
        for sel in [".inzeratylok", ".lokace", "span.locate", ".inzeratymisto"]:
            el = soup.select_one(sel)
            if el:
                location = el.get_text(strip=True)
                break

        return {
            "source": "bazos.sk",
            "url": url,
            "title": title,
            "price": price,
            "description": detail_text[:600],
            "mileage": mileage,
            "image_count": image_count,
            "seller": seller,
            "location": location,
            "seller_type": seller_type,
        }
    except Exception as e:
        print(f"  Bazos detail error: {e}")
        return None

# ── Autobazar ──────────────────────────────────────────────────────────────────
def scrape_autobazar(pages=4):
    base = "https://www.autobazar.eu"
    urls = []

    # Try multiple sitemap URL patterns
    sitemap_candidates = [
        f"{base}/sitemap.xml",
        f"{base}/sitemap-index.xml",
        f"{base}/sitemap_index.xml",
    ]

    sitemap_urls = []
    for candidate in sitemap_candidates:
        try:
            r = requests.get(candidate, headers=HEADERS, timeout=10)
            if r.status_code == 200 and len(r.text) > 500:
                soup = BeautifulSoup(r.text, "xml")
                for loc in soup.select("sitemap loc, loc"):
                    u = loc.get_text(strip=True)
                    if any(k in u for k in ["offer", "inzerat", "automobil", "detail"]):
                        sitemap_urls.append(u)
                if sitemap_urls:
                    print(f"  Found {len(sitemap_urls)} sub-sitemaps")
                    break
        except Exception as e:
            print(f"  Sitemap error: {e}")

    # Fallback: if sitemap discovery fails, use real category result pages from the homepage.
    if not sitemap_urls:
        fallback_pages = [
            f"{base}/vysledky/osobne-vozidla/bmw/",
            f"{base}/vysledky/osobne-vozidla/audi/",
            f"{base}/vysledky/osobne-vozidla/skoda/",
            f"{base}/vysledky/osobne-vozidla/volkswagen/",
            f"{base}/vysledky/osobne-vozidla/mercedes-benz/",
            f"{base}/vysledky/osobne-vozidla/volvo/",
        ]
        for page_url in fallback_pages:
            try:
                r = request_with_retry(page_url, timeout=15)
                soup = BeautifulSoup(r.text, "html.parser")
                found = 0
                for link in soup.select("a[href*='/detail']"):
                    href = link.get("href", "")
                    if href.startswith("http"):
                        full_url = href
                    elif href.startswith("/"):
                        full_url = base + href
                    else:
                        continue
                    if full_url not in urls:
                        urls.append(full_url)
                        found += 1
                print(f"  Autobazar fallback '{page_url.split('/')[-2]}': {found} listing URLs")
                if urls:
                    break
            except Exception as e:
                print(f"  Autobazar fallback error for '{page_url}': {e}")

    # Fetch detail URLs from sub-sitemaps
    for smap in sitemap_urls[:pages]:
        try:
            r = request_with_retry(smap, timeout=15)
            soup = BeautifulSoup(r.text, "xml")
            locs = soup.select("url loc")
            for loc in locs:
                u = loc.get_text(strip=True)
                if "/detail" in u and u not in urls:
                    urls.append(u)
            print(f"  Sub-sitemap fetched: {len(locs)} URLs")
            time.sleep(1)
        except Exception as e:
            print(f"  Sub-sitemap error: {e}")

    print(f"  Autobazar: {len(urls)} listing URLs found")
    listings = []
    for url in urls[:60]:
        l = scrape_autobazar_detail(url)
        if l:
            listings.append(l)
        time.sleep(1)
    return listings

def scrape_autobazar_detail(url):
    try:
        r = request_with_retry(url, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Title from og:title
        title = ""
        og = soup.select_one('meta[property="og:title"]')
        if og:
            title = og.get("content", "").strip()

        # Price
        price = ""
        for el in soup.select("div.text-xl, div.font-semibold, span.font-semibold"):
            t = el.get_text(strip=True)
            if "€" in t and any(c.isdigit() for c in t):
                price = t
                break

        # Mileage
        mileage = None
        for el in soup.select("span, div"):
            t = el.get_text(strip=True)
            if re.search(r"\d{3,6}\s*km", t, re.IGNORECASE) and len(t) < 20:
                mileage = parse_km(t)
                if mileage:
                    break

        # Photos
        photos = soup.select("img[src*='autobazar'], img[src*='img.autobazar']")
        image_count = len(photos)

        # Seller
        seller = ""
        for el in soup.select("div.font-bold, span.font-bold"):
            t = el.get_text(strip=True)
            if t and 2 < len(t) < 40 and "€" not in t and "km" not in t.lower():
                seller = t
                break

        # Location
        location = ""
        loc_el = soup.select_one("a[href*='maps.google.com']")
        if loc_el:
            location = loc_el.get_text(strip=True)

        # Description
        desc = ""
        for sel in ["div.description", "section.description", "[class*='popis']"]:
            el = soup.select_one(sel)
            if el:
                desc = el.get_text(" ", strip=True)[:600]
                break

        page_text = normalize_text(soup.get_text())
        seller_type = "dealer" if any(w in page_text for w in ["autorizovany", "s.r.o."]) else "private"

        return {
            "source": "autobazar.eu",
            "url": url,
            "title": title,
            "price": price,
            "description": desc,
            "mileage": mileage,
            "image_count": image_count,
            "seller": seller,
            "location": location,
            "seller_type": seller_type,
        }
    except Exception as e:
        print(f"  Autobazar detail error: {e}")
        return None

# ── Scoring (focus on actual value + honest presentation) ────────────────────
def score_listing(listing):
    title = normalize_text(listing.get("title", ""))
    description = normalize_text(listing.get("description", ""))
    seller = normalize_text(listing.get("seller", ""))
    seller_type = listing.get("seller_type", "private")
    mileage = listing.get("mileage")
    price = parse_price(listing.get("price", ""))

    if not title:
        listing["score"] = 0
        listing["reason"] = "missing title"
        listing["red_flags"] = "missing title"
        return listing

    if seller_type == "dealer" or any(signal in seller for signal in DEALER_SIGNALS):
        listing["score"] = 0
        listing["reason"] = "dealer seller"
        listing["red_flags"] = "dealer seller"
        return listing

    score = 0
    reasons = []

    # Brand: desirable = +3, others = +1 (not as important)
    brand_hit = next((brand for brand in DESIRABLE_NORMALIZED if brand in title), None)
    if brand_hit:
        score += 3
        reasons.append(brand_hit)
    else:
        score += 1
        reasons.append("solid brand")

    # MILEAGE: hard requirement
    if mileage is None:
        listing["score"] = 0
        listing["reason"] = "mileage unknown"
        listing["red_flags"] = "mileage unknown"
        return listing
    elif mileage > MAX_MILEAGE:
        listing["score"] = 0
        listing["reason"] = "too much mileage"
        listing["red_flags"] = "too much mileage"
        return listing
    elif mileage <= 90000:
        score += 3
        reasons.append("low mileage")
    else:
        score += 1
        reasons.append(f"within limit {mileage:,} km")

    # PRICE: must be declared
    if price is None:
        listing["score"] = 0
        listing["reason"] = "price not declared"
        listing["red_flags"] = "price not declared"
        return listing
    score += 1
    reasons.append("price declared")

    # DESCRIPTION: honest & simple = GOOD, overselling = BAD
    if is_lazy_description(description):
        listing["score"] = 0
        listing["reason"] = "empty description"
        listing["red_flags"] = "empty description"
        return listing

    desc_len = len(description)
    if desc_len < 100:
        # Small, honest description = good (not overly marketed)
        score += 3
        reasons.append("honest brief listing")
    elif desc_len < 250:
        # Moderate description
        score += 2
        reasons.append("clear description")
    elif desc_len < 400:
        # Longer but not excessive
        score += 1
        reasons.append("detailed listing")
    else:
        # Too long/professional = potential over-marketing
        score += 0
        reasons.append("extensive marketing")

    # VALUE SIGNALS: proof of quality
    value_indicators = 0
    if any(w in description for w in ["servisna", "servis", "service", "maintenance", "údržba"]):
        value_indicators += 1
        reasons.append("service history")
    if any(w in description for w in ["originál", "original", "org", "vin"]):
        value_indicators += 1
        reasons.append("documented history")
    if any(w in description for w in ["full", "kompletny", "kompletnú", "all records", "všetky"]):
        value_indicators += 1
        reasons.append("full documentation")
    
    score += min(value_indicators, 2)  # Cap at +2 for documentation

    # FINALIZE SCORE
    listing["score"] = max(1, min(10, score))
    listing["reason"] = ", ".join(reasons[:4])
    listing["red_flags"] = "none"
    return listing

# ── Cross-check (verify not on other platform) ────────────────────────────────
def cross_check(listing):
    """Search opposite marketplace to verify listing isn't also posted there.
    If found on opposite platform, it's likely not exclusive/good — reject it."""
    source = listing.get("source", "")
    title = listing.get("title", "")
    price_str = listing.get("price", "")
    location = listing.get("location", "")
    seller = listing.get("seller", "")
    
    if not title or len(title) < 5:
        return False  # Can't search with empty title
    
    # Extract key words for search (first 3-4 important words)
    title_words = title.split()[:4]
    query = " ".join(title_words)
    
    # Extract price digits for matching
    price_digits = re.sub(r"[^\d]", "", price_str)
    
    def match_listing(card_text, card_price, card_seller):
        """Score how well a found listing matches our candidate."""
        match_score = 0
        
        # Price match
        if price_digits and price_digits in re.sub(r"[^\d]", "", card_price):
            match_score += 2
        
        # Title words match
        title_match_count = sum(1 for w in title_words if w.lower() in card_text.lower())
        if title_match_count >= 3:
            match_score += 2
        elif title_match_count >= 2:
            match_score += 1
        
        # Location match
        if location and location.lower() in card_text.lower():
            match_score += 1
        
        # Seller match (if populated)
        if seller and seller.lower() in card_text.lower():
            match_score += 1
        
        return match_score
    
    try:
        if source == "autobazar.eu":
            # Search bazos.sk
            print(f"  Cross-checking on bazos.sk: '{query}'")
            search_url = f"https://auto.bazos.sk/?hledat={requests.utils.quote(query)}&rubriky=auto"
            r = request_with_retry(search_url, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            
            for item in soup.select(".inzeraty"):
                card_text = item.get_text(" ")
                
                # Extract price from listing
                price_el = item.select_one(".inzeratycena")
                card_price = price_el.get_text() if price_el else ""
                
                # Extract seller
                seller_el = item.select_one(".inzeratyprodavajuci")
                card_seller = seller_el.get_text() if seller_el else ""
                
                match_score = match_listing(card_text, card_price, card_seller)
                
                if match_score >= 3:
                    print(f"    ✗ FOUND ON BAZOS: {title[:40]}")
                    return True
            
        elif source == "bazos.sk":
            # Search autobazar.eu
            print(f"  Cross-checking on autobazar.eu: '{query}'")
            search_url = f"https://www.autobazar.eu/vyhladavanie/?q={requests.utils.quote(query)}"
            r = request_with_retry(search_url, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            
            for item in soup.select("a[href*='/detail']"):
                card_text = item.get_text(" ")
                card_price = item.get_text()  # Price usually in link text
                card_seller = ""  # Not always available in search results
                
                match_score = match_listing(card_text, card_price, card_seller)
                
                if match_score >= 3:
                    print(f"    ✗ FOUND ON AUTOBAZAR: {title[:40]}")
                    return True
    
    except Exception as e:
        print(f"    ⚠ Cross-check error: {e}")
        # On error, don't reject the listing — assume it's OK
        return False
    
    return False

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(msg):
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=10,
    )
    response.raise_for_status()

def format_listing(l, rank):
    km = l.get("mileage")
    km_str = f"{km:,} km".replace(",", " ") if km else "km unknown"
    stars = "⭐" * min(round(l.get("score", 0) / 2), 5)
    return (
        f"<b>#{rank} — {escape(str(l.get('title', '')))}</b>\n"
        f"{stars} {l.get('score', 0)}/10\n"
        f"💰 {escape(str(l.get('price', 'N/A')))}  📏 {escape(km_str)}\n"
        f"🖼 {escape(str(l.get('image_count', '?')))} photos  👤 {escape(str(l.get('seller', 'N/A')))}\n"
        f"📍 {escape(str(l.get('location', 'N/A')))}\n"
        f"💬 {escape(str(l.get('reason', '')))}\n"
        f"🔗 {escape(str(l.get('url', '')))}"
    )

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    require_config()
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Monterra Cars starting...")
    seen = load_seen()

    print("\nScraping Bazos.sk...")
    bazos = scrape_bazos(pages=4)
    print(f"  → {len(bazos)} fetched")

    print("\nScraping Autobazar.eu...")
    autobazar = scrape_autobazar(pages=4)
    print(f"  → {len(autobazar)} fetched")

    # Deduplicate
    all_seen_urls = set()
    all_listings = []
    for l in bazos + autobazar:
        if l["url"] not in all_seen_urls:
            all_seen_urls.add(l["url"])
            all_listings.append(l)
    print(f"Unique listings: {len(all_listings)}")

    # Remove already sent + fingerprint dedup
    fingerprints = load_fingerprints()
    new = []
    for l in all_listings:
        if l["url"] not in seen and not is_duplicate(l, fingerprints):
            new.append(l)
            fp = compute_fingerprint(l["title"], l["price"], l["mileage"])
            fingerprints.add(fp)
    print(f"New (not seen, not duplicate): {len(new)}")
    save_fingerprints(fingerprints)

    # Hard filters
    filtered = []
    for l in new:
        if l.get("seller_type") == "dealer":
            print(f"  SKIP dealer: {l.get('seller', '?')} — {l.get('title', '')[:40]}")
            continue
        if l.get("mileage") and l["mileage"] > MAX_MILEAGE:
            print(f"  SKIP mileage {l['mileage']}km: {l.get('title', '')[:40]}")
            continue
        filtered.append(l)
    print(f"After hard filters: {len(filtered)}")

    # Pre-filter by desirable brand
    desirable = [l for l in filtered if any(b in l.get("title", "").lower() for b in DESIRABLE)]
    other = [l for l in filtered if l not in desirable]
    print(f"Desirable brand: {len(desirable)}, other: {len(other)}")

    # Score
    to_score = (desirable + other)[:30]
    print(f"\nScoring {len(to_score)} listings...")
    for l in to_score:
        score_listing(l)
        print(f"  {l.get('score', 0)}/10 — {l.get('title', '')[:50]}")
        time.sleep(0.2)

    # Filter by min score
    scored = sorted(
        [l for l in to_score if l.get("score", 0) >= MIN_SCORE],
        key=lambda x: x["score"], reverse=True
    )
    print(f"\nAbove min score ({MIN_SCORE}): {len(scored)}")

    # Cross-check against opposite platform
    print("\nCross-checking against opposite platform...")
    verified = []
    for l in scored:
        if cross_check(l):
            print(f"  SKIP (cross-listed): {l['title'][:40]}")
        else:
            print(f"  OK (exclusive): {l['title'][:40]}")
            verified.append(l)
        time.sleep(1.5)  # Be respectful to sites
        if len(verified) >= TOP_N:
            break
    
    # Final selection
    print("\nFinal selection...")
    final = sorted(verified, key=lambda x: x["score"], reverse=True)[:TOP_N]
    for l in final:
        print(f"  ✓ {l['score']}/10: {l['title'][:40]}")
        print(f"      → {l['reason']}")

    # Save seen
    for l in new:
        seen.add(l["url"])
    save_seen(seen)

    # Send
    if not final:
        send_telegram(f"🚗 <b>Monterra — {datetime.now().strftime('%d %b %Y')}</b>\n\nNo strong candidates today (either no matches or all cross-listed on other platforms).")
        print("No candidates.")
        return

    # Batch send
    header_msg = (
        f"🚗 <b>Monterra Cars — {datetime.now().strftime('%d %b %Y')}</b>\n"
        f"<b>{len(final)}</b> candidates from {len(new)} new listings\n━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(header_msg)
    time.sleep(0.5)
    for i, l in enumerate(final, 1):
        send_telegram(format_listing(l, i))
        time.sleep(0.2)

    print(f"\nDone. Sent {len(final)} listings.")

if __name__ == "__main__":
    main()
