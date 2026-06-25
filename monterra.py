import os, json, time, re, requests
from datetime import datetime
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("MONTERRA_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("MONTERRA_CHAT_ID")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")

SEEN_FILE   = "data/seen_listings.json"
MAX_MILEAGE = 135000
MAX_PHOTOS  = 8
TOP_N       = 5
MIN_SCORE   = 6

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

DESIRABLE = ["bmw", "audi", "mercedes", "škoda", "skoda", "porsche", "volkswagen", "volvo", "lexus"]

# ── Seen listings ──────────────────────────────────────────────────────────────
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    os.makedirs("data", exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ── Mileage ────────────────────────────────────────────────────────────────────
def parse_km(text):
    text = re.sub(r"[\s\xa0]", "", text)
    m = re.search(r"(\d{4,6})km", text, re.IGNORECASE)
    if m:
        v = int(m.group(1))
        if 500 < v < 600000:
            return v
    return None

# ── Bazos ──────────────────────────────────────────────────────────────────────
def scrape_bazos(pages=4):
    base = "https://auto.bazos.sk"
    seen_urls = set()
    urls = []

    for page in range(pages):
        url = f"{base}/" if page == 0 else f"{base}/?od={page * 20}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
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
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Title
        title = ""
        for sel in ["h1.nadpis", "h1"]:
            el = soup.select_one(sel)
            if el:
                title = el.get_text(strip=True)
                break

        # Price — look for element containing € or EUR
        price = ""
        for el in soup.find_all(["b", "strong", "span", "div"]):
            t = el.get_text(strip=True)
            if ("€" in t or "EUR" in t) and any(c.isdigit() for c in t) and len(t) < 30:
                price = t
                break

        # popisdetail has specs + description
        detail_el = soup.select_one("div.popisdetail")
        detail_text = detail_el.get_text(" ", strip=True) if detail_el else ""
        mileage = parse_km(detail_text)

        # Photos — carousel-cell-image is the correct class
        photos = soup.select("img.carousel-cell-image")
        image_count = len(photos)

        # Seller — name is in the href as jmeno= parameter, not the link text
        seller = ""
        seller_link = soup.select_one("a[href*='hodnotenie.php']")
        if seller_link:
            href = seller_link.get("href", "")
            m = re.search(r"jmeno=([^&]+)", href)
            if m:
                seller = requests.utils.unquote(m.group(1)).replace("+", " ").strip()

        # Dealer detection
        dealer_signals = ["s.r.o", "a.s.", "motors", "group", "bazar", "bazár", "trade", "cars s", "auto s"]
        seller_lower = seller.lower()
        is_dealer = any(d in seller_lower for d in dealer_signals)
        seller_type = "dealer" if is_dealer else "private"

        # Location — try multiple selectors
        location = ""
        for sel in [".inzeratylok", ".lokace", "span.locate", ".inzeratymisto"]:
            el = soup.select_one(sel)
            if el:
                location = el.get_text(strip=True)
                break
        # fallback: look for PSČ pattern (Slovak postal code) in page text
        if not location:
            m = re.search(r"(\d{3}\s?\d{2})", soup.get_text())
            if m:
                location = m.group(1)

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
            print(f"  Trying {candidate}: {r.status_code} {len(r.text)} bytes")
            if r.status_code == 200 and len(r.text) > 500:
                soup = BeautifulSoup(r.text, "xml")
                # get sub-sitemap URLs containing "offer" or "inzerat"
                for loc in soup.select("sitemap loc, loc"):
                    u = loc.get_text(strip=True)
                    if any(k in u for k in ["offer", "inzerat", "automobil", "detail"]):
                        sitemap_urls.append(u)
                if sitemap_urls:
                    print(f"  Found {len(sitemap_urls)} sub-sitemaps")
                    break
        except Exception as e:
            print(f"  Sitemap candidate error: {e}")

    # Fetch detail URLs from sub-sitemaps
    for smap in sitemap_urls[:pages]:
        try:
            r = requests.get(smap, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(r.text, "xml")
            locs = soup.select("url loc")
            for loc in locs:
                u = loc.get_text(strip=True)
                if "/detail" in u and u not in urls:
                    urls.append(u)
            print(f"  Sub-sitemap {smap[-30:]}: {len(locs)} locs")
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
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Title from og:title — reliable even in SSR
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

        # Mileage from span containing km
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

        # Seller name
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

        page_text = soup.get_text().lower()
        seller_type = "dealer" if any(w in page_text for w in ["autorizovaný predajca", "s.r.o.", "predajca"]) else "private"

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

# ── AI Scoring ─────────────────────────────────────────────────────────────────
def score_listing(listing):
    photos = listing.get("image_count", 0)
    desc_len = len(listing.get("description", ""))
    title = listing.get("title", "")
    desirable = any(b in title.lower() for b in DESIRABLE)

    prompt = f"""Score this car listing 1-10 for a broker who wants private sellers with BAD listings.

STRICT SCORING RULES — apply these exactly:
- {photos} photos: {"10+ photos = MAXIMUM score is 4" if photos >= 10 else "7-9 photos = MAXIMUM score is 5" if photos >= 7 else "4-6 photos = MAXIMUM score is 7" if photos >= 4 else "1-3 photos = good, score can reach 10"}
- Description {desc_len} chars: {"200+ chars = detailed, penalise -2" if desc_len >= 200 else "under 200 chars = thin, bonus +1"}
- Brand: {"desirable brand detected = bonus +2" if desirable else "undesirable brand = MAXIMUM score is 4"}

Title: {title}
Price: {listing.get("price")}
Photos: {photos}
Mileage: {listing.get("mileage")} km
Description: {listing.get("description", "")[:200]}

Respond ONLY with valid JSON, no other text:
{{"score": <1-10>, "reason": "<one sentence>", "red_flags": "<issues or none>"}}"""

    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 150, "temperature": 0.1},
                timeout=20,
            )
            if r.status_code == 429:
                print(f"  Rate limited, waiting 15s...")
                time.sleep(15)
                continue
            data = r.json()
            if "choices" not in data:
                print(f"  Groq error: {data}")
                break
            raw = data["choices"][0]["message"]["content"].strip()
            raw = re.sub(r"^```.*?\n|```$", "", raw, flags=re.MULTILINE).strip()
            print(f"  Groq: {raw[:80]}")
            parsed = json.loads(raw)
            listing["score"] = int(parsed["score"])
            listing["reason"] = parsed.get("reason", "")
            listing["red_flags"] = parsed.get("red_flags", "none")
            return listing
        except json.JSONDecodeError as e:
            print(f"  JSON parse error: {e} — raw: {raw[:100]}")
        except Exception as e:
            print(f"  Score attempt {attempt+1} error: {e}")
        time.sleep(5)

    listing["score"] = 0
    listing["reason"] = "scoring failed"
    listing["red_flags"] = ""
    return listing

