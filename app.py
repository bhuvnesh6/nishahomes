from flask import Flask, render_template, request, redirect, flash, jsonify, send_from_directory, send_file
from pymongo import MongoClient
from dotenv import load_dotenv
import pandas as pd
import os
import math
from flask_cors import CORS
from bson import ObjectId
from werkzeug.utils import secure_filename
import time
from datetime import datetime
from img_to_text import extract_text_from_image
#from video_to_audio import extract_audio_from_video
from make_contact import create_contact
import tempfile
#import cv2
import os
import time
from flask import session
import random
import requests
from datetime import timedelta
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import io
import calendar
import re
import json
import fitz  # PyMuPDF — pip install pymupdf

# NEW: Cloudinary (used for Inventory / project image & video uploads)
import cloudinary
import cloudinary.uploader
import cloudinary.api
import subprocess
import shutil
# NEW: branding overlay for images
from PIL import Image, ImageDraw, ImageFont

# Load env
load_dotenv()

# NEW: Cloudinary config - reads from .env
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=60)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "supersecretkey")

CORS(app)


# Upload folder
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Mongo Config
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
dai_collection = db["DAI"]
# CHANGED: was db["project"] - now uses "projects" per the Inventory feature
projects_collection = db["projects"]

# -----------------------------------------------------------------
# DIAGNOSTIC: prints, on startup, which Mongo DB this process is
# actually connected to. Check `docker logs <container>` right after
# the container starts. If MONGO_URI/DB_NAME show as None/empty, or
# the host doesn't match your real DB, your .env is not being loaded
# inside the container (missing env_file / --env-file, or excluded by
# .dockerignore) and the app has silently fallen back to a default
# local Mongo connection instead of your intended database — that
# alone explains "works on localhost, admin missing on VPS": the VPS
# container may be pointed at a different Mongo than you think.
# -----------------------------------------------------------------
print(f"[startup] DB_NAME env var = {DB_NAME!r}")
print(f"[startup] MONGO_URI is set = {bool(MONGO_URI)}")
try:
    print(f"[startup] teamAssign doc count in this DB = {db['teamAssign'].count_documents({})}")
    print(f"[startup] roles present = {sorted(set(db['teamAssign'].distinct('roll')))}")
except Exception as _diag_err:
    print(f"[startup] Could NOT query teamAssign - Mongo connection problem: {_diag_err}")

# Helper function
def serialize_doc(doc):
    doc["_id"] = str(doc["_id"])

    for key, value in doc.items():
        if isinstance(value, float) and math.isnan(value):
            doc[key] = None
        elif isinstance(value, datetime):
            doc[key] = format_ist(value)

    return doc

def format_ist(dt):
    """Formats a UTC datetime into IST 'hh:mm AM/PM . dd/mm/yyyy'"""
    if not isinstance(dt, datetime):
        return "-"
    ist = dt + timedelta(hours=5, minutes=30)
    return ist.strftime("%I:%M %p . %d/%m/%Y")

def get_collection_data(collection_name):
    collection = db[collection_name]
    data = list(collection.find())
    return [serialize_doc(doc) for doc in data]


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return response


# -----------------------------------------------------------------
# NEW: PARTNER ACCESS LOCK
# Partners (roll == "partner" in teamAssign) may ONLY reach the
# inventory dashboard, the projects API, uploaded media, and logout.
# Everything else (leads, team, exports, admin/emp dashboards, etc.)
# redirects them straight back to /inventory.
# -----------------------------------------------------------------
PARTNER_ALLOWED_PATHS = {"/inventory", "/logout", "/add-inventory"}
PARTNER_ALLOWED_PREFIXES = ("/api/projects", "/api/ai", "/uploads", "/static")

@app.before_request
def restrict_partner_access():
    if request.method == "OPTIONS":
        return  # let CORS preflight through untouched

    if session.get("role") == "partner":
        path = request.path
        if path in PARTNER_ALLOWED_PATHS:
            return
        if path.startswith(PARTNER_ALLOWED_PREFIXES):
            return
        return redirect("/inventory")


def parse_lead_date(date_str):
    """Leads store Date as DD-MM-YYYY. Defensive about other formats too."""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


# NEW: only show leads dated from July 2026 up to "now". Any lead whose
# Date field is missing or doesn't parse in any known format is dropped
# entirely (never shown as "undated") — same rule applies automatically
# to any new lead added going forward, as long as its Date field parses
# and isn't in the future.
JULY_2026_START = datetime(2026, 7, 1)

def filter_by_july_range(docs):
    now = datetime.utcnow()
    kept = []
    for d in docs:
        parsed = parse_lead_date(d.get("Date"))
        # NEW: fall back to "Created At" (written by /add-lead) if "Date"
        # is missing or unparseable — this covers leads created before
        # /add-lead started also writing "Date", so they don't silently
        # vanish from /api/leads and friends.
        if not parsed:
            parsed = parse_created_at_str(d.get("Created At"))
        if not parsed:
            continue
        if JULY_2026_START <= parsed <= now:
            kept.append(d)
    return kept


def get_date_range(period):
    """Returns (start_date, end_date) for the given export period, or (None, None) for 'all'."""
    now = datetime.utcnow()

    if period == "this_month":
        start = datetime(now.year, now.month, 1)
        return start, now

    if period == "last_month":
        # last month + current month combined
        first_of_this_month = datetime(now.year, now.month, 1)
        last_month_end = first_of_this_month - timedelta(days=1)
        start = datetime(last_month_end.year, last_month_end.month, 1)
        return start, now

    if period == "last_3_months":
        # current month + previous 2 months (3 months total, inclusive)
        month = now.month - 2
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        start = datetime(year, month, 1)
        return start, now

    return None, None  # "all"


# =============================
# DASHBOARD PERIOD FILTER (Today / This Month / Last Month / Last 3 Months / Lifetime)
# Uses the "Created At" field written by /add-lead (format "%Y-%m-%d %H:%M:%S").
# =============================
def get_dashboard_period_range(period):
    """Returns (start_datetime, end_datetime) for the dashboard period filter,
    or (None, None) for 'lifetime' (meaning: no filtering, include everything —
    even leads that have no 'Created At' field at all)."""
    now = datetime.utcnow()

    if period == "today":
        start = datetime(now.year, now.month, now.day)
        return start, now

    if period == "this_month":
        start = datetime(now.year, now.month, 1)
        return start, now

    if period == "last_month":
        first_of_this_month = datetime(now.year, now.month, 1)
        last_month_end = first_of_this_month - timedelta(seconds=1)
        start = datetime(last_month_end.year, last_month_end.month, 1)
        return start, last_month_end

    if period == "last_3_months":
        month = now.month - 2
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        start = datetime(year, month, 1)
        return start, now

    return None, None  # "lifetime"


def parse_created_at_str(s):
    """Parses the 'Created At' field written by /add-lead: 'YYYY-MM-DD HH:MM:SS'."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

# =============================
# AI PROPERTY AUTOFILL (Mistral)
# =============================
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# Schema for the PARTNER "Add Inventory" form (Image 1)
INVENTORY_FIELDS_SCHEMA = {
    "listingBasis": "Individual property | Project",
    "dealType": "For Sale | For Rent",
    "propertyType": "Apartment | Villa | Plot | Independent House | Commercial | Farmhouse",
    "propertyTitle": "short catchy listing title",
    "locality": "locality / landmark",
    "configuration": "e.g. 2 BHK",
    "furnishing": "Unfurnished | Semi-furnished | Fully furnished",
    "areaUnit": "sqft",
    "carpetArea": "number only, as string",
    "superArea": "number only, as string",
    "floor": "e.g. 3 of 5",
    "bathrooms": "number only, as string",
    "facing": "East | West | North | South | North-East | North-West | South-East | South-West",
    "parking": "number of parking spots, as string",
    "possession": "Ready to move | Under construction",
    "price": "number only, no currency symbol, as string",
    "quickNotes": "short internal note, 1 sentence",
    "description": "polished 3-5 sentence marketing description"
}

# Schema for the ADMIN "Add Project" form (Image 2)
PROJECT_FIELDS_SCHEMA = {
    "name": "project name",
    "location": "location, e.g. Sidon, Himachal Pradesh",
    "propertyType": "Apartment | Villa | Plot | Independent House | Commercial",
    "possession": "New launch | Ready to move | Under construction",
    "configuration": "e.g. Studio / 1 BHK / 2 BHK",
    "startingPrice": "e.g. 70 Lakhs onwards",
    "description": "3-5 sentence project note / marketing description"
}


def extract_text_from_pdf(pdf_path):
    """
    Text-extracts a PDF. Tries the fast direct-text path first (works for
    brochures/typed PDFs). Any page with no extractable text is assumed
    scanned/image-only, so it's rasterized and sent through the existing
    extract_text_from_image() OCR helper instead.
    """
    text_chunks = []
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            page_text = page.get_text().strip()
            if page_text:
                text_chunks.append(page_text)
                continue

            # Scanned page — rasterize and OCR it
            pix = page.get_pixmap(dpi=200)
            tmp_img = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            tmp_img.close()
            pix.save(tmp_img.name)
            try:
                ocr_text = extract_text_from_image(tmp_img.name)
                if ocr_text:
                    text_chunks.append(ocr_text)
            except Exception as ocr_err:
                print(f"[pdf-extract] OCR fallback failed: {ocr_err}")
            finally:
                os.remove(tmp_img.name)
        doc.close()
    except Exception as e:
        print(f"[pdf-extract] failed: {e}")
    return "\n".join(text_chunks)


def call_mistral_generate(extracted_text, schema):
    """Sends extracted OCR/PDF text to Mistral and asks it to fill the given field schema, returning parsed JSON."""
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY not configured in .env")

    system_prompt = (
        "You are a real-estate data-entry assistant. Given raw text extracted "
        "from property photos and/or a brochure PDF (may be messy OCR output), "
        "fill in the JSON fields described below as best you can infer. "
        "Return ONLY a valid JSON object — no markdown fences, no commentary. "
        "If a field cannot be determined from the text, return an empty string for it. "
        f"Fields and guidance: {json.dumps(schema)}"
    )

    payload = {
        "model": "mistral-large-latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": extracted_text[:12000] or "No text could be extracted."}
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"}
    }

    resp = requests.post(
        MISTRAL_API_URL,
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)

# NEW: separate Mistral key used ONLY for lead-intent classification (Hot/Warm/Cold),
# kept apart from MISTRAL_API_KEY (used for AI autofill) so usage/quota don't mix.
MISTRAL_API_KEY2 = os.getenv("MISTRAL_API_KEY2")


def classify_lead_intent(lead_snapshot: dict, call_log: dict):
    """
    Uses MISTRAL_API_KEY2 to classify a lead's buying intent as Hot / Warm / Cold,
    based on the lead's stored details plus the call log just submitted.
    Returns {"intent": "Hot"|"Warm"|"Cold", "reason": "..."} or None on failure.
    """
    if not MISTRAL_API_KEY2:
        print("[intent] MISTRAL_API_KEY2 not configured — skipping intent classification")
        return None

    context = {
        "lead": {
            "name": lead_snapshot.get("name"),
            "location": lead_snapshot.get("location"),
            "property": lead_snapshot.get("property"),
            "budget": lead_snapshot.get("budget"),
            "timeline": lead_snapshot.get("timeline"),
            "note": lead_snapshot.get("note"),
        },
        "latest_call_log": {
            "callStatus": call_log.get("callStatus"),
            "customerResponse": call_log.get("customerResponse"),
            "interestLevel": call_log.get("interestLevel"),
            "objection": call_log.get("objection"),
            "followupTimeline": call_log.get("followupTimeline"),
            "callPriority": call_log.get("callPriority"),
            "callerRemarks": call_log.get("callerRemarks"),
        }
    }

    system_prompt = (
        "You are a real-estate CRM assistant. Given a lead's profile and the "
        "latest call log recorded by a sales agent, classify the lead's buying "
        "intent as exactly one of: Hot, Warm, Cold. "
        "Hot = ready to move soon, strong interest, budget matches, no major objection. "
        "Warm = interested but hesitant, budget mismatch, or comparing options. "
        "Cold = not interested, wrong number, unreachable repeatedly, or explicitly declined. "
        "Return ONLY a valid JSON object with keys 'intent' and 'reason' "
        "(reason: max one short sentence). No markdown, no commentary."
    )

    payload = {
        "model": "mistral-large-latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context)}
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }

    try:
        resp = requests.post(
            MISTRAL_API_URL,
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY2}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        intent = str(result.get("intent", "")).strip().capitalize()
        if intent not in ("Hot", "Warm", "Cold"):
            intent = "Warm"
        return {"intent": intent, "reason": result.get("reason", "")}
    except Exception as e:
        print(f"[intent] classification failed: {e}")
        return None

# =============================
# NEW: BRANDING OVERLAY (images only)
# =============================
# Replace these near the top of app.py
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_REG  = os.path.join(FONT_DIR, "DejaVuSans.ttf")
BRAND_ORANGE = (237, 128, 73)
BRAND_NAVY_SOLID = (16, 19, 28)   # solid, not the old semi-transparent tuple

_SYSTEM_FONT_FALLBACKS = {
    "bold": [
        FONT_BOLD,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "regular": [
        FONT_REG,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}

def _font(weight, size):
    """Robust font loader. Tries your bundled fonts, then common Linux
    system fonts, and only as an absolute last resort falls back to PIL's
    built-in font — kept at a REQUESTED size instead of the old tiny
    default, which is what made your text invisible."""
    for path in _SYSTEM_FONT_FALLBACKS.get(weight, []):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _format_price_display(price_str):
    """Formats price for the big headline. Pure numbers get comma
    grouping + ₹; free-text prices (e.g. '85 Lakhs') are shown as-is
    with a ₹ prefix if missing."""
    if not price_str:
        return ""
    s = str(price_str).strip()
    cleaned = re.sub(r"[^\d]", "", s)
    if cleaned and cleaned == s.replace(",", ""):
        return f"₹{int(cleaned):,}"
    return s if s.startswith("₹") else f"₹{s}"


def _indian_price_words(price_str):
    """Pure numeric prices get a '2 Crore 12 Lakh' style breakdown line,
    like your reference image. Free-text prices return None (line skipped)."""
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d]", "", str(price_str))
    if not cleaned or not cleaned.isdigit() or int(cleaned) < 100000:
        return None
    n = int(cleaned)
    crore, rem = divmod(n, 10000000)
    lakh, _ = divmod(rem, 100000)
    parts = []
    if crore: parts.append(f"{crore} Crore")
    if lakh: parts.append(f"{lakh} Lakh")
    return " ".join(parts) or None


def build_branded_image(image_bytes, fields):
    """
    Returns branded JPEG bytes styled after the reference design:
    orange top bar -> the ORIGINAL, UNCROPPED photo -> a solid navy info
    panel appended below the photo (title / location / price / config) ->
    divider -> contact bar. Nothing is ever drawn on top of the photo
    itself except the small "For Sale" pill, so the property photo is
    never obscured.
    """
    import textwrap

    CONTACT_NUMBER = "+91 73035 15710"   # fixed branding contact number

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    # Normalize width so font sizing is consistent regardless of source resolution
    TARGET_W = 1080
    if img.width != TARGET_W:
        new_h = int(img.height * (TARGET_W / img.width))
        img = img.resize((TARGET_W, new_h), Image.LANCZOS)
    W, H = img.size

    top_bar_h = int(W * 0.095)
    panel_h   = int(W * 0.40)
    contact_h = int(W * 0.11)
    total_H = top_bar_h + H + panel_h + contact_h

    canvas = Image.new("RGB", (W, total_H), BRAND_NAVY_SOLID)
    canvas.paste(img, (0, top_bar_h))
    draw = ImageDraw.Draw(canvas, "RGBA")

    # ---------- TOP BAR ----------
    draw.rectangle([0, 0, W, top_bar_h], fill=BRAND_ORANGE)
    f_brand = _font("bold", int(top_bar_h * 0.42))
    f_tag   = _font("regular", int(top_bar_h * 0.24))
    draw.text((int(W * 0.035), top_bar_h * 0.28), "NISHA HOMES", font=f_brand, fill="white")
    tag = "Trusted Real Estate Advisor"
    tag_w = draw.textlength(tag, font=f_tag)
    draw.text((W - tag_w - int(W * 0.035), top_bar_h * 0.40), tag, font=f_tag, fill="white")

    # ---------- DEAL-TYPE PILL ----------
    deal = (fields.get("dealType") or "For Sale").upper()
    f_pill = _font("bold", int(W * 0.032))
    pill_pad_x = int(W * 0.03)
    pill_h = int(W * 0.06)
    pill_w = draw.textlength(deal, font=f_pill) + pill_pad_x * 2
    px, py = int(W * 0.035), top_bar_h + int(W * 0.03)
    draw.rounded_rectangle([px, py, px + pill_w, py + pill_h], radius=pill_h // 2, fill=(255, 255, 255, 245))
    draw.text((px + pill_pad_x, py + pill_h * 0.20), deal, font=f_pill, fill=BRAND_ORANGE)

    # ---------- INFO PANEL (solid navy, BELOW the photo) ----------
    panel_top = top_bar_h + H
    draw.rectangle([0, panel_top, W, panel_top + panel_h], fill=BRAND_NAVY_SOLID)

    pad_x = int(W * 0.045)
    y = panel_top + int(panel_h * 0.10)

    f_title = _font("bold", int(W * 0.052))
    title = fields.get("propertyTitle") or ""
    max_chars = max(10, int(W / (f_title.size * 0.52)))
    for line in textwrap.wrap(title, width=max_chars)[:2]:
        draw.text((pad_x, y), line, font=f_title, fill="white")
        y += int(f_title.size * 1.22)

    y += int(panel_h * 0.025)
    f_meta = _font("regular", int(W * 0.030))
    draw.text((pad_x, y), "📍 " + (fields.get("locality") or ""), font=f_meta, fill=(210, 214, 224))
    y += int(f_meta.size * 1.6)

    f_price = _font("bold", int(W * 0.062))
    draw.text((pad_x, y), _format_price_display(fields.get("price")), font=f_price, fill=BRAND_ORANGE)
    y += int(f_price.size * 1.05)

    words = _indian_price_words(fields.get("price"))
    if words:
        f_words = _font("regular", int(W * 0.026))
        draw.text((pad_x, y), f"({words})", font=f_words, fill=(180, 186, 198))
        y += int(f_words.size * 1.5)
    else:
        y += int(W * 0.01)

    sub = " · ".join(filter(None, [
        fields.get("configuration"),
        fields.get("superArea") and f'{fields["superArea"]} sq.ft'
    ]))
    if sub:
        draw.text((pad_x, y), sub, font=f_meta, fill=(210, 214, 224))

    # ---------- DIVIDER + CONTACT BAR ----------
    divider_y = panel_top + panel_h
    draw.line([(pad_x, divider_y - 1), (W - pad_x, divider_y - 1)], fill=(255, 255, 255, 40), width=2)
    draw.rectangle([0, divider_y, W, divider_y + contact_h], fill=BRAND_NAVY_SOLID)

    icon_r = int(contact_h * 0.30)
    icon_cx, icon_cy = pad_x + icon_r, divider_y + contact_h // 2
    draw.ellipse([icon_cx - icon_r, icon_cy - icon_r, icon_cx + icon_r, icon_cy + icon_r], fill=BRAND_ORANGE)
    f_icon = _font("bold", int(icon_r * 1.1))
    icon_glyph = "\u260E"  # ☎ — if this doesn't render on your server's fonts, swap for "Call:"
    iw = draw.textlength(icon_glyph, font=f_icon)
    draw.text((icon_cx - iw / 2, icon_cy - f_icon.size * 0.62), icon_glyph, font=f_icon, fill="white")

    f_contact = _font("bold", int(W * 0.042))
    draw.text((icon_cx + icon_r * 1.7, icon_cy - f_contact.size * 0.55), CONTACT_NUMBER, font=f_contact, fill="white")

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=92)
    out.seek(0)
    return out

def _get_video_dimensions(path):
    """Uses ffprobe (bundled with ffmpeg) to get (width, height) of a video."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", path
    ]
    out = subprocess.check_output(cmd).decode().strip()
    w, h = out.split("x")
    return int(w), int(h)


