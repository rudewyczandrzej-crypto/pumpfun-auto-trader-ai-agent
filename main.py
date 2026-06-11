import os
import re
import json
import html
import time
import asyncio
import logging
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import requests
import feedparser
import websockets
from dotenv import load_dotenv
from supabase import create_client, Client
from groq import Groq

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

# =========================
# CONFIG
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip()

PUMPPORTAL_WS_URL = os.getenv("PUMPPORTAL_WS_URL", "wss://pumpportal.fun/api/data").strip()
PUMPPORTAL_TRADE_URL = os.getenv("PUMPPORTAL_TRADE_URL", "https://pumpportal.fun/api/trade").strip()
PUMPPORTAL_API_KEY = os.getenv("PUMPPORTAL_API_KEY", "").strip()

DEXSCREENER_BASE_URL = os.getenv("DEXSCREENER_BASE_URL", "https://api.dexscreener.com").strip().rstrip("/")

# watch / paper / live
TRADING_MODE = os.getenv("TRADING_MODE", "watch").strip().lower()
TRADING_CONFIRM = os.getenv("TRADING_CONFIRM", "").strip()

TRADE_AMOUNT_SOL = float(os.getenv("TRADE_AMOUNT_SOL", "0.005"))
SLIPPAGE_PERCENT = float(os.getenv("SLIPPAGE_PERCENT", "20"))
PRIORITY_FEE_SOL = float(os.getenv("PRIORITY_FEE_SOL", "0.00005"))
TRADE_POOL = os.getenv("TRADE_POOL", "auto").strip()

TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "25"))
STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", "15"))
TRAILING_STOP_PERCENT = float(os.getenv("TRAILING_STOP_PERCENT", "12"))
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "600"))

MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", "2"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "5"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "2"))
ONE_TRADE_PER_MINT = os.getenv("ONE_TRADE_PER_MINT", "true").lower() in ["1", "true", "yes", "y"]

MIN_AI_SCORE_TO_TRADE = int(os.getenv("MIN_AI_SCORE_TO_TRADE", "7"))
MIN_AI_SCORE_TO_ALERT = int(os.getenv("MIN_AI_SCORE_TO_ALERT", "6"))

ENABLE_PUMPPORTAL_NEW_TOKENS = os.getenv("ENABLE_PUMPPORTAL_NEW_TOKENS", "true").lower() in ["1", "true", "yes", "y"]
ENABLE_X_RSS = os.getenv("ENABLE_X_RSS", "true").lower() in ["1", "true", "yes", "y"]
X_RSS_CHECK_INTERVAL_SECONDS = int(os.getenv("X_RSS_CHECK_INTERVAL_SECONDS", "60"))
TRADE_MANAGE_INTERVAL_SECONDS = int(os.getenv("TRADE_MANAGE_INTERVAL_SECONDS", "15"))
PUMP_RECONNECT_SECONDS = int(os.getenv("PUMP_RECONNECT_SECONDS", "10"))

MAX_ALERTS_PER_HOUR = int(os.getenv("MAX_ALERTS_PER_HOUR", "15"))

AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.15"))

DEFAULT_X_RSS_FEEDS = [x.strip() for x in os.getenv("X_RSS_FEEDS", "").split(",") if x.strip()]

BAD_NAME_KEYWORDS = [
    x.strip().lower()
    for x in os.getenv("BAD_NAME_KEYWORDS", "test,scam,rug,honeypot,do not buy,fake").split(",")
    if x.strip()
]

HEALTH_ALERT_COOLDOWN_SECONDS = int(os.getenv("HEALTH_ALERT_COOLDOWN_SECONDS", "1800"))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("pumpfun-auto-trader-ai")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("Missing GROQ_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

http = requests.Session()
http.headers.update({"User-Agent": "PumpFunAutoTraderAI/1.0", "Accept": "application/json,text/plain,*/*"})

SOLANA_ADDRESS_RE = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
PUMPFUN_LINK_RE = re.compile(r"https?://(?:www\.)?pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})", re.I)

LAST_HEALTH_ALERT_TS = 0.0


# =========================
# HELPERS
# =========================

def escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def safe_json_loads(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end+1])
        raise


def short_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-6:]}" if addr and len(addr) > 12 else str(addr)


def extract_solana_addresses(text: str) -> List[str]:
    found = set()
    for m in PUMPFUN_LINK_RE.finditer(text or ""):
        found.add(m.group(1))
    for m in SOLANA_ADDRESS_RE.finditer(text or ""):
        found.add(m.group(0))
    return list(found)


def contains_bad_name(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in BAD_NAME_KEYWORDS)


def live_trading_enabled() -> bool:
    return (
        TRADING_MODE == "live"
        and TRADING_CONFIRM == "YES_I_UNDERSTAND"
        and bool(PUMPPORTAL_API_KEY)
    )