# ── Cross-check ────────────────────────────────────────────────────────────────
def normalize(t):
    return re.sub(r"\s+", " ", t.lower().strip())

def cross_check(listing):
    title_words = listing["title"].split()[:3]
    query = " ".join(title_words)
    price_digits = re.sub(r"[^\d]", "", listing.get("price", ""))
    location = normalize(listing.get("location", ""))
    seller = normalize(listing.get("seller", ""))

    def count_matches(card_text):
        score = 0
        if price_digits and price_digits in re.sub(r"[^\d]", "", card_text):
            score += 1
        if any(w.lower() in card_text for w in title_words):
            score += 1
        if location and any(p.strip() in card_text for p in location.split(",")):
            score += 1
        if seller and seller in card_text:
            score += 1
        return score

    try:
        if listing["source"] == "bazos.sk":
            r = requests.get(
                f"https://www.autobazar.eu/vyhladavanie/?q={requests.utils.quote(query)}",
                headers=HEADERS, timeout=12
            )
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select("a[href*='/detail']"):
                if count_matches(normalize(card.get_text(" "))) >= 3:
                    print(f"  Cross-listed on autobazar: {listing['title'][:40]}")
                    return True

        elif listing["source"] == "autobazar.eu":
            r = requests.get(
                f"https://auto.bazos.sk/?hledat={requests.utils.quote(query)}&rubriky=auto",
                headers=HEADERS, timeout=12
            )
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select(".inzeraty .inzeratynadpis, .inzeraty .popis"):
                if count_matches(normalize(card.get_text(" "))) >= 3:
                    print(f"  Cross-listed on bazos: {listing['title'][:40]}")
                    return True

    except Exception as e:
        print(f"  Cross-check error: {e}")

    return False

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(msg):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=10,
    )

