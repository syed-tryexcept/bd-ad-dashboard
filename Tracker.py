"""
BD e-commerce competitor ad tracker - v3
Field names were confirmed from a real SearchAPI probe (probe.json).

  python tracker.py --import-old  re-use ads from ads_old.db (older run), NO credits
  python tracker.py --offline   read probe.json, NO API credit, write CSV files to out/
  python tracker.py --local     rebuild CSV/dashboard from saved data (0 credits); add --sheets to push
  python tracker.py --pages electronics   list untracked electronics pages;  --add-top electronics 5  tracks the top 5
  python tracker.py --first     test ONE target only (1 credit)
  python tracker.py             fetch from SearchAPI (uses credits), save to ads.db, write CSVs
  python tracker.py --sheets    same as above, and also push to Google Sheets
  python tracker.py --probe     save one raw response to probe.json (1 credit; --force to overwrite)

pip install requests pandas gspread
"""
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
import requests

API_KEY = os.environ.get("SEARCHAPI_KEY", "")
ENDPOINT = "https://www.searchapi.io/api/v1/search"
DB_PATH = "ads.db"
OUT_DIR = "out"
MAX_PAGES = 1            # each page = ~1 credit and ~30 ads. Keep at 1 while testing.
SHEET_NAME = "BD_Ecommerce_Ad_Tracker"
GOOGLE_CREDS_FILE = "service_account.json"   # never commit this file

# page_id targets = real competitors (best). q targets = keyword discovery.
# Every target costs MAX_PAGES credit(s) per run.
TARGETS = [
    # electronics
    {"niche": "electronics", "name": "RANA Electronics", "page_id": "233962723135546"},
    {"niche": "electronics", "name": "oraimo store BD", "page_id": "301602833044256"},
    {"niche": "electronics", "name": "Oraimo Daily Deals BD", "page_id": "1332546279946401"},
    # honey / food
    {"niche": "honey", "name": "Falaq Food", "page_id": "108554018655539"},
    {"niche": "honey", "name": "Honeyraj", "page_id": "582000904985959"},
    # skincare
    {"niche": "skincare", "name": "Bio-Xin Cosmeceuticals", "page_id": "317388458469194"},
    {"niche": "skincare", "name": "Skincare Shop", "page_id": "111268950602442"},
    # general e-commerce
    {"niche": "general", "name": "ModinaShop.com", "page_id": "103557021277799"},
]

OFFERS = {
    "Free delivery": r"free delivery|ফ্রি ডেলিভারি|ডেলিভারি চার্জ ফ্রি",
    "Cash on delivery": r"cash on delivery|\bcod\b|ক্যাশ অন ডেলিভারি|ডেলিভারির সময় টাকা|অগ্রিম টাকা ছাড়া",
    "Discount": r"\d+\s*%|ডিসকাউন্ট|discount|ছাড়",
    "Combo / BOGO": r"buy 1 get 1|combo|কম্বো|একটির সাথে একটি",
    "Urgency": r"limited stock|সীমিত|আজই অর্ডার|order now|স্টক শেষ",
    "Original / Official": r"100% original|অরিজিনাল|official|অফিসিয়াল",
    "Warranty": r"warranty|ওয়ারেন্টি|guarantee|গ্যারান্টি",
    "EMI / Installment": r"\bemi\b|ইএমআই|installment|কিস্তি|ডাউন পেমেন্ট|down payment",
    "Replacement / Return": r"replacement|রিপ্লেসমেন্ট|\breturn\b|রিটার্ন|ফেরত",
}

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
REQUESTS_USED = 0