# =========================
# DB
# =========================

def ensure_user(telegram_id: str) -> None:
    existing = supabase.table("users").select("id").eq("telegram_id", telegram_id).execute()
    if existing.data:
        return
    supabase.table("users").insert({"telegram_id": telegram_id, "alerts_enabled": True}).execute()


def get_alert_users() -> List[Dict[str, Any]]:
    return supabase.table("users").select("*").eq("alerts_enabled", True).execute().data or []


def get_all_users() -> List[Dict[str, Any]]:
    return supabase.table("users").select("*").execute().data or []


def set_alerts_enabled(telegram_id: str, enabled: bool) -> None:
    ensure_user(telegram_id)
    supabase.table("users").update({"alerts_enabled": enabled}).eq("telegram_id", telegram_id).execute()


def add_source(telegram_id: str, name: str, rss_url: str) -> Dict[str, Any]:
    return supabase.table("x_sources").insert({
        "telegram_id": telegram_id,
        "name": name,
        "rss_url": rss_url,
        "active": True,
    }).execute().data[0]


def get_sources(telegram_id: Optional[str] = None) -> List[Dict[str, Any]]:
    q = supabase.table("x_sources").select("*").eq("active", True)
    if telegram_id:
        q = q.eq("telegram_id", telegram_id)
    return q.order("created_at", desc=False).execute().data or []


def delete_source(source_id: int, telegram_id: str) -> bool:
    r = supabase.table("x_sources").select("id").eq("id", source_id).eq("telegram_id", telegram_id).eq("active", True).execute()
    if not r.data:
        return False
    supabase.table("x_sources").update({"active": False}).eq("id", source_id).eq("telegram_id", telegram_id).execute()
    return True


def seen_source_item(source_id: int, item_id: str) -> bool:
    r = supabase.table("seen_source_items").select("id").eq("source_id", source_id).eq("item_id", item_id).limit(1).execute()
    return bool(r.data)


def mark_source_item_seen(source_id: int, item_id: str) -> None:
    try:
        supabase.table("seen_source_items").insert({"source_id": source_id, "item_id": item_id}).execute()
    except Exception:
        pass


def count_recent_trades(telegram_id: str, hours: int) -> int:
    cutoff = iso(now_utc() - timedelta(hours=hours))
    r = supabase.table("trades").select("id").eq("telegram_id", telegram_id).gte("created_at", cutoff).execute()
    return len(r.data or [])


def count_open_trades(telegram_id: str) -> int:
    r = supabase.table("trades").select("id").eq("telegram_id", telegram_id).eq("status", "open").execute()
    return len(r.data or [])


def has_trade_for_mint(telegram_id: str, mint: str) -> bool:
    if not ONE_TRADE_PER_MINT:
        return False
    r = supabase.table("trades").select("id").eq("telegram_id", telegram_id).eq("mint", mint).limit(1).execute()
    return bool(r.data)


def count_recent_alerts(telegram_id: str, hours: int = 1) -> int:
    cutoff = iso(now_utc() - timedelta(hours=hours))
    r = supabase.table("sent_alerts").select("id").eq("telegram_id", telegram_id).gte("created_at", cutoff).execute()
    return len(r.data or [])


def mark_alert_sent(telegram_id: str, mint: str, alert_type: str, payload: Dict[str, Any]) -> None:
    try:
        supabase.table("sent_alerts").insert({
            "telegram_id": telegram_id, "mint": mint, "alert_type": alert_type, "payload": payload
        }).execute()
    except Exception:
        pass


def store_detected_token(mint: str, source: str, payload: Dict[str, Any]) -> None:
    try:
        existing = supabase.table("detected_tokens").select("id").eq("mint", mint).limit(1).execute()
        if existing.data:
            return
        supabase.table("detected_tokens").insert({"mint": mint, "source": source, "payload": payload}).execute()
    except Exception as e:
        logger.warning("Could not store token: %s", e)


def save_trade(telegram_id: str, mint: str, trade: Dict[str, Any]) -> Dict[str, Any]:
    expires_at = now_utc() + timedelta(seconds=MAX_HOLD_SECONDS)
    r = supabase.table("trades").insert({
        "telegram_id": telegram_id,
        "mint": mint,
        "mode": TRADING_MODE,
        "status": "open",
        "amount_sol": TRADE_AMOUNT_SOL,
        "entry_price_usd": trade.get("entry_price_usd"),
        "highest_price_usd": trade.get("entry_price_usd"),
        "buy_signature": trade.get("buy_signature"),
        "buy_response": trade.get("buy_response"),
        "ai_score": trade.get("ai_score"),
        "source_type": trade.get("source_type"),
        "expires_at": iso(expires_at),
        "raw": trade,
    }).execute()
    return r.data[0]


