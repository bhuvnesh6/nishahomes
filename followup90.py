"""
Nisha Homes - 90-Day Value-First Follow-Up Engine  (followup90.py)

Put this file NEXT TO app.py. app.py loads it with one line (see patch notes):
    import followup90; followup90.register(globals())

Customers are sent via the Super Admin's Wirebase instance
(wb_send_via_super_admin / send_wirebase_image already in app.py).

Mongo collections created:
  followup90_customers  - one row per lead: segment, state, touch_no, next_touch_at,
                          assets_sent, listings_sent, media_sent ...   (the "memory")
  followup90_touch_log  - one row per message attempt (exact text, media, status)
  followup90_journeys   - editable journeys (4 segments x 14 nodes) + spare pool
  settings  _id="followup90" - engine on/off, quality score, caps
"""
import os, re, json, time, random, threading, socket
from datetime import datetime, timedelta
from flask import request, session, jsonify
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

NEEDED = [
    "app", "db", "projects_collection", "settings_collection", "system_locks_collection",
    "wb_send_via_super_admin", "send_wirebase_image", "phone_last10", "is_number_blocked",
    "run_property_vector_search", "get_ist_now", "format_ist", "parse_price_to_number",
    "_format_price_display", "_classify_media_urls", "JULY_2026_START", "_is_lost_lead",
    "detect_optout_level", "find_lead_by_any_phone",
]

SEGMENTS = {"buyer": "Buyer", "tenant": "Tenant (Rent)", "seller": "Seller", "investor": "Investor"}
ACTIVE_STATES = ("ACTIVE_SEQUENCE", "QUARTERLY_NURTURE")
IST = timedelta(hours=5, minutes=30)
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "followup90_seed.json")
OWNER = f"{socket.gethostname()}-{os.getpid()}"

# Guardrail (guide D6): a message containing any of these is never sent / never saved.
BANNED = re.compile(
    r"still looking|any update|have you decided|last chance|hurry|limited|only \d+ left|"
    r"prices will rise|guaranteed|assured returns|jaldi karein|sirf kuch hi bache", re.I)

DEFAULT_CFG = {
    "enabled": False,          # master switch - OFF until you turn it on from the page
    "min_score": 60,           # only leads with AIScore >= this (or High / Very High interest)
    "daily_cap": 60,           # max messages per day (all customers)
    "per_scan": 12,            # max messages per 5-minute scan
    "enroll_per_day": 40,      # max NEW leads put into the journey per day
    "gap_min": 25, "gap_max": 70,   # random seconds between two sends (anti-ban)
    "test_day_minutes": 0,     # TEST: 1 journey day = N minutes (0 = real days)
    "ignore_window": False,    # TEST: ignore 10:30-13:00 / 17:30-20:00 Mon-Sat window
}


def _now():
    return datetime.utcnow()


def cfg():
    d = settings_collection.find_one({"_id": "followup90"}) or {}
    return {**DEFAULT_CFG, **{k: v for k, v in d.items() if k != "_id"}}


def day_len(c):
    m = c.get("test_day_minutes") or 0
    return timedelta(minutes=m) if m else timedelta(days=1)


def anchor(start, day_no, c):
    """Day N of the journey = start + (N-1) days at 11:00 IST (guide: Touch NEXT)."""
    if c.get("test_day_minutes"):
        return start + (day_no - 1) * day_len(c)
    t = (start + IST).replace(hour=11, minute=0, second=0, microsecond=0) + timedelta(days=day_no - 1)
    return t - IST


def in_window(c):
    if c.get("ignore_window") or c.get("test_day_minutes"):
        return True
    t = get_ist_now()
    if t.weekday() == 6:      # Sunday
        return False
    m = t.hour * 60 + t.minute
    return 630 <= m < 780 or 1050 <= m < 1200     # 10:30-13:00 and 17:30-20:00


# ----------------------------------------------------------------- journeys
def seed_journeys(force=None):
    """Loads followup90_seed.json (built from the PDF). Existing edited journeys are
    never overwritten unless force=<segment | 'spare'>."""
    with open(SEED_FILE, encoding="utf-8") as fh:
        seed = json.load(fh)
    for seg, nodes in seed["segments"].items():
        if force == seg or (force is None and not jr_c.find_one({"_id": seg})):
            jr_c.replace_one({"_id": seg}, {"_id": seg, "title": SEGMENTS[seg], "nodes": nodes}, upsert=True)
    if force == "spare" or (force is None and not jr_c.find_one({"_id": "spare"})):
        jr_c.replace_one({"_id": "spare"}, {"_id": "spare", "title": "Spare pool", "nodes": seed["spare"]}, upsert=True)


