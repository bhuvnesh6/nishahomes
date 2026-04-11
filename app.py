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
import tempfile
import cv2
import os
import time
from flask import session
import random
import requests
from datetime import timedelta


# Load env
load_dotenv()

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=60) 
app.secret_key = "supersecretkey"

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

projects_collection = db["project"]

# 🔥 Helper function (YOU WERE MISSING THIS)
def serialize_doc(doc):
    doc["_id"] = str(doc["_id"])
    
    for key, value in doc.items():
        if isinstance(value, float) and math.isnan(value):
            doc[key] = None
    
    return doc


def get_collection_data(collection_name):
    collection = db[collection_name]
    data = list(collection.find())
    return [serialize_doc(doc) for doc in data]



@app.route("/")
def loginpage():
    # ✅ If already logged in → redirect based on role
    if "user_id" in session:
        role = session.get("role")

        if role == "admin":
            return redirect("/admin")
        elif role == "emp":
            return redirect("/emp")
        else:
            # fallback (invalid role)
            session.clear()
            return redirect("/")

    # ✅ Not logged in → show login page
    return render_template("index.html")


#login system
@app.route("/login", methods=["POST"])
def login():
    number = request.form.get("number")

# clean + convert
    number = number.replace("+", "").strip()

    try:
     number = int(number)
    except:
     flash("Invalid phone number format")
     return redirect("/")
    password = request.form.get("password")

    remember = request.form.get("remember")

    
    
    collection = db["teamAssign"]

    user = collection.find_one({
        "Employee number": number,
        "password": password
    })

    if user:
        # Store session
        session["user_id"] = str(user["_id"])
        session["role"] = user.get("roll")
        session["employee_name"] = user.get("Employee name")
        session["employee_number"] = user.get("Employee number")

        if remember:
         session.permanent = True
        else:
         session.permanent = False
    
        # Redirect based on role
        if user.get("roll") == "admin":
            return redirect("/admin")
        elif user.get("roll") == "emp":
            return redirect("/emp")
        else:
            flash("Invalid role")
            return redirect("/")
    else:
        flash("Invalid number or password")
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
    # ✅ Only check if logged in
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

        # 🔥 FIX HERE
        lead = clean_nan(lead)

        return jsonify(lead), 200

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

@app.route("/api/end-data")
def end_data():
    return jsonify(get_collection_data("endData"))

@app.route("/api/selling-leads")
def selling_leads():
    return jsonify(get_collection_data("sellingLeads"))

@app.route("/api/end-data")
def get_end_data():

    collection = db["endData"]   # ✅ define collection

    number = request.args.get("number")

    # 🔎 If number provided → return single lead
    if number:
        try:
            lead = collection.find_one({"Number": int(number)})
        except:
            lead = collection.find_one({"Number": number})

        if lead:
            return jsonify(serialize_doc(lead))
        return jsonify({"error": "Not found"}), 404

    # 📦 If no number → return all leads
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

        if not name or not number:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        # ✅ CLEAN NUMBER
        number = str(number).strip()
        number = number.replace("+", "")
        number = "".join(filter(str.isdigit, number))

        if not number:
            return jsonify({"success": False, "message": "Invalid phone number"}), 400

        # ✅ CONVERT TO INT64
        number = int(number)

        collection = db["teamAssign"]

        # ✅ prevent duplicate
        existing = collection.find_one({"Employee number": number})
        if existing:
            return jsonify({"success": False, "message": "Employee already exists"}), 400

        # ✅ GENERATE PASSWORD
        clean_name = name.lower().replace(" ", "")
        rand_digits = random.randint(100, 9999)  # 3–4 digits
        password = f"{clean_name}@{rand_digits}"

        new_member = {
            "Employee name": name,
            "Employee number": number,
            "password": password,
            "roll": role,  # ✅ as requested (roll, not role)
            "Leads": [],
            "Active": True
        }

        collection.insert_one(new_member)

        # ✅ SEND TO N8N WEBHOOK
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

        # ✅ CLEAN + CONVERT SAME AS ADD
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

from flask import request