# ---------- helpers ----------
def text_of(v):
    """Turn str / {'text': ..} / list / None into a clean string (never a dict)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for k in ("text", "title", "url", "link_url"):
            if v.get(k):
                return text_of(v[k])
        return ""
    if isinstance(v, list):
        return " ".join(t for t in (text_of(x) for x in v) if t)
    return str(v)


def to_dt(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def headline_cta_link(snap):
    """Single-video ads have no headline; carousel/link ads keep it inside snapshot.cards."""
    title = cta = landing = ""
    for card in snap.get("cards") or []:
        if isinstance(card, dict):
            title = title or text_of(card.get("title"))
            cta = cta or text_of(card.get("cta_text"))
            landing = landing or text_of(card.get("link_url"))
    title = title or text_of(snap.get("title"))
    cta = cta or text_of(snap.get("cta_text"))
    landing = landing or text_of(snap.get("link_url")) or text_of(snap.get("extra_links"))
    return title, cta, landing


# ---------- 1. fetch ----------
def fetch(target, pages=MAX_PAGES):
    global REQUESTS_USED
    params = {"engine": "meta_ad_library", "country": "BD", "api_key": API_KEY}
    if "page_id" in target:
        params["page_id"] = target["page_id"]
    else:
        params["q"] = target["q"]
    ads = []
    for _ in range(pages):
        r = requests.get(ENDPOINT, params=params, timeout=60)
        REQUESTS_USED += 1
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        ads += data.get("ads", [])
        token = (data.get("pagination") or {}).get("next_page_token")
        if not token:
            break
        params["next_page_token"] = token
    return ads


# ---------- 2. store (history is the whole point) ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS ads(
        ad_id TEXT PRIMARY KEY, niche TEXT, page_id TEXT, page_name TEXT,
        start_date TEXT, end_date TEXT, is_active INTEGER, collation_count INTEGER,
        display_format TEXT, platforms TEXT, headline TEXT, body TEXT, cta TEXT,
        landing_link TEXT, ad_link TEXT, page_likes INTEGER,
        first_seen TEXT, last_seen TEXT, raw TEXT)""")
    return con


def parse_ad(ad, niche, today):
    snap = ad.get("snapshot") or {}
    ad_id = str(ad.get("ad_archive_id") or "")
    start, end = to_dt(ad.get("start_date")), to_dt(ad.get("end_date"))
    title, cta, landing = headline_cta_link(snap)
    return {
        "ad_id": ad_id, "niche": niche,
        "page_id": str(ad.get("page_id") or snap.get("page_id") or ""),
        "page_name": ad.get("page_name") or snap.get("page_name") or "",
        "start_date": start.strftime("%Y-%m-%d") if start else "",
        "end_date": end.strftime("%Y-%m-%d") if end else "",
        "is_active": 1 if ad.get("is_active") else 0,
        "collation_count": int(ad.get("collation_count") or 1),
        "display_format": snap.get("display_format") or "",
        "platforms": ",".join(ad.get("publisher_platform") or []),
        "headline": title, "body": text_of(snap.get("body")), "cta": cta,
        "landing_link": landing,
        "ad_link": f"https://www.facebook.com/ads/library/?id={ad_id}",
        "page_likes": int(snap.get("page_like_count") or 0),
        "first_seen": today, "last_seen": today,
        "raw": json.dumps(ad, ensure_ascii=False),
    }


def save(con, ads, niche):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for ad in ads:
        row = parse_ad(ad, niche, today)
        if not row["ad_id"]:
            continue
        con.execute(
            """INSERT INTO ads VALUES(:ad_id,:niche,:page_id,:page_name,:start_date,:end_date,
                 :is_active,:collation_count,:display_format,:platforms,:headline,:body,:cta,
                 :landing_link,:ad_link,:page_likes,:first_seen,:last_seen,:raw)
               ON CONFLICT(ad_id) DO UPDATE SET last_seen=excluded.last_seen,
                 is_active=excluded.is_active, end_date=excluded.end_date,
                 collation_count=excluded.collation_count, headline=excluded.headline,
                 body=excluded.body, page_likes=excluded.page_likes, raw=excluded.raw""",
            row,
        )
    con.commit()


# ---------- 3. analyse ----------
def build_frame(con):
    df = pd.read_sql("SELECT * FROM ads WHERE start_date != ''", con)
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    df["start_date"] = pd.to_datetime(df["start_date"])
    df["last_seen"] = pd.to_datetime(df["last_seen"])
    # NOTE: the API's end_date is NOT a stop signal for active ads (it showed "yesterday" on an active ad).
    df["seen_today"] = df["last_seen"] >= today
    end = df["last_seen"].where(~df["seen_today"], today)
    df["days_running"] = (end - df["start_date"]).dt.days

    def tier(d):
        return "Super winner (30+)" if d >= 30 else "Winning (15-29)" if d >= 15 else "Testing (<15)"

    df["tier"] = df["days_running"].apply(tier)
    return df.drop(columns=["raw"]).sort_values("days_running", ascending=False)


