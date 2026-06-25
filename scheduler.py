#!/usr/bin/env python3
"""
DuoBro Report Scheduler v3 — Autossuficiente (Python puro)

Faz TUDO sem depender de n8n:
  1. Decide quais reports gerar (diário/semanal/mensal)
  2. Dedup via NocoDB (chave por cliente + tipo)
  3. Google Service Account JWT auth
  4. Chama APIs: GA4, Meta Ads, Meta Pixel, Search Console
  5. Formata o report (Markdown)
  6. Envia via Telegram Bot API
  7. Loga tudo

Cron: 0 10 * * * cd /opt/report-scheduler && /usr/bin/python3 scheduler.py
"""

import json
import time
import sys
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from base64 import urlsafe_b64encode
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
LOG_FILE = Path("/data/scheduler.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# NocoDB
NOCO_TOKEN = "nc_pat_1aOj3QbDFESJWURvzf83z8vRBfALrPWOOWxuafUP"
NOCO_BASE_ID = "pehi4ooc5aoweo5"
NOCO_TABLE_ID = "mlnq9hkm7gzh4r1"

# Telegram
TELEGRAM_BOT_TOKEN = "8405091435:AAHKtKFsJvjSVRA8-s4L74VCYG99b-vbu4Y"
TELEGRAM_CHAT_ID = "-1004297981334"  # Alertas Clientes

# Google Service Account
SA_EMAIL = "gmail-n8n@youtube-435621.iam.gserviceaccount.com"
SA_KEY_PATH = "/run/secrets/sa_key"

# Meta (Facebook) Access Token
META_TOKEN = "EAARlkr1FSPkBRTqjZCA18mBODNvxcvbZAZCPzMjjJ8fwdy8ZCLspSWJpHwceW2lLjfZCo4GbSMDtDbI49qOWYeo24KYfBAIqjRQKAKSqM11x7M3fC9MmtumXlTzjj3ZADGdhKNuZCAP3rd9WNfZAnmPlhDOCNKqZBYD6TGpyJ2q7p9w1RdZArWBsAvONK1tZCQYZBi4izAZDZD"

# Clients
CLIENTS = {
    "implamed": {
        "name": "Implamed",
        "ga4_property_id": "504562215",
        "meta_ad_account_id": "act_3585109751718588",
        "meta_pixel_id": "917662183383302",
        "search_console_site": "sc-domain:clinicaimplamed.com.br",
        "telegram_topic": 8,
    },
    "leprime": {
        "name": "Le Prime",
        "ga4_property_id": "533572525",
        "meta_ad_account_id": "act_180177218109841",
        "meta_pixel_id": "460157146189264",
        "search_console_site": "https://www.leprimeacabamentos.com.br/",
        "telegram_topic": 5,
    },
}

# ══════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("report-scheduler")


# ══════════════════════════════════════════════════════════
# GOOGLE AUTH
# ══════════════════════════════════════════════════════════
def google_jwt(scope: str) -> str:
    """Generate a Google OAuth2 access token via JWT Bearer exchange."""
    with open(SA_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    header = urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    now = int(time.time())
    claim = urlsafe_b64encode(json.dumps({
        "iss": SA_EMAIL,
        "scope": scope,
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600,
        "iat": now,
    }).encode()).rstrip(b"=").decode()
    msg = f"{header}.{claim}".encode()
    sig = urlsafe_b64encode(private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())).rstrip(b"=").decode()
    jwt = f"{header}.{claim}.{sig}"

    # Exchange JWT for access token
    data = f"grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion={jwt}".encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return resp["access_token"]


# ══════════════════════════════════════════════════════════
# DATE MATH
# ══════════════════════════════════════════════════════════
def compute_date_range(report_type: str) -> tuple:
    now = datetime.now()
    if report_type == "mensal":
        end = now - timedelta(days=1)
        start = end - timedelta(days=29)
        display = f"{start.strftime('%d/%m')} a {end.strftime('%d/%m')}/{end.year}"
    elif report_type == "semanal":
        dow = now.weekday()
        days = 1 if dow == 6 else dow + 2
        end = now - timedelta(days=days)
        start = end - timedelta(days=6)
        display = f"{start.strftime('%d/%m')} a {end.strftime('%d/%m')}/{end.year}"
    else:
        yesterday = now - timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d"), yesterday.strftime("%d/%m/%Y")
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), display


def decide_reports(now: datetime) -> list:
    due = ["diario"]
    if now.day == 1:
        due.append("mensal")
    if now.weekday() == 0:
        due.append("semanal")
    return due