@app.route("/api/assign-lead", methods=["POST"])
def assign_lead():
    try:
        data = request.json

        collection_name = data.get("collection")
        raw_number = data.get("leadNumber")
        assign_to = data.get("assignTo")

        if not collection_name or not raw_number or not assign_to:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        lead_collection = db[collection_name]
        employee_collection = db["teamAssign"]

        # Normalize number
        lead_number = normalize_number(raw_number)

        # 1️⃣ Update Lead using REGEX match (handles all formats)
        lead_result = lead_collection.update_one(
            {
                "Phone Number": {
                    "$regex": lead_number
                }
            },
            {"$set": {"AssignTo": assign_to}}
        )

        if lead_result.matched_count == 0:
            return jsonify({"success": False, "message": "Lead not found"}), 404

        # 2️⃣ Update Employee Lead List
        employee = employee_collection.find_one({"Employee name": assign_to})

        if not employee:
            return jsonify({"success": False, "message": "Employee not found"}), 404

        existing_leads = employee.get("Leads", "")

        if existing_leads:
            clean = existing_leads.strip("{}")
            leads_list = [x.strip() for x in clean.split(",") if x.strip()]
        else:
            leads_list = []

        formatted_number = f"+{lead_number}"

        if formatted_number not in leads_list:
            leads_list.append(formatted_number)

        new_leads_string = "{" + ", ".join(leads_list) + "}"

        employee_collection.update_one(
            {"_id": employee["_id"]},
            {"$set": {"Leads": new_leads_string}}
        )

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/api/bulk-assign-leads", methods=["POST"])
def bulk_assign_leads():
    try:
        data = request.json

        collection_name = data.get("collection")
        lead_numbers = data.get("leadNumbers")
        assign_to = data.get("assignTo")

        if not collection_name or not lead_numbers or not assign_to:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        lead_collection = db[collection_name]
        employee_collection = db["teamAssign"]

        # Normalize all numbers
        cleaned_numbers = [normalize_number(num) for num in lead_numbers]

        # 1️⃣ Update leads one by one using regex (safer for mixed formats)
        matched_count = 0

        for number in cleaned_numbers:
            result = lead_collection.update_one(
                {
                    "Phone Number": {
                        "$regex": number
                    }
                },
                {"$set": {"AssignTo": assign_to}}
            )
            matched_count += result.matched_count

        if matched_count == 0:
            return jsonify({"success": False, "message": "No leads matched"}), 404

        # 2️⃣ Update Employee Lead List
        employee = employee_collection.find_one({"Employee name": assign_to})

        if not employee:
            return jsonify({"success": False, "message": "Employee not found"}), 404

        existing_leads = employee.get("Leads", "")

        if existing_leads:
            clean = existing_leads.strip("{}")
            leads_list = [x.strip() for x in clean.split(",") if x.strip()]
        else:
            leads_list = []

        for number in cleaned_numbers:
            formatted = f"+{number}"
            if formatted not in leads_list:
                leads_list.append(formatted)

        new_leads_string = "{" + ", ".join(leads_list) + "}"

        employee_collection.update_one(
            {"_id": employee["_id"]},
            {"$set": {"Leads": new_leads_string}}
        )

        return jsonify({
            "success": True,
            "assignedCount": matched_count
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


def normalize_number(number):
    if not number:
        return ""

    number = str(number).strip()
    number = number.replace("+", "")
    number = number.replace("@s.whatsapp.net", "")
    number = number.replace("@c.us", "")
    number = "".join(filter(str.isdigit, number))

    return number


#reassign function

import re

@app.route("/api/reassign-lead", methods=["POST"])
def reassign_lead():
    try:
        data = request.json

        phone = data.get("phone")
        new_employee_number = data.get("newEmployeeNumber")
        collection_name = data.get("collection")

        # 🔒 Validate input
        if not phone or not new_employee_number or not collection_name:
            return jsonify({"error": "Missing required fields"}), 400

        phone = phone.replace("+", "").strip()
        formatted_phone = f"+{phone}"

        team_collection = db["teamAssign"]
        lead_collection = db[collection_name]

        # 🔥 ESCAPE REGEX PROPERLY
        safe_phone_regex = re.escape(formatted_phone)

        # 1️⃣ Find current employee safely
        current_employee = team_collection.find_one({
            "Leads": {"$regex": safe_phone_regex}
        })

        if not current_employee:
            return jsonify({"error": "Lead not found in any employee"}), 404

        # 2️⃣ Remove lead from old employee
        old_leads_string = current_employee.get("Leads", "{}")

        old_list = old_leads_string.strip("{}").split(",")
        old_list = [l.strip() for l in old_list if l.strip()]

        updated_old_list = [l for l in old_list if l != formatted_phone]

        new_old_string = "{" + ", ".join(updated_old_list) + "}"

        team_collection.update_one(
            {"_id": current_employee["_id"]},
            {"$set": {"Leads": new_old_string}}
        )

        # 3️⃣ Add lead to new employee
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

        # 4️⃣ Update AssignTo in Lead document safely
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

        # 🔥 Magic happens here
        result = end_collection.update_one(
            {"Number": number},          # Find by number
            {
                "$inc": {"Call_attempt": 1},   # Increment by 1
                "$setOnInsert": {"Number": number}
            },
            upsert=True   # If not found → create document
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


db = client["NishaHomesData"]
collection = db["endData"]


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

        # 🔥 Fields we want to update (excluding _id, Number, Call_attempt)
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

        result = collection.update_one(
            {"Number": number},   # 🔎 Find by Number
            {
                "$set": update_fields,
                "$inc": {"Call_attempt": 1}  # 🔥 increment safely
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

        # 1️⃣ Get collection name dynamically
        collection_name = data.get("collection")
        if not collection_name:
            return jsonify({"error": "Collection name is required"}), 400

        collection = db[collection_name]

        # 2️⃣ Extract phone number (required for upsert)
        phone_number = data.get("Phone Number")
        if not phone_number:
            return jsonify({"error": "Phone Number is required"}), 400

        # 3️⃣ Remove collection key from document
        data.pop("collection", None)

        # 4️⃣ Upsert (update if exists, insert if not)
        result = collection.update_one(
            {"Phone Number": phone_number},
            {"$set": data},
            upsert=True
        )

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

        # 1️⃣ Validate collection
        collection_name = data.get("collection")
        if not collection_name:
            return jsonify({"error": "Collection name is required"}), 400

        # 🔒 Optional: restrict collections (recommended)
        allowed_collections = [
            "Leads",
            "RentalLeads",
            "sellingLeads",
            "agentLeads",
            "endData",
            "teamAssign",
            "orderhouseofcakes", 
            "tasks",
        ]

        if collection_name not in allowed_collections:
            return jsonify({"error": "Invalid collection"}), 400

        collection = db[collection_name]

        # 2️⃣ Validate phone / number
        raw_number = data.get("Phone Number") or data.get("Number")

        if not raw_number:
            return jsonify({"error": "Phone Number or Number is required"}), 400

        # Normalize (uses your existing function)
        normalized_number = normalize_number(raw_number)

        if not normalized_number:
            return jsonify({"error": "Invalid phone number"}), 400

        # 3️⃣ Build filter dynamically
        if collection_name == "endData":
            filter_query = {"Number": normalized_number}
        else:
            filter_query = {
                "Phone Number": {
                    "$regex": normalized_number
                }
            }

        # 4️⃣ Build update operations
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

        # 5️⃣ Perform update (Upsert allowed)
        result = collection.update_one(
            filter_query,
            update_query,
            upsert=True
        )

        # 6️⃣ Return updated document
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
    
    
    
# Endpoint 1: Image -> Text
@app.route('/image-to-text', methods=['POST'])
def image_to_text():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    temp = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
    temp.close()  # 🔥 IMPORTANT

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

    # 🔥 TEMP VIDEO (not permanent)
    temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    temp_video.close()
    file.save(temp_video.name)

    # 🔥 FINAL AUDIO (saved in uploads)
    audio_filename = f"{int(time.time())}.mp3"
    audio_path = os.path.join(app.config["UPLOAD_FOLDER"], audio_filename)

    # 🎬 Extract audio
    extract_audio_from_video(temp_video.name, audio_path)

    # 🧹 Delete temp video
    os.remove(temp_video.name)

    # ✅ Return usable URL
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
        projects = list(projects_collection.find({}, {"_id": 0}))
        return jsonify({
            "status": "success",
            "count": len(projects),
            "data": projects
        }), 200
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500



# POST: Add new project
# -------------------------------
@app.route("/api/projects", methods=["POST"])
def add_project():
    try:
        data = request.get_json()

        # Required fields validation
        required_fields = ["name", "location", "description", "budget", "category", "img"]
        for field in required_fields:
            if field not in data:
                return jsonify({
                    "status": "error",
                    "message": f"{field} is required"
                }), 400

        # Insert into DB
        projects_collection.insert_one(data)

        return jsonify({
            "status": "success",
            "message": "Project added successfully"
        }), 201

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