def pick_spare(cu):
    sp = jr_c.find_one({"_id": "spare"}) or {"nodes": []}
    for n in sp["nodes"]:
        if n.get("enabled", True) and n["asset"] not in cu.get("assets_sent", []):
            return n
    return None


# ----------------------------------------------------------------- lead helpers
def first_name(lead):
    n = (lead.get("Lead Name") or lead.get("Name") or "").strip()
    n = re.sub(r"[^A-Za-z\u0900-\u097F ]", "", n).strip()
    return n.split()[0].title() if n else "Sir/Madam"


def req_snapshot(lead, ed):
    loc = (lead.get("Location Interested In") or ed.get("Location Interested In")
           or lead.get("Property Location") or "").strip()
    if loc.lower() in ("ncr", "delhi ncr", "ncr region"):
        loc = ""
    return {
        "location": loc,
        "budget": str(lead.get("Budget Range") or ed.get("Budget Range") or lead.get("Expected Price") or "").strip(),
        "property_type": str(lead.get("Property Type") or ed.get("Property Type") or "").strip(),
        "bhk": str(lead.get("Configuration") or ed.get("Configuration") or "").strip(),
    }


def req_phrase(r):
    p = " ".join(x for x in (r["bhk"], r["property_type"] if r["property_type"] != r["bhk"] else "") if x).strip()
    p = p or "property"
    return p + (" in " + r["location"] if r["location"] else "")


def seg_for(coll, lead, ed):
    lt = (lead.get("LeadType") or "").strip().lower()
    if lt == "agent" or coll == "agentLeads":
        return None
    if lt == "seller" or coll == "sellingLeads":
        return "seller"
    if lt == "buyer_rental" or coll == "RentalLeads":
        return "tenant"
    blob = " ".join(str(lead.get(k) or "") for k in ("Size_Purpose_Note", "note", "Final Intent", "AICallSummary"))
    blob += " " + str(ed.get("Caller Remarks") or "")
    return "investor" if re.search(r"invest|rental income|\broi\b|returns", blob, re.I) else "buyer"


def quality_ok(lead, ed, c):
    """HIGH-QUALITY ONLY: High / Very High interest, or AI score >= min_score. Never cold / lost / opted-out."""
    if lead.get("followup_optout") or (ed and _is_lost_lead(ed)):
        return False
    lvl = (ed.get("Interest Level") or "").strip().lower()
    if lvl == "cold":
        return False
    if lvl in ("very high", "high"):
        return True
    s = lead.get("AIScore")
    return isinstance(s, (int, float)) and s >= c["min_score"]


def enroll(coll, lead, ed=None, force=False, c=None):
    c = c or cfg()
    ed = ed or {}
    p10 = phone_last10(lead.get("Phone Number"))
    if not p10 or is_number_blocked(p10):
        return None
    seg = seg_for(coll, lead, ed)
    if not seg or (not force and not quality_ok(lead, ed, c)):
        return None
    t = _now()
    nxt, li = t, None
    lu = lead.get("lastUserMsgAtUTC")
    if isinstance(lu, datetime) and t - lu < timedelta(hours=72):   # customer is mid-conversation
        li, nxt = lu, lu + timedelta(hours=72)
    doc = {
        "phone10": p10, "phone91": "91" + p10, "lead_id": str(lead["_id"]), "collection": coll,
        "name": first_name(lead), "segment": seg, "state": "ACTIVE_SEQUENCE", "touch_no": 0,
        "sequence_start": nxt, "next_touch_at": nxt, "assets_sent": [], "listings_sent": [],
        "media_sent": [], "sends_total": 0, "fail_count": 0, "opt_out": False,
        "last_outbound_at": None, "last_inbound_at": li, "last_property_at": None,
        "score": lead.get("AIScore"), "created_at": t, "updated_at": t,
    }
    try:
        return cust_c.insert_one(doc).inserted_id
    except DuplicateKeyError:
        return None