def analyse(df):
    texts = (df["headline"].fillna("") + " " + df["body"].fillna("")).tolist()
    rows = []
    for name, pat in OFFERS.items():
        n = sum(1 for t in texts if re.search(pat, t, re.IGNORECASE))
        rows.append({"offer": name, "ads": n, "pct": round(100 * n / max(len(texts), 1), 1)})
    offers = pd.DataFrame(rows).sort_values("ads", ascending=False)

    bigrams = Counter()
    for t in texts:
        words = [w for w in re.sub(r"[.,!?:;|()\"'।#]", " ", EMOJI.sub(" ", t)).lower().split() if len(w) > 1]
        bigrams.update(set(zip(words, words[1:])))   # once per ad
    hooks = pd.DataFrame(
        [(" ".join(k), v) for k, v in bigrams.most_common(30) if v > 1], columns=["phrase", "ads"]
    )
    return offers, hooks


# ---------- 4. export ----------
def export_csv(frames):
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, df in frames.items():
        df.to_csv(os.path.join(OUT_DIR, f"{name}.csv"), index=False, encoding="utf-8-sig")  # Excel-safe Bangla


def export_sheets(frames):
    import gspread
    sh = gspread.service_account(filename=GOOGLE_CREDS_FILE).open(SHEET_NAME)
    for tab, df in frames.items():
        try:
            ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab, rows=10, cols=10)
        ws.clear()
        ws.resize(rows=len(df) + 50, cols=max(len(df.columns), 5))
        clean = df.astype(object).where(df.notna(), "")
        clean = clean.map(lambda v: v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else v)
        ws.update(range_name="A1", values=[list(df.columns)] + clean.values.tolist())


def import_old(con, path="ads_old.db"):
    """Re-use ads already downloaded by an older version: NO API credits needed."""
    old = sqlite3.connect(path)
    n = 0
    for niche, raw in old.execute("SELECT niche, raw FROM ads"):
        save(con, [json.loads(raw)], niche)
        n += 1
    print(f"Imported {n} ads from {path}")