def get_open_trades() -> List[Dict[str, Any]]:
    return supabase.table("trades").select("*").eq("status", "open").order("created_at", desc=False).execute().data or []


def update_trade(trade_id: int, fields: Dict[str, Any]) -> None:
    supabase.table("trades").update(fields).eq("id", trade_id).execute()


def close_trade(trade_id: int, status: str, exit_price: Optional[float], response: Dict[str, Any], note: str) -> None:
    update_trade(trade_id, {
        "status": status,
        "exit_price_usd": exit_price,
        "sell_response": response,
        "close_note": note,
        "closed_at": iso(now_utc()),
    })


def recent_detected(limit: int = 10) -> List[Dict[str, Any]]:
    return supabase.table("detected_tokens").select("*").order("created_at", desc=True).limit(limit).execute().data or []


# =========================
# DATA
# =========================

def fetch_dexscreener_token(mint: str) -> List[Dict[str, Any]]:
    try:
        url = f"{DEXSCREENER_BASE_URL}/tokens/v1/solana/{mint}"
        r = http.get(url, timeout=15)
        if r.status_code >= 400:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def summarize_dex_pairs(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not pairs:
        return {"has_data": False}

    def score(p):
        liq = ((p.get("liquidity") or {}).get("usd") or 0)
        vol5 = ((p.get("volume") or {}).get("m5") or 0)
        tx5 = ((p.get("txns") or {}).get("m5") or {})
        return float(liq or 0) + float(vol5 or 0) + (tx5.get("buys", 0) + tx5.get("sells", 0)) * 100

    best = sorted(pairs, key=score, reverse=True)[0]
    tx5 = ((best.get("txns") or {}).get("m5") or {})
    volume = best.get("volume") or {}

    return {
        "has_data": True,
        "dex": best.get("dexId"),
        "pair": best.get("pairAddress"),
        "price_usd": safe_float(best.get("priceUsd")),
        "liquidity_usd": safe_float((best.get("liquidity") or {}).get("usd")),
        "fdv": safe_float(best.get("fdv")),
        "market_cap": safe_float(best.get("marketCap")),
        "volume_5m": safe_float(volume.get("m5")),
        "volume_1h": safe_float(volume.get("h1")),
        "txns_5m": {"buys": tx5.get("buys"), "sells": tx5.get("sells")},
        "url": best.get("url"),
    }


def parse_feed_entries(feed_url: str) -> List[Dict[str, str]]:
    try:
        feed = feedparser.parse(feed_url)
        items = []
        for e in feed.entries[:15]:
            title = str(getattr(e, "title", "") or "")
            summary = re.sub(r"<[^>]+>", "", str(getattr(e, "summary", "") or ""))
            link = str(getattr(e, "link", "") or "")
            item_id = str(getattr(e, "id", "") or link or title)
            items.append({"id": item_id, "title": title, "summary": summary, "link": link})
        return items
    except Exception:
        return []


# =========================
# AI
# =========================

def evaluate_candidate_with_ai(context: Dict[str, Any]) -> Dict[str, Any]:
    prompt = f"""
Ти AI-фільтр для pump.fun auto-trading тесту.

Контекст:
- Це експеримент з дуже малою сумою.
- Ти НЕ даєш фінансову пораду.
- Ти маєш вирішити, чи бот може автоматично зробити tiny test trade.
- Відхиляй, якщо це схоже на спам, rug, fake, запізнілий сигнал, слабкий catalyst, поганий source, або даних мало.
- Для live торгівлі потрібен дуже строгий фільтр.
- Якщо сумніваєшся — approve_trade=false.

Дані:
{json.dumps(context, ensure_ascii=False, indent=2)}

Поверни тільки JSON:
{{
  "should_alert": true/false,
  "approve_trade": true/false,
  "score": 0-10,
  "category": "x_call/new_token/hype/risk/ignore",
  "title": "короткий заголовок українською",
  "summary_uk": "що сталося",
  "why_trade_test": "чому це може бути варте tiny test trade або чому ні",
  "risks": ["..."],
  "confidence": 0-10
}}
"""
    try:
        c = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a strict pump.fun auto-trading risk filter. Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=AI_TEMPERATURE,
        )
        data = safe_json_loads(c.choices[0].message.content or "{}")
        return {
            "should_alert": bool(data.get("should_alert", False)),
            "approve_trade": bool(data.get("approve_trade", False)),
            "score": int(data.get("score", 0)),
            "category": str(data.get("category", "ignore")),
            "title": str(data.get("title", "")),
            "summary_uk": str(data.get("summary_uk", "")),
            "why_trade_test": str(data.get("why_trade_test", "")),
            "risks": data.get("risks", []),
            "confidence": int(data.get("confidence", 0)),
        }
    except Exception as e:
        logger.error("AI evaluation failed: %s", e)
        return {"should_alert": False, "approve_trade": False, "score": 0, "category": "ignore", "title": "AI failed", "summary_uk": "", "why_trade_test": "", "risks": [], "confidence": 0}


