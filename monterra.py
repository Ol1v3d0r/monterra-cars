import os
import json
import time
import re
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("MONTERRA_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("MONTERRA_CHAT_ID")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")

SEEN_FILE    = "data/seen_listings.json"
MAX_MILEAGE  = 135000
TOP_N        = 5
MIN_SCORE    = 6

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
}

# ── Seen listings ──────────────────────────────────────────────────────────────
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    os.makedirs("data", exist_ok=True)
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ── Mileage helper ─────────────────────────────────────────────────────────────
def parse_km(text: str):
    """Extract first plausible km figure from a string."""
    text = text.replace("\xa0", " ").replace(",", "").replace(" ", "")
    m = re.search(r"(\d{4,6})km", text, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if 1000 < val < 500000:
            return val
    return None

# ── Bazos scraper ──────────────────────────────────────────────────────────────
def scrape_bazos_listing(url: str) -> dict | None:
    """Fetch and parse a single Bazos detail page."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        title = soup.select_one("h1.nadpis")
        title = title.get_text(strip=True) if title else ""

        price_el = soup.select_one(".cena b")
        price = price_el.get_text(strip=True) if price_el else ""

        # description + specs live in div.popisdetail
        detail = soup.select_one("div.popisdetail")
        detail_text = detail.get_text(" ", strip=True) if detail else ""

        # mileage from detail text
        mileage = parse_km(detail_text)

        # photo count
        photos = soup.select("div.carousel img, .fotos img, #bigphoto img")
        # bazos often puts thumbs in a table
        if not photos:
            photos = soup.select("img[src*='foto']")
        image_count = len(photos)

        # seller name — appears in the contact table
        seller_el = soup.select_one("table.listainzeratov td b, .inzeratykontakt b")
        seller = seller_el.get_text(strip=True) if seller_el else ""

        # location
        loc_el = soup.select_one("span.locate, .inzeratylok")
        location = loc_el.get_text(strip=True) if loc_el else ""

        return {
            "source": "bazos.sk",
            "url": url,
            "title": title,
            "price": price,
            "description": detail_text,
            "mileage": mileage,
            "image_count": image_count,
            "seller": seller,
            "location": location,
            "seller_type": "private",  # bazos is private-only
        }
    except Exception as e:
        print(f"  Bazos detail error {url}: {e}")
        return None

def scrape_bazos(pages=4) -> list[dict]:
    """Scrape listing URLs from Bazos auto index, then fetch each detail page."""
    base = "https://auto.bazos.sk"
    urls = []

    for page in range(pages):
        index_url = f"{base}/" if page == 0 else f"{base}/?od={page * 20}"
        try:
            r = requests.get(index_url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.select(".inzeratynadpis a")
            for a in links:
                href = a.get("href", "")
                if href.startswith("/inzerat/"):
                    urls.append(base + href)
            time.sleep(1.5)
        except Exception as e:
            print(f"  Bazos index page {page} error: {e}")

    print(f"  Bazos: {len(urls)} listing URLs found")
    listings = []
    for url in urls:
        result = scrape_bazos_listing(url)
        if result:
            listings.append(result)
        time.sleep(1.2)
    return listings

# ── Autobazar scraper ──────────────────────────────────────────────────────────
def scrape_autobazar_listing(url: str) -> dict | None:
    """Fetch and parse a single Autobazar detail page."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # title from og tag (reliable)
        title = ""
        og_title = soup.select_one('meta[property="og:title"]')
        if og_title:
            title = og_title.get("content", "")

        # price — div with text-xl font-semibold containing €
        price = ""
        for el in soup.select("div.text-xl.font-semibold"):
            t = el.get_text(strip=True)
            if "€" in t:
                price = t
                break

        # mileage — span with those exact tailwind classes containing km
        mileage = None
        for el in soup.select("span.font-bold"):
            t = el.get_text(strip=True)
            if "km" in t.lower():
                mileage = parse_km(t)
                if mileage:
                    break

        # image count — count og:image tags or gallery imgs
        photos = soup.select("img[src*='autobazar'], img[src*='s.autobazar']")
        image_count = len(photos)

        # seller name
        seller = ""
        for el in soup.select("div.font-bold"):
            t = el.get_text(strip=True)
            if t and len(t) < 40 and not any(c in t for c in ["€", "km", "kW"]):
                seller = t
                break

        # location — the google maps link
        location = ""
        loc_el = soup.select_one("a[href*='maps.google.com']")
        if loc_el:
            location = loc_el.get_text(strip=True)

        # description
        desc = ""
        desc_el = soup.select_one("div.description, section.description, [class*='description']")
        if desc_el:
            desc = desc_el.get_text(" ", strip=True)

        # dealer detection — autobazar mixes private and dealers
        page_text = soup.get_text().lower()
        seller_type = "dealer" if any(w in page_text for w in ["autorizovaný predajca", "autobazár", "s.r.o", "auto s.r.o"]) else "private"

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
        print(f"  Autobazar detail error {url}: {e}")
        return None

def scrape_autobazar(pages=4) -> list[dict]:
    """Scrape listing URLs from Autobazar index, then fetch each detail page."""
    base = "https://www.autobazar.eu"
    urls = []

    for page in range(1, pages + 1):
        index_url = f"{base}/osobne-automobily/?page={page}"
        try:
            r = requests.get(index_url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            # detail links follow /detail/ pattern
            links = soup.select("a[href*='/detail/']")
            for a in links:
                href = a.get("href", "")
                full = base + href if href.startswith("/") else href
                if full not in urls:
                    urls.append(full)
            time.sleep(1.5)
        except Exception as e:
            print(f"  Autobazar index page {page} error: {e}")

    print(f"  Autobazar: {len(urls)} listing URLs found")
    listings = []
    for url in urls:
        result = scrape_autobazar_listing(url)
        if result:
            listings.append(result)
        time.sleep(1.2)
    return listings

# ── Cross-platform check ───────────────────────────────────────────────────────
def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())

def cross_check(listing: dict) -> bool:
    """
    Search the OTHER platform for this car.
    Match on: price + make/model + location + seller name.
    3 out of 4 matching = cross-listed.
    Returns True if cross-listed (should be skipped).
    """
    title_words = listing["title"].split()[:3]  # e.g. ["Audi", "A3", "2021"]
    search_query = " ".join(title_words)
    price_raw = re.sub(r"[^\d]", "", listing.get("price", ""))
    location = normalize(listing.get("location", ""))
    seller = normalize(listing.get("seller", ""))

    try:
        if listing["source"] == "bazos.sk":
            url = f"https://www.autobazar.eu/vyhladavanie/?q={requests.utils.quote(search_query)}"
            r = requests.get(url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")

            for card in soup.select("a[href*='/detail/']"):
                card_text = normalize(card.get_text(" ", strip=True))
                matches = 0
                if price_raw and price_raw in re.sub(r"[^\d]", "", card_text):
                    matches += 1
                if any(w.lower() in card_text for w in title_words):
                    matches += 1
                if location and any(loc_part in card_text for loc_part in location.split(",")):
                    matches += 1
                if seller and seller in card_text:
                    matches += 1
                if matches >= 3:
                    print(f"  Cross-listed (bazos→autobazar, {matches}/4 matches): {listing['title']}")
                    return True

        elif listing["source"] == "autobazar.eu":
            url = f"https://auto.bazos.sk/?hledat={requests.utils.quote(search_query)}&rubriky=auto"
            r = requests.get(url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")

            for card in soup.select(".inzeraty .inzeratynadpis, .inzeraty .popis"):
                card_text = normalize(card.get_text(" ", strip=True))
                matches = 0
                if price_raw and price_raw in re.sub(r"[^\d]", "", card_text):
                    matches += 1
                if any(w.lower() in card_text for w in title_words):
                    matches += 1
                if location and any(loc_part in card_text for loc_part in location.split(",")):
                    matches += 1
                if seller and seller in card_text:
                    matches += 1
                if matches >= 3:
                    print(f"  Cross-listed (autobazar→bazos, {matches}/4 matches): {listing['title']}")
                    return True

    except Exception as e:
        print(f"  Cross-check error for {listing['title']}: {e}")
        # Don't skip on error — let it through and you check manually
        return False

    return False

# ── AI scoring (Groq) ──────────────────────────────────────────────────────────
def score_listing(listing: dict) -> dict:
    prompt = f"""You are helping a car broker in Slovakia find private car listings worth approaching.

The broker's model: find private sellers with low-quality listings (bad photos, thin description), get permission to re-list professionally across all platforms, earn a small commission on sale.

Score this listing 1-10. High score = worth approaching. Low score = skip.

What makes a HIGH score:
- Few photos (1-4) and/or visibly low quality
- Vague or thin description
- Private seller confirmed
- Desirable brand: BMW, Audi, Mercedes, Škoda, Porsche, Volkswagen, Volvo, Lexus

What makes a LOW score:
- Many photos (7+) especially professional ones
- Detailed description
- Dealer listing
- Undesirable or very common car

Listing:
Title: {listing.get('title')}
Price: {listing.get('price')}
Source: {listing.get('source')}
Photos: {listing.get('image_count')}
Mileage: {listing.get('mileage')} km
Seller type: {listing.get('seller_type')}
Description: {listing.get('description', '')[:400]}

Reply ONLY in this exact JSON, no other text:
{{"score": <1-10>, "reason": "<one sentence>", "red_flags": "<dealbreakers or none>"}}"""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.2,
            },
            timeout=15,
        )
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        listing["score"] = result.get("score", 0)
        listing["reason"] = result.get("reason", "")
        listing["red_flags"] = result.get("red_flags", "")
    except Exception as e:
        print(f"  Score error: {e}")
        listing["score"] = 0
        listing["reason"] = "Scoring failed"
        listing["red_flags"] = ""
    return listing

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    if not r.ok:
        print(f"  Telegram error: {r.text}")

def format_listing(listing: dict, rank: int) -> str:
    mileage = listing.get("mileage")
    mileage_str = f"{mileage:,} km".replace(",", " ") if mileage else "not found"
    score = listing.get("score", 0)
    stars = "⭐" * min(round(score / 2), 5)

    return (
        f"<b>#{rank} — {listing.get('title', '')}</b>\n"
        f"{stars} <b>{score}/10</b>\n"
        f"💰 {listing.get('price', 'N/A')}\n"
        f"📏 {mileage_str}\n"
        f"🖼 {listing.get('image_count', '?')} photos\n"
        f"📍 {listing.get('location', 'N/A')}\n"
        f"👤 {listing.get('seller', 'N/A')}\n"
        f"💬 {listing.get('reason', '')}\n"
        f"🔗 {listing.get('url', '')}"
    )

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Monterra Cars starting...")
    seen = load_seen()

    # 1. Scrape both platforms
    print("\nScraping Bazos.sk...")
    bazos = scrape_bazos(pages=4)
    print(f"  → {len(bazos)} listings fetched")

    print("\nScraping Autobazar.eu...")
    autobazar = scrape_autobazar(pages=4)
    print(f"  → {len(autobazar)} listings fetched")

    all_listings = bazos + autobazar

    # 2. Remove already seen
    new = [l for l in all_listings if l["url"] not in seen]
    print(f"\nNew listings (not seen before): {len(new)}")

    # 3. Hard filters: mileage + dealer
    filtered = []
    for l in new:
        if l.get("seller_type") == "dealer":
            continue
        m = l.get("mileage")
        if m and m > MAX_MILEAGE:
            continue
        filtered.append(l)
    print(f"After mileage + dealer filter: {len(filtered)}")

    # 4. AI score
    print("\nScoring with AI...")
    for l in filtered:
        score_listing(l)
        print(f"  {l['score']}/10 — {l['title'][:50]}")
        time.sleep(0.4)

    # 5. Keep only good scores, sort
    scored = sorted(
        [l for l in filtered if l.get("score", 0) >= MIN_SCORE],
        key=lambda x: x["score"],
        reverse=True
    )
    print(f"\nAbove min score ({MIN_SCORE}): {len(scored)}")

    # 6. Cross-platform check — only on scored candidates
    print("\nCross-checking top candidates...")
    final = []
    for l in scored:
        if cross_check(l):
            print(f"  SKIP (cross-listed): {l['title']}")
        else:
            final.append(l)
            print(f"  OK (not cross-listed): {l['title']}")
        time.sleep(1.5)
        if len(final) >= TOP_N:
            break  # stop once we have enough

    # 7. Mark all new listings as seen
    for l in new:
        seen.add(l["url"])
    save_seen(seen)

    # 8. Send to Telegram
    if not final:
        send_telegram(
            "🚗 <b>Monterra — " + datetime.now().strftime("%d %b %Y") + "</b>\n\n"
            "No strong candidates found today."
        )
        print("\nNo candidates. Sent empty report.")
        return

    send_telegram(
        f"🚗 <b>Monterra Cars — {datetime.now().strftime('%d %b %Y')}</b>\n"
        f"<b>{len(final)}</b> candidates from {len(new)} new listings\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    time.sleep(0.5)

    for i, listing in enumerate(final, 1):
        send_telegram(format_listing(listing, i))
        time.sleep(0.5)

    print(f"\nDone. Sent {len(final)} listings.")

if __name__ == "__main__":
    main()