HTML = r"""<!doctype html><meta charset="utf-8"><title>BD Ad Tracker</title>
<style>
body{font-family:system-ui,"Nirmala UI","Noto Sans Bengali",sans-serif;margin:0;background:#f4f5f7;color:#222}
header{background:#1f2a44;color:#fff;padding:14px 20px;font-size:18px}.w{padding:16px 20px;max-width:1300px;margin:auto}
.r{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}.c{background:#fff;border-radius:8px;padding:12px 16px;box-shadow:0 1px 3px #0002;flex:1;min-width:220px}
.k{font-size:26px;font-weight:700}select,input{padding:6px;margin:0 8px 8px 0}
.b{display:flex;align-items:center;margin:3px 0;font-size:13px}.b span{width:170px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.b i{display:block;height:14px;background:#3b6fd4;border-radius:3px;margin-right:6px}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}td,th{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
td.t{max-width:520px;white-space:pre-wrap}</style>
<header>BD E-commerce Competitor Ad Tracker</header><div class="w">
<div class="r"><div class="c"><select id="niche"></select><select id="page"></select>
Min days <input id="days" type="number" value="0" style="width:60px"> <input id="q" placeholder="search text"></div></div>
<div class="r"><div class="c">Ads<div class="k" id="k1"></div></div><div class="c">15+ days<div class="k" id="k2"></div></div>
<div class="c">30+ days<div class="k" id="k3"></div></div><div class="c">Pages<div class="k" id="k4"></div></div></div>
<div class="r"><div class="c"><b>Pages with most 15+ day ads</b><div id="pages"></div></div>
<div class="c"><b>Offers / hooks used (ads)</b><div id="offers"></div></div></div>
<table><thead><tr><th>Page</th><th>Days</th><th>Type</th><th>Ad text</th><th>Link</th></tr></thead><tbody id="tb"></tbody></table></div>
<script>
const D=__DATA__,O=__OFFERS__,$=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const uniq=k=>[...new Set(D.map(x=>x[k]))].sort();
$("niche").innerHTML='<option value="">All niches</option>'+uniq("niche").map(v=>`<option>${esc(v)}</option>`).join("");
$("page").innerHTML='<option value="">All pages</option>'+uniq("page_name").map(v=>`<option>${esc(v)}</option>`).join("");
const bars=(el,rows)=>{const m=Math.max(1,...rows.map(r=>r[1]));el.innerHTML=rows.map(r=>`<div class="b"><span title="${esc(r[0])}">${esc(r[0])}</span><i style="width:${r[1]/m*240}px"></i>${r[1]}</div>`).join("")};
function render(){
 const n=$("niche").value,p=$("page").value,d=+$("days").value||0,q=$("q").value.toLowerCase();
 const R=D.filter(x=>(!n||x.niche==n)&&(!p||x.page_name==p)&&x.days_running>=d&&(!q||(x.headline+" "+x.body).toLowerCase().includes(q)));
 $("k1").textContent=R.length;$("k2").textContent=R.filter(x=>x.days_running>=15).length;
 $("k3").textContent=R.filter(x=>x.days_running>=30).length;$("k4").textContent=new Set(R.map(x=>x.page_name)).size;
 const c={};R.filter(x=>x.days_running>=15).forEach(x=>c[x.page_name]=(c[x.page_name]||0)+1);
 bars($("pages"),Object.entries(c).sort((a,b)=>b[1]-a[1]).slice(0,10));
 bars($("offers"),Object.entries(O).map(([k,v])=>[k,R.filter(x=>new RegExp(v,"i").test(x.headline+" "+x.body)).length]).sort((a,b)=>b[1]-a[1]));
 $("tb").innerHTML=R.slice(0,200).map(x=>`<tr><td>${esc(x.page_name)}</td><td>${x.days_running}</td><td>${esc(x.display_format)}</td><td class="t">${esc((x.headline?x.headline+"\n":"")+x.body.slice(0,400))}</td><td><a href="${esc(x.ad_link)}" target="_blank">open</a></td></tr>`).join("");
}
["niche","page","days","q"].forEach(id=>$(id).addEventListener("input",render));render();
</script>"""


def export_html(frame):
    d = frame[["niche", "page_name", "days_running", "display_format", "headline", "body", "ad_link"]].fillna("")
    page = (HTML.replace("__DATA__", d.to_json(orient="records", force_ascii=False))
                .replace("__OFFERS__", json.dumps(OFFERS, ensure_ascii=False))
                .replace("</script>\n", "</script>\n"))
    # keep user text from closing the script tag early
    head, body = page.split("<script>\n", 1)
    body = body[: body.rfind("</script>")].replace("</", "<\\/") + "</script>"
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(head + "<script>\n" + body)


EXTRA_FILE = "extra_targets.json"   # written by --add-top, read on every run


def load_targets():
    extra = json.load(open(EXTRA_FILE, encoding="utf-8")) if os.path.exists(EXTRA_FILE) else []
    return list(TARGETS) + extra


