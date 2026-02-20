from flask import Flask, render_template, request, redirect, flash, jsonify, send_from_directory
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

# Load env
load_dotenv()

app = Flask(__name__)
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
def home():
    return render_template("upload.html")

@app.route("/admin")
def admin():
    return render_template("admin.html")

@app.route("/assign")
def assign():
    return render_template("assign.html")


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

        if not name or not number:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        collection = db["teamAssign"]

        # prevent duplicate
        existing = collection.find_one({"Employee number": number})
        if existing:
            return jsonify({"success": False, "message": "Employee already exists"}), 400

        new_member = {
            "Employee name": name,
            "Employee number": number,
            "Leads": [],
            "Active": True
        }

        collection.insert_one(new_member)

        return jsonify({"success": True, "message": "Team member added successfully"})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/api/remove-team-assign/<number>", methods=["DELETE"])
def remove_team_member(number):
    try:
        collection = db["teamAssign"]

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
        lead_number = str(data.get("leadNumber")).replace("+", "").strip()
        assign_to = data.get("assignTo")

        if not collection_name or not lead_number or not assign_to:
            return jsonify({"success": False, "message": "Missing fields"}), 400

        lead_collection = db[collection_name]
        employee_collection = db["teamAssign"]

        # 1️⃣ Update Lead AssignTo
        lead_result = lead_collection.update_one(
            {"Phone Number": lead_number},
            {"$set": {"AssignTo": assign_to}}
        )

        if lead_result.matched_count == 0:
            return jsonify({"success": False, "message": "Lead not found"}), 404

        # 2️⃣ Update Employee Leads List
        employee = employee_collection.find_one({"Employee name": assign_to})

        if not employee:
            return jsonify({"success": False, "message": "Employee not found"}), 404

        existing_leads = employee.get("Leads")

        formatted_number = f"+{lead_number}"

        if existing_leads:
            # remove brackets
            clean = existing_leads.strip("{}")
            leads_list = [x.strip() for x in clean.split(",") if x.strip()]
        else:
            leads_list = []

        # prevent duplicate
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


@app.route("/uploads/<filename>")
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



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)