# =========================
# PUMPPORTAL TRADING
# =========================

def pumpportal_trade(action: str, mint: str, amount: Any, denominated_in_sol: bool) -> Dict[str, Any]:
    if not PUMPPORTAL_API_KEY:
        raise RuntimeError("Missing PUMPPORTAL_API_KEY")

    url = f"{PUMPPORTAL_TRADE_URL}?api-key={PUMPPORTAL_API_KEY}"
    body = {
        "action": action,
        "mint": mint,
        "amount": amount,
        "denominatedInSol": "true" if denominated_in_sol else "false",
        "slippage": SLIPPAGE_PERCENT,
        "priorityFee": PRIORITY_FEE_SOL,
        "pool": TRADE_POOL,
        "skipPreflight": "false",
    }

    r = http.post(url, json=body, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 400:
        raise RuntimeError(f"PumpPortal HTTP {r.status_code}: {data}")

    return data


def extract_signature(resp: Dict[str, Any]) -> Optional[str]:
    for key in ["signature", "txSignature", "tx", "transactionSignature"]:
        if isinstance(resp, dict) and resp.get(key):
            return str(resp.get(key))
    if isinstance(resp, dict):
        text = json.dumps(resp)
        m = re.search(r"[1-9A-HJ-NP-Za-km-z]{64,100}", text)
        if m:
            return m.group(0)
    return None


# =========================
# FORMAT
# =========================

def format_alert(mint: str, context: Dict[str, Any], ai: Dict[str, Any], trade_result: Optional[Dict[str, Any]] = None) -> str:
    dex = context.get("dex_summary") or {}
    risks = ai.get("risks") or []
    risks_text = "\n".join([f"• {escape(x)}" for x in risks[:5]]) or "н/д"

    pump_url = f"https://pump.fun/coin/{mint}"
    dex_url = dex.get("url")
    dex_line = f'\n📊 <a href="{escape(dex_url)}">DexScreener</a>' if dex_url else ""

    trade_text = ""
    if trade_result:
        trade_text = f"""

<b>Trade:</b>
Mode: {escape(TRADING_MODE)}
Action: {escape(trade_result.get("action"))}
Amount: {escape(trade_result.get("amount_sol"))} SOL
Status: {escape(trade_result.get("status"))}
Tx: {escape(short_addr(trade_result.get("signature") or ""))}
"""

    msg = f"""
🚨 <b>PumpFun Auto Trader AI</b>

<b>{escape(ai.get("title"))}</b>

🧪 <b>Mode:</b> {escape(TRADING_MODE)}
🧠 <b>AI score:</b> {escape(ai.get("score"))}/10
🎯 <b>Confidence:</b> {escape(ai.get("confidence"))}/10
📌 <b>Category:</b> {escape(ai.get("category"))}

🪙 <b>Mint:</b>
<code>{escape(mint)}</code>

🔗 <a href="{escape(pump_url)}">Pump.fun</a>{dex_line}

<b>Market quick data:</b>
Price: {escape(dex.get("price_usd")) or "н/д"}
Liquidity: {escape(dex.get("liquidity_usd")) or "н/д"}
FDV: {escape(dex.get("fdv")) or "н/д"}
Vol 5m: {escape(dex.get("volume_5m")) or "н/д"}
Tx 5m: {escape((dex.get("txns_5m") or {}).get("buys"))} buys / {escape((dex.get("txns_5m") or {}).get("sells"))} sells

<b>AI summary:</b>
{escape(ai.get("summary_uk"))}

<b>Why tiny test trade / why watch:</b>
{escape(ai.get("why_trade_test"))}

<b>Risks:</b>
{risks_text}
{trade_text}

<i>Експериментальний бот. Не фінансова порада.</i>
"""
    return msg.strip()


def format_trade_update(trade: Dict[str, Any], status: str, price: Optional[float], pnl_pct: Optional[float], note: str) -> str:
    emoji = "✅" if status in ["tp", "trailing_tp"] else "🛑" if status == "sl" else "⏱"
    return f"""
{emoji} <b>PumpFun trade update</b>

Mint: <code>{escape(trade.get("mint"))}</code>
Status: {escape(status)}
Exit price: {escape(price)}
PnL approx: {escape(round(pnl_pct, 2) if pnl_pct is not None else None)}%
Note: {escape(note)}

<i>Експериментальний режим.</i>
""".strip()


# =========================
# CORE
# =========================

async def process_candidate(application: Application, mint: str, source_type: str, source_payload: Dict[str, Any], force: bool = False) -> int:
    if not mint:
        return 0

    store_detected_token(mint, source_type, source_payload)

    if contains_bad_name(json.dumps(source_payload, ensure_ascii=False)):
        return 0

    pairs = fetch_dexscreener_token(mint)
    dex_summary = summarize_dex_pairs(pairs)

    context = {
        "mint": mint,
        "source_type": source_type,
        "source_payload": source_payload,
        "dex_summary": dex_summary,
        "pump_url": f"https://pump.fun/coin/{mint}",
        "detected_at": iso(now_utc()),
        "trading_mode": TRADING_MODE,
    }

    ai = evaluate_candidate_with_ai(context)

    if not force:
        if not ai.get("should_alert") or int(ai.get("score", 0)) < MIN_AI_SCORE_TO_ALERT:
            return 0

    sent = 0
    users = get_alert_users()

    for user in users:
        telegram_id = str(user["telegram_id"])

        if count_recent_alerts(telegram_id, 1) >= MAX_ALERTS_PER_HOUR:
            continue

        trade_result = None

        can_trade = (
            ai.get("approve_trade")
            and int(ai.get("score", 0)) >= MIN_AI_SCORE_TO_TRADE
            and count_recent_trades(telegram_id, 1) < MAX_TRADES_PER_HOUR
            and count_recent_trades(telegram_id, 24) < MAX_DAILY_TRADES
            and count_open_trades(telegram_id) < MAX_OPEN_TRADES
            and not has_trade_for_mint(telegram_id, mint)
        )

        if can_trade and TRADING_MODE in ["paper", "live"]:
            entry_price = dex_summary.get("price_usd")
            if TRADING_MODE == "paper":
                trade_result = {
                    "status": "paper_open",
                    "action": "paper_buy",
                    "amount_sol": TRADE_AMOUNT_SOL,
                    "signature": None,
                    "response": {"paper": True},
                }
                save_trade(telegram_id, mint, {
                    "entry_price_usd": entry_price,
                    "buy_signature": None,
                    "buy_response": {"paper": True},
                    "ai_score": ai.get("score"),
                    "source_type": source_type,
                })

            elif live_trading_enabled():
                try:
                    resp = pumpportal_trade("buy", mint, TRADE_AMOUNT_SOL, True)
                    sig = extract_signature(resp)
                    trade_result = {
                        "status": "live_open",
                        "action": "buy",
                        "amount_sol": TRADE_AMOUNT_SOL,
                        "signature": sig,
                        "response": resp,
                    }
                    save_trade(telegram_id, mint, {
                        "entry_price_usd": entry_price,
                        "buy_signature": sig,
                        "buy_response": resp,
                        "ai_score": ai.get("score"),
                        "source_type": source_type,
                    })
                except Exception as e:
                    trade_result = {
                        "status": "buy_failed",
                        "action": "buy",
                        "amount_sol": TRADE_AMOUNT_SOL,
                        "signature": None,
                        "response": {"error": str(e)},
                    }

        msg = format_alert(mint, context, ai, trade_result)
        await application.bot.send_message(
            chat_id=telegram_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        mark_alert_sent(telegram_id, mint, source_type, {"context": context, "ai": ai, "trade": trade_result})
        sent += 1
        await asyncio.sleep(0.25)

    return sent


async def manage_open_trades(application: Application) -> None:
    trades = get_open_trades()

    for trade in trades:
        try:
            mint = trade["mint"]
            pairs = fetch_dexscreener_token(mint)
            dex = summarize_dex_pairs(pairs)
            price = dex.get("price_usd")
            entry = safe_float(trade.get("entry_price_usd"))

            if price is None or entry is None or entry <= 0:
                # If no price is available and max hold expired, sell anyway.
                pass
            else:
                highest = safe_float(trade.get("highest_price_usd")) or entry
                if price > highest:
                    highest = price
                    update_trade(int(trade["id"]), {"highest_price_usd": highest})

                pnl_pct = ((price - entry) / entry) * 100

                status = None
                note = ""

                if pnl_pct >= TAKE_PROFIT_PERCENT:
                    status = "tp"
                    note = f"Take profit reached: +{pnl_pct:.2f}%"
                elif pnl_pct <= -STOP_LOSS_PERCENT:
                    status = "sl"
                    note = f"Stop loss reached: {pnl_pct:.2f}%"
                else:
                    drop_from_high = ((highest - price) / highest) * 100 if highest else 0
                    if highest > entry and drop_from_high >= TRAILING_STOP_PERCENT and pnl_pct > 0:
                        status = "trailing_tp"
                        note = f"Trailing stop after profit. PnL {pnl_pct:.2f}%"

                if status:
                    resp = {}
                    if TRADING_MODE == "live" and live_trading_enabled():
                        try:
                            resp = pumpportal_trade("sell", mint, "100%", False)
                        except Exception as e:
                            resp = {"error": str(e)}
                            note += f" | sell failed: {e}"
                    else:
                        resp = {"paper": True, "action": "paper_sell"}

                    close_trade(int(trade["id"]), status, price, resp, note)
                    msg = format_trade_update(trade, status, price, pnl_pct, note)
                    await application.bot.send_message(chat_id=str(trade["telegram_id"]), text=msg, parse_mode=ParseMode.HTML)
                    continue

            # Max hold expiry
            exp_raw = trade.get("expires_at")
            if exp_raw:
                exp = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00"))
                if now_utc() >= exp:
                    resp = {}
                    if TRADING_MODE == "live" and live_trading_enabled():
                        try:
                            resp = pumpportal_trade("sell", mint, "100%", False)
                        except Exception as e:
                            resp = {"error": str(e)}
                    else:
                        resp = {"paper": True, "action": "paper_sell"}

                    pnl_pct = None
                    if price is not None and entry:
                        pnl_pct = ((price - entry) / entry) * 100

                    close_trade(int(trade["id"]), "expired", price, resp, "Max hold time reached.")
                    msg = format_trade_update(trade, "expired", price, pnl_pct, "Max hold time reached.")
                    await application.bot.send_message(chat_id=str(trade["telegram_id"]), text=msg, parse_mode=ParseMode.HTML)

            await asyncio.sleep(0.2)

        except Exception as e:
            logger.warning("Manage trade failed: %s", e)


# =========================
# SOURCES
# =========================

async def poll_x_sources(application: Application, manual_telegram_id: Optional[str] = None) -> int:
    if not ENABLE_X_RSS:
        return 0

    sources = get_sources(manual_telegram_id)

    if not manual_telegram_id:
        for i, url in enumerate(DEFAULT_X_RSS_FEEDS):
            sources.append({"id": -1000-i, "telegram_id": None, "name": f"env_{i+1}", "rss_url": url})

    total = 0

    for source in sources:
        sid = int(source["id"])
        entries = parse_feed_entries(source["rss_url"])

        for entry in entries:
            item_id = entry.get("id") or entry.get("link") or entry.get("title")
            if sid > 0 and seen_source_item(sid, item_id):
                continue

            text = f"{entry.get('title','')} {entry.get('summary','')} {entry.get('link','')}"
            addresses = extract_solana_addresses(text)

            if sid > 0:
                mark_source_item_seen(sid, item_id)

            for mint in addresses[:3]:
                payload = {
                    "source_name": source.get("name"),
                    "rss_url": source.get("rss_url"),
                    "title": entry.get("title"),
                    "summary": entry.get("summary"),
                    "link": entry.get("link"),
                    "mint": mint,
                }
                total += await process_candidate(application, mint, "x_rss_call", payload, force=False)

    return total


def extract_mint_from_event(event: Dict[str, Any]) -> Optional[str]:
    for key in ["mint", "token", "ca", "contractAddress", "address"]:
        v = event.get(key)
        if isinstance(v, str) and SOLANA_ADDRESS_RE.fullmatch(v):
            return v
    addrs = extract_solana_addresses(json.dumps(event, ensure_ascii=False))
    return addrs[0] if addrs else None


async def pumpportal_listener(application: Application) -> None:
    if not ENABLE_PUMPPORTAL_NEW_TOKENS:
        return

    await asyncio.sleep(5)

    while True:
        try:
            logger.info("Connecting PumpPortal WS")
            async with websockets.connect(PUMPPORTAL_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                logger.info("Subscribed to new tokens.")

                async for msg in ws:
                    try:
                        event = json.loads(msg)
                    except Exception:
                        continue

                    mint = extract_mint_from_event(event)
                    if not mint:
                        continue

                    if contains_bad_name(f"{event.get('name','')} {event.get('symbol','')}"):
                        continue

                    payload = {"event": event, "name": event.get("name"), "symbol": event.get("symbol"), "mint": mint}
                    await process_candidate(application, mint, "pump_new_token", payload, force=False)

        except Exception as e:
            logger.error("PumpPortal listener error: %s", e)
            logger.error(traceback.format_exc())
            await asyncio.sleep(PUMP_RECONNECT_SECONDS)


# =========================
# JOBS / HEALTH
# =========================

async def send_health_alert(application: Application, title: str, details: str) -> None:
    global LAST_HEALTH_ALERT_TS
    if time.time() - LAST_HEALTH_ALERT_TS < HEALTH_ALERT_COOLDOWN_SECONDS:
        return
    LAST_HEALTH_ALERT_TS = time.time()

    text = f"⚠️ <b>PumpFun Auto Trader health alert</b>\n\n<b>{escape(title)}</b>\n\n{escape(details)}"
    for u in get_all_users():
        try:
            await application.bot.send_message(chat_id=str(u["telegram_id"]), text=text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def scheduled_x_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await poll_x_sources(context.application)
    except Exception as e:
        logger.error("X poll error: %s", e)
        await send_health_alert(context.application, "X/RSS poll error", str(e))


async def scheduled_manage_trades(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await manage_open_trades(context.application)
    except Exception as e:
        logger.error("Manage trades error: %s", e)
        await send_health_alert(context.application, "Manage trades error", str(e))


# =========================
# COMMANDS
# =========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    ensure_user(str(chat.id))
    await update.message.reply_text(f"""
👋 <b>PumpFun Auto Trader AI</b>

Mode: <b>{escape(TRADING_MODE)}</b>
Live enabled: <b>{escape(live_trading_enabled())}</b>

Команди:
/source_add name rss_url
/sources
/source_delete ID
/scan_x
/watch_mint CA
/open_trades
/close_all
/stats
/trading_status
/alerts_on
/alerts_off
/help

⚠️ Реальна торгівля можлива тільки якщо:
TRADING_MODE=live
TRADING_CONFIRM=YES_I_UNDERSTAND
PUMPPORTAL_API_KEY заповнений

Нема підтвердження кожної позиції — це одноразовий env-запобіжник.
""".strip(), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text("""
<b>Help</b>

/scan_x — перевірити RSS джерела
/watch_mint CA — вручну оцінити CA
/open_trades — відкриті paper/live trades
/close_all — вручну закрити всі відкриті trades
/stats — статистика тестових trades
/trading_status — режим і ліміти

<b>Modes:</b>
watch — тільки alerts
paper — симуляція buy/sell
live — реальні tiny trades через PumpPortal Lightning API

Live НЕ питає підтвердження кожної угоди. Підтвердження — тільки Railway env:
TRADING_CONFIRM=YES_I_UNDERSTAND
""".strip(), parse_mode=ParseMode.HTML)


async def source_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    ensure_user(str(chat.id))
    if len(context.args) < 2:
        await update.message.reply_text("Напиши: /source_add name rss_url")
        return
    row = add_source(str(chat.id), context.args[0], " ".join(context.args[1:]))
    await update.message.reply_text(f"✅ Додав source #{row['id']}")


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    sources = get_sources(str(chat.id))
    if not sources:
        await update.message.reply_text("Джерел немає.")
        return
    lines = ["📋 <b>Sources:</b>\n"]
    for s in sources:
        lines.append(f"#{s['id']} — <b>{escape(s['name'])}</b>\n{escape(s['rss_url'])}\n")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def source_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Напиши: /source_delete ID")
        return
    ok = delete_source(int(context.args[0]), str(chat.id))
    await update.message.reply_text("🗑 Видалено." if ok else "Не знайшов.")


async def scan_x_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    ensure_user(str(chat.id))
    await update.message.reply_text("🔍 Сканую X/RSS...")
    sent = await poll_x_sources(context.application, manual_telegram_id=str(chat.id))
    await update.message.reply_text(f"Готово. Alerts/trades: {sent}" if sent else "Нічого нормального не знайшов.")


async def watch_mint_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    ensure_user(str(chat.id))
    if not context.args:
        await update.message.reply_text("Напиши: /watch_mint CA")
        return
    mint = context.args[0].strip()
    if not SOLANA_ADDRESS_RE.fullmatch(mint):
        await update.message.reply_text("Це не схоже на Solana CA.")
        return
    sent = await process_candidate(context.application, mint, "manual_watch", {"manual": True, "mint": mint}, force=True)
    if not sent:
        await update.message.reply_text("AI не дав alert/trade.")


async def open_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    trades = [t for t in get_open_trades() if str(t["telegram_id"]) == str(chat.id)]
    if not trades:
        await update.message.reply_text("Немає відкритих trades.")
        return
    lines = ["📋 <b>Open trades:</b>\n"]
    for t in trades:
        lines.append(
            f"#{t['id']} — <code>{escape(short_addr(t['mint']))}</code>\n"
            f"mode: {escape(t['mode'])}, amount: {escape(t['amount_sol'])} SOL\n"
            f"entry: {escape(t['entry_price_usd'])}, high: {escape(t.get('highest_price_usd'))}\n"
            f"expires: {escape(t.get('expires_at'))}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def close_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)
    trades = [t for t in get_open_trades() if str(t["telegram_id"]) == telegram_id]

    if not trades:
        await update.message.reply_text("Немає відкритих trades для закриття.")
        return

    await update.message.reply_text(f"Закриваю open trades: {len(trades)}...")

    closed = 0
    failed = 0

    for trade in trades:
        mint = trade["mint"]
        price = None
        pnl_pct = None
        resp = {}
        note = "Manual close_all command."

        try:
            dex = summarize_dex_pairs(fetch_dexscreener_token(mint))
            price = dex.get("price_usd")
            entry = safe_float(trade.get("entry_price_usd"))
            if price is not None and entry:
                pnl_pct = ((price - entry) / entry) * 100

            if TRADING_MODE == "live" and live_trading_enabled():
                resp = pumpportal_trade("sell", mint, "100%", False)
            else:
                resp = {"paper": True, "action": "manual_paper_sell"}

            close_trade(int(trade["id"]), "manual_closed", price, resp, note)
            closed += 1

        except Exception as e:
            failed += 1
            resp = {"error": str(e)}
            update_trade(int(trade["id"]), {"sell_response": resp, "close_note": f"Manual close failed: {e}"})

        await asyncio.sleep(0.25)

    await update.message.reply_text(
        f"✅ close_all finished. Closed: {closed}, failed: {failed}",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)
    rows = supabase.table("trades").select("*").eq("telegram_id", telegram_id).execute().data or []

    if not rows:
        await update.message.reply_text("Ще немає trades для статистики.")
        return

    total = len(rows)
    open_count = len([r for r in rows if r.get("status") == "open"])
    tp = len([r for r in rows if r.get("status") in ["tp", "trailing_tp"]])
    sl = len([r for r in rows if r.get("status") == "sl"])
    expired = len([r for r in rows if r.get("status") == "expired"])
    manual = len([r for r in rows if r.get("status") == "manual_closed"])

    closed = total - open_count
    winrate = (tp / closed * 100) if closed else 0

    text = f"""
📊 <b>PumpFun test stats</b>

Total trades: {total}
Open: {open_count}
Closed: {closed}

TP / trailing TP: {tp}
SL: {sl}
Expired: {expired}
Manual closed: {manual}

Winrate by TP touch: {winrate:.2f}%

<i>Це груба статистика для тесту, не точний PnL.</i>
"""
    await update.message.reply_text(text.strip(), parse_mode=ParseMode.HTML)


async def trading_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = f"""
⚙️ <b>Trading status</b>

TRADING_MODE: <b>{escape(TRADING_MODE)}</b>
Live enabled: <b>{escape(live_trading_enabled())}</b>

TRADE_AMOUNT_SOL: {TRADE_AMOUNT_SOL}
TP: {TAKE_PROFIT_PERCENT}%
SL: {STOP_LOSS_PERCENT}%
Trailing: {TRAILING_STOP_PERCENT}%
Max hold: {MAX_HOLD_SECONDS}s

Max trades/hour: {MAX_TRADES_PER_HOUR}
Max daily trades: {MAX_DAILY_TRADES}
Max open trades: {MAX_OPEN_TRADES}

PumpPortal API key set: {escape(bool(PUMPPORTAL_API_KEY))}
Confirm ok: {escape(TRADING_CONFIRM == "YES_I_UNDERSTAND")}
"""
    await update.message.reply_text(text.strip(), parse_mode=ParseMode.HTML)


async def alerts_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    set_alerts_enabled(str(chat.id), True)
    await update.message.reply_text("✅ Alerts on")


async def alerts_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return
    set_alerts_enabled(str(chat.id), False)
    await update.message.reply_text("⏸ Alerts off")


# =========================
# MAIN
# =========================

async def post_init(application: Application) -> None:
    if ENABLE_PUMPPORTAL_NEW_TOKENS:
        asyncio.create_task(pumpportal_listener(application))


def main() -> None:
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("source_add", source_add_command))
    application.add_handler(CommandHandler("sources", sources_command))
    application.add_handler(CommandHandler("source_delete", source_delete_command))
    application.add_handler(CommandHandler("scan_x", scan_x_command))
    application.add_handler(CommandHandler("watch_mint", watch_mint_command))
    application.add_handler(CommandHandler("open_trades", open_trades_command))
    application.add_handler(CommandHandler("close_all", close_all_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("trading_status", trading_status_command))
    application.add_handler(CommandHandler("alerts_on", alerts_on_command))
    application.add_handler(CommandHandler("alerts_off", alerts_off_command))

    if ENABLE_X_RSS:
        application.job_queue.run_repeating(scheduled_x_poll, interval=X_RSS_CHECK_INTERVAL_SECONDS, first=30)

    application.job_queue.run_repeating(scheduled_manage_trades, interval=TRADE_MANAGE_INTERVAL_SECONDS, first=20)

    logger.info("PumpFun Auto Trader AI started.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