def _build_brand_bars(fields, video_w):
    """
    Builds the same top-bar / info-panel / contact-bar graphics used for
    branded images, but as two standalone PNGs sized to `video_w` — these
    get composited onto video frames with ffmpeg instead of pasted with PIL.
    Returns: (top_bar_png_bytes, top_bar_h, bottom_bar_png_bytes, bottom_bar_h)
    """
    import textwrap
    CONTACT_NUMBER = "+91 73035 15710"
    W = video_w

    top_bar_h = int(W * 0.095)
    panel_h   = int(W * 0.40)
    contact_h = int(W * 0.11)
    bottom_h  = panel_h + contact_h

    # ---- TOP BAR ----
    top_img = Image.new("RGB", (W, top_bar_h), BRAND_ORANGE)
    draw = ImageDraw.Draw(top_img, "RGBA")
    f_brand = _font("bold", int(top_bar_h * 0.42))
    f_tag   = _font("regular", int(top_bar_h * 0.24))
    draw.text((int(W * 0.035), top_bar_h * 0.28), "NISHA HOMES", font=f_brand, fill="white")
    tag = "Trusted Real Estate Advisor"
    tag_w = draw.textlength(tag, font=f_tag)
    draw.text((W - tag_w - int(W * 0.035), top_bar_h * 0.40), tag, font=f_tag, fill="white")

    deal = (fields.get("dealType") or "For Sale").upper()
    f_pill = _font("bold", int(W * 0.032))
    pill_pad_x = int(W * 0.03)
    pill_h = int(W * 0.06)
    pill_w = draw.textlength(deal, font=f_pill) + pill_pad_x * 2
    px = int(W * 0.035)
    py = max(0, top_bar_h - pill_h - int(W * 0.015))
    draw.rounded_rectangle([px, py, px + pill_w, py + pill_h], radius=pill_h // 2, fill=(255, 255, 255, 245))
    draw.text((px + pill_pad_x, py + pill_h * 0.20), deal, font=f_pill, fill=BRAND_ORANGE)

    # ---- BOTTOM: INFO PANEL + CONTACT BAR ----
    bottom_img = Image.new("RGB", (W, bottom_h), BRAND_NAVY_SOLID)
    draw2 = ImageDraw.Draw(bottom_img, "RGBA")

    pad_x = int(W * 0.045)
    y = int(panel_h * 0.10)

    f_title = _font("bold", int(W * 0.052))
    title = fields.get("propertyTitle") or ""
    max_chars = max(10, int(W / (f_title.size * 0.52)))
    for line in textwrap.wrap(title, width=max_chars)[:2]:
        draw2.text((pad_x, y), line, font=f_title, fill="white")
        y += int(f_title.size * 1.22)

    y += int(panel_h * 0.025)
    f_meta = _font("regular", int(W * 0.030))
    draw2.text((pad_x, y), "📍 " + (fields.get("locality") or ""), font=f_meta, fill=(210, 214, 224))
    y += int(f_meta.size * 1.6)

    f_price = _font("bold", int(W * 0.062))
    draw2.text((pad_x, y), _format_price_display(fields.get("price")), font=f_price, fill=BRAND_ORANGE)
    y += int(f_price.size * 1.05)

    words = _indian_price_words(fields.get("price"))
    if words:
        f_words = _font("regular", int(W * 0.026))
        draw2.text((pad_x, y), f"({words})", font=f_words, fill=(180, 186, 198))
        y += int(f_words.size * 1.5)
    else:
        y += int(W * 0.01)

    sub = " · ".join(filter(None, [
        fields.get("configuration"),
        fields.get("superArea") and f'{fields["superArea"]} sq.ft'
    ]))
    if sub:
        draw2.text((pad_x, y), sub, font=f_meta, fill=(210, 214, 224))

    divider_y = panel_h
    draw2.line([(pad_x, divider_y - 1), (W - pad_x, divider_y - 1)], fill=(255, 255, 255, 40), width=2)

    icon_r = int(contact_h * 0.30)
    icon_cx, icon_cy = pad_x + icon_r, divider_y + contact_h // 2
    draw2.ellipse([icon_cx - icon_r, icon_cy - icon_r, icon_cx + icon_r, icon_cy + icon_r], fill=BRAND_ORANGE)
    f_icon = _font("bold", int(icon_r * 1.1))
    icon_glyph = "\u260E"
    iw = draw2.textlength(icon_glyph, font=f_icon)
    draw2.text((icon_cx - iw / 2, icon_cy - f_icon.size * 0.62), icon_glyph, font=f_icon, fill="white")

    f_contact = _font("bold", int(W * 0.042))
    draw2.text((icon_cx + icon_r * 1.7, icon_cy - f_contact.size * 0.55), CONTACT_NUMBER, font=f_contact, fill="white")

    top_buf = io.BytesIO(); top_img.save(top_buf, format="PNG"); top_buf.seek(0)
    bottom_buf = io.BytesIO(); bottom_img.save(bottom_buf, format="PNG"); bottom_buf.seek(0)

    return top_buf.read(), top_bar_h, bottom_buf.read(), bottom_h


def build_branded_video(video_bytes, fields):
    """
    Brands a video the same way build_branded_image brands photos: an
    orange top bar above the clip, a navy info+contact panel below it.
    Requires the `ffmpeg`/`ffprobe` binaries to be installed and on PATH
    on the server (e.g. `apt-get install ffmpeg` in your Dockerfile).
    Returns branded MP4 bytes. Raises RuntimeError if ffmpeg is missing,
    or subprocess.CalledProcessError if the encode fails.
    """
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg/ffprobe not installed on this server — cannot brand video")

    tmp_in = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    tmp_in.write(video_bytes); tmp_in.close()
    tmp_top = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp_bottom = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    tmp_out.close()

    try:
        TARGET_W = 1080
        src_w, src_h = _get_video_dimensions(tmp_in.name)
        scaled_h = int(src_h * (TARGET_W / src_w))
        if scaled_h % 2:
            scaled_h += 1  # ffmpeg needs even dimensions

        top_bytes, top_h, bottom_bytes, bottom_h = _build_brand_bars(fields, TARGET_W)
        tmp_top.write(top_bytes); tmp_top.close()
        tmp_bottom.write(bottom_bytes); tmp_bottom.close()

        total_h = top_h + scaled_h + bottom_h
        if total_h % 2:
            total_h += 1

        filter_complex = (
            f"[0:v]scale={TARGET_W}:{scaled_h},setsar=1[v0];"
            f"[v0]pad={TARGET_W}:{total_h}:0:{top_h}:color=0x10131c[padded];"
            f"[padded][1:v]overlay=0:0[tmp1];"
            f"[tmp1][2:v]overlay=0:{top_h + scaled_h}[vout]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", tmp_in.name,
            "-i", tmp_top.name,
            "-i", tmp_bottom.name,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            tmp_out.name
        ]
        subprocess.run(cmd, check=True, capture_output=True)

        with open(tmp_out.name, "rb") as f:
            return f.read()
    finally:
        for p in (tmp_in.name, tmp_top.name, tmp_bottom.name, tmp_out.name):
            try:
                os.remove(p)
            except Exception:
                pass

@app.route("/")
def loginpage():
    # If already logged in -> redirect based on role
    if "user_id" in session:
        role = session.get("role")

        if role == "admin":
            return redirect("/admin")
        elif role == "emp":
            return redirect("/admin")
        elif role == "partner":
            return redirect("/inventory")
        else:
            # fallback (invalid role)
            session.clear()
            return redirect("/")

    # Not logged in -> show login page
    return render_template("index.html")


#login system
@app.route("/login", methods=["POST"])
def login():
    raw_number = request.form.get("number", "")
    raw_password = request.form.get("password", "")

    number = normalize_number(raw_number)

    if not number:
        flash("Invalid phone number format")
        return redirect("/")

    try:
        number = int(number)
    except ValueError:
        flash("Invalid phone number format")
        return redirect("/")

    password = raw_password.strip()

    remember = request.form.get("remember")

    collection = db["teamAssign"]

    user = collection.find_one({
        "Employee number": number,
        "password": password
    })

    print(f"[login] number={number} found_user={bool(user)}"
          + (f" roll={user.get('roll')!r}" if user else ""))

    if not user:
        flash("Invalid number or password")
        return redirect("/")

    role = (user.get("roll") or "").strip().lower()

    # Store session
    session["user_id"] = str(user["_id"])
    session["role"] = role
    session["employee_name"] = user.get("Employee name")
    session["employee_number"] = user.get("Employee number")
    session.permanent = bool(remember)

    # Redirect based on role
    # Redirect based on role
    if role == "admin":
        return redirect("/admin")
    elif role == "emp":
        return redirect("/admin")
    elif role == "partner":
        return redirect("/inventory")
    else:
        flash("Invalid role")
        return redirect("/")


@app.route("/upload_page")
def upload_page():
    return render_template("upload.html")


@app.route("/admin")
def admin():
    if not session.get("user_id") or session.get("role") not in ("admin", "emp"):
        return redirect("/")

    return render_template(
        "admin.html",
        employee_name=session.get("employee_name"),
        employee_number=session.get("employee_number"),
        role=session.get("role")
    )

@app.route("/emp")
def emp():
    # Only check if logged in
    if not session.get("user_id"):
        return redirect("/")

    return render_template(
        "emp.html",
        employee_name=session.get("employee_name"),
        employee_number=session.get("employee_number")
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/assignehd")
def assign():
    return render_template("assignehd.html")



@app.route("/leadjourney")
def leadjourney():
    return render_template("leadjourney.html")


@app.route("/manageteam")
def manageteam():
    return render_template("manageteam.html")

@app.route("/status")
def status():
    return render_template("status.html")

@app.route("/addtemplete")
def addtemplete():
    return render_template("addtemplete.html")

@app.route("/addlead")
def addlead():
    return render_template("addlead.html")


@app.route("/leadjourney")
def lead_journey():
    return render_template("leadjourney.html")


@app.route("/inventory")
def inventory():
    if not session.get("user_id"):
        return redirect("/")

    role = session.get("role")
    if role not in ("admin", "emp", "partner"):
        session.clear()
        return redirect("/")

    return render_template(
        "inventory_dash.html",
        employee_name=session.get("employee_name"),
        employee_number=session.get("employee_number"),
        role=role
    )


@app.route("/upload", methods=["POST"])
def upload():
    try:
        sheet_name = request.form.get("sheet_name")

        if not sheet_name:
            flash("Collection name is required")
            return redirect("/")

        if "file" not in request.files:
            flash("No file selected")
            return redirect("/")

        file = request.files["file"]

        if file.filename == "":
            flash("Please select a CSV file")
            return redirect("/")

        filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
        file.save(filepath)

        df = pd.read_csv(filepath)
        df = df.where(pd.notnull(df), None)

        data = df.to_dict(orient="records")

        if not data:
            flash("CSV is empty")
            return redirect("/")

        collection = db[sheet_name]
        collection.insert_many(data)

        flash(f"Successfully inserted {len(data)} records into '{sheet_name}' collection!")
        return redirect("/")

    except Exception as e:
        flash(f"Error: {str(e)}")
        return redirect("/")


# =============================
# APIs
# =============================

# CHANGED: now filtered to July 2026 -> today only (see filter_by_july_range)
@app.route("/api/leads")
def leads():
    return jsonify(filter_by_july_range(get_collection_data("Leads")))

#single lead
def clean_nan(data):
    for key, value in data.items():
        if isinstance(value, float) and math.isnan(value):
            data[key] = None
    return data


@app.route("/api/get-lead-single", methods=["POST"])
def get_lead():
    try:
        data = request.get_json()

        collection_name = data.get("collection")
        phone_number = data.get("phone")

        if not collection_name or not phone_number:
            return jsonify({"error": "collection and phone are required"}), 400

        collection = db[collection_name]

        lead = collection.find_one({"Phone Number": str(phone_number)})

        if not lead:
            return jsonify({"message": "No Lead"}), 404

        # Remove unwanted fields
        lead.pop("_id", None)
        lead.pop("Phone Number", None)

        lead = clean_nan(lead)

        return jsonify(lead), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


#realtorsdata
@app.route("/api/realtors")
def realtors():
    return jsonify(get_collection_data("Realtors"))


@app.route("/api/update-realtor", methods=["POST"])
def update_realtor():
    try:
        data = request.get_json()

        phone = data.get("phone")
        updates = data.get("updates")  # dict of fields to update

        if not phone or not updates:
            return jsonify({"error": "phone and updates are required"}), 400

        # Clean phone (remove +, spaces, etc.)
        phone = str(phone)
        phone = re.sub(r"\D", "", phone)

        try:
            phone = int(phone)
        except:
            return jsonify({"error": "Invalid phone format"}), 400

        collection = db["Realtors"]

        # Remove invalid fields
        if "_id" in updates:
            del updates["_id"]

        # Clean NaN if coming from frontend
        updates = clean_nan(updates)

        result = collection.update_one(
            {"Phone Number": phone},
            {"$set": updates}
        )

        if result.matched_count == 0:
            return jsonify({"message": "No realtor found"}), 404

        return jsonify({
            "message": "Realtor updated successfully",
            "modified_count": result.modified_count
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/hofcorders")
def hofcorders():
    return jsonify(get_collection_data("orderhouseofcakes"))

# CHANGED: now filtered to July 2026 -> today only
@app.route("/api/rental-leads")
def rental_leads():
    return jsonify(filter_by_july_range(get_collection_data("RentalLeads")))

# CHANGED: now filtered to July 2026 -> today only
@app.route("/api/agent-leads")
def agent_leads():
    return jsonify(filter_by_july_range(get_collection_data("agentLeads")))

# CHANGED: now filtered to July 2026 -> today only
@app.route("/api/selling-leads")
def selling_leads():
    return jsonify(filter_by_july_range(get_collection_data("sellingLeads")))

@app.route("/api/end-data")
def get_end_data():

    collection = db["endData"]   # define collection

    number = request.args.get("number")

    # If number provided -> return single lead
    if number:
        try:
            lead = collection.find_one({"Number": int(number)})
        except:
            lead = collection.find_one({"Number": number})

        if lead:
            return jsonify(serialize_doc(lead))
        return jsonify({"error": "Not found"}), 404

    # If no number -> return all leads
    leads = list(collection.find())
    return jsonify([serialize_doc(l) for l in leads])


@app.route("/api/get-team-assign", methods=["GET"])
def get_team_assign():
    collection = db["teamAssign"]
    data = list(collection.find())

    return jsonify([serialize_doc(doc) for doc in data])


@app.route("/api/add-team-assign", methods=["POST"])
def add_team_member():
    try:
        data = request.json

        name = data.get("name")
        number = data.get("number")
        role = data.get("role", "emp")  # default emp

        if role not in ("admin", "emp", "partner"):
            return jsonify({"success": False, "message": "Invalid role"}), 400

        if not name or not number:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        # CLEAN NUMBER
        number = str(number).strip()
        number = number.replace("+", "")
        number = "".join(filter(str.isdigit, number))

        if not number:
            return jsonify({"success": False, "message": "Invalid phone number"}), 400

        # CONVERT TO INT64
        number = int(number)

        collection = db["teamAssign"]

        # prevent duplicate
        existing = collection.find_one({"Employee number": number})
        if existing:
            return jsonify({"success": False, "message": "Employee already exists"}), 400

        # GENERATE PASSWORD
        clean_name = name.lower().replace(" ", "")
        rand_digits = random.randint(100, 9999)  # 3-4 digits
        password = f"{clean_name}@{rand_digits}"

        new_member = {
            "Employee name": name,
            "Employee number": number,
            "password": password,
            "roll": role,  # as requested (roll, not role)
            "Leads": [],
            "Active": True
        }

        collection.insert_one(new_member)

        # SEND TO N8N WEBHOOK
        try:
            requests.post(
                "https://n8n.phishnix.site/webhook/recevingdataofteammember",
                json={
                    "name": name,
                    "number": number,
                    "password": password,
                    "login_url": "https://api.phishnix.site",
                    "message": f"Welcome {name}, your account has been created"
                },
                timeout=5
            )
        except Exception as webhook_error:
            print("Webhook failed:", webhook_error)  # don't break main flow

        return jsonify({
            "success": True,
            "message": "Team member added successfully"
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/remove-team-assign/<number>", methods=["DELETE"])
def remove_team_member(number):
    try:
        collection = db["teamAssign"]

        number = str(number).strip()
        number = number.replace("+", "")
        number = "".join(filter(str.isdigit, number))

        if not number:
            return jsonify({"success": False, "message": "Invalid number"}), 400

        number = int(number)

        result = collection.delete_one({"Employee number": number})

        if result.deleted_count == 0:
            return jsonify({"success": False, "message": "Member not found"}), 404

        return jsonify({"success": True, "message": "Team member removed"})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500



@app.route("/api/reset-team-password/<number>", methods=["POST"])
def reset_team_password(number):
    """
    Regenerates a plaintext password for an existing team member
    (covers older records created before/without a password field,
    or whenever an admin wants to issue a fresh one).
    """
    try:
        if session.get("role") != "admin":
            return jsonify({"success": False, "message": "Admin only"}), 403

        number = str(number).strip()
        number = number.replace("+", "")
        number = "".join(filter(str.isdigit, number))
        if not number:
            return jsonify({"success": False, "message": "Invalid number"}), 400
        number = int(number)

        collection = db["teamAssign"]
        member = collection.find_one({"Employee number": number})
        if not member:
            return jsonify({"success": False, "message": "Team member not found"}), 404

        name = member.get("Employee name", "user")
        clean_name = name.lower().replace(" ", "")
        rand_digits = random.randint(100, 9999)
        new_password = f"{clean_name}@{rand_digits}"

        collection.update_one(
            {"_id": member["_id"]},
            {"$set": {"password": new_password}}
        )

        # Notify via the same webhook used on creation (best-effort, won't break the response)
        try:
            requests.post(
                "https://n8n.phishnix.site/webhook/recevingdataofteammember",
                json={
                    "name": name,
                    "number": number,
                    "password": new_password,
                    "login_url": "https://api.phishnix.site",
                    "message": f"Hi {name}, your password has been reset"
                },
                timeout=5
            )
        except Exception as webhook_error:
            print("Webhook failed:", webhook_error)

        return jsonify({
            "success": True,
            "message": "Password reset successfully",
            "password": new_password
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

#post apis



def normalize_number(number):
    if not number:
        return ""

    number = str(number).strip()
    number = number.replace("+", "")
    number = number.replace("@s.whatsapp.net", "")
    number = number.replace("@c.us", "")
    number = "".join(filter(str.isdigit, number))

    return number


@app.route("/api/assign-lead", methods=["POST"])
def assign_lead():
    try:
        data = request.json or {}

        collection_name = data.get("collection")
        lead_id = data.get("leadId")            # Mongo _id of the lead doc
        assign_to_number = data.get("assignToNumber")  # employee number, NOT name

        if not collection_name or not lead_id or not assign_to_number:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        try:
            assign_to_number = int(str(assign_to_number).strip())
        except ValueError:
            return jsonify({"success": False, "message": "Invalid employee number"}), 400

        try:
            obj_id = ObjectId(lead_id)
        except Exception:
            return jsonify({"success": False, "message": "Invalid lead id"}), 400

        employee = db["teamAssign"].find_one({"Employee number": assign_to_number})
        if not employee:
            return jsonify({"success": False, "message": "Employee not found"}), 404

        assigner_name = session.get("employee_name") or "Unknown"
        assigner_number = session.get("employee_number")

        history_entry = {
            "by": assigner_name,
            "byNumber": assigner_number,
            "to": employee.get("Employee name"),
            "toNumber": assign_to_number,
            "at": datetime.utcnow()
        }

        result = db[collection_name].update_one(
            {"_id": obj_id},
            {
                "$set": {
                    "AssignTo": employee.get("Employee name"),
                    "AssignToNumber": assign_to_number,
                    "AssignedBy": assigner_name,
                    "AssignedByNumber": assigner_number,
                    "AssignedAt": datetime.utcnow()
                },
                "$push": {"AssignmentHistory": history_entry}
            }
        )

        if result.matched_count == 0:
            return jsonify({"success": False, "message": "Lead not found"}), 404

        return jsonify({"success": True})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

# CHANGED: every user (admin or emp) now only sees leads assigned to the
# employee number they are CURRENTLY logged in as — even admin-to-admin
# assignments stay private to the assignee. Previously any admin saw
# every admin's assigned leads.
@app.route('/api/assigned-leads')
def assigned_leads():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    employee_number = session.get("employee_number")  # stored as int at login time

    collection_type_map = {
        "Leads": "buying",
        "RentalLeads": "rental",
        "sellingLeads": "selling",
        "agentLeads": "agent",
    }

    if not employee_number:
        return jsonify({"success": False, "message": "No employee number in session"}), 400

    query = {
        "AssignTo": {"$exists": True, "$nin": [None, ""]},
        "AssignToNumber": employee_number
    }

    # Cap per-collection to keep this fast/light with ~8k+ assigned docs.
    PER_COLLECTION_LIMIT = 500

    all_docs = []
    try:
        for collection_name, lead_type in collection_type_map.items():
            docs = (
                db[collection_name]
                .find(query)
                .sort("AssignedAt", -1)
                .limit(PER_COLLECTION_LIMIT)
            )
            for d in docs:
                d = serialize_doc(d)
                d["_leadType"] = lead_type
                d["_collection"] = collection_name
                all_docs.append(d)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

    def sort_key(doc):
        return doc.get("AssignedAt") or ""

    all_docs.sort(key=sort_key, reverse=True)

    return jsonify({"success": True, "data": all_docs})



@app.route("/api/get-lead-by-id", methods=["POST"])
def get_lead_by_id():
    try:
        data = request.json or {}
        collection_name = data.get("collection")
        lead_id = data.get("id")

        if not collection_name or not lead_id:
            return jsonify({"error": "collection and id are required"}), 400

        lead = db[collection_name].find_one({"_id": ObjectId(lead_id)})
        if not lead:
            return jsonify({"error": "Lead not found"}), 404

        return jsonify({"success": True, "data": serialize_doc(lead)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update-lead-by-id", methods=["POST"])
def update_lead_by_id():
    try:
        data = request.json or {}
        collection_name = data.get("collection")
        lead_id = data.get("id")
        set_fields = data.get("set", {})

        if not collection_name or not lead_id:
            return jsonify({"error": "collection and id are required"}), 400
        if not set_fields:
            return jsonify({"error": "No fields to update"}), 400

        set_fields.pop("_id", None)

        result = db[collection_name].update_one(
            {"_id": ObjectId(lead_id)},
            {"$set": set_fields}
        )

        if result.matched_count == 0:
            return jsonify({"error": "Lead not found"}), 404

        updated = db[collection_name].find_one({"_id": ObjectId(lead_id)})
        return jsonify({"success": True, "data": serialize_doc(updated)})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/bulk-assign-leads", methods=["POST"])
def bulk_assign_leads():
    try:
        data = request.json or {}

        collection_name = data.get("collection")
        lead_ids = data.get("leadIds", [])
        assign_to_number = data.get("assignToNumber")

        if not collection_name or not lead_ids or not assign_to_number:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        try:
            assign_to_number = int(str(assign_to_number).strip())
        except ValueError:
            return jsonify({"success": False, "message": "Invalid employee number"}), 400

        employee = db["teamAssign"].find_one({"Employee number": assign_to_number})
        if not employee:
            return jsonify({"success": False, "message": "Employee not found"}), 404

        try:
            obj_ids = [ObjectId(i) for i in lead_ids]
        except Exception:
            return jsonify({"success": False, "message": "Invalid lead id in list"}), 400

        assigner_name = session.get("employee_name") or "Unknown"
        assigner_number = session.get("employee_number")

        history_entry = {
            "by": assigner_name,
            "byNumber": assigner_number,
            "to": employee.get("Employee name"),
            "toNumber": assign_to_number,
            "at": datetime.utcnow()
        }

        result = db[collection_name].update_many(
            {"_id": {"$in": obj_ids}},
            {
                "$set": {
                    "AssignTo": employee.get("Employee name"),
                    "AssignToNumber": assign_to_number,
                    "AssignedBy": assigner_name,
                    "AssignedByNumber": assigner_number,
                    "AssignedAt": datetime.utcnow()
                },
                "$push": {"AssignmentHistory": history_entry}
            }
        )

        return jsonify({"success": True, "assignedCount": result.modified_count})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


#reassign function


@app.route("/api/reassign-lead", methods=["POST"])
def reassign_lead():
    try:
        data = request.json

        phone = data.get("phone")
        new_employee_number = data.get("newEmployeeNumber")
        collection_name = data.get("collection")

        # Validate input
        if not phone or not new_employee_number or not collection_name:
            return jsonify({"error": "Missing required fields"}), 400

        phone = phone.replace("+", "").strip()
        formatted_phone = f"+{phone}"

        team_collection = db["teamAssign"]
        lead_collection = db[collection_name]

        # ESCAPE REGEX PROPERLY
        safe_phone_regex = re.escape(formatted_phone)

        # 1. Find current employee safely
        current_employee = team_collection.find_one({
            "Leads": {"$regex": safe_phone_regex}
        })

        if not current_employee:
            return jsonify({"error": "Lead not found in any employee"}), 404

        # 2. Remove lead from old employee
        old_leads_string = current_employee.get("Leads", "{}")

        old_list = old_leads_string.strip("{}").split(",")
        old_list = [l.strip() for l in old_list if l.strip()]

        updated_old_list = [l for l in old_list if l != formatted_phone]

        new_old_string = "{" + ", ".join(updated_old_list) + "}"

        team_collection.update_one(
            {"_id": current_employee["_id"]},
            {"$set": {"Leads": new_old_string}}
        )

        # 3. Add lead to new employee
        new_employee = team_collection.find_one({
            "Employee number": int(new_employee_number)
        })

        if not new_employee:
            return jsonify({"error": "New employee not found"}), 404

        new_leads_string = new_employee.get("Leads", "{}")

        new_list = new_leads_string.strip("{}").split(",")
        new_list = [l.strip() for l in new_list if l.strip()]

        if formatted_phone not in new_list:
            new_list.append(formatted_phone)

        updated_new_string = "{" + ", ".join(new_list) + "}"

        team_collection.update_one(
            {"_id": new_employee["_id"]},
            {"$set": {"Leads": updated_new_string}}
        )

        # 4. Update AssignTo in Lead document safely
        lead_doc = lead_collection.find_one({"Phone Number": phone})

        if not lead_doc:
            return jsonify({"error": "Lead not found in lead collection"}), 404

        current_assign = lead_doc.get("AssignTo")

        new_employee_name = new_employee["Employee name"]

        if not current_assign:
            updated_assign = new_employee_name
        else:
            current_assign = str(current_assign)
            names = [n.strip() for n in current_assign.split(",") if n.strip()]

            if new_employee_name not in names:
                names.append(new_employee_name)

            updated_assign = ", ".join(names)

        lead_collection.update_one(
            {"_id": lead_doc["_id"]},
            {"$set": {"AssignTo": updated_assign}}
        )

        return jsonify({"success": True})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/call-attempt", methods=["POST"])
def call_attempt():
    """
    Fires whenever ANY 'Call' button is clicked, from ANY page (All Leads,
    Assigned Leads, Followups, Hot/Warm/Cold, etc). Increments Total Calls,
    pushes a {At, By} entry into CallHistory (array, so every call attempt
    is kept, not just the latest), and also writes a lightweight row into
    callLogs so the dashboard's "Team Activity - Calls Today" picks it up
    automatically (that widget already aggregates callLogs by DateOnly).
    """
    try:
        data = request.json or {}
        number = data.get("number")

        if not number:
            return jsonify({"error": "Number is required"}), 400

        # Clean number
        number = str(number).replace("+", "").strip()

        employee_name = session.get("employee_name") or data.get("employee") or "Unknown"
        now = datetime.utcnow()
        formatted_dt = format_ist(now)
        today_str = now.strftime("%Y-%m-%d")

        end_collection = db["endData"]

        end_collection.update_one(
            {"Number": number},
            {
                "$inc": {"Call_attempt": 1},
                "$setOnInsert": {"Number": number},
                "$push": {
                    "CallHistory": {
                        "$each": [{
                            "At": now,
                            "AtFormatted": formatted_dt,
                            "By": employee_name
                        }],
                        "$slice": -100
                    }
                }
            },
            upsert=True
        )

        # Also record it in callLogs so Team Activity / Total Calls count it
        call_logs_collection.insert_one({
            "Number": number,
            "Name": data.get("name", ""),
            "LeadType": data.get("leadType", ""),
            "CallAttemptOnly": True,
            "CallDateTimeFormatted": formatted_dt,
            "CalledBy": employee_name,
            "CreatedAt": now,
            "DateOnly": today_str
        })

        updated_doc = end_collection.find_one({"Number": number})

        return jsonify({
            "success": True,
            "Number": number,
            "Call_attempt": updated_doc.get("Call_attempt", 1),
            "CalledBy": employee_name,
            "CallDateTime": formatted_dt
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


#adding call logs

call_logs_collection = db["callLogs"]

# ============================================================
# CALL LOG - replaces the n8n webhook, writes straight to Mongo
# ============================================================

COLLECTION_MAP = {
    "buying": "Leads",
    "rental": "RentalLeads",
    "selling": "sellingLeads",
    "agent": "agentLeads",
    "other": "Leads"
}


@app.route("/api/call-log", methods=["POST"])
def add_call_log():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON body provided"}), 400

        phone = data.get("phone")
        if not phone:
            return jsonify({"error": "Phone is required"}), 400

        number = normalize_number(phone)
        if not number:
            return jsonify({"error": "Invalid phone number"}), 400

        if not number.startswith("91"):
            number = "91" + number

        employee_name = session.get("employee_name") or data.get("employee") or "Unknown"

        now = datetime.utcnow()
        today_str = now.strftime("%Y-%m-%d")
        formatted_dt = format_ist(now)

        end_collection = db["endData"]

        # Figure out which call attempt number this is, BEFORE incrementing
        existing_end_doc = end_collection.find_one({"Number": number})
        current_attempt = (existing_end_doc or {}).get("Call_attempt", 0)
        attempt_number = current_attempt + 1

        log_entry = {
            "Number": number,
            "Name": data.get("name", ""),
            "LeadType": data.get("leadType", ""),
            "CallAttemptNumber": attempt_number,
            "CallDateTimeFormatted": formatted_dt,
            "CallStatus": data.get("callStatus", ""),
            "CustomerResponse": data.get("customerResponse", ""),
            "InterestLevel": data.get("interestLevel", ""),
            "Configuration": data.get("configuration", ""),
            "Objection": data.get("objection", ""),
            "FollowupTimeline": data.get("followupTimeline", ""),
            "NextCallDate": data.get("nextCallDate", ""),
            "CallPriority": data.get("callPriority", ""),
            "Status": data.get("status", "Pending"),
            "CallerRemarks": data.get("callerRemarks", ""),
            "LeadSnapshot": {
                "Location": data.get("location", ""),
                "Property": data.get("property", ""),
                "Budget": data.get("budget", ""),
                "Timeline": data.get("timeline", ""),
                "Note": data.get("note", "")
            },
            "CalledBy": employee_name,
            "CreatedAt": now,
            "DateOnly": today_str
        }

        call_logs_collection.insert_one(log_entry)

        lead_type = data.get("leadType")
        collection_name = COLLECTION_MAP.get(lead_type)
        if collection_name:
            lead_doc = db[collection_name].find_one({"Phone Number": {"$regex": number}})
            if lead_doc:
                db["endData"].update_one(
                    {"Number": number},
                    {"$set": {"LeadId": str(lead_doc["_id"])}},
                    upsert=True
                )
                db[collection_name].update_one(
                    {"_id": lead_doc["_id"]},
                    {"$set": {"callBy": session.get("user_id")}}
                )

        update_fields = {
            "Call Status": data.get("callStatus", ""),
            "Customer Response": data.get("customerResponse", ""),
            "Interest Level": data.get("interestLevel", ""),
            "Configuration": data.get("configuration", ""),
            "Objection / Reason": data.get("objection", ""),
            "Next Follow-up Timeline": data.get("followupTimeline", ""),
            "Next Call Date": data.get("nextCallDate", ""),
            "Call Priority": data.get("callPriority", ""),
            "Status": data.get("status", "Pending"),
            "Caller Remarks": data.get("callerRemarks", ""),
            "Location Interested In": data.get("location", ""),
            "Property Type": data.get("property", ""),
            "Budget Range": data.get("budget", ""),
            "Customer Name": data.get("name", ""),
            "lastCallBy": employee_name,
            "lastCallAt": now,
            "lastCallAtFormatted": formatted_dt,
            "lastUpdatedAt": now
        }
        update_fields = {k: v for k, v in update_fields.items() if v not in [None, ""]}

        update_fields["LastLeadType"] = data.get("leadType", "")

        end_collection.update_one(
            {"Number": number},
            {
                "$set": update_fields,
                "$inc": {"Call_attempt": 1},
                "$push": {
                    "RecentLogs": {
                        "$each": [{
                            "CallAttemptNumber": attempt_number,
                            "CallDateTimeFormatted": formatted_dt,
                            "CallStatus": data.get("callStatus", ""),
                            "CustomerResponse": data.get("customerResponse", ""),
                            "CalledBy": employee_name,
                            "At": now,
                            "Remarks": data.get("callerRemarks", "")
                        }],
                        "$slice": -10
                    }
                }
            },
            upsert=True
        )

        # NEW: AI intent classification (Hot / Warm / Cold) using MISTRAL_API_KEY2
        intent_result = classify_lead_intent(
            {
                "name": data.get("name", ""),
                "location": data.get("location", ""),
                "property": data.get("property", ""),
                "budget": data.get("budget", ""),
                "timeline": data.get("timeline", ""),
                "note": data.get("note", "")
            },
            data
        )
        if intent_result:
            end_collection.update_one(
                {"Number": number},
                {"$set": {
                    "AI Intent": intent_result["intent"],
                    "AI Intent Reason": intent_result.get("reason", ""),
                    "AI Intent At": now
                }}
            )

        updated_doc = end_collection.find_one({"Number": number})

        return jsonify({
            "success": True,
            "message": "Call log saved",
            "attempt_number": attempt_number,
            "call_datetime": formatted_dt,
            "data": serialize_doc(updated_doc) if updated_doc else None
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/call-logs/<phone>", methods=["GET"])
def get_call_logs(phone):
    try:
        number = normalize_number(phone)
        if not number.startswith("91"):
            number = "91" + number

        logs = list(call_logs_collection.find({"Number": number}).sort("CreatedAt", -1))
        for l in logs:
            l["_id"] = str(l["_id"])
            if isinstance(l.get("CreatedAt"), datetime):
                l["CreatedAt"] = l["CreatedAt"].isoformat()

        return jsonify({"success": True, "count": len(logs), "logs": logs}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dashboard-stats", methods=["GET"])
def dashboard_stats():
    try:
        end_collection = db["endData"]
        today_str = datetime.utcnow().strftime("%Y-%m-%d")

        all_docs = list(end_collection.find({}, {
            "Number": 1, "Status": 1, "Next Follow-up Timeline": 1
        }))

        followup_count = 0
        pending_count = 0
        done_count = 0

        for d in all_docs:
            status = (d.get("Status") or "").strip().lower()
            followup = (d.get("Next Follow-up Timeline") or "").strip()
            if followup:
                followup_count += 1
            if status == "done":
                done_count += 1
            else:
                pending_count += 1

        today_logs = list(call_logs_collection.find({"DateOnly": today_str}, {"CalledBy": 1}))
        calls_today_total = len(today_logs)

        by_employee = {}
        for l in today_logs:
            name = l.get("CalledBy", "Unknown")
            by_employee[name] = by_employee.get(name, 0) + 1

        return jsonify({
            "success": True,
            "followup_count": followup_count,
            "pending_count": pending_count,
            "done_count": done_count,
            "calls_today_total": calls_today_total,
            "calls_today_by_employee": by_employee
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


# =============================
# NEW: FOLLOW-UPS DUE TODAY / THIS WEEK (for admin dashboard widget)
# Scans every lead across all 4 collections, looks up its endData
# doc for "Next Call Date", and buckets it into today / this week.
# Visible to admin & emp; shows follow-ups set by ANY employee/admin.
# =============================
@app.route("/api/dashboard-followups", methods=["GET"])
def dashboard_followups():
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"success": False, "message": "Staff only"}), 403

    today = datetime.utcnow().date()
    now = datetime.utcnow()
    week_start = today - timedelta(days=today.weekday())   # Monday
    week_end = week_start + timedelta(days=6)               # Sunday

    def parse_followup_date(s):
        if not s:
            return None
        s = str(s).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    end_collection = db["endData"]
    collections = {
        "buying": "Leads", "rental": "RentalLeads",
        "selling": "sellingLeads", "agent": "agentLeads"
    }

    today_list, week_list = [], []

    try:
        for lead_type, coll_name in collections.items():
            for lead in db[coll_name].find():
                # NEW: only consider leads dated July 2026 -> now (same rule as filter_by_july_range)
                lead_date = parse_lead_date(lead.get("Date"))
                if not lead_date or lead_date < JULY_2026_START or lead_date > now:
                    continue

                # Verify via phone number against the Leads-family collection (this loop already
                # scans the real lead docs, so a match here IS the phone-number verification)
                phone = normalize_number(lead.get("Phone Number", ""))
                if not phone:
                    continue
                if not phone.startswith("91"):
                    phone = "91" + phone

                ed = end_collection.find_one({"Number": phone})
                if not ed:
                    continue

                # NEW: endData record itself must also fall in the July -> now window
                ed_created = ed.get("lastUpdatedAt")
                if isinstance(ed_created, datetime):
                    if ed_created < JULY_2026_START or ed_created > now:
                        continue

                fdate = parse_followup_date(ed.get("Next Call Date"))
                if not fdate or fdate < today:
                    continue   # only today-forward, not overdue

                def _clean(v, default="-"):
                    # NEW: guards against pandas/CSV-imported NaN floats, which Python's
                    # jsonify serializes as the literal token `NaN` — invalid JSON that
                    # breaks JSON.parse() in the browser. None/empty also fall back safely.
                    if v is None or v == "":
                        return default
                    if isinstance(v, float) and math.isnan(v):
                        return default
                    return v

                entry = {
                    "id": str(lead["_id"]),
                    "collection": coll_name,
                    "leadType": lead_type,
                    "name": _clean(lead.get("Lead Name") or lead.get("Name"), "Unknown"),
                    "phone": phone,
                    "assignedTo": _clean(lead.get("AssignTo"), "Unassigned"),
                    "nextCallDate": _clean(ed.get("Next Call Date")),
                    "nextFollowupTimeline": _clean(ed.get("Next Follow-up Timeline")),
                    "callStatus": _clean(ed.get("Call Status")),
                    "location": _clean(lead.get("Location Interested In") or lead.get("Property Location")),
                    # NEW: extra context for the follow-up popup
                    "propertyType": _clean(lead.get("Property Type") or ed.get("Property Type")),
                    "budget": _clean(lead.get("Budget Range") or lead.get("Expected Price")),
                    "customerResponse": _clean(ed.get("Customer Response")),
                    "interestLevel": _clean(ed.get("Interest Level")),
                    "callerRemarks": _clean(ed.get("Caller Remarks")),
                    "callAttempts": ed.get("Call_attempt") if isinstance(ed.get("Call_attempt"), int) else 0,
                }

                # CHANGED: bucket exclusively — same-day goes to "today", everything else in-week to "week"
                if fdate == today:
                    today_list.append(entry)
                elif week_start <= fdate <= week_end:
                    week_list.append(entry)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

    return jsonify({
        "success": True,
        "todayCount": len(today_list),
        "weekCount": len(week_list),
        "todayLeads": today_list,
        "weekLeads": week_list
    }), 200


@app.route("/api/dashboard-intent-leads", methods=["GET"])
def dashboard_intent_leads():
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"success": False, "message": "Staff only"}), 403

    def map_interest_to_bucket(level):
        lvl = (level or "").strip().lower()
        if lvl in ("very high", "high"):
            return "Hot"
        if lvl == "medium":
            return "Warm"
        if lvl == "cold":
            return "Cold"
        return None

    end_collection = db["endData"]
    docs = list(end_collection.find({
        "Interest Level": {"$in": ["Very High", "High", "Medium", "Cold"]}
    }))

    def _clean(v, default="-"):
        if v is None or v == "":
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        return v

    buckets = {"Hot": [], "Warm": [], "Cold": []}
    for d in docs:
        bucket = map_interest_to_bucket(d.get("Interest Level"))
        if bucket not in buckets:
            continue
        buckets[bucket].append({
            "number": d.get("Number", ""),
            "name": _clean(d.get("Customer Name"), "Unknown"),
            "location": _clean(d.get("Location Interested In")),
            "propertyType": _clean(d.get("Property Type")),
            "budget": _clean(d.get("Budget Range")),
            "callStatus": _clean(d.get("Call Status")),
            "customerResponse": _clean(d.get("Customer Response")),
            "interestLevel": _clean(d.get("Interest Level")),
            "callerRemarks": _clean(d.get("Caller Remarks")),
            "nextFollowupTimeline": _clean(d.get("Next Follow-up Timeline")),
            "nextCallDate": _clean(d.get("Next Call Date")),
            "callAttempts": d.get("Call_attempt") if isinstance(d.get("Call_attempt"), int) else 0,
            "leadId": d.get("LeadId", ""),
            "type": d.get("LastLeadType", "buying") or "buying",
        })

    return jsonify({
        "success": True,
        "hotCount": len(buckets["Hot"]),
        "warmCount": len(buckets["Warm"]),
        "coldCount": len(buckets["Cold"]),
        "hotLeads": buckets["Hot"],
        "warmLeads": buckets["Warm"],
        "coldLeads": buckets["Cold"]
    }), 200


# =============================
# DASHBOARD OVERVIEW — single period-aware endpoint that powers every
# widget on the Dashboard page: KPI totals, Pipeline Health, Follow-ups,
# Team Activity, Lead Intent (AI), Trends and Breakdown.
#
# period = "today" | "this_month" | "last_month" | "last_3_months" | "lifetime"
#
# Filtering is based on each lead's "Created At" field. For "lifetime",
# every lead is included — even ones with no "Created At" at all (as
# requested). For any other period, leads without a parseable "Created At"
# are excluded (there's no reliable way to place them in a specific window).
# =============================
@app.route("/api/dashboard-overview", methods=["GET"])
def dashboard_overview():
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"success": False, "message": "Staff only"}), 403

    period = request.args.get("period", "lifetime")
    start, end = get_dashboard_period_range(period)
    now = datetime.utcnow()

    collections = {
        "buying": "Leads", "rental": "RentalLeads",
        "selling": "sellingLeads", "agent": "agentLeads"
    }

    totals = {"buying": 0, "rental": 0, "selling": 0, "agent": 0}
    trend_counts = {}       # "YYYY-MM-DD" -> count
    location_counts = {}    # location -> count
    qualifying_phones = {}  # normalized "91xxxxxxxxxx" -> leadType

    try:
        for lead_type, coll_name in collections.items():
            for lead in db[coll_name].find():
                created_dt = parse_created_at_str(lead.get("Created At"))

                if start is not None:
                    if not created_dt or created_dt < start or created_dt > end:
                        continue
                # lifetime (start is None): include regardless of Created At

                totals[lead_type] += 1

                if created_dt:
                    d_str = created_dt.strftime("%Y-%m-%d")
                    trend_counts[d_str] = trend_counts.get(d_str, 0) + 1

                loc = (lead.get("Location Interested In") or lead.get("Property Location")
                       or lead.get("Operating City") or "").strip()
                if loc:
                    location_counts[loc] = location_counts.get(loc, 0) + 1

                phone = normalize_number(lead.get("Phone Number", ""))
                if phone:
                    if not phone.startswith("91"):
                        phone = "91" + phone
                    qualifying_phones[phone] = lead_type

        total_leads = sum(totals.values())

        # ---- Pipeline health / status breakdown / intent / follow-ups ----
        end_collection = db["endData"]
        followup_count = pending_count = done_count = 0
        status_counts = {}
        intent_buckets = {"Hot": [], "Warm": [], "Cold": []}
        today_list, week_list = [], []

        def map_interest_to_bucket(level):
            lvl = (level or "").strip().lower()
            if lvl in ("very high", "high"):
                return "Hot"
            if lvl == "medium":
                return "Warm"
            if lvl == "cold":
                return "Cold"
            return None

        def _clean(v, default="-"):
            if v is None or v == "":
                return default
            if isinstance(v, float) and math.isnan(v):
                return default
            return v

        def parse_followup_date(s):
            if not s:
                return None
            s = str(s).strip()
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    return datetime.strptime(s, fmt).date()
                except ValueError:
                    continue
            return None

        today = now.date()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)

        if qualifying_phones:
            for ed in end_collection.find({"Number": {"$in": list(qualifying_phones.keys())}}):
                number = ed.get("Number")
                lead_type = qualifying_phones.get(number, "buying")

                status_val = (ed.get("Status") or "").strip().lower()
                followup_val = (ed.get("Next Follow-up Timeline") or "").strip()
                if followup_val:
                    followup_count += 1
                if status_val == "done":
                    done_count += 1
                else:
                    pending_count += 1

                call_status = ed.get("Call Status") or "No Log"
                status_counts[call_status] = status_counts.get(call_status, 0) + 1

                bucket = map_interest_to_bucket(ed.get("Interest Level"))
                if bucket:
                    intent_buckets[bucket].append({
                        "number": number, "name": _clean(ed.get("Customer Name"), "Unknown"),
                        "location": _clean(ed.get("Location Interested In")),
                        "propertyType": _clean(ed.get("Property Type")),
                        "budget": _clean(ed.get("Budget Range")),
                        "callStatus": _clean(ed.get("Call Status")),
                        "customerResponse": _clean(ed.get("Customer Response")),
                        "interestLevel": _clean(ed.get("Interest Level")),
                        "callerRemarks": _clean(ed.get("Caller Remarks")),
                        "nextFollowupTimeline": _clean(ed.get("Next Follow-up Timeline")),
                        "nextCallDate": _clean(ed.get("Next Call Date")),
                        "callAttempts": ed.get("Call_attempt") if isinstance(ed.get("Call_attempt"), int) else 0,
                        "leadId": ed.get("LeadId", ""),
                        "type": lead_type,
                    })

                fdate = parse_followup_date(ed.get("Next Call Date"))
                if fdate and fdate >= today:
                    entry = {
                        "leadType": lead_type,
                        "name": _clean(ed.get("Customer Name"), "Unknown"),
                        "phone": number,
                        "assignedTo": "-",
                        "nextCallDate": _clean(ed.get("Next Call Date")),
                        "nextFollowupTimeline": _clean(ed.get("Next Follow-up Timeline")),
                        "callStatus": _clean(ed.get("Call Status")),
                        "location": _clean(ed.get("Location Interested In")),
                        "propertyType": _clean(ed.get("Property Type")),
                        "budget": _clean(ed.get("Budget Range")),
                        "customerResponse": _clean(ed.get("Customer Response")),
                        "interestLevel": _clean(ed.get("Interest Level")),
                        "callerRemarks": _clean(ed.get("Caller Remarks")),
                        "callAttempts": ed.get("Call_attempt") if isinstance(ed.get("Call_attempt"), int) else 0,
                    }
                    if fdate == today:
                        today_list.append(entry)
                    elif week_start <= fdate <= week_end:
                        week_list.append(entry)

        # ---- Team activity: calls within the selected period, by the call log's own timestamp ----
        call_query = {}
        if start is not None:
            call_query["CreatedAt"] = {"$gte": start, "$lte": end}
        by_employee = {}
        calls_period_total = 0
        for log in call_logs_collection.find(call_query, {"CalledBy": 1}):
            calls_period_total += 1
            name = log.get("CalledBy", "Unknown")
            by_employee[name] = by_employee.get(name, 0) + 1

        top_locations = dict(sorted(location_counts.items(), key=lambda x: -x[1])[:5])

        return jsonify({
            "success": True,
            "period": period,
            "totals": {"total": total_leads, **totals},
            "pipeline": {
                "followup_count": followup_count,
                "pending_count": pending_count,
                "done_count": done_count,
                "calls_period_total": calls_period_total
            },
            "team_activity": {"by_employee": by_employee},
            "intent": {
                "hotCount": len(intent_buckets["Hot"]),
                "warmCount": len(intent_buckets["Warm"]),
                "coldCount": len(intent_buckets["Cold"]),
                "hotLeads": intent_buckets["Hot"],
                "warmLeads": intent_buckets["Warm"],
                "coldLeads": intent_buckets["Cold"]
            },
            "followups": {
                "todayCount": len(today_list), "weekCount": len(week_list),
                "todayLeads": today_list, "weekLeads": week_list
            },
            "trend": trend_counts,
            "status_breakdown": status_counts,
            "top_locations": top_locations
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/delete-lead", methods=["DELETE"])
def delete_lead():
    try:
        data = request.json

        lead_id = data.get("id")
        collection_name = data.get("collection")

        if not lead_id or not collection_name:
            return jsonify({"error": "Missing id or collection"}), 400

        if collection_name not in [
            "Leads",
            "RentalLeads",
            "sellingLeads",
            "agentLeads",
            "endData"
        ]:
            return jsonify({"error": "Invalid collection"}), 400

        collection = db[collection_name]

        result = collection.delete_one({
            "_id": ObjectId(lead_id)
        })

        if result.deleted_count == 0:
            return jsonify({"error": "Lead not found"}), 404

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

#excel

@app.route("/api/export-leads", methods=["POST"])
def export_leads():
    """
    Builds an Excel export of leads.
    """
    try:
        data = request.json or {}
        leads_input = data.get("leads", [])
        period = data.get("period")                 # "this_month" | "last_month" | "last_3_months" | "all" | None
        lead_type_filter = data.get("type", "all")   # "all" | "buying" | "rental" | "selling" | "agent"

        # Date-range based export (used when no explicit page-selection is sent)
        if not leads_input and period:
            start_date, end_date = get_date_range(period)
            types_to_scan = (
                [lead_type_filter] if lead_type_filter != "all"
                else ["buying", "rental", "selling", "agent"]
            )

            leads_input = []
            for t in types_to_scan:
                collection_name = COLLECTION_MAP.get(t, "Leads")

                try:
                    cursor = db[collection_name].find()
                except Exception as scan_err:
                    print(f"[export] Failed scanning collection '{collection_name}': {scan_err}")
                    continue

                for d in cursor:
                    try:
                        if start_date:
                            lead_date = parse_lead_date(d.get("Date"))
                            if not lead_date or lead_date < start_date or lead_date > end_date:
                                continue
                        leads_input.append({
                            "id": str(d["_id"]),
                            "phone": d.get("Phone Number", ""),
                            "type": t,
                            "name": d.get("Lead Name") or d.get("Name") or ""
                        })
                    except Exception as row_err:
                        print(f"[export] Skipping malformed doc in '{collection_name}': {row_err}")
                        continue

        if not leads_input:
            return jsonify({"error": "No leads found for the selected filters"}), 400

        end_collection = db["endData"]
        rows = []
        max_calls = 0
        skipped = 0

        for item in leads_input:
            try:
                lead_id = item.get("id")
                lead_type = item.get("type", "buying")
                collection_name = COLLECTION_MAP.get(lead_type, "Leads")

                raw_phone = item.get("phone", "")
                phone = normalize_number(raw_phone)

                valid_phone = bool(phone) and len(phone) >= 8
                if valid_phone and not phone.startswith("91"):
                    phone = "91" + phone

                lead_doc = None

                if lead_id:
                    try:
                        lead_doc = db[collection_name].find_one({"_id": ObjectId(lead_id)})
                    except Exception:
                        lead_doc = None

                if not lead_doc and valid_phone:
                    try:
                        safe_phone = re.escape(phone)
                        lead_doc = db[collection_name].find_one(
                            {"Phone Number": {"$regex": safe_phone}}
                        )
                    except Exception:
                        lead_doc = None

                lead_doc = lead_doc or {}

                end_doc = {}
                call_logs = []
                if valid_phone:
                    try:
                        end_doc = end_collection.find_one({"Number": phone}) or {}
                    except Exception as end_err:
                        print(f"[export] end-data lookup failed for {phone}: {end_err}")
                    try:
                        call_logs = list(
                            call_logs_collection.find({"Number": phone}).sort("CreatedAt", 1)
                        )
                    except Exception as log_err:
                        print(f"[export] call-log lookup failed for {phone}: {log_err}")

                max_calls = max(max_calls, len(call_logs))

                rows.append({
                    "name": lead_doc.get("Lead Name") or lead_doc.get("Name") or item.get("name") or "Unknown",
                    "phone": ("+" + phone) if valid_phone else (str(raw_phone) or "-"),
                    "type": str(lead_type).capitalize(),
                    "location": lead_doc.get("Location Interested In") or lead_doc.get("Property Location") or "-",
                    "property": lead_doc.get("Property Type", "-"),
                    "budget": lead_doc.get("Budget Range") or lead_doc.get("Expected Price") or "-",
                    "assigned_to": lead_doc.get("AssignTo", "-"),
                    "call_status": end_doc.get("Call Status", "-"),
                    "interest_level": end_doc.get("Interest Level", "-"),
                    "next_followup": end_doc.get("Next Follow-up Timeline", "-"),
                    "next_call_date": end_doc.get("Next Call Date", "-"),
                    "total_calls": len(call_logs),
                    "calls": call_logs
                })

            except Exception as item_err:
                skipped += 1
                print(f"[export] Skipping row due to error: {item_err}")
                continue

        if not rows:
            return jsonify({"error": "No leads could be exported (all rows failed)"}), 400

        # Build workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads Export"

        base_headers = [
            "Lead Name", "Phone", "Type", "Location", "Property", "Budget",
            "Assigned To", "Current Status", "Interest Level",
            "Next Follow-up", "Next Call Date", "Total Calls"
        ]
        call_headers = []
        for i in range(1, max_calls + 1):
            call_headers += [
                f"Call {i} - Date & Time", f"Call {i} - Status",
                f"Call {i} - Response", f"Call {i} - Remarks"
            ]
        headers = base_headers + call_headers
        ws.append(headers)

        header_fill = PatternFill(start_color="2D3142", end_color="2D3142", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True, size=11)
        thin = Side(style="thin", color="D1D5DB")
        thin_border = Border(left=thin, right=thin, top=thin, bottom=thin)

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border
        ws.row_dimensions[1].height = 28

        accent_fill = PatternFill(start_color="FDF1EB", end_color="FDF1EB", fill_type="solid")
        for r_idx, row in enumerate(rows, start=2):
            base_values = [
                row["name"], row["phone"], row["type"], row["location"], row["property"],
                row["budget"], row["assigned_to"], row["call_status"], row["interest_level"],
                row["next_followup"], row["next_call_date"], row["total_calls"]
            ]
            call_values = []
            for c in row["calls"]:
                call_values += [
                    format_ist(c.get("CreatedAt")),
                    c.get("CallStatus", "-"),
                    c.get("CustomerResponse", "-"),
                    c.get("CallerRemarks", "-")
                ]
            call_values += ["-"] * (len(call_headers) - len(call_values))

            full_row = base_values + call_values
            for col_idx, val in enumerate(full_row, start=1):
                cell = ws.cell(row=r_idx, column=col_idx, value=val)
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                if r_idx % 2 == 0:
                    cell.fill = accent_fill

        widths = [22, 16, 10, 18, 16, 14, 16, 20, 14, 18, 14, 10] + [20, 16, 18, 22] * max_calls
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

        if skipped:
            print(f"[export] Completed with {skipped} row(s) skipped out of {len(leads_input)} requested")

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        filename = f"Leads_Export_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


#wp templetes

@app.route("/api/wp-template", methods=["POST"])
def create_wp_template():
    try:
        name = request.form.get("name")
        message = request.form.get("message")
        file = request.files.get("media")

        if not name or not message:
            return jsonify({"error": "Name and message required"}), 400

        filename = None
        file_type = None

        if file:
            ext = file.filename.split('.')[-1]
            unique_name = str(int(time.time())) + "_" + secure_filename(file.filename)
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
            file.save(file_path)

            filename = unique_name

            if ext.lower() in ["jpg", "jpeg", "png", "webp"]:
                file_type = "image"
            elif ext.lower() in ["mp4", "mov", "avi"]:
                file_type = "video"
            else:
                file_type = "file"

        data = {
            "name": name,
            "message": message,
            "media": filename,
            "type": file_type,
            "createdAt": datetime.utcnow()
        }

        db.wp.insert_one(data)

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/wp-template", methods=["GET"])
def get_wp_templates():
    try:
        templates = list(db.wp.find().sort("createdAt", -1))

        for t in templates:
            t["_id"] = str(t["_id"])

        return jsonify(templates)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/api/wp-template/<id>", methods=["DELETE"])
def delete_wp_template(id):
    try:
        template = db.wp.find_one({"_id": ObjectId(id)})

        if not template:
            return jsonify({"error": "Template not found"}), 404

        # Delete file from uploads folder
        if template.get("media"):
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], template["media"])
            if os.path.exists(file_path):
                os.remove(file_path)

        # Delete from MongoDB
        db.wp.delete_one({"_id": ObjectId(id)})

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


update_lead_db = client["NishaHomesData"]
update_lead_collection = update_lead_db["endData"]


@app.route("/update-lead", methods=["POST"])
def update_lead():
    try:
        data = request.get_json()

        if not data or not isinstance(data, list):
            return jsonify({"error": "Invalid input format"}), 400

        output = data[0].get("output", {})
        number = output.get("number")

        if not number:
            return jsonify({"error": "Number is required"}), 400

        # Fields we want to update (excluding _id, Number, Call_attempt)
        update_fields = {
            "Customer Name": output.get("customerName"),
            "Lead Source": output.get("leadSource"),
            "Property Type": output.get("propertyType"),
            "Preferred Location": output.get("preferredLocation"),
            "Budget Range": output.get("budgetRange"),
            "Call Status": output.get("callStatus"),
            "Transaction Type": output.get("transactionType"),
            "Configuration": output.get("configuration"),
            "Customer Response": output.get("customerResponse"),
            "Interest Level": output.get("interestLevel"),
            "Objection / Reason": output.get("objectionReason"),
            "Next Follow-up Timeline": output.get("nextFollowupTimeline"),
            "Caller Remarks": output.get("callerRemarks"),
            "Call Priority": output.get("callPriority"),
            "Next Call Date": output.get("nextCallDate"),
            "done": output.get("done"),
            "Lead type": output.get("leadType"),
            "Lead score": output.get("leadScore"),
            "lastUpdatedAt": datetime.utcnow()
        }

        # Remove None values (clean update)
        update_fields = {k: v for k, v in update_fields.items() if v is not None}

        result = update_lead_collection.update_one(
            {"Number": number},   # Find by Number
            {
                "$set": update_fields,
                "$inc": {"Call_attempt": 1}  # increment safely
            },
            upsert=False  # Do NOT create new document automatically
        )

        if result.matched_count == 0:
            return jsonify({"message": "No document found with this number"}), 404

        return jsonify({"message": "Lead updated successfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/check-assign")
def check_assign():
    return "assign route section reached"

#https://www.karmandrones.com/

# Allowed values for the LeadType field on every Leads-family document
VALID_LEAD_TYPES = {"buyer_purchase", "buyer_rental", "seller", "agent"}


@app.route("/add-lead", methods=["POST"])
def add_lead():
    try:
        data = request.json

        # 1. Get collection name dynamically
        collection_name = data.get("collection")
        if not collection_name:
            return jsonify({"error": "Collection name is required"}), 400

        collection = db[collection_name]

        # 2. Extract phone number (required for upsert)
        phone_number = data.get("Phone Number")
        if not phone_number:
            return jsonify({"error": "Phone Number is required"}), 400

        # NEW: normalize/validate LeadType — defaults to "buyer_purchase"
        # if missing or not one of the accepted values.
        lead_type = str(data.get("LeadType", "")).strip()
        if lead_type not in VALID_LEAD_TYPES:
            lead_type = "buyer_purchase"
        data["LeadType"] = lead_type

        # 3. Remove collection key from document
        data.pop("collection", None)

        # NEW: "Created At" timestamp, formatted as "YYYY-MM-DD HH:MM:SS"
        # (e.g. 2026-07-14 13:58:12). Uses $setOnInsert so it's only written
        # once — the first time this lead is created — and never gets
        # overwritten on later upserts/updates to the same phone number.
        data.pop("Created At", None)  # never let incoming payload override it
        now = datetime.utcnow()
        created_at_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # NEW: also write "Date" (DD-MM-YYYY) on first insert — this is the
        # field parse_lead_date()/filter_by_july_range() actually filter on
        # for /api/leads, /api/rental-leads, /api/selling-leads, /api/agent-leads.
        # Without it, leads created via this endpoint were silently excluded
        # from every list even though they existed in Mongo.
        if not str(data.get("Date", "")).strip():
            data["Date"] = now.strftime("%d-%m-%Y")

        # 4. Upsert (update if exists, insert if not)
        result = collection.update_one(
            {"Phone Number": phone_number},
            {
                "$set": data,
                "$setOnInsert": {"Created At": created_at_str}
            },
            upsert=True
        )

        name = data.get("Lead Name")

        create_contact(name, phone_number)

        return jsonify({
            "success": True,
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id else None
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/modify-document", methods=["POST"])
def modify_document():
    try:
        data = request.json

        if not data:
            return jsonify({"error": "No JSON body provided"}), 400

        # 1. Validate collection
        collection_name = data.get("collection")
        if not collection_name:
            return jsonify({"error": "Collection name is required"}), 400

        # Optional: restrict collections (recommended)
        allowed_collections = [
            "Leads",
            "RentalLeads",
            "sellingLeads",
            "agentLeads",
            "endData",
            "teamAssign",
            "orderhouseofcakes",
            "tasks",
            "Realtors"
        ]

        if collection_name not in allowed_collections:
            return jsonify({"error": "Invalid collection"}), 400

        collection = db[collection_name]

        # 2. Validate phone / number
        raw_number = (
            data.get("Phone Number") or
            data.get("Number") or
            data.get("Employee number")
        )

        if not raw_number:
            return jsonify({"error": "Phone Number / Number / Employee number is required"}), 400

        normalized_number = normalize_number(raw_number)

        if not normalized_number:
            return jsonify({"error": "Invalid phone number"}), 400

        # 3. Build filter dynamically
        if collection_name == "endData":
            filter_query = {"Number": normalized_number}
        else:
            filter_query = {
                "$or": [
                    {"Phone Number": normalized_number},
                    {"Employee number": normalized_number},
                    {"Number": normalized_number},

                    {"Phone Number": {"$regex": str(normalized_number)}},
                    {"Employee number": {"$regex": str(normalized_number)}},
                    {"Number": {"$regex": str(normalized_number)}},
                ]
            }

        # 4. Build update operations
        update_query = {}

        # SET (add/update fields)
        set_fields = data.get("set", {})
        if set_fields:
            update_query["$set"] = set_fields

        # UNSET (delete fields)
        unset_fields = data.get("unset", [])
        if unset_fields:
            update_query["$unset"] = {field: "" for field in unset_fields}

        # PUSH (append to array)
        push_fields = data.get("push", {})
        if push_fields:
            update_query["$push"] = push_fields

        # INC (increment numbers)
        inc_fields = data.get("inc", {})
        if inc_fields:
            update_query["$inc"] = inc_fields

        if not update_query:
            return jsonify({"error": "No update operations provided"}), 400

        # 5. Perform update (Upsert allowed)
        result = collection.update_one(
            filter_query,
            update_query,
            upsert=True
        )

        # 6. Return updated document
        updated_doc = collection.find_one(filter_query)

        return jsonify({
            "success": True,
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id else None,
            "updated_document": serialize_doc(updated_doc) if updated_doc else None
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/add-task', methods=['POST'])
def add_task():
    data = request.json

    emp = data.get("phone")
    task_text = data.get("task")
    status = data.get("status", "pending")

    if not emp or not task_text:
        return jsonify({"success": False, "message": "Missing data"})

    normalized_number = normalize_number(emp)

    task_id = f"{normalized_number}_{int(time.time()*1000)}"

    task_obj = {
        "id": task_id,
        "text": task_text,
        "status": status,
        "created_at": int(time.time())
    }

    result = db.teamAssign.update_one(
        {
            "$or": [
                {"Employee number": normalized_number},
                {"Employee number": {"$regex": str(normalized_number)}},
            ]
        },
        {
            "$push": {"tasks": task_obj}
        },
        upsert=True
    )

    return jsonify({
        "success": True,
        "message": "Task added",
        "task_id": task_id
    })


@app.route('/update-task-status', methods=['POST'])
def update_task_status():
    data = request.json

    emp = data.get("phone")
    task_id = data.get("task_id")
    status = data.get("status")

    if not emp or not task_id or not status:
        return jsonify({"success": False, "message": "Missing fields"})

    result = db.teamassign.update_one(
        {
            "Phone Number": emp,
            "tasks.id": task_id
        },
        {
            "$set": {
                "tasks.$.status": status
            }
        }
    )

    if result.modified_count:
        return jsonify({"success": True, "message": "Status updated"})
    else:
        return jsonify({"success": False, "message": "Task not found"})


@app.route('/get-tasks/<phone>', methods=['GET'])
def get_tasks(phone):
    user = db.teamassign.find_one({"Phone Number": phone})

    if not user:
        return jsonify([])

    return jsonify(user.get("tasks", []))


# Endpoint 1: Image -> Text
@app.route('/image-to-text', methods=['POST'])
def image_to_text():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    temp = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
    temp.close()

    file.save(temp.name)

    text = extract_text_from_image(temp.name)

    os.remove(temp.name)  # cleanup

    return jsonify({'text': text})

# Endpoint 2: Video -> Audio (returns HTML player)
@app.route('/video-to-audio', methods=['POST'])
def video_to_audio():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    # TEMP VIDEO (not permanent)
    temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    temp_video.close()
    file.save(temp_video.name)

    # FINAL AUDIO (saved in uploads)
    audio_filename = f"{int(time.time())}.mp3"
    audio_path = os.path.join(app.config["UPLOAD_FOLDER"], audio_filename)

    # Extract audio
    extract_audio_from_video(temp_video.name, audio_path)

    # Delete temp video
    os.remove(temp_video.name)

    # Return usable URL
    return jsonify({
        "audio_url": f"/uploads/{audio_filename}"
    })


@app.route('/get-audio')
def get_audio():
    path = request.args.get('path')
    return send_file(path, mimetype='audio/mpeg')

@app.route('/video-to-frames', methods=['POST'])
def video_to_frames():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    # temp video
    temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    temp_video.close()
    file.save(temp_video.name)

    # output folder
    folder_name = str(int(time.time()))
    frames_folder = os.path.join(app.config["UPLOAD_FOLDER"], folder_name)
    os.makedirs(frames_folder, exist_ok=True)

    # extract frames
    cap = cv2.VideoCapture(temp_video.name)
    fps = cap.get(cv2.CAP_PROP_FPS)

    if fps == 0:
        fps = 25  # fallback

    interval = int(fps)

    count = 0
    frame_number = 0

    while True:
        success, frame = cap.read()
        if not success:
            break

        if frame_number % interval == 0:
            frame_path = os.path.join(frames_folder, f"frame_{count}.jpg")
            cv2.imwrite(frame_path, frame)
            count += 1

        frame_number += 1

    cap.release()
    os.remove(temp_video.name)

    base_url = request.host_url.rstrip('/')

    frame_urls = [
        f"{base_url}/uploads/{folder_name}/frame_{i}.jpg"
        for i in range(count)
    ]

    return jsonify({
        "frames": frame_urls,
        "total_frames": count
    })


#project


@app.route("/api/projects", methods=["GET"])
def get_projects():
    try:
        role = session.get("role")
        query = {}

        if role == "partner":
            query = {"ownerNumber": session.get("employee_number")}

        status_filter = request.args.get("status")
        if status_filter:
            query["status"] = status_filter

        # NEW
        kind_filter = request.args.get("kind")
        if kind_filter:
            query["kind"] = kind_filter

        projects = list(projects_collection.find(query).sort("createdAt", -1))

        return jsonify({
            "success": True,
            "data": [serialize_doc(p) for p in projects]
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "data": [], "error": str(e)}), 500

# =============================
# CRM STAGE (extends projects)
# =============================
LIST_STAGES = [
    "Approved", "Customer Shared", "Site Visit Scheduled", "Negotiation",
    "Token Received", "Agreement Done", "Registration Done",
    "Sold", "Rented", "Closed", "Cancelled"
]

@app.route("/api/projects/stage/<project_id>", methods=["POST"])
def update_project_stage(project_id):
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"status": "error", "message": "Staff only"}), 403

    data = request.json or {}
    stage = data.get("stage")
    if stage not in LIST_STAGES:
        return jsonify({"status": "error", "message": "Invalid stage"}), 400

    project = projects_collection.find_one({"_id": ObjectId(project_id)})
    if not project:
        return jsonify({"status": "error", "message": "Project not found"}), 404

    history_entry = {
        "action": f"Stage: {stage}",
        "remark": data.get("remark", ""),
        "at": datetime.utcnow(),
        "by": session.get("employee_name")
    }

    projects_collection.update_one(
        {"_id": ObjectId(project_id)},
        {
            "$set": {"stage": stage, "lastUpdatedAt": datetime.utcnow()},
            "$push": {"history": history_entry}
        }
    )

    updated = projects_collection.find_one({"_id": ObjectId(project_id)})
    return jsonify({"status": "success", "data": serialize_doc(updated)}), 200


# =============================
# REQUIREMENTS DESK
# =============================
requirements_collection = db["requirements"]

REQ_STATUS_LABELS = {
    "new": "New", "broadcasted": "Broadcasted", "responses": "Responses",
    "matched": "Matched", "visit": "Visit scheduled", "closed": "Closed",
    "cancelled": "Cancelled", "expired": "Expired", "rejected": "Rejected"
}


@app.route("/api/requirements", methods=["GET"])
def get_requirements():
    if not session.get("user_id"):
        return jsonify({"success": False, "data": []}), 401

    role = session.get("role")
    query = {}
    if role == "partner":
        num = session.get("employee_number")
        query = {"$or": [
            {"submittedByNumber": num},
            {"broadcastTo": "all"},
            {"broadcastTo": num}
        ]}

    docs = list(requirements_collection.find(query).sort("createdAt", -1))
    return jsonify({"success": True, "data": [serialize_doc(d) for d in docs]}), 200


@app.route("/api/requirements/add", methods=["POST"])
def add_requirement():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    data = request.json or {}
    role = session.get("role")

    if role == "partner":
        submitted_by_number = session.get("employee_number")
        submitted_by_name = session.get("employee_name")
    else:
        submitted_by_number = data.get("onBehalfNumber") or session.get("employee_number")
        submitted_by_name = data.get("onBehalfName") or session.get("employee_name")

    location = (data.get("location") or "").strip()
    if not location:
        return jsonify({"success": False, "message": "Location is required"}), 400

    doc = {
        "reqType": data.get("reqType", "Buy"),
        "propertyType": data.get("propertyType", ""),
        "config": data.get("config", ""),
        "location": location,
        "budgetMin": data.get("budgetMin", ""),
        "budgetMax": data.get("budgetMax", ""),
        "areaMin": data.get("areaMin", ""),
        "areaMax": data.get("areaMax", ""),
        "furnishing": data.get("furnishing", ""),
        "possession": data.get("possession", ""),
        "notes": data.get("notes", ""),
        "priority": data.get("priority", "Medium"),
        "clientName": data.get("clientName", ""),
        "clientMobile": data.get("clientMobile", ""),
        "submittedByNumber": submitted_by_number,
        "submittedByName": submitted_by_name,
        "status": "new",
        "broadcastTo": [],
        "responses": [],
        "history": [{
            "action": "Submitted", "remark": "",
            "at": datetime.utcnow(), "by": submitted_by_name
        }],
        "createdAt": datetime.utcnow()
    }

    result = requirements_collection.insert_one(doc)
    return jsonify({"success": True, "id": str(result.inserted_id)}), 201


@app.route("/api/requirements/status/<req_id>", methods=["POST"])
def update_requirement_status(req_id):
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"success": False, "message": "Staff only"}), 403

    data = request.json or {}
    status = data.get("status")
    if status not in REQ_STATUS_LABELS:
        return jsonify({"success": False, "message": "Invalid status"}), 400

    req_doc = requirements_collection.find_one({"_id": ObjectId(req_id)})
    if not req_doc:
        return jsonify({"success": False, "message": "Not found"}), 404

    requirements_collection.update_one(
        {"_id": ObjectId(req_id)},
        {
            "$set": {"status": status},
            "$push": {"history": {
                "action": REQ_STATUS_LABELS.get(status, status),
                "remark": data.get("remark", ""),
                "at": datetime.utcnow(),
                "by": session.get("employee_name")
            }}
        }
    )
    return jsonify({"success": True}), 200


@app.route("/api/requirements/broadcast/<req_id>", methods=["POST"])
def broadcast_requirement(req_id):
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"success": False, "message": "Staff only"}), 403

    data = request.json or {}
    to = data.get("to")  # "all" or a list of employee numbers

    req_doc = requirements_collection.find_one({"_id": ObjectId(req_id)})
    if not req_doc:
        return jsonify({"success": False, "message": "Not found"}), 404

    if to == "all":
        broadcast_to = "all"
    else:
        existing = req_doc.get("broadcastTo", [])
        if existing == "all":
            existing = []
        existing = list(set(existing + (to or [])))
        broadcast_to = existing

    new_status = "broadcasted" if req_doc.get("status") == "new" else req_doc.get("status")

    requirements_collection.update_one(
        {"_id": ObjectId(req_id)},
        {
            "$set": {"broadcastTo": broadcast_to, "status": new_status},
            "$push": {"history": {
                "action": "Broadcasted", "remark": "",
                "at": datetime.utcnow(), "by": session.get("employee_name")
            }}
        }
    )
    return jsonify({"success": True}), 200


@app.route("/api/requirements/respond/<req_id>", methods=["POST"])
def respond_requirement(req_id):
    if session.get("role") != "partner":
        return jsonify({"success": False, "message": "Partner only"}), 403

    data = request.json or {}
    partner_number = session.get("employee_number")
    partner_name = session.get("employee_name")

    resp = {
        "partnerNumber": partner_number,
        "partnerName": partner_name,
        "type": data.get("type", "Need More Details"),
        "at": datetime.utcnow(),
        "property": data.get("property", {})
    }

    req_doc = requirements_collection.find_one({"_id": ObjectId(req_id)})
    if not req_doc:
        return jsonify({"success": False, "message": "Not found"}), 404

    responses = [r for r in req_doc.get("responses", []) if r.get("partnerNumber") != partner_number]
    responses.append(resp)

    new_status = "responses" if req_doc.get("status") == "broadcasted" else req_doc.get("status")

    requirements_collection.update_one(
        {"_id": ObjectId(req_id)},
        {
            "$set": {"responses": responses, "status": new_status},
            "$push": {"history": {
                "action": f"{partner_name}: {resp['type']}",
                "remark": "", "at": datetime.utcnow(), "by": partner_name
            }}
        }
    )
    return jsonify({"success": True}), 200


@app.route("/api/requirements/delete/<req_id>", methods=["DELETE"])
def delete_requirement(req_id):
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"success": False, "message": "Staff only"}), 403
    requirements_collection.delete_one({"_id": ObjectId(req_id)})
    return jsonify({"success": True}), 200


# =============================
# PARTNER MANAGEMENT (extends teamAssign)
# =============================
@app.route("/api/toggle-team-active/<number>", methods=["POST"])
def toggle_team_active(number):
    if session.get("role") != "admin":
        return jsonify({"success": False, "message": "Admin only"}), 403

    try:
        number = int(normalize_number(number))
    except ValueError:
        return jsonify({"success": False, "message": "Invalid number"}), 400

    collection = db["teamAssign"]
    member = collection.find_one({"Employee number": number})
    if not member:
        return jsonify({"success": False, "message": "Not found"}), 404

    new_active = not member.get("Active", True)
    collection.update_one({"_id": member["_id"]}, {"$set": {"Active": new_active}})
    return jsonify({"success": True, "active": new_active}), 200


@app.route("/api/update-team-areas", methods=["POST"])
def update_team_areas():
    if session.get("role") != "admin":
        return jsonify({"success": False, "message": "Admin only"}), 403

    data = request.json or {}
    try:
        number = int(normalize_number(str(data.get("number", ""))))
    except ValueError:
        return jsonify({"success": False, "message": "Invalid number"}), 400

    db["teamAssign"].update_one(
        {"Employee number": number},
        {"$set": {"Areas": data.get("areas", "")}}
    )
    return jsonify({"success": True}), 200


# =============================
# SETTINGS (corporate/agent share settings)
# =============================
settings_collection = db["settings"]

@app.route("/api/settings", methods=["GET"])
def get_settings_api():
    if not session.get("user_id"):
        return jsonify({"success": False}), 401
    doc = settings_collection.find_one({"_id": "global"}) or {}
    doc.pop("_id", None)
    return jsonify({"success": True, "data": doc}), 200


@app.route("/api/settings", methods=["POST"])
def save_settings_api():
    if session.get("role") != "admin":
        return jsonify({"success": False, "message": "Admin only"}), 403
    data = request.json or {}
    fields = {k: data.get(k, "") for k in ["corporate", "agent", "advisorName", "website", "landing", "cta"]}
    settings_collection.update_one({"_id": "global"}, {"$set": fields}, upsert=True)
    return jsonify({"success": True}), 200


# =============================
# NEW: WHATSAPP SHARE TEXT BUILDER
# =============================
@app.route("/api/projects/share-text/<project_id>", methods=["GET"])
def project_share_text(project_id):
    try:
        p = projects_collection.find_one({"_id": ObjectId(project_id)})
        if not p:
            return jsonify({"success": False, "message": "Not found"}), 404
        s = settings_collection.find_one({"_id": "global"}) or {}
        return jsonify({"success": True, "text": build_whatsapp_share_text(p, s)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


def build_whatsapp_share_text(p, settings):
    L = []
    L.append(p.get("name") or p.get("propertyTitle") or "")
    L.append(f"📍 {p.get('location') or p.get('locality') or ''}")
    L.append(f"💰 {p.get('budget') or p.get('startingPrice') or ''}")
    if p.get("configuration"): L.append(f"🛏️ {p['configuration']}")
    area = []
    if p.get("superArea"):  area.append(f"{p['superArea']} sq.ft built-up")
    if p.get("carpetArea"): area.append(f"carpet {p['carpetArea']} sq.ft")
    if area: L.append("📐 " + " · ".join(area))
    if p.get("furnishing"): L.append(f"🛋️ {p['furnishing']}")
    if p.get("possession"): L.append(f"🏗️ {p['possession']}")
    if p.get("facing"):     L.append(f"🧭 {p['facing']}-facing")
    if p.get("floor"):      L.append(f"🏢 Floor {p['floor']}")
    if p.get("bathrooms"):  L.append(f"🛁 {p['bathrooms']} bathrooms")
    if p.get("parking"):    L.append(f"🅿️ {p['parking']} parking")
    L.append("─" * 20)
    if p.get("description"): L.append(p["description"])
    L.append("─" * 20)

    # Fixed CTA + contact block
    L.append("📅 Schedule Your Private Site Visit")
    L.append("🏡 Request Complete Property Details")
    L.append("🔗 View for exclusive listings: https://www.squareyards.com/agent/nisha/492906")
    L.append("━" * 16)
    L.append("🏡 Nisha Homes")
    L.append("Your Trusted Real Estate Advisor")
    L.append("💬 Nisha Homes Main Office")
    L.append("https://wa.me/917303515710")
    L.append("👤 Business Coordinator")
    L.append("https://wa.me/918130505710")
    L.append("━" * 16)

    return "\n".join(L)

# =============================
# COORDINATOR DASHBOARD STATS
# =============================
@app.route("/api/inventory-dashboard-stats", methods=["GET"])
def inventory_dashboard_stats():
    if session.get("role") not in ("admin", "emp"):
        return jsonify({"success": False}), 403

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    closed_stages = {"Sold", "Rented", "Closed", "Cancelled"}

    all_projects = list(projects_collection.find())
    all_reqs = list(requirements_collection.find())
    all_partners = list(db["teamAssign"].find({"roll": "partner"}))

    today_inventory = sum(1 for p in all_projects if p.get("createdAt") and p["createdAt"] >= today_start)
    today_requirements = sum(1 for r in all_reqs if r.get("createdAt") and r["createdAt"] >= today_start)
    pending_inventory = sum(1 for p in all_projects if p.get("status") == "pending")
    pending_requirements = sum(1 for r in all_reqs if r.get("status") == "new")
    live_stock = sum(1 for p in all_projects if p.get("status") == "approved")
    sold = sum(1 for p in all_projects if p.get("stage") == "Sold")
    rented = sum(1 for p in all_projects if p.get("stage") == "Rented")
    visits = (sum(1 for p in all_projects if p.get("stage") == "Site Visit Scheduled") +
              sum(1 for r in all_reqs if r.get("status") == "visit"))
    inventory_closed = sum(1 for p in all_projects if p.get("stage") in closed_stages)
    requirements_closed = sum(1 for r in all_reqs if r.get("status") in ("closed", "matched"))

    perf = []
    for p in all_partners:
        num = p.get("Employee number")
        p_inv = [x for x in all_projects if x.get("ownerNumber") == num]
        p_req = [x for x in all_reqs if x.get("submittedByNumber") == num]
        approved = sum(1 for x in p_inv if x.get("status") == "approved" or x.get("stage") in closed_stages)
        deals = sum(1 for x in p_inv if x.get("stage") in ("Sold", "Rented"))
        conv = round(deals * 100 / len(p_inv)) if p_inv else 0
        perf.append({
            "name": p.get("Employee name"),
            "inventory": len(p_inv),
            "requirements": len(p_req),
            "approved": approved,
            "deals": deals,
            "conversion": conv
        })
    perf.sort(key=lambda x: (-x["deals"], -x["inventory"]))

    return jsonify({
        "success": True,
        "todayInventory": today_inventory,
        "todayRequirements": today_requirements,
        "pendingInventory": pending_inventory,
        "pendingRequirements": pending_requirements,
        "liveStock": live_stock,
        "sold": sold,
        "rented": rented,
        "visits": visits,
        "inventoryClosed": inventory_closed,
        "requirementsClosed": requirements_closed,
        "partnerPerformance": perf
    }), 200


# =============================
# AI: SHARED GENERATE-PROPERTY ENDPOINT
# =============================
@app.route("/api/ai/generate-property", methods=["POST"])
def ai_generate_property():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    try:
        form_type = request.form.get("form_type", "inventory")
        combined_text = []

        for f in request.files.getlist("images"):
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[-1] or ".png"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.close()
            f.save(tmp.name)
            try:
                txt = extract_text_from_image(tmp.name)
                if txt:
                    combined_text.append(txt)
            except Exception as img_err:
                print(f"[ai-generate] image OCR failed: {img_err}")
            finally:
                os.remove(tmp.name)

        # NEW: reference screenshot(s) — OCR'd for context only, NEVER uploaded/saved
        # to Cloudinary or Mongo. Purely used to extract extra text for Mistral.
        for f in request.files.getlist("screenshot"):
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[-1] or ".png"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.close()
            f.save(tmp.name)
            try:
                txt = extract_text_from_image(tmp.name)
                if txt:
                    combined_text.append(txt)
            except Exception as scr_err:
                print(f"[ai-generate] screenshot OCR failed: {scr_err}")
            finally:
                os.remove(tmp.name)

        pdf_file = request.files.get("pdf")
        if pdf_file and pdf_file.filename:
            tmp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp_pdf.close()
            pdf_file.save(tmp_pdf.name)
            try:
                txt = extract_text_from_pdf(tmp_pdf.name)
                if txt:
                    combined_text.append(txt)
            finally:
                os.remove(tmp_pdf.name)

        full_text = "\n\n".join(combined_text).strip()
        if not full_text:
            return jsonify({"success": False, "message": "Could not extract any text from the uploaded photos/PDF"}), 400

        schema = INVENTORY_FIELDS_SCHEMA if form_type == "inventory" else PROJECT_FIELDS_SCHEMA
        result = call_mistral_generate(full_text, schema)

        return jsonify({"success": True, "data": result})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


# -------------------------------
# POST /api/projects/upload
# -------------------------------
@app.route("/api/projects/upload", methods=["POST"])
def upload_project():
    try:
        if not session.get("user_id"):
            return jsonify({"status": "error", "message": "Login required"}), 401

        name = request.form.get("name")
        location = request.form.get("location")
        description = request.form.get("description")
        budget = request.form.get("budget")
        category = request.form.get("category")
        file = request.files.get("media")

        # Validation
        if not all([name, location, description, budget, category, file]):
            return jsonify({
                "status": "error",
                "message": "All fields required"
            }), 400

        # Detect image vs video from extension
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        video_exts = {"mp4", "mov", "avi", "webm", "mkv"}
        resource_type = "video" if ext in video_exts else "image"

        # Upload straight to Cloudinary (no local disk write)
        upload_result = cloudinary.uploader.upload(
            file,
            resource_type=resource_type,
            folder="nishahomes/projects"
        )

        file_url = upload_result.get("secure_url")
        public_id = upload_result.get("public_id")

        # NEW: optional PDF / brochure upload for the project
        pdf_url = None
        pdf_file = request.files.get("pdf")
        if pdf_file and pdf_file.filename:
            pdf_upload = cloudinary.uploader.upload(
                pdf_file,
                resource_type="raw",
                folder="nishahomes/projects/docs"
            )
            pdf_url = pdf_upload.get("secure_url")

        # Save in DB
        project_data = {
            "name": name,
            "location": location,
            "description": description,
            "budget": budget,
            "category": category,
            "img": file_url,
            "mediaUrl": file_url,
            "mediaPublicId": public_id,
            "type": resource_type,
            "pdfUrl": pdf_url,
            "status": "pending" if session.get("role") == "partner" else "approved",
            "ownerNumber": session.get("employee_number"),
            "ownerName": session.get("employee_name"),
            "ownerRole": session.get("role"),
            "createdAt": datetime.utcnow()
        }

        result = projects_collection.insert_one(project_data)

        return jsonify({
            "status": "success",
            "message": "Project uploaded successfully",
            "id": str(result.inserted_id),
            "url": file_url
        }), 201

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# -------------------------------
# PARTNER "Add Inventory" upload
# (also used by admin/emp via the same modal — branding + screenshot-OCR aware)
# -------------------------------
@app.route("/api/projects/upload-inventory", methods=["POST"])
def upload_inventory():
    try:
        if not session.get("user_id"):
            return jsonify({"status": "error", "message": "Login required"}), 401

        f = request.form.get
        required = ["propertyType", "propertyTitle", "locality", "price"]
        if not all(f(k) for k in required):
            return jsonify({"status": "error", "message": "Property type, title, locality and price are required"}), 400

        # NEW: branding toggle + fields used to render the overlay onto photos
        branding = request.form.get("branding") == "true"
        brand_fields = {
            "dealType": f("dealType", ""), "propertyTitle": f("propertyTitle", ""),
            "locality": f("locality", ""), "price": f("price", ""),
            "configuration": f("configuration", ""), "superArea": f("superArea", "")
        }

        media_urls, media_public_ids = [], []
        for file in request.files.getlist("photos"):
            if not file or not file.filename:
                continue
            if branding:
                branded = build_branded_image(file.read(), brand_fields)
                up = cloudinary.uploader.upload(branded, resource_type="image", folder="nishahomes/inventory")
            else:
                up = cloudinary.uploader.upload(file, resource_type="image", folder="nishahomes/inventory")
            media_urls.append(up.get("secure_url"))
            media_public_ids.append(up.get("public_id"))

        for file in request.files.getlist("videos"):
            if not file or not file.filename:
                continue
            if branding:
                try:
                    branded_video = build_branded_video(file.read(), brand_fields)
                    up = cloudinary.uploader.upload(branded_video, resource_type="video", folder="nishahomes/inventory")
                except Exception as vid_err:
                    print(f"[branding] video branding failed, uploading original: {vid_err}")
                    file.seek(0)
                    up = cloudinary.uploader.upload(file, resource_type="video", folder="nishahomes/inventory")
            else:
                up = cloudinary.uploader.upload(file, resource_type="video", folder="nishahomes/inventory")
            media_urls.append(up.get("secure_url"))
            media_public_ids.append(up.get("public_id"))

        pdf_url = None
        pdf_file = request.files.get("pdf")
        if pdf_file and pdf_file.filename:
            up = cloudinary.uploader.upload(pdf_file, resource_type="raw", folder="nishahomes/inventory/docs")
            pdf_url = up.get("secure_url")

        inventory_data = {
            "listingBasis": f("listingBasis", ""),
            "dealType": f("dealType", ""),
            "category": f("propertyType", ""),
            "name": f("propertyTitle", ""),
            "location": f("locality", ""),
            "configuration": f("configuration", ""),
            "furnishing": f("furnishing", ""),
            "areaUnit": f("areaUnit", "sqft"),
            "carpetArea": f("carpetArea", ""),
            "superArea": f("superArea", ""),
            "floor": f("floor", ""),
            "bathrooms": f("bathrooms", ""),
            "facing": f("facing", ""),
            "parking": f("parking", ""),
            "possession": f("possession", ""),
            "budget": f("price", ""),
            "landingPageLink": f("landingPageLink", ""),
            "mapLink": f("mapLink", ""),
            "quickNotes": f("quickNotes", ""),
            "description": f("description", ""),
            "img": media_urls[0] if media_urls else None,
            "mediaUrls": media_urls,
            "mediaPublicIds": media_public_ids,
            "pdfUrl": pdf_url,
            "type": "inventory",
            "status": "pending" if session.get("role") == "partner" else "approved",
            "ownerNumber": session.get("employee_number"),
            "ownerName": session.get("employee_name"),
            "ownerRole": session.get("role"),
            "createdAt": datetime.utcnow()
        }

        embed_and_attach(inventory_data)
        result = projects_collection.insert_one(inventory_data)
        return jsonify({"status": "success", "id": str(result.inserted_id)}), 201

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


# PUT/POST-style update - allows editing fields and/or replacing media
@app.route("/api/projects/update/<project_id>", methods=["POST"])
def update_project(project_id):
    try:
        if not session.get("user_id"):
            return jsonify({"status": "error", "message": "Login required"}), 401

        query = {"_id": ObjectId(project_id)}
        # Partners can only edit their own listings
        if session.get("role") == "partner":
            query["ownerNumber"] = session.get("employee_number")

        project = projects_collection.find_one(query)
        if not project:
            return jsonify({"status": "error", "message": "Project not found or not yours"}), 404

        update_fields = {}
        for field in ["name", "location", "description", "budget", "category"]:
            val = request.form.get(field)
            if val:
                update_fields[field] = val

        file = request.files.get("media")
        if file and file.filename:
            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            video_exts = {"mp4", "mov", "avi", "webm", "mkv"}
            resource_type = "video" if ext in video_exts else "image"

            # Remove old asset from Cloudinary before uploading the new one
            old_public_id = project.get("mediaPublicId")
            if old_public_id:
                try:
                    cloudinary.uploader.destroy(
                        old_public_id,
                        resource_type=project.get("type", "image")
                    )
                except Exception as cerr:
                    print("Cloudinary delete failed:", cerr)

            upload_result = cloudinary.uploader.upload(
                file, resource_type=resource_type, folder="nishahomes/projects"
            )
            update_fields["img"] = upload_result.get("secure_url")
            update_fields["mediaUrl"] = upload_result.get("secure_url")
            update_fields["mediaPublicId"] = upload_result.get("public_id")
            update_fields["type"] = resource_type

        if not update_fields:
            return jsonify({"status": "error", "message": "No fields to update"}), 400

        update_fields["lastUpdatedAt"] = datetime.utcnow()
        merged_preview = {**project, **update_fields}
        update_fields["embedding"] = get_embedding(build_inventory_embedding_text(merged_preview))
        projects_collection.update_one({"_id": ObjectId(project_id)}, {"$set": update_fields})

        updated = projects_collection.find_one({"_id": ObjectId(project_id)})
        return jsonify({"status": "success", "data": serialize_doc(updated)}), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


# delete a project (and its Cloudinary asset)
@app.route("/api/projects/delete/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    try:
        if not session.get("user_id"):
            return jsonify({"status": "error", "message": "Login required"}), 401

        query = {"_id": ObjectId(project_id)}
        # Partners can only delete their own listings
        if session.get("role") == "partner":
            query["ownerNumber"] = session.get("employee_number")

        project = projects_collection.find_one(query)
        if not project:
            return jsonify({"status": "error", "message": "Project not found or not yours"}), 404

        public_id = project.get("mediaPublicId")
        if public_id:
            try:
                cloudinary.uploader.destroy(
                    public_id,
                    resource_type=project.get("type", "image")
                )
            except Exception as cerr:
                print("Cloudinary delete failed:", cerr)

        projects_collection.delete_one({"_id": ObjectId(project_id)})
        return jsonify({"status": "success", "message": "Project deleted"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



# Approve / send-back a pending listing. Admin only.
@app.route("/api/projects/approve/<project_id>", methods=["POST"])
def approve_project(project_id):
    try:
        if not session.get("user_id") or session.get("role") != "admin":
            return jsonify({"status": "error", "message": "Admin login required"}), 403

        project = projects_collection.find_one({"_id": ObjectId(project_id)})
        if not project:
            return jsonify({"status": "error", "message": "Project not found"}), 404

        action = (request.json or {}).get("action", "approve")  # "approve" | "reject"
        new_status = "approved" if action == "approve" else "pending"

        projects_collection.update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {
                "status": new_status,
                "reviewedBy": session.get("employee_name"),
                "reviewedAt": datetime.utcnow()
            }}
        )

        updated = projects_collection.find_one({"_id": ObjectId(project_id)})
        return jsonify({"status": "success", "data": serialize_doc(updated)}), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/add-project")
def add_project_page():
    return render_template("upload_project.html")


@app.route("/add-inventory")
def add_inventory_page():
    if not session.get("user_id"):
        return redirect("/")
    return render_template("add_inventory.html")


@app.route("/api/add-end-data", methods=["POST"])
def add_end_data():
    try:
        data = request.json

        if not data:
            return jsonify({"error": "No JSON body provided"}), 400

        collection_name = data.get("collection")

        if collection_name != "endData":
            return jsonify({"error": "Invalid collection"}), 400

        collection = db["endData"]

        raw_number = data.get("Number")

        if not raw_number:
            return jsonify({"error": "Number is required"}), 400

        # Normalize number
        number = normalize_number(raw_number)

        if not number:
            return jsonify({"error": "Invalid number"}), 400

        # Ensure Indian format (add 91 if missing)
        if not number.startswith("91"):
            number = "91" + number

        # Clean payload
        data.pop("collection", None)
        data["Number"] = number

        # Remove empty / None values (IMPORTANT)
        clean_data = {k: v for k, v in data.items() if v not in [None, "", []]}

        # Add timestamp
        clean_data["lastUpdatedAt"] = datetime.utcnow()

        # Check if record exists
        existing_doc = collection.find_one({"Number": number})

        if existing_doc:
            # Update ONLY provided fields
            update_query = {
                "$set": clean_data,
                "$inc": {"Call_attempt": int(data.get("Call_attempt", 1))}
            }

            collection.update_one({"Number": number}, update_query)

            message = "Data updated successfully"

        else:
            # Insert new document
            clean_data["Call_attempt"] = int(data.get("Call_attempt", 1))

            collection.insert_one(clean_data)

            message = "Data inserted successfully"

        # Fetch updated doc
        updated_doc = collection.find_one({"Number": number})

        return jsonify({
            "success": True,
            "message": message,
            "data": serialize_doc(updated_doc)
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/get-lead-by-number", methods=["POST"])
def get_lead_by_number():
    collection = db["endData"]

    data = request.get_json()

    if not data or ("number" not in data and "leadId" not in data):
        return jsonify({"error": "number or leadId is required"}), 400

    lead_id = data.get("leadId")
    number = data.get("number")

    lead = None

    # Try matching by LeadId (the Mongo _id of the Lead doc) first
    if lead_id:
        lead = collection.find_one({"LeadId": str(lead_id)})

    # Fallback: match through number (91number), same as before
    if not lead and number:
        normalized = normalize_number(number)
        if normalized and not normalized.startswith("91"):
            normalized = "91" + normalized

        try:
            lead = collection.find_one({"Number": int(normalized)})
        except:
            lead = collection.find_one({"Number": normalized})

        if not lead:
            try:
                lead = collection.find_one({"Number": int(number)})
            except:
                pass
        if not lead:
            lead = collection.find_one({"Number": str(number)})

    if lead:
        return jsonify({
            "success": True,
            "data": serialize_doc(lead)
        })

    return jsonify({
        "success": False,
        "error": "Lead not found"
    }), 404


# =============================
# DAI (Disable-AI) collection
# =============================

@app.route("/api/toggle-ai", methods=["POST"])
def toggle_ai():
    """
    Toggles whether AI is disabled for a lead.
    """
    try:
        data = request.json or {}
        lead_id = data.get("leadId")
        phone = data.get("phone")
        name = data.get("name")

        if not lead_id:
            return jsonify({"success": False, "message": "leadId is required"}), 400

        lead_id = str(lead_id)
        existing = dai_collection.find_one({"leadId": lead_id})

        if existing:
            dai_collection.delete_one({"leadId": lead_id})
            return jsonify({
                "success": True,
                "disabled": False,
                "message": "AI re-enabled for this lead"
            })
        else:
            normalized_phone = normalize_number(phone)
            if normalized_phone and not normalized_phone.startswith("91"):
                normalized_phone = "91" + normalized_phone

            doc = {
                "leadId": lead_id,
                "Phone Number": normalized_phone,
                "Lead Name": name or "",
                "createdAt": datetime.utcnow()
            }
            dai_collection.insert_one(doc)
            return jsonify({
                "success": True,
                "disabled": True,
                "message": "AI disabled for this lead"
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/dai-list", methods=["GET"])
def get_dai_list():
    """Returns every lead currently DAI-flagged (AI disabled)."""
    try:
        docs = list(dai_collection.find())
        return jsonify([serialize_doc(d) for d in docs])
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def remove_assign_to_from_leads():
    """
    ONE-TIME CLEANUP: strips AssignTo (and related assignment fields)
    from every document in the Leads collection.
    Run once, then delete this function and its call below.
    """
    result = db["Leads"].update_many(
        {},
        {
            "$unset": {
                "AssignTo": "",
                "AssignToNumber": "",
                "AssignedBy": "",
                "AssignedByNumber": "",
                "AssignedAt": ""
            }
        }
    )
    print(f"[cleanup] matched: {result.matched_count}, modified: {result.modified_count}")



# =============================
# ADMIN: "Manage Project" — Add Project (multi-photo/video, AI-fillable)
# Saves into the SAME projects_collection as inventory, tagged kind="project"
# Branding + screenshot-OCR aware, same as upload_inventory
# =============================
@app.route("/api/projects/upload-project", methods=["POST"])
def upload_project_v2():
    try:
        if not session.get("user_id"):
            return jsonify({"status": "error", "message": "Login required"}), 401

        f = request.form.get
        required = ["name", "location", "propertyType"]
        if not all(f(k) for k in required):
            return jsonify({"status": "error", "message": "Project name, location and property type are required"}), 400

        video_links_raw = f("videoLinks", "") or ""
        video_links = [v.strip() for v in video_links_raw.splitlines() if v.strip()]

        # NEW: branding toggle + fields used to render the overlay onto photos
        branding = request.form.get("branding") == "true"
        brand_fields = {
            "dealType": "New Launch", "propertyTitle": f("name", ""),
            "locality": f("location", ""), "price": f("startingPrice", ""),
            "configuration": f("configuration", ""), "superArea": ""
        }

        media_urls, media_public_ids = [], []
        has_image, has_video = False, False

        for file in request.files.getlist("photos"):
            if not file or not file.filename:
                continue
            if branding:
                branded = build_branded_image(file.read(), brand_fields)
                up = cloudinary.uploader.upload(branded, resource_type="image", folder="nishahomes/projects")
            else:
                up = cloudinary.uploader.upload(file, resource_type="image", folder="nishahomes/projects")
            media_urls.append(up.get("secure_url"))
            media_public_ids.append(up.get("public_id"))
            has_image = True

        for file in request.files.getlist("videos"):
            if not file or not file.filename:
                continue
            if branding:
                try:
                    branded_video = build_branded_video(file.read(), brand_fields)
                    up = cloudinary.uploader.upload(branded_video, resource_type="video", folder="nishahomes/projects")
                except Exception as vid_err:
                    print(f"[branding] video branding failed, uploading original: {vid_err}")
                    file.seek(0)
                    up = cloudinary.uploader.upload(file, resource_type="video", folder="nishahomes/projects")
            else:
                up = cloudinary.uploader.upload(file, resource_type="video", folder="nishahomes/projects")
            media_urls.append(up.get("secure_url"))
            media_public_ids.append(up.get("public_id"))
            has_video = True

        pdf_url = None
        pdf_file = request.files.get("pdf")
        if pdf_file and pdf_file.filename:
            up = cloudinary.uploader.upload(pdf_file, resource_type="raw", folder="nishahomes/projects/docs")
            pdf_url = up.get("secure_url")

        starting_price = f("startingPrice", "")

        project_data = {
            "kind": "project",                     # distinguishes from partner "inventory" listings
            "name": f("name", ""),
            "location": f("location", ""),
            "propertyType": f("propertyType", ""),
            "category": f("propertyType", ""),     # kept for compatibility with existing card/filter code
            "possession": f("possession", ""),
            "configuration": f("configuration", ""),
            "startingPrice": starting_price,
            "budget": starting_price,              # kept for compatibility with existing card display
            "description": f("description", ""),
            "videoLinks": video_links,
            "img": media_urls[0] if media_urls else None,
            "mediaUrl": media_urls[0] if media_urls else None,
            "mediaUrls": media_urls,
            "mediaPublicIds": media_public_ids,
            "type": "video" if (has_video and not has_image) else "image",
            "pdfUrl": pdf_url,
            "status": "approved",
            "ownerNumber": session.get("employee_number"),
            "ownerName": session.get("employee_name"),
            "ownerRole": session.get("role"),
            "createdAt": datetime.utcnow()
        }

        embed_and_attach(project_data)
        result = projects_collection.insert_one(project_data)
        return jsonify({"status": "success", "id": str(result.inserted_id)}), 201

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


#embddings

# =============================
# VECTOR SEARCH / RAG - INVENTORY EMBEDDINGS
# =============================
MISTRAL_EMBED_URL = "https://api.mistral.ai/v1/embeddings"
VECTOR_INDEX_NAME = "searchdata"  # must match your Atlas Search index name exactly


def get_embedding(text: str):
    """Returns a 1024-dim embedding vector for the given text using Mistral, or None on failure."""
    if not text or not text.strip():
        return None
    if not MISTRAL_API_KEY:
        print("[embed] MISTRAL_API_KEY not configured")
        return None
    try:
        resp = requests.post(
            MISTRAL_EMBED_URL,
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json={"model": "mistral-embed", "input": [text[:8000]]},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"[embed] failed: {e}")
        return None


def build_inventory_embedding_text(doc: dict) -> str:
    """Flattens the fields that matter for semantic search into one text blob."""
    parts = [
        doc.get("name", ""),
        doc.get("category", ""),
        doc.get("propertyType", ""),
        doc.get("location", ""),
        doc.get("configuration", ""),
        doc.get("furnishing", ""),
        doc.get("possession", ""),
        doc.get("dealType", ""),
        doc.get("listingBasis", ""),
        doc.get("budget", "") or doc.get("startingPrice", ""),
        doc.get("description", ""),
        doc.get("quickNotes", "")
    ]
    return " | ".join(str(p) for p in parts if p)


def embed_and_attach(doc: dict) -> dict:
    """Mutates doc in-place, adding an 'embedding' field. Returns doc for convenience."""
    text = build_inventory_embedding_text(doc)
    doc["embedding"] = get_embedding(text)
    return doc


def backfill_inventory_embeddings():
    """ONE-TIME: embeds every existing project/inventory doc missing an 'embedding' field."""
    docs = list(projects_collection.find({"embedding": {"$exists": False}}))
    print(f"[backfill] {len(docs)} docs need embedding")
    for d in docs:
        text = build_inventory_embedding_text(d)
        emb = get_embedding(text)
        if emb:
            projects_collection.update_one({"_id": d["_id"]}, {"$set": {"embedding": emb}})
            print(f"[backfill] embedded: {d.get('name')}")
        else:
            print(f"[backfill] SKIPPED (no text/embed failed): {d.get('name')}")


@app.route("/api/ai/backfill-embeddings", methods=["POST"])
def api_backfill_embeddings():
    """Manually trigger backfill via HTTP instead of editing __main__. Admin only."""
    if session.get("role") != "admin":
        return jsonify({"success": False, "message": "Admin only"}), 403
    try:
        backfill_inventory_embeddings()
        return jsonify({"success": True, "message": "Backfill complete, check server logs"}), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/ai/search-inventory", methods=["POST"])
def search_inventory():
    """
    RAG search endpoint for the WhatsApp AI bot (n8n etc.) to call.
    Body: { "query": "3bhk under 80 lakhs in Sidon", "limit": 5,
            "propertyType": "Apartment", "dealType": "For Sale" }  <- filters optional
    """
    try:
        data = request.json or {}
        query_text = (data.get("query") or "").strip()
        limit = int(data.get("limit", 5))

        if not query_text:
            return jsonify({"success": False, "message": "query is required"}), 400

        query_vector = get_embedding(query_text)
        if not query_vector:
            return jsonify({"success": False, "message": "Could not embed query"}), 500

        match_filter = {"status": "approved"}
        if data.get("propertyType"):
            match_filter["category"] = data["propertyType"]
        if data.get("dealType"):
            match_filter["dealType"] = data["dealType"]

        pipeline = [
            {
                "$vectorSearch": {
                    "index": VECTOR_INDEX_NAME,
                    "path": "embedding",
                    "queryVector": query_vector,
                    "numCandidates": 100,
                    "limit": limit,
                    "filter": match_filter
                }
            },
            {
                "$project": {
                    "embedding": 0,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]

        results = list(projects_collection.aggregate(pipeline))
        return jsonify({"success": True, "data": [serialize_doc(r) for r in results]}), 200

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

# =============================
# CAMPAIGN BUILDER (OpenAI-backed, no Mongo)
# =============================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


@app.route("/campaign-builder")
def campaign_builder_page():
    if not session.get("user_id"):
        return redirect("/")
    # file must live at: templates/campaign-builder.html
    return render_template("campaign-builder.html")


@app.route("/api/ai/campaign", methods=["POST"])
def ai_campaign():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    if not OPENROUTER_API_KEY:
        return jsonify({"success": False, "message": "OPENROUTER_API_KEY not set in .env"}), 500

    resp = None
    try:
        data = request.json or {}
        system_prompt = data.get("system", "")
        user_prompt = data.get("prompt", "")
        max_tokens = int(data.get("max_tokens", 4000))
        temperature = float(data.get("temperature", 0.7))
        image = data.get("image")  # optional: {"media_type": "...", "data": "<base64>"}

        if not user_prompt:
            return jsonify({"success": False, "message": "prompt is required"}), 400

        user_content = []
        if image and image.get("data"):
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.get('media_type', 'image/png')};base64,{image['data']}"
                }
            })
        user_content.append({"type": "text", "text": user_prompt})

        payload = {
            "model": data.get("model") or "openai/gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        resp = requests.post(
            OPENROUTER_API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://nishahomes.com",   # OpenRouter recommends/requires this
                "X-Title": "Nisha Homes Campaign Builder"
            },
            json=payload,
            timeout=90
        )
        resp.raise_for_status()
        result = resp.json()
        text = result["choices"][0]["message"]["content"]

        return jsonify({"success": True, "text": text})

    except requests.exceptions.HTTPError as e:
        try:
            err_body = resp.json() if resp is not None else {}
        except Exception:
            err_body = {}
        msg = (err_body.get("error") or {}).get("message") or str(e)
        status = resp.status_code if resp is not None else 500
        return jsonify({"success": False, "message": msg}), status
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500


if __name__ == "__main__":
    #remove_assign_to_from_leads()
    app.run(host="0.0.0.0", port=8000)