def format_listing(l, rank):
    km = l.get("mileage")
    km_str = f"{km:,} km".replace(",", " ") if km else "km unknown"
    stars = "⭐" * min(round(l.get("score", 0) / 2), 5)
    return (
        f"<b>#{rank} — {l.get('title', '')}</b>\n"
        f"{stars} {l.get('score', 0)}/10\n"
        f"💰 {l.get('price', 'N/A')}  📏 {km_str}\n"
        f"🖼 {l.get('image_count', '?')} photos  👤 {l.get('seller', 'N/A')}\n"
        f"📍 {l.get('location', 'N/A')}\n"
        f"💬 {l.get('reason', '')}\n"
        f"🔗 {l.get('url', '')}"
    )

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
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

    # Remove already sent
    new = [l for l in all_listings if l["url"] not in seen]
    print(f"New (not seen before): {len(new)}")

    # Hard filter: mileage + dealer + too many photos
    filtered = []
    for l in new:
        if l.get("seller_type") == "dealer":
            print(f"  SKIP dealer: {l.get('seller', '?')} — {l.get('title', '')[:40]}")
            continue
        if l.get("mileage") and l["mileage"] > MAX_MILEAGE:
            print(f"  SKIP mileage {l['mileage']}km: {l.get('title', '')[:40]}")
            continue
        if l.get("image_count", 0) > MAX_PHOTOS:
            print(f"  SKIP photos {l['image_count']}: {l.get('title', '')[:40]}")
            continue
        filtered.append(l)
    print(f"After hard filters: {len(filtered)}")

    # Pre-filter by desirable brand before scoring (saves Groq calls)
    desirable = [l for l in filtered if any(b in l.get("title", "").lower() for b in DESIRABLE)]
    other = [l for l in filtered if l not in desirable]
    print(f"Desirable brand: {len(desirable)}, other: {len(other)}")

    # Score desirable first, then others if needed
    to_score = (desirable + other)[:30]
    print(f"\nScoring {len(to_score)} listings with AI...")
    for l in to_score:
        score_listing(l)
        print(f"  {l.get('score', 0)}/10 — {l.get('title', '')[:50]}")
        time.sleep(3)

    # Filter by min score
    scored = sorted(
        [l for l in to_score if l.get("score", 0) >= MIN_SCORE],
        key=lambda x: x["score"], reverse=True
    )
    print(f"\nAbove min score ({MIN_SCORE}): {len(scored)}")

    # Cross-check
    print("\nCross-checking...")
    final = []
    for l in scored:
        if cross_check(l):
            print(f"  SKIP (cross-listed): {l['title'][:40]}")
        else:
            final.append(l)
            print(f"  OK: {l['title'][:40]}")
        time.sleep(1.5)
        if len(final) >= TOP_N:
            break

    # Save seen
    for l in new:
        seen.add(l["url"])
    save_seen(seen)

    # Send
    if not final:
        send_telegram(f"🚗 <b>Monterra — {datetime.now().strftime('%d %b %Y')}</b>\n\nNo strong candidates today.")
        print("No candidates.")
        return

    send_telegram(
        f"🚗 <b>Monterra Cars — {datetime.now().strftime('%d %b %Y')}</b>\n"
        f"<b>{len(final)}</b> candidates from {len(new)} new listings\n━━━━━━━━━━━━━━━━━━━━"
    )
    time.sleep(0.5)
    for i, l in enumerate(final, 1):
        send_telegram(format_listing(l, i))
        time.sleep(0.5)

    print(f"\nDone. Sent {len(final)} listings.")

if __name__ == "__main__":
    main()