def enroll_new(c):
    ist = get_ist_now()
    day_start = ist.replace(hour=0, minute=0, second=0, microsecond=0) - IST
    room = c["enroll_per_day"] - cust_c.count_documents({"created_at": {"$gte": day_start}})
    if room <= 0:
        return 0
    known = {d["phone10"] for d in cust_c.find({}, {"phone10": 1})}
    n = 0
    for coll in ("Leads", "RentalLeads", "sellingLeads"):
        q = {"AIScore": {"$gte": c["min_score"]}, "DateObj": {"$gte": JULY_2026_START},
             "followup_optout": {"$ne": True}}
        for lead in db[coll].find(q).sort("AIScore", -1).limit(300):
            if n >= room:
                return n
            p10 = phone_last10(lead.get("Phone Number"))
            if not p10 or p10 in known:
                continue
            ed = db["endData"].find_one({"Number": "91" + p10}) or {}
            if enroll(coll, lead, ed, c=c):
                n += 1
                known.add(p10)
    for e in db["endData"].find({"Interest Level": {"$in": ["Very High", "High"]}}, {"Number": 1}).limit(300):
        if n >= room:
            break
        p10 = phone_last10(e.get("Number"))
        if not p10 or p10 in known:
            continue
        coll, lead = find_lead_by_any_phone(p10)
        if lead and coll != "agentLeads":
            ed = db["endData"].find_one({"Number": "91" + p10}) or {}
            if enroll(coll, lead, ed, c=c):
                n += 1
                known.add(p10)
    return n


# ----------------------------------------------------------------- media (from inventory, NEVER the banner)
_pool = {"at": 0, "items": []}


def media_pool():
    if _pool["items"] and time.time() - _pool["at"] < 300:
        return _pool["items"]
    items = []
    for d in projects_collection.find({"status": "approved"},
                                      {"name": 1, "location": 1, "mediaUrls": 1, "bannerUrl": 1, "img": 1, "uniqueId": 1}):
        banner = {d.get("bannerUrl"), d.get("img")}
        imgs, _v = _classify_media_urls(d.get("mediaUrls") or [])
        for u in imgs:
            if u not in banner:
                items.append({"url": u, "name": d.get("name") or "", "location": d.get("location") or "",
                              "uid": d.get("uniqueId")})
    _pool.update(at=time.time(), items=items)
    return items


def pick_pool_media(cu):
    pool = media_pool()
    if not pool:
        return None
    used = set(cu.get("media_sent", []))
    fresh = [i for i in pool if i["url"] not in used]
    return random.choice(fresh or pool)["url"]


def listing_media(uid, cu):
    d = projects_collection.find_one({"uniqueId": uid}, {"mediaUrls": 1, "bannerUrl": 1, "img": 1}) or {}
    banner = {d.get("bannerUrl"), d.get("img")}
    imgs, _v = _classify_media_urls(d.get("mediaUrls") or [])
    imgs = [u for u in imgs if u not in banner]
    if not imgs:
        return None
    fresh = [u for u in imgs if u not in cu.get("media_sent", [])]
    return (fresh or imgs)[0]