# ══════════════════════════════════════════════════════════
# NOCODB
# ══════════════════════════════════════════════════════════
def nocodb_req(method, path, body=None):
    url = f"https://app.nocodb.com{path}"
    headers = {"xc-token": NOCO_TOKEN, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_dedup():
    return nocodb_req("GET", f"/api/v2/tables/{NOCO_TABLE_ID}/records").get("list", [])


def dedup_check(records, key):
    today = datetime.now().strftime("%Y-%m-%d")
    for r in records:
        if r.get("workflow") == key:
            return r.get("last_run_date") == today
    return False


def dedup_mark(records, key):
    today = datetime.now().strftime("%Y-%m-%d")
    for r in records:
        if r.get("workflow") == key:
            nocodb_req("PATCH", f"/api/v2/tables/{NOCO_TABLE_ID}/records",
                       [{"Id": r["Id"], "last_run_date": today}])
            return
    nocodb_req("POST", f"/api/v2/tables/{NOCO_TABLE_ID}/records",
               [{"workflow": key, "last_run_date": today}])


# ══════════════════════════════════════════════════════════
# API CALLS
# ══════════════════════════════════════════════════════════
def api_request(url, method="POST", body=None, headers=None, params=None):
    """Make an HTTP request with optional query params."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_ga4_main(token, property_id, date_start, date_end):
    """Fetch GA4 main metrics: sessions, users, pageviews, bounce rate, etc."""
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    body = {
        "dateRanges": [{"startDate": date_start, "endDate": date_end}],
        "metrics": [
            {"name": "sessions"}, {"name": "totalUsers"},
            {"name": "newUsers"}, {"name": "screenPageViews"},
            {"name": "averageSessionDuration"}, {"name": "bounceRate"},
            {"name": "conversions"},
        ],
    }
    return api_request(url, body=body, headers={"Authorization": f"Bearer {token}"})


def fetch_ga4_pages(token, property_id, date_start, date_end):
    """Fetch top 5 pages by views."""
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    body = {
        "dateRanges": [{"startDate": date_start, "endDate": date_end}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}],
        "limit": 5,
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
    }
    return api_request(url, body=body, headers={"Authorization": f"Bearer {token}"})


def fetch_ga4_sources(token, property_id, date_start, date_end):
    """Fetch top 5 traffic sources by sessions."""
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    body = {
        "dateRanges": [{"startDate": date_start, "endDate": date_end}],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}],
        "limit": 5,
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
    }
    return api_request(url, body=body, headers={"Authorization": f"Bearer {token}"})


def fetch_ga4_events(token, property_id, date_start, date_end):
    """Fetch custom events."""
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    body = {
        "dateRanges": [{"startDate": date_start, "endDate": date_end}],
        "dimensions": [{"name": "eventName"}],
        "metrics": [{"name": "eventCount"}],
        "limit": 20,
        "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}],
    }
    return api_request(url, body=body, headers={"Authorization": f"Bearer {token}"})


def fetch_meta_ads(token, ad_account_id, date_start, date_end):
    """Fetch Meta Ads insights — matches n8n API format."""
    url = f"https://graph.facebook.com/v21.0/{ad_account_id}/insights"
    params = {
        "access_token": token,
        "time_range": json.dumps({"since": date_start, "until": date_end}),
        "fields": "spend,impressions,reach,clicks,ctr,cpc,cpm,actions,cost_per_action_type,campaign_name",
        "level": "account",
    }
    return api_request(url, method="GET", params=params)


def fetch_meta_pixel(token, pixel_id, date_start, date_end):
    """Fetch Meta Pixel event stats — matches n8n API format."""
    import pytz
    brt = pytz.timezone("America/Sao_Paulo")
    start_dt = brt.localize(datetime.strptime(date_start, "%Y-%m-%d").replace(hour=0, minute=0, second=0))
    end_dt = brt.localize(datetime.strptime(date_end, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
    
    url = f"https://graph.facebook.com/v21.0/{pixel_id}/stats"
    params = {
        "access_token": token,
        "start_time": int(start_dt.timestamp()),
        "end_time": int(end_dt.timestamp()),
        "aggregation": "event",
    }
    return api_request(url, method="GET", params=params)


def fetch_search_console(token, site_url, date_start, date_end):
    """Fetch Search Console organic data."""
    import urllib.parse
    url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
    body = {
        "startDate": date_start,
        "endDate": date_end,
        "dimensions": ["query"],
        "rowLimit": 5,
    }
    return api_request(url, body=body, headers={"Authorization": f"Bearer {token}"})


# ══════════════════════════════════════════════════════════
# FORMATTERS
# ══════════════════════════════════════════════════════════
def safe_num(val):
    try: return f"{int(float(val)):,}".replace(",", ".")
    except: return "—"

def safe_pct(val):
    try: return f"{float(val)*100:.1f}%"
    except: return "—"

def safe_pct_meta(val):
    """Meta API returns CTR already as percentage (e.g. 2.517 = 2.517%), no multiply needed"""
    try: return f"{float(val):.2f}%"
    except: return "—"

def safe_money(val):
    try: return f"R$ {float(val):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except: return "—"

def safe_dur(seconds):
    try:
        s = float(seconds)
        m, s = divmod(int(s), 60)
        return f"{m}m{s:02d}s"
    except: return "0m00s"


def md_escape(text):
    """Escape Markdown special characters for Telegram."""
    if not text:
        return ""
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in escape_chars else c for c in str(text))


def format_report(client, report_type, date_display, data):
    """Format a complete report in Markdown."""
    emoji = {"SEMANAL": "📅", "MENSAL": "📆"}.get(report_type, "☀️")
    r = f"📊 *REPORT {report_type} — {client['name']}*\n{emoji} {date_display}{' (ontem)' if report_type == 'DIÁRIO' else ''}\n"
    r += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    # Meta Ads
    r += "💰 *META ADS*\n"
    ma = data.get("meta_ads")
    if ma:
        r += f"├ Investimento: {safe_money(ma.get('spend'))}\n"
        r += f"├ Impressões: {safe_num(ma.get('impressions'))}\n"
        r += f"├ Alcance: {safe_num(ma.get('reach'))}\n"
        r += f"├ Cliques: {safe_num(ma.get('clicks'))} (CTR {safe_pct_meta(ma.get('ctr'))})\n"
        r += f"├ CPC: {safe_money(ma.get('cpc'))} | CPM: {safe_money(ma.get('cpm'))}\n"
        # Build lookup maps for actions and costs
        actions_map = {}
        for a in ma.get("actions", []) or []:
            actions_map[a.get("action_type","")] = a.get("value","0")
        cost_map = {}
        for c in ma.get("cost_per_action_type", []) or []:
            cost_map[c.get("action_type","")] = c.get("value","0")
        
        # Primary: messaging conversations (WhatsApp campaign)
        conv_key = None
        for k in actions_map:
            if "messaging_conversation_started" in k:
                conv_key = k
                break
        if conv_key:
            r += f"├ Conversas: {safe_num(actions_map.get(conv_key))}\n"
            cpa_conv = cost_map.get(conv_key)
            if cpa_conv:
                r += f"├ CPA Conversa: {safe_money(cpa_conv)}\n"
        # Secondary: leads (form) — only show if present AND different from conversas
        lead_val = actions_map.get("lead", "0")
        if lead_val and lead_val != "0" and str(lead_val) != str(actions_map.get(conv_key or "", "")):
            r += f"├ Leads (form): {safe_num(lead_val)}\n"
            cpa_lead = cost_map.get("lead")
            if cpa_lead:
                r += f"├ CPA Lead: {safe_money(cpa_lead)}\n"
        r += "└─\n\n"
    else:
        r += "└ ⚠️ Sem dados\n\n"

    # GA4
    r += "🌐 *SITE (GA4)*\n"
    ga = data.get("ga4_main")
    if ga:
        r += f"├ Sessões: {safe_num(ga.get('sessions'))}\n"
        r += f"├ Usuários: {safe_num(ga.get('totalUsers'))} ({safe_num(ga.get('newUsers'))} novos)\n"
        r += f"├ Pageviews: {safe_num(ga.get('screenPageViews'))}\n"
        r += f"├ Duração: {safe_dur(ga.get('averageSessionDuration'))}\n"
        r += f"├ Rejeição: {safe_pct(ga.get('bounceRate'))}\n"
        if int(float(ga.get("conversions", 0) or 0)) > 0:
            r += f"├ Conversões: {safe_num(ga.get('conversions'))}\n"
        r += "└─\n\n"
    else:
        r += "└ ⚠️ Sem dados\n\n"

    # Top Pages
    pages = data.get("ga4_pages", [])
    if pages:
        r += "📄 *TOP PÁGINAS*\n"
        for i, p in enumerate(pages[:5]):
            r += f"{'└' if i == min(len(pages),5)-1 else '├'} {md_escape(p['path'])} — {p['views']}\n"
        r += "\n"

    # Traffic Sources
    sources = data.get("ga4_sources", [])
    if sources:
        r += "📡 *FONTES DE TRÁFEGO*\n"
        for i, s in enumerate(sources[:5]):
            r += f"{'└' if i == min(len(sources),5)-1 else '├'} {md_escape(s['source'])}: {s['sessions']} sessões\n"
        r += "\n"

    # Custom Events
    events = data.get("ga4_events", {})
    ek = list(events.keys())
    if ek:
        r += "🎯 *EVENTOS CUSTOMIZADOS*\n"
        for i, ev in enumerate(ek):
            r += f"{'└' if i == len(ek)-1 else '├'} {md_escape(ev)}: {safe_num(events[ev])}\n"
        r += "\n"

    # Pixel
    r += "🎯 *PIXEL (Eventos)*\n"
    pe = data.get("pixel_events", {})
    pk = list(pe.keys())
    if pk:
        for i, ev in enumerate(pk):
            r += f"{'└' if i == len(pk)-1 else '├'} {md_escape(ev)}: {safe_num(pe[ev])}\n"
        r += "\n"
    else:
        r += "└ Sem eventos no período\n\n"

    # Search Console
    r += "🔍 *ORGÂNICO (Search Console)*\n"
    sc = data.get("search_console")
    if sc:
        r += f"├ Cliques: {safe_num(sc.get('clicks'))}\n"
        r += f"├ Impressões: {safe_num(sc.get('impressions'))}\n"
        r += f"├ CTR: {safe_pct(sc.get('ctr'))}\n"
        r += f"├ Posição: {sc.get('position', 0):.1f}\n"
        for i, q in enumerate(sc.get("queries", [])[:5]):
            r += f"{'└' if i == min(len(sc.get('queries',[])),5)-1 else '├'} \"{md_escape(q['query'])}\" — {q['clicks']} cliques\n"
        r += "\n"
    else:
        r += "└ Sem dados\n\n"

    r += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    r += f"🤖 _Relatório {report_type.lower()} automático_"
    return r


# ══════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════
def telegram_send(chat_id, text, topic_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if topic_id:
        body["message_thread_id"] = topic_id
    return api_request(url, method="POST", body=body)


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def main(dedup_prefix=""):
    t0 = datetime.now()
    log.info("=" * 60)
    log.info(f"🚀 Report Scheduler v3 — {t0.strftime('%Y-%m-%d %H:%M:%S')}")

    # Check SA key
    if not Path(SA_KEY_PATH).exists():
        log.error(f"❌ SA key not found: {SA_KEY_PATH}")
        log.error("   Run: scp sa-key.pem root@manager1:/opt/report-scheduler/sa-key.pem")
        sys.exit(1)

    # Dedup
    try:
        dedup = get_dedup()
        log.info(f"📋 NocoDB: {len(dedup)} dedup records")
    except Exception as e:
        log.error(f"❌ NocoDB: {e}")
        sys.exit(1)

    # Decision
    due = decide_reports(t0)
    log.info(f"📅 Devidos: {', '.join(due)}")

    # Auth tokens (once for all clients)
    try:
        ga_token = google_jwt("https://www.googleapis.com/auth/analytics.readonly https://www.googleapis.com/auth/webmasters.readonly")
        log.info("🔑 Google token OK")
    except Exception as e:
        log.error(f"❌ Google auth: {e}")
        sys.exit(1)

    # Process each client
    results = []
    for ckey, ccfg in CLIENTS.items():
        log.info(f"\n🏢 {ccfg['name']}")

        for rtype in due:
            dkey = f"{dedup_prefix}{ckey}-{rtype}"
            if dedup_check(dedup, dkey):
                log.info(f"  ⏭️  {dkey} — já rodou")
                results.append((ckey, rtype, "skipped"))
                continue

            log.info(f"  🚀 {dkey}")
            date_start, date_end, date_display = compute_date_range(rtype)

            try:
                # Gather all data
                rd = {}

                # GA4
                ga = fetch_ga4_main(ga_token, ccfg["ga4_property_id"], date_start, date_end)
                agg = {}
                if ga.get("rows"):
                    mh = ga["metricHeaders"]
                    for row in ga["rows"]:
                        for i, mv in enumerate(row["metricValues"]):
                            agg[mh[i]["name"]] = agg.get(mh[i]["name"], 0) + float(mv["value"])
                    nrows = len(ga["rows"])
                    if nrows > 1:
                        if "averageSessionDuration" in agg: agg["averageSessionDuration"] /= nrows
                        if "bounceRate" in agg: agg["bounceRate"] /= nrows
                rd["ga4_main"] = agg

                # GA4 Pages
                gp = fetch_ga4_pages(ga_token, ccfg["ga4_property_id"], date_start, date_end)
                rd["ga4_pages"] = [
                    {"path": r["dimensionValues"][0]["value"], "views": safe_num(r["metricValues"][0]["value"])}
                    for r in (gp.get("rows") or [])
                ]

                # GA4 Sources
                gs = fetch_ga4_sources(ga_token, ccfg["ga4_property_id"], date_start, date_end)
                rd["ga4_sources"] = [
                    {"source": r["dimensionValues"][0]["value"], "sessions": safe_num(r["metricValues"][0]["value"])}
                    for r in (gs.get("rows") or [])
                ]

                # GA4 Events
                ge = fetch_ga4_events(ga_token, ccfg["ga4_property_id"], date_start, date_end)
                events = {}
                for row in (ge.get("rows") or []):
                    name = row["dimensionValues"][0]["value"]
                    events[name] = events.get(name, 0) + int(row["metricValues"][0]["value"])
                rd["ga4_events"] = events

                # Search Console
                sc = fetch_search_console(ga_token, ccfg["search_console_site"], date_start, date_end)
                sc_data = {}
                if sc.get("rows"):
                    rows = sc["rows"]
                    sc_data["clicks"] = sum(r["clicks"] for r in rows)
                    sc_data["impressions"] = sum(r["impressions"] for r in rows)
                    sc_data["ctr"] = sum(r["ctr"] for r in rows) / len(rows)
                    sc_data["position"] = sum(r["position"] for r in rows) / len(rows)
                    sc_data["queries"] = [{"query": r["keys"][0], "clicks": r["clicks"]} for r in rows[:5]]
                rd["search_console"] = sc_data

                # Meta Ads
                try:
                    ma = fetch_meta_ads(META_TOKEN, ccfg["meta_ad_account_id"], date_start, date_end)
                    rd["meta_ads"] = (ma.get("data") or [{}])[0]  # Account-level, single result
                except Exception as e:
                    log.warning(f"    ⚠️  Meta Ads: {e}")
                    rd["meta_ads"] = None

                # Meta Pixel
                try:
                    mp = fetch_meta_pixel(META_TOKEN, ccfg["meta_pixel_id"], date_start, date_end)
                    pixel_events = {}
                    if mp.get("data"):
                        for hour_entry in mp["data"]:
                            for ev_entry in hour_entry.get("data", []):
                                name = ev_entry.get("value", "?")
                                count = int(ev_entry.get("count", 0))
                                if name and count > 0:
                                    pixel_events[name] = pixel_events.get(name, 0) + count
                    rd["pixel_events"] = pixel_events
                except Exception as e:
                    log.warning(f"    ⚠️  Pixel: {e}")
                    rd["pixel_events"] = {}

                log.info(f"    GA4: {agg.get('sessions',0)} sessions, Meta: spend={rd['meta_ads'].get('spend','?') if rd['meta_ads'] else '?'}, SC: {sc_data.get('clicks',0)} clicks")

                # Format
                report = format_report(ccfg, rtype.upper(), date_display, rd)

                # Send Telegram
                resp = telegram_send(TELEGRAM_CHAT_ID, report, ccfg.get("telegram_topic"))
                log.info(f"    ✅ Telegram OK (msg_id={resp.get('result',{}).get('message_id','?')})")

                # Dedup mark
                dedup_mark(dedup, dkey)
                results.append((ckey, rtype, "ok"))

            except Exception as e:
                log.error(f"    ❌ {e}")
                results.append((ckey, rtype, "failed"))

    # Summary
    elapsed = (datetime.now() - t0).total_seconds()
    ok = sum(1 for r in results if r[2] == "ok")
    skip = sum(1 for r in results if r[2] == "skipped")
    fail = sum(1 for r in results if r[2] == "failed")
    log.info(f"\n📊 {ok} ok, {skip} skip, {fail} fail — {elapsed:.1f}s")
    log.info("=" * 60)
    return 1 if fail else 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dedup-prefix", default="", help="Prefix for dedup keys (e.g. 'bbc-')")
    args = p.parse_args()
    sys.exit(main(dedup_prefix=args.dedup_prefix))
