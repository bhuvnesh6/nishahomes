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

# NEW: Cloudinary (used for Inventory / project image & video uploads)
import cloudinary
import cloudinary.uploader
import cloudinary.api

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
PARTNER_ALLOWED_PATHS = {"/inventory", "/logout"}
PARTNER_ALLOWED_PREFIXES = ("/api/projects", "/uploads", "/static")

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



@app.route("/")
def loginpage():
    # If already logged in -> redirect based on role
    if "user_id" in session:
        role = session.get("role")

        if role == "admin":
            return redirect("/admin")
        elif role == "emp":
            return redirect("/emp")
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

    # FIX: previously this used its own ad-hoc cleanup
    # (number.replace("+", "").strip()), which only strips a leading "+"
    # and outer whitespace. Any other stray character (space, dash,
    # invisible autofill artifact, etc.) made int(number) either raise or
    # produce a value that didn't match what's stored in Mongo, so the
    # find_one() below silently returned None -> "Invalid number or
    # password" -> redirect("/"). That's exactly the 302 -> GET "/"
    # pattern you saw for the admin login while the partner login (whose
    # number happened to be "clean") succeeded.
    #
    # Now reuses the same normalize_number() helper used everywhere else
    # in the app, so admin and partner numbers are cleaned identically.
    number = normalize_number(raw_number)

    if not number:
        flash("Invalid phone number format")
        return redirect("/")

    try:
        number = int(number)
    except ValueError:
        flash("Invalid phone number format")
        return redirect("/")

    # FIX: also strip the password, in case of trailing/leading spaces
    # from autofill/copy-paste, which would otherwise cause a silent
    # match failure the same way.
    password = raw_password.strip()

    remember = request.form.get("remember")

    collection = db["teamAssign"]

    user = collection.find_one({
        "Employee number": number,
        "password": password
    })

    # DIAGNOSTIC: check `docker logs <container>` after a failed login.
    # If found_user=False, the DB this container is connected to simply
    # does not have a matching Employee number/password document — a
    # data/connection issue, not a code issue (see startup diagnostic
    # above, and double-check MONGO_URI/DB_NAME reach the container).
    print(f"[login] number={number} found_user={bool(user)}"
          + (f" roll={user.get('roll')!r}" if user else ""))

    if not user:
        flash("Invalid number or password")
        return redirect("/")

    # FIX: normalize the stored role too (strip + lowercase) so stray
    # whitespace or case differences in the DB don't silently fall
    # through to the "Invalid role" branch.
    role = (user.get("roll") or "").strip().lower()

    # Store session
    session["user_id"] = str(user["_id"])
    session["role"] = role
    session["employee_name"] = user.get("Employee name")
    session["employee_number"] = user.get("Employee number")
    session.permanent = bool(remember)

    # Redirect based on role
    if role == "admin":
        return redirect("/admin")
    elif role == "emp":
        return redirect("/emp")
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
    if not session.get("user_id") or session.get("role") != "admin":
        return redirect("/")

    return render_template(
        "admin.html",
        employee_name=session.get("employee_name"),
        employee_number=session.get("employee_number")
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


# NOTE: duplicate route path "/leadjourney" registered twice under two
# different view function names (leadjourney / lead_journey). Flask allows
# this (different endpoint names), so it is left as-is; only true syntax
# errors have been fixed in this pass.
@app.route("/leadjourney")
def lead_journey():
    return render_template("leadjourney.html")


# NEW: Inventory Dashboard page
# Accessible to admin, emp, and partner. Partners are locked to ONLY this
# route (see restrict_partner_access above); admin/emp can also reach it
# from their normal dashboards.
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

@app.route("/api/leads")
def leads():
    return jsonify(get_collection_data("Leads"))

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

@app.route("/api/rental-leads")
def rental_leads():
    return jsonify(get_collection_data("RentalLeads"))

@app.route("/api/agent-leads")
def agent_leads():
    return jsonify(get_collection_data("agentLeads"))

@app.route("/api/selling-leads")
def selling_leads():
    return jsonify(get_collection_data("sellingLeads"))

# NOTE: "/api/end-data" was originally defined twice (once as end_data()
# returning the raw collection dump, once as get_end_data() with the
# number-filter logic). Flask does not allow two view functions mapped to
# the same route+methods combo at import time in some configurations, and
# having both is redundant/confusing regardless. Kept only the more
# complete version (get_end_data) which supersedes the first.
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

        # NEW: validate role - now includes "partner"
        # Admin onboards partners from the same "Manage Team" flow; they
        # log in through the same /login form and get routed to /inventory.
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

        # CLEAN + CONVERT SAME AS ADD
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

@app.route('/api/assigned-leads')
def assigned_leads():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    role = session.get("role")
    employee_number = session.get("employee_number")  # stored as int at login time

    # Assigned leads aren't a separate collection — they're documents
    # inside these four collections that have an AssignTo/AssignToNumber
    # field set (by /api/assign-lead or /api/bulk-assign-leads).
    collection_type_map = {
        "Leads": "buying",
        "RentalLeads": "rental",
        "sellingLeads": "selling",
        "agentLeads": "agent",
    }

    query = {"AssignTo": {"$exists": True, "$nin": [None, ""]}}

    # emp -> only their own assigned leads. admin -> everyone's.
    if role != "admin":
        if not employee_number:
            return jsonify({"success": False, "message": "No employee number in session"}), 400
        query["AssignToNumber"] = employee_number

    all_docs = []
    try:
        for collection_name, lead_type in collection_type_map.items():
            docs = db[collection_name].find(query)
            for d in docs:
                d = serialize_doc(d)
                d["_leadType"] = lead_type
                d["_collection"] = collection_name
                all_docs.append(d)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

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
    try:
        data = request.json
        number = data.get("number")

        if not number:
            return jsonify({"error": "Number is required"}), 400

        # Clean number
        number = str(number).replace("+", "").strip()

        end_collection = db["endData"]

        # Increment or create
        result = end_collection.update_one(
            {"Number": number},          # Find by number
            {
                "$inc": {"Call_attempt": 1},   # Increment by 1
                "$setOnInsert": {"Number": number}
            },
            upsert=True   # If not found -> create document
        )

        # Get updated count
        updated_doc = end_collection.find_one({"Number": number})

        return jsonify({
            "success": True,
            "Number": number,
            "Call_attempt": updated_doc.get("Call_attempt", 1)
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

        # Find the lead doc by phone (it already has a Mongo _id by
        # default), copy that _id onto endData as LeadId, and stamp who
        # made this call directly onto the Lead document (callBy field).
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
        return jsonify({"error": str(e)}), 500

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

    Hardened so a single bad record (missing/garbage phone, bad ObjectId,
    a transient Mongo hiccup on one collection, etc.) can never abort the
    whole export - it's skipped and logged, and the export still returns
    whatever rows it could successfully build.
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

                # Guard: an empty/too-short phone must never be used as a
                # regex filter - that would match arbitrary documents and
                # pull the wrong lead's data into this row.
                valid_phone = bool(phone) and len(phone) >= 8
                if valid_phone and not phone.startswith("91"):
                    phone = "91" + phone

                lead_doc = None

                # 1) Try by _id first - most reliable, no regex needed
                if lead_id:
                    try:
                        lead_doc = db[collection_name].find_one({"_id": ObjectId(lead_id)})
                    except Exception:
                        lead_doc = None

                # 2) Fall back to a safely-escaped phone regex match
                if not lead_doc and valid_phone:
                    try:
                        safe_phone = re.escape(phone)
                        lead_doc = db[collection_name].find_one(
                            {"Phone Number": {"$regex": safe_phone}}
                        )
                    except Exception:
                        lead_doc = None

                lead_doc = lead_doc or {}

                # end-data / call-logs lookups, also guarded individually
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
                # Never let one bad lead take down the whole export
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


# -----------------------------------------------------------------
# FIX: this used to be `db = client["NishaHomesData"]`, which
# silently REASSIGNED the global `db` used by every other route in
# this file (including /api/export-leads). That meant every route
# below this point - and, more importantly, every route ABOVE it that
# ran after this module finished loading - was querying whatever
# database "NishaHomesData" is, instead of the one configured via
# DB_NAME in your .env. This is almost certainly why exports (and
# other reads) sometimes silently returned nothing.
#
# We now use dedicated variable names so /update-lead keeps working
# exactly as before, without touching the shared `db` / `collection`
# names used everywhere else.
# -----------------------------------------------------------------
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

        # 3. Remove collection key from document
        data.pop("collection", None)

        # 4. Upsert (update if exists, insert if not)
        result = collection.update_one(
            {"Phone Number": phone_number},
            {"$set": data},
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
# NOTE: this route calls extract_audio_from_video(), which comes from a
# commented-out import ("#from video_to_audio import extract_audio_from_video")
# at the top of the file. It is a syntax-valid function, but will raise a
# NameError at request time unless that import is restored. Left as-is
# structurally since re-enabling it is a behavior change, not a syntax fix.
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

# NOTE: this route uses cv2, which comes from a commented-out import
# ("#import cv2") at the top of the file. It is syntax-valid but will raise
# a NameError at request time unless that import is restored.
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

# REPLACED: now supports partner-scoped visibility.
# - admin / emp -> sees every project (all partners + their own uploads)
# - partner     -> sees ONLY their own projects (matched on employee_number)
# - no session  -> public read (e.g. marketing site), sees everything


# REPLACED: now supports partner-scoped visibility.
# - admin / emp -> sees every project (all partners + their own uploads)
# - partner     -> sees ONLY their own projects (matched on employee_number)
# - no session  -> public read (e.g. marketing site), sees everything
@app.route("/api/projects", methods=["GET"])
def get_projects():
    try:
        role = session.get("role")
        query = {}

        if role == "partner":
            query = {"ownerNumber": session.get("employee_number")}

        # ?status=approved / ?status=pending filter
        status_filter = request.args.get("status")
        if status_filter:
            query["status"] = status_filter

        projects = list(projects_collection.find(query).sort("createdAt", -1))

        # FIX: wrap response in {success, data} instead of returning a bare
        # array. Both inventory_dash.html (`allProjects = d.data || []`)
        # and admin.html's Send-Property modal (`spProjects = d.data || []`)
        # read `.data` off the response — a bare array made `.data`
        # always undefined, so listings never rendered no matter their
        # status. This was the only bug; the query/filter logic above was
        # already correct.
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
        # staff can attribute this to a partner, or keep it as their own
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

# -------------------------------
# REPLACED: POST /api/projects/upload
# Now uploads image/video straight to Cloudinary instead of local disk,
# and tags the resulting document with who uploaded it so partners only
# ever see/manage their own inventory.
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

        # Save in DB
        # partner uploads start "pending" and need admin approval.
        # admin/emp uploads are auto-approved since staff added them directly.
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
    - If the lead is NOT in the DAI collection -> insert it (AI disabled).
    - If the lead IS in the DAI collection -> remove it (AI re-enabled).
    Keyed primarily by leadId (Mongo _id of the lead doc, as a string),
    with Phone Number / Lead Name stored alongside for reference/debugging.
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
            # Currently disabled -> remove -> AI re-enabled
            dai_collection.delete_one({"leadId": lead_id})
            return jsonify({
                "success": True,
                "disabled": False,
                "message": "AI re-enabled for this lead"
            })
        else:
            # Currently enabled -> insert -> AI disabled
            normalized_phone = normalize_number(phone)  # strips '+', spaces, @s.whatsapp.net etc.
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)