def add_top(frame, niche, n=5):
    tracked = {str(t.get("page_id")) for t in load_targets()}
    f = frame[(frame["niche"] == niche) & (~frame["page_id"].isin(tracked))]
    g = (f.groupby(["page_id", "page_name"])
          .agg(ads_15plus=("days_running", lambda s: int((s >= 15).sum())), likes=("page_likes", "max"))
          .reset_index().sort_values(["ads_15plus", "likes"], ascending=False).head(n))
    extra = json.load(open(EXTRA_FILE, encoding="utf-8")) if os.path.exists(EXTRA_FILE) else []
    extra += [{"niche": niche, "name": r.page_name, "page_id": str(r.page_id)} for r in g.itertuples()]
    json.dump(extra, open(EXTRA_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nAdded {len(g)} '{niche}' pages to {EXTRA_FILE} (delete a line there to remove one):")
    print(g.to_string(index=False))


def top_pages(frame, niche=None):
    if niche:
        tracked = {str(t.get("page_id")) for t in load_targets()}
        frame = frame[(frame["niche"] == niche) & (~frame["page_id"].isin(tracked))]
    g = (frame.groupby(["page_id", "page_name"])
              .agg(ads=("ad_id", "count"), ads_15plus=("days_running", lambda s: int((s >= 15).sum())),
                   likes=("page_likes", "max"))
              .reset_index().sort_values(["ads_15plus", "ads"], ascending=False))
    os.makedirs(OUT_DIR, exist_ok=True)
    g.to_csv(os.path.join(OUT_DIR, "Top_Pages.csv"), index=False, encoding="utf-8-sig")
    print(f"\nTop {niche or ''} pages not yet tracked (saved to out/Top_Pages.csv):" if niche else "\nTop competitor pages found in your data (also saved to out/Top_Pages.csv):")
    print(g.head(25 if niche else 15).to_string(index=False))


if __name__ == "__main__":
    args = set(sys.argv[1:])
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if "--probe" in args:
        if os.path.exists("probe.json") and "--force" not in args:
            sys.exit("probe.json already exists - not spending another credit. Add --force to overwrite.")
        t = {"q": "smartwatch bd"}
        data = requests.get(ENDPOINT, params={"engine": "meta_ad_library", "country": "BD",
                                              "q": t["q"], "api_key": API_KEY}, timeout=60).json()
        if "ads" not in data:
            sys.exit(f"No 'ads' in response (wrong or missing key?): {str(data)[:300]}")
        json.dump(data, open("probe.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        sys.exit(f"Saved probe.json with {len(data['ads'])} ads.")

    con = init_db()
    if "--import-old" in args:
        import_old(con)
    elif args & {"--pages", "--local", "--add-top"}:
        pass   # uses saved data only, no credits
    elif "--offline" in args:
        save(con, json.load(open("probe.json", encoding="utf-8")).get("ads", []), "electronics")
    else:
        if not API_KEY:
            sys.exit("SEARCHAPI_KEY is not set in this window.")
        for tgt in (load_targets()[:1] if "--first" in args else load_targets()):
            ads = fetch(tgt)
            save(con, ads, tgt["niche"])
            seen = sorted({a.get("page_name") or "" for a in ads})[:3]
            print(f"{tgt.get('name') or tgt.get('q')}: {len(ads)} ads, page names returned: {seen}")

    frame = build_frame(con)
    offers, hooks = analyse(frame)
    frames = {
        "All_Ads": frame,
        "Winning_15plus": frame[frame["days_running"] >= 15],
        "Offer_Patterns": offers,
        "Top_Phrases": hooks,
    }
    export_csv(frames)
    export_html(frame)
    argv = sys.argv[1:]
    nxt = lambda flag, k=1: argv[argv.index(flag) + k] if flag in argv and len(argv) > argv.index(flag) + k and not argv[argv.index(flag) + k].startswith("--") else None
    if "--pages" in args:
        top_pages(frame, nxt("--pages"))
    if "--add-top" in args:
        add_top(frame, nxt("--add-top") or "electronics", int(nxt("--add-top", 2) or 5))
    if "--sheets" in args:
        export_sheets(frames)

    n = max(len(frame), 1)
    print(f"Ads in database: {len(frame)}   |   API requests used this run: {REQUESTS_USED}")
    print(f"Have headline: {(frame['headline'] != '').sum()}/{n}   body: {(frame['body'] != '').sum()}/{n}   "
          f"cta: {(frame['cta'] != '').sum()}/{n}   landing link: {(frame['landing_link'] != '').sum()}/{n}")
    print(f"Running 15+ days: {(frame['days_running'] >= 15).sum()}   30+ days: {(frame['days_running'] >= 30).sum()}")
    print(f"Dashboard: {os.path.abspath(os.path.join(OUT_DIR, 'dashboard.html'))}")