# ----------------------------------------------------------------- property matching + message building
def _n(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


def fmt_price(raw):
    v = parse_price_to_number(raw)
    if v and v >= 1e7:
        return f"₹{_n(v / 1e7)} Cr"
    if v and v >= 1e5:
        return f"₹{_n(v / 1e5)} Lakh"
    return _format_price_display(raw)


def view_url(uid):
    return f"{os.getenv('PUBLIC_BASE_URL', 'https://crm.nishahomes.com').rstrip('/')}/view/{uid}"


def find_listings(req, rent, exclude, need):
    """Only approved inventory that matches AREA (+BHK +BUDGET when known) and wasn't sent before."""
    if not req["location"]:
        return []
    q = " ".join(x for x in (req["bhk"], req["property_type"], "in", req["location"], "budget", req["budget"]) if x)
    try:
        res = run_property_vector_search(
            query_text=q, deal_type="For Rent" if rent else "For Sale", location=req["location"],
            budget=req["budget"] or None, property_type=req["property_type"], bhk=req["bhk"], limit=10) or []
    except Exception as e:
        print(f"[fu90] listing search failed: {e}")
        return []
    nb = re.sub(r"\s", "", req["bhk"]).lower()
    out = []
    for p in res:
        r = p.get("_matchReasons") or []
        if "location_match" not in r or "budget_mismatch" in r:
            continue
        if req["budget"] and not ({"budget_match", "budget_close"} & set(r)):
            continue
        if nb and nb not in re.sub(r"\s", "", p.get("configuration") or "").lower():
            continue
        if not p.get("uniqueId") or p["uniqueId"] in exclude:
            continue
        out.append(p)
        if len(out) >= need:
            break
    return out


def listing_vars(p):
    spec_area = p.get("carpetArea") or p.get("superArea")
    size = f"{spec_area} {p.get('areaUnit') or 'sqft'}" if spec_area else ""
    spec = " · ".join(x for x in ((p.get("configuration") or "").strip(), size) if x)
    if not (p.get("location") and p.get("price") and spec):
        return None
    return {"2": p["location"], "3": spec, "4": fmt_price(p["price"]),
            "5": (p.get("possession") or "").strip(), "6": view_url(p["uniqueId"])}


def render(text, v):
    """Fills {{n}}. {{5}}/{{6}} lines are dropped when empty; any other empty variable -> None (never send blank)."""
    out = []
    for line in (text or "").split("\n"):
        ks = re.findall(r"\{\{(\d)\}\}", line)
        miss = [k for k in ks if not v.get(k)]
        if miss:
            if all(k in ("5", "6") for k in miss):
                continue
            return None
        out.append(re.sub(r"\{\{(\d)\}\}", lambda m: str(v[m.group(1)]), line))
    return "\n".join(out).strip()


def resolve(cu, node, req, v0, seg):
    """Decides EXACTLY what goes out for this slot. Returns dict or None."""
    v, reason, listings, ps, cur = dict(v0), None, [], [], node
    kind = node.get("kind", "EDU")
    if kind in ("PROP", "PROP2"):
        need = 2 if kind == "PROP2" else 1
        found = find_listings(req, seg == "tenant", set(cu.get("listings_sent", [])), need)
        lv = [listing_vars(p) for p in found]
        if len(found) >= need and all(lv):
            ps = found
            listings = [p["uniqueId"] for p in ps]
            if need == 1:
                v.update(lv[0])
            else:
                v["2"] = f'{lv[0]["2"]} · {lv[0]["3"]} · {lv[0]["4"]}/month'
                v["3"] = f'{lv[1]["2"]} · {lv[1]["3"]} · {lv[1]["4"]}/month'
        else:
            reason, cur = "NO_VERIFIED_MATCH", (node.get("alt") or pick_spare(cu))
    elif node["asset"] in cu.get("assets_sent", []):
        reason, cur = "ASSET_ALREADY_SENT", pick_spare(cu)
    if not cur:
        return None

    def build(n):
        vv = dict(v)
        if n["asset"] in ("MKT-01", "RE-06"):
            vv["2"] = req["location"]
        body = render(n.get("message"), vv)
        if not body:
            return None
        foot = render(n.get("footer") or "", vv)
        return body + ("\n\n" + foot if foot else "")

    text = build(cur)
    if not text and cur is node:                     # e.g. MKT-01 without a locality -> next spare poster
        reason, cur, ps, listings = "MISSING_VARIABLE", pick_spare(cu), [], []
        text = build(cur) if cur else None
    if not text:
        return None

    media, mode = None, cur.get("media_mode", "auto")
    if mode == "custom" and cur.get("media_url"):
        media = cur["media_url"]
    elif mode == "auto":
        media = listing_media(ps[0]["uniqueId"], cu) if ps else pick_pool_media(cu)
    return {"asset": cur["asset"], "template": cur.get("template", ""), "kind": cur.get("kind", "EDU"),
            "text": text, "media": media, "listings": listings, "fallback": reason}


# ----------------------------------------------------------------- sending one customer
def _log(cu, r, status, err=None, touch=None, day=None):
    log_c.insert_one({
        "cust_id": cu["_id"], "phone10": cu["phone10"], "name": cu.get("name"), "segment": cu.get("segment"),
        "touch_no": touch, "day_no": day, "asset_id": (r or {}).get("asset"), "template": (r or {}).get("template"),
        "kind": (r or {}).get("kind"), "message_text": (r or {}).get("text"), "media_url": (r or {}).get("media"),
        "listing_uids": (r or {}).get("listings", []), "fallback_reason": (r or {}).get("fallback"),
        "status": status, "error": err, "sent_at": _now(), "replied": False})


def _set(cu, **f):
    f["updated_at"] = _now()
    cust_c.update_one({"_id": cu["_id"]}, {"$set": f})


def deliver(phone, text, media):
    if media:
        ok, err, _r = send_wirebase_image(phone, media, text[:1024])
        if ok:
            return True, None
        print(f"[fu90] image send failed ({err}) - falling back to text")
    ok, err, _r = wb_send_via_super_admin(phone, {"type": "text", "message": text})
    return ok, err


def send_next(cid, c):
    cu = cust_c.find_one({"_id": cid})
    if not cu or cu["state"] not in ACTIVE_STATES or cu.get("opt_out"):
        return "skip"
    t = _now()
    lead = db[cu["collection"]].find_one({"_id": ObjectId(cu["lead_id"])})
    if not lead or lead.get("followup_optout") or is_number_blocked(cu["phone10"]):
        _set(cu, state="STOPPED", opt_out=True, stop_reason="LEAD_GONE_OR_OPTED_OUT")
        return "skip"
    ed = db["endData"].find_one({"Number": cu["phone91"]}) or {}
    seg = seg_for(cu["collection"], lead, ed)
    if not seg:
        _set(cu, state="STOPPED", stop_reason="AGENT_LEAD")
        return "skip"
    if seg != cu["segment"]:                          # segment changed -> keep touch_no + assets_sent
        _set(cu, segment=seg)
        cu["segment"] = seg

    # rule 2: a reply stops everything
    li, lo = cu.get("last_inbound_at"), cu.get("last_outbound_at")
    lu = lead.get("lastUserMsgAtUTC")
    if isinstance(lu, datetime) and (not li or lu > li):
        li = lu
        _set(cu, last_inbound_at=lu)
    if li and (not lo or li > lo):
        _set(cu, state="PAUSED")
        return "skip"
    # rule 7: max 3 automated messages in any 7 days
    if not c.get("test_day_minutes") and log_c.count_documents(
            {"cust_id": cu["_id"], "status": "sent", "sent_at": {"$gte": t - timedelta(days=7)}}) >= 3:
        _set(cu, next_touch_at=t + timedelta(days=2))
        return "skip"

    journey = jr_c.find_one({"_id": seg}) or {"nodes": []}
    nodes = journey["nodes"]
    idx = cu["touch_no"]
    nurture = cu["state"] == "QUARTERLY_NURTURE" or idx >= len(nodes)
    req = req_snapshot(lead, ed)
    v0 = {"1": cu["name"], "2": req_phrase(req)}
    day = None
    if not nurture:
        node = nodes[idx]
        day = node.get("day")
        if node.get("enabled", True) is False:       # admin switched this day off -> skip it
            _log(cu, {"asset": node["asset"]}, "skipped", "node disabled", idx + 1, day)
            _advance(cu, nodes, idx + 1, t, c)
            return "skip"
    else:
        dl = day_len(c)
        lp = cu.get("last_property_at")
        prop = next((n for n in nodes if n.get("kind") in ("PROP", "PROP2")), None)
        node = prop if (prop and (not lp or t - lp >= 90 * dl)) else pick_spare(cu)
        if not node:
            _set(cu, state="NURTURE_DONE", stop_reason="NO_UNUSED_POSTERS")
            return "skip"

    r = resolve(cu, node, req, v0, seg)
    if not r:
        _set(cu, next_touch_at=t + timedelta(days=1))
        _log(cu, None, "skipped", "could not build a valid message", idx + 1, day)
        return "skip"
    if BANNED.search(r["text"]):
        _set(cu, next_touch_at=t + timedelta(days=1))
        _log(cu, r, "blocked", "banned word in message", idx + 1, day)
        return "skip"

    ok, err = deliver(cu["phone91"], r["text"], r["media"])
    touch = idx + 1
    if not ok:
        if err and "Super Admin" in str(err):         # WhatsApp itself is down -> not the customer's fault
            print(f"[fu90] WhatsApp not ready: {err} - aborting this scan")
            return "abort"
        fails = cu.get("fail_count", 0) + 1
        _log(cu, r, "failed", str(err)[:300], touch, day)
        if fails >= 2:
            _set(cu, state="STOPPED", fail_count=fails, flagged=True, stop_reason="DELIVERY_FAILED_2X")
        else:
            _set(cu, fail_count=fails, next_touch_at=t + max(timedelta(hours=2), day_len(c) / 12))
        return "skip"

    _log(cu, r, "sent", None, touch, day)
    upd = {"$set": {"touch_no": touch, "last_outbound_at": t, "fail_count": 0, "updated_at": t},
           "$inc": {"sends_total": 1},
           "$push": {"assets_sent": r["asset"]}}
    if r["listings"]:
        upd["$push"]["listings_sent"] = {"$each": r["listings"]}
        upd["$set"]["last_property_at"] = t
    if r["media"]:
        upd["$push"]["media_sent"] = r["media"]
    cust_c.update_one({"_id": cu["_id"]}, upd)
    _advance({**cu, "last_outbound_at": t}, nodes, touch, t, c, nurture)
    return "sent"


def _advance(cu, nodes, touch, t, c, nurture=False):
    """Writes next_touch_at. Journey: start + (day-1) at 11:00 IST, but never sooner than 1 day after this send."""
    dl = day_len(c)
    if nurture:
        _set(cu, touch_no=touch, state="QUARTERLY_NURTURE", next_touch_at=t + 30 * dl)
    elif touch >= len(nodes):
        _set(cu, touch_no=touch, state="QUARTERLY_NURTURE", next_touch_at=t + 7 * dl)
    else:
        planned = anchor(cu["sequence_start"], nodes[touch]["day"], c)
        _set(cu, touch_no=touch, next_touch_at=max(planned, t + dl))


# ----------------------------------------------------------------- scheduler
def _lock(ttl):
    t = _now()
    r = system_locks_collection.find_one_and_update(
        {"_id": "fu90_scheduler", "$or": [{"lockedUntil": {"$lt": t}}, {"lockedBy": OWNER}]},
        {"$set": {"lockedUntil": t + timedelta(seconds=ttl), "lockedBy": OWNER, "lockedAt": t}})
    if r is not None:
        return True
    try:
        system_locks_collection.insert_one({"_id": "fu90_scheduler", "lockedUntil": t + timedelta(seconds=ttl),
                                            "lockedBy": OWNER, "lockedAt": t})
        return True
    except Exception:
        return False


def housekeeping(c):
    """72 h of silence after a customer reply -> journey resumes (guide rule 2)."""
    t = _now()
    need = 3 * day_len(c)
    for cu in cust_c.find({"state": "PAUSED"}):
        li = cu.get("last_inbound_at")
        if li and t - li >= need:
            _set(cu, state="ACTIVE_SEQUENCE", next_touch_at=t)


def run_scan():
    c = cfg()
    housekeeping(c)
    if not c["enabled"] or not in_window(c):
        return {"ran": False}
    if not _lock(1500):
        return {"ran": False, "reason": "locked"}
    enrolled = enroll_new(c)
    day_start = get_ist_now().replace(hour=0, minute=0, second=0, microsecond=0) - IST
    sent_today = log_c.count_documents({"status": "sent", "sent_at": {"$gte": day_start}})
    room = min(c["per_scan"], c["daily_cap"] - sent_today)
    sent = 0
    if room > 0:
        due = list(cust_c.find({"state": {"$in": list(ACTIVE_STATES)}, "opt_out": {"$ne": True},
                                "next_touch_at": {"$lte": _now()}}).sort("next_touch_at", 1).limit(room * 4))
        for d in due:
            if sent >= room or not in_window(c):
                break
            _lock(1500)
            try:
                res = send_next(d["_id"], c)
            except Exception:
                import traceback
                traceback.print_exc()
                continue
            if res == "abort":
                break
            if res == "sent":
                sent += 1
                time.sleep(random.uniform(c["gap_min"], c["gap_max"]))
    print(f"[fu90] scan done: enrolled={enrolled} sent={sent}")
    return {"ran": True, "enrolled": enrolled, "sent": sent}


def _loop():
    print("[fu90] scheduler started")
    while True:
        try:
            run_scan()
        except Exception:
            import traceback
            traceback.print_exc()
        time.sleep(300)


# ----------------------------------------------------------------- inbound hook (called from both WhatsApp webhooks)
def on_inbound(number, text):
    p10 = phone_last10(number)
    cu = cust_c.find_one({"phone10": p10}) if p10 else None
    if not cu:
        return
    t = _now()
    f = {"last_inbound_at": t, "updated_at": t}
    if detect_optout_level(text) in ("hard", "soft"):
        f.update(state="STOPPED", opt_out=True, stop_reason="CUSTOMER_STOP")
    elif cu["state"] in ACTIVE_STATES:
        f["state"] = "PAUSED"                         # chatbot / human answers first
    cust_c.update_one({"_id": cu["_id"]}, {"$set": f})
    log_c.update_many({"cust_id": cu["_id"], "status": "sent", "replied": False,
                       "sent_at": {"$gte": t - timedelta(hours=72)}}, {"$set": {"replied": True, "replied_at": t}})


# ----------------------------------------------------------------- API routes
def _staff():
    return session.get("role") in ("admin", "emp")


def _admin():
    return session.get("role") == "admin"


def _deny(admin_only=False):
    if admin_only and not _admin():
        return jsonify({"success": False, "message": "Admin only"}), 403
    if not _staff():
        return jsonify({"success": False, "message": "Staff only"}), 403
    return None


def _cj(d):
    f = lambda k: format_ist(d.get(k)) if isinstance(d.get(k), datetime) else "-"
    return {"id": str(d["_id"]), "name": d.get("name"), "phone": d.get("phone91"), "segment": d.get("segment"),
            "state": d.get("state"), "touch": d.get("touch_no", 0), "next": f("next_touch_at"),
            "last": f("last_outbound_at"), "sends": d.get("sends_total", 0), "score": d.get("score"),
            "reason": d.get("stop_reason", ""), "fails": d.get("fail_count", 0)}


def _routes():
    @app.route("/api/fu90/journeys")
    def fu90_journeys():
        e = _deny()
        if e: return e
        out = {d["_id"]: d["nodes"] for d in jr_c.find()}
        return jsonify({"success": True, "journeys": out, "segments": SEGMENTS})

    @app.route("/api/fu90/node", methods=["POST"])
    def fu90_node():
        e = _deny(True)
        if e: return e
        d = request.json or {}
        seg, f = d.get("segment"), d.get("fields") or {}
        match = {"nodes.asset": d.get("asset")} if seg == "spare" else {"nodes.touch": int(d.get("touch", 0))}
        sets = {}
        for k in ("message", "footer", "media_url"):
            if k in f:
                sets[f"nodes.$.{k}"] = str(f[k] or "")
        if f.get("media_mode") in ("auto", "custom", "none"):
            sets["nodes.$.media_mode"] = f["media_mode"]
        if "enabled" in f:
            sets["nodes.$.enabled"] = bool(f["enabled"])
        if "day" in f and seg != "spare":
            sets["nodes.$.day"] = max(1, min(90, int(f["day"])))
        if "alt_message" in f:
            sets["nodes.$.alt.message"] = str(f["alt_message"] or "")
        for k, val in sets.items():
            if k.endswith(("message", "footer")) and BANNED.search(val):
                return jsonify({"success": False, "message": f"Banned phrase found: “{BANNED.search(val).group(0)}” (guide rule 5)"}), 400
        if "nodes.$.media_mode" in sets and sets["nodes.$.media_mode"] == "custom" and not sets.get("nodes.$.media_url"):
            return jsonify({"success": False, "message": "Custom media needs a URL"}), 400
        r = jr_c.update_one({"_id": seg, **match}, {"$set": sets})
        return jsonify({"success": r.matched_count == 1})

    @app.route("/api/fu90/journey/reset/<seg>", methods=["POST"])
    def fu90_reset(seg):
        e = _deny(True)
        if e: return e
        if seg not in SEGMENTS and seg != "spare":
            return jsonify({"success": False}), 400
        seed_journeys(force=seg)
        return jsonify({"success": True})

    @app.route("/api/fu90/settings", methods=["GET", "POST"])
    def fu90_settings():
        e = _deny(request.method == "POST")
        if e: return e
        if request.method == "POST":
            d, sets = request.json or {}, {}
            for k, dv in DEFAULT_CFG.items():
                if k in d:
                    sets[k] = bool(d[k]) if isinstance(dv, bool) else int(d[k])
            if sets:
                settings_collection.update_one({"_id": "followup90"}, {"$set": sets}, upsert=True)
        return jsonify({"success": True, "settings": cfg()})

    @app.route("/api/fu90/stats")
    def fu90_stats():
        e = _deny()
        if e: return e
        day_start = get_ist_now().replace(hour=0, minute=0, second=0, microsecond=0) - IST
        states = {r["_id"]: r["n"] for r in cust_c.aggregate([{"$group": {"_id": "$state", "n": {"$sum": 1}}}])}
        segs = {r["_id"]: r["n"] for r in cust_c.aggregate([{"$group": {"_id": "$segment", "n": {"$sum": 1}}}])}
        sent = log_c.count_documents({"status": "sent"})
        replied = log_c.count_documents({"status": "sent", "replied": True})
        return jsonify({"success": True, "states": states, "segments": segs, "sentTotal": sent,
                        "sentToday": log_c.count_documents({"status": "sent", "sent_at": {"$gte": day_start}}),
                        "failed": log_c.count_documents({"status": "failed"}),
                        "fallbacks": log_c.count_documents({"fallback_reason": {"$ne": None}}),
                        "replyRate": round(replied * 100 / sent, 1) if sent else 0,
                        "optOuts": cust_c.count_documents({"stop_reason": "CUSTOMER_STOP"}),
                        "mediaPool": len(media_pool()), "inWindow": in_window(cfg())})

    @app.route("/api/fu90/customers")
    def fu90_customers():
        e = _deny()
        if e: return e
        q = {}
        for k in ("state", "segment"):
            if request.args.get(k):
                q[k] = request.args[k]
        s = (request.args.get("q") or "").strip()
        if s:
            q["$or"] = [{"name": {"$regex": re.escape(s), "$options": "i"}}, {"phone10": {"$regex": re.escape(s)}}]
        rows = list(cust_c.find(q).sort("updated_at", -1).limit(300))
        return jsonify({"success": True, "count": len(rows), "data": [_cj(r) for r in rows]})

    @app.route("/api/fu90/customer/<cid>/log")
    def fu90_customer_log(cid):
        e = _deny()
        if e: return e
        rows = list(log_c.find({"cust_id": ObjectId(cid)}).sort("sent_at", -1).limit(60))
        return jsonify({"success": True, "data": [{
            "touch": r.get("touch_no"), "day": r.get("day_no"), "asset": r.get("asset_id"), "status": r.get("status"),
            "fallback": r.get("fallback_reason"), "text": r.get("message_text"), "media": r.get("media_url"),
            "error": r.get("error"), "at": format_ist(r.get("sent_at")), "replied": r.get("replied")} for r in rows]})

    @app.route("/api/fu90/customer/<cid>/action", methods=["POST"])
    def fu90_customer_action(cid):
        e = _deny()
        if e: return e
        cu = cust_c.find_one({"_id": ObjectId(cid)})
        if not cu:
            return jsonify({"success": False}), 404
        a, t = (request.json or {}).get("action"), _now()
        sets = {"pause": {"state": "PAUSED"},
                "resume": {"state": "ACTIVE_SEQUENCE", "next_touch_at": t, "opt_out": False, "fail_count": 0},
                "handoff": {"state": "HUMAN_HANDOFF"},
                "stop": {"state": "STOPPED", "opt_out": True, "stop_reason": "MANUAL"},
                "skip": {"touch_no": cu.get("touch_no", 0) + 1, "next_touch_at": t}}.get(a)
        if a == "restart":
            sets = {"state": "ACTIVE_SEQUENCE", "touch_no": 0, "sequence_start": t, "next_touch_at": t,
                    "assets_sent": [], "listings_sent": [], "media_sent": [], "fail_count": 0, "opt_out": False}
        if not sets:
            return jsonify({"success": False, "message": "Unknown action"}), 400
        _set(cu, **sets)
        return jsonify({"success": True})

    @app.route("/api/fu90/enroll", methods=["POST"])
    def fu90_enroll():
        e = _deny()
        if e: return e
        d = request.json or {}
        try:
            lead = db[d["collection"]].find_one({"_id": ObjectId(d["id"])})
        except Exception:
            lead = None
        if not lead:
            return jsonify({"success": False, "message": "Lead not found"}), 404
        ed = db["endData"].find_one({"Number": "91" + (phone_last10(lead.get("Phone Number")) or "")}) or {}
        ok = enroll(d["collection"], lead, ed, force=True)
        return jsonify({"success": bool(ok), "message": "" if ok else "Already enrolled / blocked / agent lead"})

    @app.route("/api/fu90/media-pool")
    def fu90_media_pool():
        e = _deny()
        if e: return e
        s = (request.args.get("q") or "").lower()
        items = [i for i in media_pool() if not s or s in (i["name"] + " " + i["location"]).lower()]
        return jsonify({"success": True, "total": len(items), "data": items[:90]})

    @app.route("/api/fu90/run", methods=["POST"])
    def fu90_run():
        e = _deny(True)
        if e: return e
        threading.Thread(target=run_scan, daemon=True, name="fu90-manual").start()
        return jsonify({"success": True, "message": "Scan started (respects ON/OFF switch and sending window)"})


_started = False


def register(ns):
    global cust_c, log_c, jr_c, _started
    globals().update({k: ns[k] for k in NEEDED})
    cust_c, log_c, jr_c = db["followup90_customers"], db["followup90_touch_log"], db["followup90_journeys"]
    try:
        cust_c.create_index("phone10", unique=True)
        cust_c.create_index([("state", 1), ("next_touch_at", 1)])
        log_c.create_index([("cust_id", 1), ("sent_at", -1)])
        seed_journeys()
    except Exception as e:
        print(f"[fu90] startup warning: {e}")
    _routes()
    ns["fu90_on_inbound"] = on_inbound
    if not _started and (os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not ns["app"].debug):
        threading.Thread(target=_loop, daemon=True, name="fu90-scheduler").start()
        _started = True
    print("[fu90] 90-day follow-up engine registered")