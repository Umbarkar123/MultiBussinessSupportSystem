import os
import logging
import traceback
import certifi
import pytz
import secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, session
from flask_cors import CORS
from twilio.rest import Client
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from openai import OpenAI
from dotenv import load_dotenv
from bson import ObjectId
from uuid import uuid4


# 1. SETUP & CONFIG
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_key")
CORS(app)

# 2. LOAD ENVIRONMENT VARIABLES
MONGO_URI = os.getenv("MONGO_URI")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
RETELL_WEBHOOK = os.getenv("RETELL_WEBHOOK_URL")

# 3. DATABASE INITIALIZATION
if MONGO_URI:
    try:
        # Clean the URI - remove any accidental spaces or hidden characters
        MONGO_URI = MONGO_URI.strip()
        
        # Vercel needs connect=False for serverless cold-starts
        # tlsCAFile=certifi.where() is the most reliable way to handle SSL on Vercel
        client = MongoClient(
            MONGO_URI,
            server_api=ServerApi('1'),
            connect=False,
            serverSelectionTimeoutMS=10000
        )
        
        # IMPORTANT: Atlas usually needs the DB name explicitly if not in URI
        db_name = "voice_agent_db"
        if "/" in MONGO_URI.split("@")[-1]:
             extracted_db = MONGO_URI.split("@")[-1].split("/")[1].split("?")[0]
             if extracted_db:
                 db_name = extracted_db
        
        db = client.get_database(db_name)
        logger.info(f"MongoDB initialized for database: {db_name}")
    except Exception as e:
        logger.error(f"Critical MongoDB Setup Error: {e}")
        db = None
else:
    db = None
    logger.error("MONGO_URI NOT FOUND IN ENVIRONMENT")

# 4. AI & EXTERNAL SERVICES
client_ai = OpenAI(api_key=OPENAI_API_KEY)

def call_llm(prompt_text, form_data):
    user_input = f"""
    Form Data Received:
    {form_data}

    Instructions:
    {prompt_text}
    """

    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an intelligent form processing assistant."},
                {"role": "user", "content": user_input}
            ],
            temperature=0.3
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error calling LLM: {e}")
        return "Error processing request."

# ------------------ DB ------------------
if db is not None:
    collection = db["call_requests"]
    admin_col = db["admin"]
    api_col = db["api_keys"]
    form_col = db["form_fields"]
    user_col = db["user"]
    logger.info("Collections initialized successfully.")
else:
    collection = None
    admin_col = None
    api_col = None
    form_col = None
    user_col = None
    logger.error("Collections NOT initialized - DB connection failed.")


# ------------------ TWILIO CONFIG ------------------
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
else:
    twilio_client = None
    logger.warning("Twilio credentials not set. Call features will be disabled.")

# RETELL_WEBHOOK is loaded from env above


@app.route('/')
def home():
    return redirect('/request')

@app.route('/client-login')
def client_login():
    return redirect('/login')

@app.route('/admin-login')
def admin_login():
    return redirect('/login')


@app.errorhandler(Exception)
def handle_exception(e):
    if hasattr(e, "code") and e.code < 500:
        return e
    return jsonify({
        "error": str(e),
        "traceback": traceback.format_exc(),
        "type": type(e).__name__
    }), 500

@app.route("/debug-env")
def debug_env():
    return jsonify({
        "cwd": os.getcwd(),
        "files": os.listdir("."),
        "templates": os.listdir("templates") if os.path.exists("templates") else "missing",
        "mongo_uri_set": bool(os.getenv("MONGO_URI")),
        "openai_key_set": bool(os.getenv("OPENAI_API_KEY"))
    })

@app.route("/request-call", methods=["POST"])
def request_call():
    data = request.json

    user_email = session.get("user")
    client_id = session.get("client_id")

    # ===== system fields =====
    data["status"] = "PENDING"
    data["conversation"] = []
    data["createdAt"] = datetime.utcnow()
    data["user_id"] = user_email
    data["client_id"] = client_id

    # ===== Insert ONCE and get call id =====
    result = collection.insert_one(data)
    call_id = str(result.inserted_id)

    # ===== Twilio call using dynamic phone =====
    phone_number = data.get("phone")

    if phone_number and twilio_client:
        try:
            twilio_client.calls.create(
                to=phone_number,
                from_=TWILIO_PHONE_NUMBER,
                url=f"{RETELL_WEBHOOK}?call_id={call_id}"
            )
        except Exception as e:
            logger.error(f"Error initiating Twilio call: {e}")

    return jsonify({"status": "ok"})


# ------------------ ROUTE 2: RETELL LIVE TRANSCRIPTS ------------------
@app.route("/retell", methods=["POST"])
def retell_webhook():
    data = request.json

    phone = data.get("from_number")
    user_text = data.get("transcript", "")
    agent_text = data.get("response", "")

    # Save conversation by phone
    collection.update_one(
        {"phone": phone},
        {
            "$push": {
                "conversation": {
                    "user": user_text,
                    "agent": agent_text
                }
            }
        }
    )

    return jsonify({"status": "logged"})


# ------------------ ROUTE 3: CALL SUMMARY AFTER END ------------------
@app.route("/call-summary", methods=["POST"])
def call_summary():
    data = request.json

    phone = data.get("from_number")

    collection.update_one(
        {"phone": phone},
        {
            "$set": {
                "status": data.get("summary_status", "Completed"),
                "call_transcript": data.get("transcript"),
                "call_summary": data.get("summary")
            }
        }
    )

    return {"status": "updated"}


# ------------------ OPTIONAL: TEST WITHOUT VOICE ------------------
@app.route("/ask", methods=["POST"])
def ask():
    return jsonify({"message": "Agent testing happens in Retell, not here."})

#
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        # 1) Check USER collection
        user = user_col.find_one({"email": email, "password": password})
        if user:
            session["user"] = user["email"]
            session["role"] = user["role"]
            session["client_id"] = str(user.get("client_id", ""))
            logger.info(f"User login successful: {user['email']}")
            return redirect("/dashboard")

        # 2) Check CLIENT collection
        client = db["clients"].find_one({"email": email, "password": password})
        if client:
            session["user"] = client["email"]
            session["role"] = "CLIENT"
            session["client_id"] = str(client["_id"])
            logger.info(f"Client login successful: {client['email']}")
            return redirect("/analytics")

        # 3) Check ADMIN collection
        admin = admin_col.find_one({
            "email": email,
            "password": password,
            "role": "ADMIN"
        })
        if admin:
            session["user"] = admin["email"]
            session["role"] = "ADMIN"
            logger.info(f"Admin login successful: {admin['email']}")
            return redirect("/super-dashboard")


        return render_template("login.html", error="Invalid credentials")

    return render_template("login.html")

@app.route("/dashboard")
def dashboard():

    # 🔥 NO LOGIN CHECK FOR LANDING ACCESS

    total = collection.count_documents({})
    pending = collection.count_documents({"status": "PENDING"})
    approved = collection.count_documents({"status": "APPROVED"})
    rejected = collection.count_documents({"status": "REJECTED"})

    recent = list(
        collection.find()
        .sort([("_id", -1)])
        .limit(5)
    )

    return render_template(
        "dashboard.html",
        total=total,
        pending=pending,
        confirmed=approved,
        cancelled=rejected,
        recent=recent
    )
@app.route("/user-dashboard")
def user_dashboard_public():

    # 🔥 CLEAR OLD SESSION
    session.clear()

    # 🔥 SET GUEST USER
    session["role"] = "USER"
    session["user"] = "guest"

    total = collection.count_documents({})
    pending = collection.count_documents({"status": "PENDING"})
    approved = collection.count_documents({"status": "APPROVED"})
    rejected = collection.count_documents({"status": "REJECTED"})

    recent = list(
        collection.find().sort("_id", -1).limit(5)
    )

    return render_template(
        "dashboard.html",
        total=total,
        pending=pending,
        confirmed=approved,
        cancelled=rejected,
        recent=recent
    )

@app.route("/calls")

def calls():
    if "role" not in session:
        return redirect("/login")

    print("==== CALLS HIT ====")
    print("SESSION:", dict(session))

    role = session.get("role")
    user_email = session.get("user")
    client_id = session.get("client_id")

    if role == "USER":

        if user_email == "guest":
            data = list(collection.find().sort("_id", -1))
        else:
            data = list(
                collection.find({"user_id": user_email})
                .sort("_id", -1)
            )


    elif role == "CLIENT":
        data = list(
            collection.find({"client_id": client_id})
            .sort("_id", -1)
        )

    else:  # ADMIN
        data = list(
            collection.find().sort("_id", -1)
        )

    # FORMAT DATA FOR DISPLAY
    formatted_calls = []
    for call in data:
        formatted = {
            "_id": call.get("_id"),
            "name": call.get("name", call.get("user_name", "Unknown")),
            "phone": call.get("phone", "N/A"),
            "time": call.get("time", ""),
            "status": call.get("status", "PENDING"),
            "app_name": call.get("app_name", call.get("service", "")),
            "data": call.get("data", {}),  # All form fields
            "createdAt": ""
        }
        
        # Format created_at timestamp
        if call.get("created_at"):
            formatted["createdAt"] = call["created_at"].strftime("%Y-%m-%d %H:%M")
        elif call.get("createdAt"):
            formatted["createdAt"] = call["createdAt"].strftime("%Y-%m-%d %H:%M")
        
        formatted_calls.append(formatted)

    return render_template("calls.html", calls=formatted_calls)

@app.route("/call/<id>")
def call_details(id):
    from bson import ObjectId
    call = collection.find_one({"_id": ObjectId(id)})
    return render_template("call_details.html", call=call)


@app.route("/agents")
def agents():
    return render_template("agents.html")


@app.route("/request")
def request_page():
    # if "role" not in session:
    #     return redirect("/login")

    role = session.get("role")
    client_id = session.get("client_id")

    owner = client_id if role == "CLIENT" else "GLOBAL"

    # form_fields = list(
    #     form_col.find({"client_id": owner}).sort("order", 1)
    # )
    # fields = list(db["form_fields"].find())

    print("SESSION CLIENT:", client_id)

    doc1 = db["form_builder"].find_one({"Client_id": client_id})
    print("TRY 1 (Client_id):", doc1)

    doc2 = db["form_builder"].find_one({"client_id": client_id})
    print("TRY 2 (client_id):", doc2)

    doc3 = db["form_builder"].find_one()
    print("TRY 3 (any doc):", doc3)

    form_doc = doc1 or doc2 or doc3

    form_fields = form_doc.get("fields", []) if form_doc else []

    return render_template(
        "request.html",
        form_fields=form_fields
    )

@app.route("/analytics")
def analytics():
    if "role" not in session or session["role"] != "CLIENT":
        return redirect("/login")

    client_id = session.get("client_id")

    total = collection.count_documents({"client_id": client_id})
    approved = collection.count_documents({"client_id": client_id, "status": "APPROVED"})
    rejected = collection.count_documents({"client_id": client_id, "status": "REJECTED"})
    pending = collection.count_documents({"client_id": client_id, "status": "PENDING"})

    recent = list(
        collection.find({"client_id": client_id})
        .sort("createdAt", -1)  # ← IMPORTANT
        .limit(10)

    )

    return render_template(
        "analytics.html",
        total=total,
        confirmed=approved,
        cancelled=rejected,
        pending=pending,
        recent=recent
    )

@app.route("/profile", methods=["GET", "POST"])
def profile():

    if "user" not in session:
        return redirect("/login")

    email = session.get("user")
    role = session.get("role")

    # Decide which collection to use
    if role == "USER":
        col = user_col
    elif role == "CLIENT":
        col = db["clients"]
    else:  # ADMIN
        col = admin_col

    # UPDATE NAME
    if request.method == "POST":
        new_name = request.form.get("name")
        col.update_one(
            {"email": email},
            {"$set": {"name": new_name}}
        )

    # Fetch updated data
    data = col.find_one({"email": email})

    return render_template("profile.html", profile=data, role=role)



import secrets

@app.route("/api-key", methods=["GET", "POST"])
def api_key_page():

    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    clients = list(db["clients"].find())

    # ===== GENERATE KEY =====
    if request.method == "POST":
        client_id = request.form.get("client_id")  # "C001"

        key = "sk_" + secrets.token_hex(16)

        api_col.insert_one({
            "client_id": client_id,
            "api_key": key,
            "createdAt": datetime.utcnow()
        })

        return redirect("/api-key")

    # ===== SHOW KEYS =====
    raw_keys = list(api_col.find().sort("createdAt", -1))
    keys = []

    for k in raw_keys:
        print("\n==== DEBUG START ====")
        print("RAW from api_keys:", repr(k["client_id"]), type(k["client_id"]))

        cid = str(k["client_id"]).strip()
        print("After strip:", repr(cid))

        client = db["clients"].find_one({"_id": cid})
        print("Client from clients collection:", client)
        print("==== DEBUG END ====\n")

        k["client_name"] = client["company_name"] if client else "Unknown"
        k["client_email"] = client["email"] if client else "-"
        keys.append(k)

    return render_template(
        "api_key.html",
        clients=clients,
        keys=keys
    )

@app.route("/regenerate-key/<id>")
def regenerate_key(id):
    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    new_key = "sk_" + secrets.token_hex(16)

    api_col.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {
                "api_key": new_key,
                "createdAt": datetime.utcnow()
            }
        }
    )

    return redirect("/api-key")

@app.route("/revoke-key/<id>")
def revoke_key(id):
    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    api_col.delete_one({"_id": ObjectId(id)})

    return redirect("/api-key")


@app.route("/api/calls", methods=["GET"])
def api_calls():
    key = request.headers.get("x-api-key")

    if not key:
        return jsonify({"error": "API key required"}), 401

    # Find API key record
    api = api_col.find_one({"api_key": key})
    if not api:
        return jsonify({"error": "Invalid API key"}), 401

    client_id = api.get("client_id")

    # Fetch only this client's call data
    calls = list(
        collection.find(
            {"client_id": client_id},
            {"_id": 0}   # hide mongo id
        ).sort("createdAt", -1)
    )

    return jsonify({
        "client_id": client_id,
        "total_calls": len(calls),
        "data": calls
    }), 200


@app.context_processor
def inject_profile():
    role = session.get("role")
    email = session.get("user")

    if role == "USER":
        profile = user_col.find_one({"email": email})

    elif role == "CLIENT":
        profile = db["clients"].find_one({"email": email})

    elif role == "ADMIN":
        profile = admin_col.find_one({"email": email})

    else:
        profile = None

    return dict(profile=profile)
#
@app.route("/super-dashboard")
def super_dashboard():
    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    calls = collection

    # ---------------- KPI ----------------
    total_calls = calls.count_documents({})
    pending_calls = calls.count_documents({"status": "PENDING"})
    approved_calls = calls.count_documents({"status": "APPROVED"})

    total_clients = db["clients"].count_documents({})
    total_users = user_col.count_documents({})

    active_clients = len(calls.distinct("client_id"))

    # ---------------- CLIENT ANALYTICS TABLE ----------------
    client_list = []
    for c in db["clients"].find():
        cid = str(c["_id"])

        total = calls.count_documents({"client_id": cid})
        pending = calls.count_documents({"client_id": cid, "status": "PENDING"})
        approved = calls.count_documents({"client_id": cid, "status": "APPROVED"})

        last_call = calls.find({"client_id": cid}).sort("createdAt", -1).limit(1)
        last_activity = ""
        for x in last_call:
            last_activity = x["createdAt"]

        client_list.append({
            "company": c.get("company_name"),
            "email": c.get("email"),
            "total": total,
            "pending": pending,
            "approved": approved,
            "last_activity": last_activity
        })

    # ---------------- USER ANALYTICS TABLE ----------------
    user_list = []
    for u in user_col.find():
        email = u.get("email")

        total = calls.count_documents({"user_id": email})
        pending = calls.count_documents({"user_id": email, "status": "PENDING"})
        approved = calls.count_documents({"user_id": email, "status": "APPROVED"})

        last_call = calls.find({"user_id": email}).sort("createdAt", -1).limit(1)
        last_activity = ""
        for x in last_call:
            last_activity = x["createdAt"]

        user_list.append({
            "email": email,
            "total": total,
            "pending": pending,
            "approved": approved,
            "last_activity": last_activity
        })

    # ---------------- RECENT CALLS ----------------
    recent = list(calls.find().sort("createdAt", -1).limit(15))

    return render_template(
        "super_dashboard.html",
        total_clients=total_clients,
        total_users=total_users,
        total_calls=total_calls,
        pending_calls=pending_calls,
        approved_calls=approved_calls,
        active_clients=active_clients,
        clients=client_list,      # ✅ LIST
        users=user_list,          # ✅ LIST
        recent=recent
    )
# ------------------ ADMIN : CLIENTS MANAGEMENT ------------------
@app.route("/manage-clients")
def manage_clients():
    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    clients = list(db["clients"].find())
    return render_template("manage_clients.html", clients=clients)


# ------------------ ADMIN : USERS MANAGEMENT ------------------
@app.route("/manage-users")
def manage_users():
    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    users = list(user_col.find())
    return render_template("manage_users.html", users=users)


# ------------------ ADMIN : SYSTEM ANALYTICS ------------------
from datetime import datetime, timedelta

@app.route("/system-analytics")
def system_analytics():
    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    # ===== BASIC COUNTS =====
    total_calls = collection.count_documents({})
    pending = collection.count_documents({"status": "PENDING"})
    approved = collection.count_documents({"status": "APPROVED"})
    rejected = collection.count_documents({"status": "REJECTED"})

    # ===== CALLS BY STATUS =====
    status_data = [
        {"status": "Pending", "count": pending},
        {"status": "Approved", "count": approved},
        {"status": "Rejected", "count": rejected},
    ]

    # ===== TOP CLIENTS =====
    pipeline_clients = [
        {
            "$group": {
                "_id": "$client_id",
                "total": {"$sum": 1},
                "approved": {
                    "$sum": {"$cond": [{"$eq": ["$status", "APPROVED"]}, 1, 0]}
                },
                "pending": {
                    "$sum": {"$cond": [{"$eq": ["$status", "PENDING"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"total": -1}},
        {"$limit": 5},
    ]

    top_clients = list(collection.aggregate(pipeline_clients))

    # ===== TOP USERS =====
    pipeline_users = [
        {
            "$group": {
                "_id": "$user_id",
                "total": {"$sum": 1},
                "approved": {
                    "$sum": {"$cond": [{"$eq": ["$status", "APPROVED"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"total": -1}},
        {"$limit": 5},
    ]

    top_users = list(collection.aggregate(pipeline_users))

    # ===== CALLS PER DAY (LAST 7 DAYS) =====
    seven_days_ago = datetime.utcnow() - timedelta(days=7)

    pipeline_days = [
        {"$match": {"createdAt": {"$gte": seven_days_ago}}},
        {
            "$group": {
                "_id": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$createdAt"}
                },
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    calls_per_day = list(collection.aggregate(pipeline_days))

    # ===== RECENT ACTIVITY =====
    recent = list(collection.find().sort("createdAt", -1).limit(10))

    return render_template(
        "system_analytics.html",
        total_calls=total_calls,
        pending=pending,
        approved=approved,
        rejected=rejected,
        status_data=status_data,
        top_clients=top_clients,
        top_users=top_users,
        calls_per_day=calls_per_day,
        recent=recent,
    )
@app.route("/booking-data")
def booking_data():

    client_id = session.get("client_id")

    # GET ALL DATA FROM call_requests ONLY
    all_data = list(
        db.call_requests.find({"client_id": client_id})
        .sort("created_at", -1)
    )
    
    # FORMAT DATA FOR UI
    for d in all_data:
        # Ensure all required fields exist
        d["name"] = d.get("name", d.get("user_name", "Unknown"))
        d["phone"] = d.get("phone", "N/A")
        d["app_name"] = d.get("app_name", d.get("service", ""))
        d["query"] = d.get("query", d.get("ai_reply", "")[:50] if d.get("ai_reply") else "")
        d["status"] = d.get("status", "PENDING")
        d["data"] = d.get("data", {})  # All form fields
        
        # Time handling
        if d.get("time"):
            d["time"] = d["time"]
        elif d.get("created_at"):
            d["time"] = d["created_at"].strftime("%H:%M")
        elif d.get("createdAt"):
            d["time"] = d["createdAt"].strftime("%H:%M")
        else:
            d["time"] = ""

    # ===== STATS =====
    total = len(all_data)
    approved = sum(1 for d in all_data if d.get("status") == "APPROVED")
    rejected = sum(1 for d in all_data if d.get("status") == "REJECTED")
    pending = sum(1 for d in all_data if d.get("status") == "PENDING")

    # ===== APPLICATION FILTER LIST =====
    apps = list(set([d.get("app_name") for d in all_data if d.get("app_name")]))

    return render_template(
        "booking_data.html",
        data=all_data,
        total=total,
        approved=approved,
        rejected=rejected,
        pending=pending,
        apps=apps
    )



@app.route("/form-settings", methods=["GET", "POST"])
def form_settings():
    if "role" not in session:
        return redirect("/login")

    role = session.get("role")
    client_id = session.get("client_id")

    owner = "GLOBAL" if role == "ADMIN" else client_id

    # ADD FIELD
    if request.method == "POST":
        label = request.form.get("label")
        field_type = request.form.get("type")
        required = True if request.form.get("required") == "on" else False

        name = label.lower().replace(" ", "_")

        form_col.insert_one({
            "client_id": owner,
            "label": label,
            "name": name,
            "type": field_type,
            "required": required,
            "order": datetime.utcnow().timestamp()
        })

        return redirect("/form-settings")

    fields = list(form_col.find({"client_id": owner}).sort("order", 1))

    return render_template("form_settings.html", fields=fields)
@app.route("/admin/calls")
def admin_calls():

    if "role" not in session or session["role"] != "ADMIN":
        return redirect("/login")

    client_id = request.args.get("client")

    if not client_id:
        clients = list(db["clients"].find({"role": "CLIENT"}))
        return render_template("admin_calls_clients.html", clients=clients)

    calls = list(db["call_requests"].find(
        {"client_id": client_id}
    ).sort("createdAt", -1))

    client = db["clients"].find_one(
        {"client_id": client_id}   # ✅ THIS FIX
    )

    return render_template(
        "admin_calls_list.html",
        calls=calls,
        client=client
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

from bson import ObjectId

@app.route("/update-status/<id>/<status>")
def update_status(id, status):
    if "role" not in session or session["role"] != "CLIENT":
        return redirect("/login")

    collection.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"status": status}}
    )

    return redirect("/analytics")

# ---------------- APPLICATION LIST ----------------
@app.route("/client/applications")
def client_applications():
    client_id = session.get("client_id")

    default_apps = ["Hotel", "Restaurant"]
    client_apps = list(db.client_apps.find({"client_id": client_id}))

    return render_template(
        "client_applications.html",
        default_apps=default_apps,
        client_apps=client_apps,
        client_id=client_id
    )


# ---------------- CREATE APPLICATION ----------------
@app.route("/client/create_application", methods=["POST"])
def create_application():
    client_id = session.get("client_id")

    data = request.get_json()
    app_name = data.get("app_name")

    if not app_name:
        return jsonify({"error": "App name required"})

    existing = db.applications.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if existing:
        return jsonify({"error": "App already exists"})

    db.applications.insert_one({
        "client_id": client_id,
        "app_name": app_name,
        "prompt": ""
    })

    return jsonify({"status": "created"})

# ---------------- APP DASHBOARD ----------------
# @app.route("/client/application/<app_name>/dashboard")
# def application_dashboard(app_name):
#     client_id = session.get("client_id")
#
#     submissions = db.form_submissions.count_documents({
#         "client_id": client_id,
#         "app_name": app_name
#     })
#
#     return render_template(
#         "app_dashboard.html",
#         app_name=app_name,
#         submissions=submissions
#     )
# @app.route("/client/application/<app_name>/dashboard")
# def application_dashboard(app_name):
#     if "role" not in session:
#         return redirect("/login")
#
#     # get submissions count for this app
#     submissions = db.submissions.count_documents({"app_name": app_name})
#
#     return render_template(
#         "app_dashboard.html",
#         app_name=app_name,
#         submissions=submissions
#     )

@app.route("/client/application/<app_name>/dashboard")
def application_dashboard(app_name):

    if "role" not in session:
        return redirect("/login")

    submissions = db.form_submissions.count_documents({
        "app_name": app_name
    })

    client_id = session.get("client_id")
    return render_template(
        "app_dashboard.html",
        app_name=app_name,
        submissions=submissions,
        client_id=client_id
    )



# ---------------- FORM BUILDER ----------------
@app.route("/client/application/<app_name>")
def open_form_builder(app_name):

    if "client_id" not in session:
        return redirect("/login")

    client_id = session.get("client_id")

    custom_form = db.form_builders.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if custom_form:
        fields = custom_form.get("fields", [])
    else:
        template = db.form_templates.find_one({
            "app_type": app_name.lower()
        })

        fields = template.get("fields", []) if template else []

        # Initialize with template fields if no form exists
        db.form_builders.insert_one({
            "client_id": client_id,
            "app_name": app_name,
            "fields": fields,
            "api_key": uuid4().hex
        })

    return render_template(
        "form_builder.html",
        fields=fields,
        app_name=app_name
    )

# ---------------- SAVE FORM ----------------
import secrets

@app.route("/client/save_form/<app_name>", methods=["POST"])
def save_form(app_name):
    client_id = session.get("client_id")
    data = request.get_json()

    existing = db.form_builders.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    # generate api key only first time
    if not existing:
        api_key = secrets.token_hex(16)
    else:
        api_key = existing["api_key"]

    db.form_builders.update_one(
        {"client_id": client_id, "app_name": app_name},
        {
            "$set": {
                "fields": data["fields"],
                "api_key": api_key,
                "app_name": app_name,
                "client_id": client_id
            }
        },
        upsert=True
    )

    return jsonify({
        "status": "saved",
        "api_key": api_key
    })







# PUBLIC FORM (used by copy link + preview)
@app.route("/form/<client_id>/<app_name>")
def public_form(client_id, app_name):

    form = db.form_builders.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if not form:
        return "Form not found"

    fields = form.get("fields", [])

    return render_template(
        "client_open_form.html",
        form=form,
        fields=fields,
        app_name=app_name,
        api_key=form.get("api_key")
    )

# Legacy route for backward compatibility - redirects to unique URL if possible
@app.route("/form/<app_name>")
def legacy_public_form(app_name):
    client_id = session.get("client_id")
    if client_id:
        return redirect(f"/form/{client_id}/{app_name}")
    
    # Fallback to search if no session (not ideal, but prevents total breakage)
    form = db.form_builders.find_one({"app_name": app_name})
    if form:
        return redirect(f"/form/{form['client_id']}/{app_name}")
    
    return "Form not found"

@app.route("/client/application/<app_name>/preview")
def preview_application(app_name):

    if "role" not in session:
        return redirect("/login")
        
    client_id = session.get("client_id")

    form = db.form_builders.find_one({
        "client_id": client_id,
        "app_name": app_name
    })
    
    if not form:
        return "Form not found"

    return render_template(
        "client_open_form.html",
        fields=form.get("fields", []),
        app_name=app_name,
        api_key=form.get("api_key"),
        base_template="layout.html"
    )




# ---------------- SUBMISSIONS ----------------
@app.route("/client/application/<app_name>/submissions")
def view_submissions(app_name):

    app_data = db.applications.find_one({"app_name": app_name})

    if not app_data:
        return "Application not found"

    submissions = list(db.submissions.find({
        "app_name": app_name
    }).sort("created_at", -1))

    return render_template(
        "submission.html",
        app_name=app_name,
        submissions=submissions
    )

# ---------------- SETTINGS ----------------
@app.route("/client/application/<app_name>/settings", methods=["GET", "POST"])
def application_settings(app_name):

    # get application from DB
    app_data = db.applications.find_one({"app_name": app_name})

    if not app_data:
        return "Application not found"

    # ---------- SAVE SETTINGS ----------
    if request.method == "POST":

        updated_name = request.form.get("app_name")
        active = True if request.form.get("status") == "on" else False
        public_form = True if request.form.get("public_form") == "on" else False
        llm_enabled = True if request.form.get("llm_enabled") == "on" else False

        db.applications.update_one(
            {"_id": app_data["_id"]},
            {
                "$set": {
                    "app_name": updated_name,
                    "active": active,
                    "public_form": public_form,
                    "llm_enabled": llm_enabled
                }
            }
        )

        return redirect(f"/client/application/{updated_name}/settings")

    # ---------- LOAD PAGE ----------
    return render_template("app_settings.html", app=app_data)

from flask import jsonify

@app.route("/client/application/<app_name>/delete", methods=["DELETE"])
def delete_application(app_name):

    if "role" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    client_id = session.get("client_id")

    # delete from main apps collection
    db.client_apps.delete_one({
        "app_name": app_name,
        "client_id": client_id
    })

    # delete form config
    db.forms.delete_many({
        "app_name": app_name,
        "client_id": client_id
    })

    # delete submissions
    db.submissions.delete_many({
        "app_name": app_name,
        "client_id": client_id
    })

    # delete llm settings
    db.llm_settings.delete_many({
        "app_name": app_name,
        "client_id": client_id
    })

    return jsonify({"status": "deleted"})

@app.route("/submit/<app_name>", methods=["POST"])
def submit_form(app_name):

    data = dict(request.form)

    data["app_name"] = app_name
    data["created_at"] = datetime.utcnow()

    db.submissions.insert_one(data)

    return redirect(f"/client/application/{app_name}/open")

# @app.route("/booking_data")
# def booking_data():
#     if "role" not in session:
#         return redirect("/login")
#
#     # allow CLIENT
#     if session.get("role") != "CLIENT":
#         return redirect("/analytics")
#
#     # your existing logic
#     recent = list(db.calls.find().sort("time", -1))
#     total = len(recent)
#     confirmed = len([c for c in recent if c["status"] == "APPROVED"])
#     cancelled = len([c for c in recent if c["status"] == "REJECTED"])
#     pending = len([c for c in recent if c["status"] == "PENDING"])
#
#     return render_template(
#         "booking_data.html",
#         recent=recent,
#         total=total,
#         confirmed=confirmed,
#         cancelled=cancelled,
#         pending=pending
#     )

@app.route("/integration")
def integration_page():

    if "role" not in session or session.get("role") != "CLIENT":
        return redirect("/login")

    client_id = session.get("client_id")

    apps = list(db.form_builders.find({"client_id": client_id}))

    return render_template("integration.html", apps=apps)

from bson import ObjectId



# APPROVE
@app.route("/approve/<id>")
def approve_booking(id):
    db.call_requests.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"status": "APPROVED"}}
    )
    return redirect(url_for("booking_data"))


# REJECT
@app.route("/reject/<id>")
def reject_booking(id):
    db.call_requests.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"status": "REJECTED"}}
    )
    return redirect(url_for("booking_data"))

@app.route("/reject-submission/<id>")
def reject_submission(id):
    db.form_submissions.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"status": "REJECTED"}}
    )
    return redirect("/booking-data")


@app.route("/api/submit/<api_key>", methods=["POST"])
def api_submit(api_key):

    form = db.form_builders.find_one({"api_key": api_key})

    if not form:
        return "Invalid API key", 403

    # support both JSON and HTML form
    if request.is_json:
        data = request.json
    else:
        data = dict(request.form)

    client_id = form["client_id"]
    app_name = form["app_name"]

    # ==========================================
    # 🔥 STEP 1: RESOLVE APP DETAILS & EXTRACT PHONE/NAME
    # ==========================================
    # 1. Identify which label corresponds to Full Name and Phone (CASE-INSENSITIVE)
    fields = form.get("fields", [])
    name_label = ""
    name_field_name = ""
    phone_label = ""
    phone_field_name = ""
    
    for field in fields:
        label_text = field.get("label", "").lower()
        field_name = field.get("name", "").lower()
        
        # Check both label and name for phone
        if "phone" in label_text or "phone" in field_name or "mobile" in label_text or "number" in label_text:
            phone_label = field.get("label")  # Use original label for extraction
            phone_field_name = field.get("name")  # Also store the field name
        
        # Check both label and name for name
        if "name" in label_text or "name" in field_name:
            name_label = field.get("label")
            name_field_name = field.get("name")

    # Log what we found for debugging
    logger.info(f"📝 Form fields detected - Name label: '{name_label}', Name field: '{name_field_name}', Phone label: '{phone_label}', Phone field: '{phone_field_name}'")
    logger.info(f"📦 Submitted data keys: {list(data.keys())}")

    # Extract values using the identified labels AND field names
    user_name = None
    phone_num = None
    
    # Try label first, then field name
    if name_label and name_label in data:
        user_name = data.get(name_label)
    elif name_field_name and name_field_name in data:
        user_name = data.get(name_field_name)
    
    if phone_label and phone_label in data:
        phone_num = data.get(phone_label)
    elif phone_field_name and phone_field_name in data:
        phone_num = data.get(phone_field_name)
    
    # Fallback to common field names if labels didn't work
    if not user_name:
        for key in ["name", "Name", "full_name", "Full Name", "fullname", "user_name", "username"]:
            if key in data:
                user_name = data[key]
                logger.info(f"✅ Found name via fallback key: '{key}' = '{user_name}'")
                break
    
    if not phone_num:
        for key in ["phone", "Phone", "phone_number", "Phone Number", "mobile", "Mobile", "phone number"]:
            if key in data:
                phone_num = data[key]
                logger.info(f"✅ Found phone via fallback key: '{key}' = '{phone_num}'")
                break

    # Clean and validate phone number
    if phone_num:
        # Remove spaces, dashes, parentheses
        phone_num = str(phone_num).strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        
        # Basic validation - must have at least 10 digits
        if len(phone_num) < 10 or not any(char.isdigit() for char in phone_num):
            logger.warning(f"Invalid phone number format: {phone_num}")
            phone_num = None

    # Set defaults for display
    display_name = user_name if user_name else "Unknown User"
    display_phone = phone_num if phone_num else "No Phone Provided"
    
    logger.info(f"🎯 Final extracted - Name: '{display_name}', Phone: '{display_phone}'")

    # 2. Get specific application prompt
    app_doc = db.applications.find_one({"app_name": app_name})
    if app_doc and app_doc.get("prompt"):
        final_prompt = app_doc.get("prompt")
    else:
        # Fallback to general LLM settings
        settings = db.llm_settings.find_one({
            "client_id": client_id,
            "app_name": app_name
        })
        if settings:
            default_p = settings.get("default_prompt", "")
            custom_p = settings.get("custom_prompt", "")
            final_prompt = default_p + "\n" + custom_p
        else:
            final_prompt = "You are a helpful assistant."

    # ==========================================
    # 🔥 STEP 2: CALL AI MODEL (OPTIONAL - SKIP IF NO API KEY)
    # ==========================================
    ai_reply = "Form received successfully"
    
    if OPENAI_API_KEY:
        try:
            response = client_ai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": final_prompt},
                    {"role": "user", "content": str(data)}
                ]
            )
            ai_reply = response.choices[0].message.content
        except Exception as e:
            logger.warning(f"AI processing skipped or failed: {e}")
            ai_reply = "Form received successfully (AI processing unavailable)"

    # ==========================================
    # 🔥 STEP 3: SAVE ALL DATA TO call_requests COLLECTION
    # ==========================================
    call_request = {
        "client_id": client_id,
        "app_name": app_name,
        "name": display_name,
        "phone": display_phone,
        "data": data,  # All form fields
        "ai_reply": ai_reply,
        "query": ai_reply[:100] if ai_reply else "",  # Short summary for table view
        "status": "PENDING",
        "time": datetime.now().strftime("%H:%M"),
        "created_at": datetime.now(),
        "createdAt": datetime.now()  # For backward compatibility
    }
    call_req_result = db.call_requests.insert_one(call_request)
    call_id = str(call_req_result.inserted_id)

    # ==========================================
    # 🔥 STEP 4: TRIGGER VOICE CALL IF VALID PHONE EXISTS
    # ==========================================
    call_initiated = False
    call_error = None
    
    if phone_num and twilio_client:
        try:
            # Ensure phone has country code
            if not phone_num.startswith('+'):
                phone_num = '+91' + phone_num  # Default to India, adjust as needed
            
            twilio_client.calls.create(
                to=phone_num,
                from_=TWILIO_PHONE_NUMBER,
                url=f"{RETELL_WEBHOOK}?call_id={call_id}&app_name={app_name}",
            )
            
            # Track call initiation WITHOUT changing status (keep as PENDING for approval)
            db.call_requests.update_one(
                {"_id": call_req_result.inserted_id},
                {"$set": {"call_initiated": True, "call_initiated_at": datetime.now()}}
            )
            
            call_initiated = True
            logger.info(f"✅ Triggered automated call for {app_name} to {phone_num}")
            
        except Exception as e:
            call_error = str(e)
            logger.error(f"❌ Failed to trigger automated call: {e}")
            
            # Track call failure WITHOUT changing status
            db.call_requests.update_one(
                {"_id": call_req_result.inserted_id},
                {"$set": {"call_initiated": False, "call_error": call_error}}
            )
    elif not phone_num:
        logger.warning(f"⚠️ No valid phone number provided for {app_name}")
    elif not twilio_client:
        logger.warning(f"⚠️ Twilio client not initialized - calls disabled")

    # ==========================================
    # 🔥 STEP 5: RETURN RESPONSE
    # ==========================================
    response_data = {
        "status": "success",
        "message": ai_reply,
        "call_id": call_id,
        "user_name": display_name,
        "phone": display_phone,
        "call_initiated": call_initiated
    }
    
    if call_error:
        response_data["call_error"] = call_error
    
    return jsonify(response_data)

@app.route("/api/approve/<id>")
def approve_api(id):
    db.requests.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"status": "APPROVED"}}
    )

    # Send SMS to user here

    return redirect("/calls")



@app.route("/save_form_builder", methods=["POST"])
def save_form_builder():

    if "client_id" not in session:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()

    client_id = session.get("client_id")
    app_name = data.get("app_name")
    fields = data.get("fields")

    # generate api key if not exists
    existing = db.form_builders.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if existing and "api_key" in existing:
        api_key = existing["api_key"]
    else:
        api_key = uuid4().hex

    db.form_builders.update_one(
        {
            "client_id": client_id,
            "app_name": app_name
        },
        {
            "$set": {
                "fields": fields,
                "api_key": api_key
            }
        },
        upsert=True
    )

    return jsonify({
        "status": "saved",
        "api_key": api_key
    })



@app.route("/client/api/<app_name>")
def api_page(app_name):

    if "client_id" not in session:
        return redirect("/login")

    client_id = session["client_id"]

    form = db.form_builders.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if not form:
        return "No form saved yet"

    return render_template("api_integration.html", form=form)

@app.route("/retell-config", methods=["POST"])
def retell_config():
    """
    Called by Retell AI (Dynamic Config) during a call.
    It receives metadata (client_id, app_name) or call_id and returns the system prompt.
    """
    data = request.get_json()
    metadata = data.get("metadata", {})
    client_id = metadata.get("client_id")
    app_name = metadata.get("app_name")
    
    # Fallback to call_id lookup if metadata is missing
    call_id = data.get("call_id") or request.args.get("call_id")
    
    if not client_id and call_id:
        # Check form submissions first
        sub = db.form_submissions.find_one({"_id": ObjectId(call_id)})
        if sub:
            client_id = sub.get("client_id")
            app_name = sub.get("app_name")
        else:
            # Check call requests
            req = db.call_requests.find_one({"_id": ObjectId(call_id)})
            if req:
                client_id = req.get("client_id")
                app_name = req.get("app_name")

    # Get prompt from consolidated collection
    settings = db.llm_settings.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if settings:
        system_prompt = settings.get("custom_prompt") or settings.get("default_prompt")
    else:
        system_prompt = "You are a professional customer support assistant."

    return jsonify({
        "agent_prompt": system_prompt
    })

@app.route("/check-session")
def check_session():
    return str(dict(session))



@app.route("/regen_key/<app_name>")
def regen_key(app_name):
    new_key = secrets.token_hex(16)

    db.form_builders.update_one(
        {"app_name": app_name, "client_id": session["client_id"]},
        {"$set": {"api_key": new_key}}
    )
    return {"status":"ok"}


@app.route("/revoke_key/<app_name>")
def revoke_key_api(app_name):
    db.form_builders.update_one(
        {"app_name": app_name, "client_id": session["client_id"]},
        {"$set": {"api_key": None}}
    )
    return {"status":"revoked"}


@app.route("/client/application/<app_name>/open")
def open_form(app_name):
    client_id = session.get("client_id")

    form = db.form_builders.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if not form:
        return "❌ Form not found"

    return render_template(
        "client_open_form.html",
        fields=form.get("fields", []),
        app_name=app_name,
        api_key=form.get("api_key")
    )

@app.route("/llm/get/<app_name>")
@app.route("/get-llm-prompt/<app_name>")
def get_llm_prompt(app_name):
    client_id = session.get("client_id")

    settings = db.llm_settings.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if settings:
        return jsonify({
            "custom_prompt": settings.get("custom_prompt", ""),
            "default_prompt": settings.get("default_prompt", "")
        })

    return jsonify({
        "custom_prompt": "",
        "default_prompt": ""
    })




@app.route("/update-llm-prompt", methods=["POST"])
@app.route("/llm/save/<app_name>", methods=["POST"])
def save_llm_prompt(app_name=None):
    client_id = session.get("client_id")
    data = request.json

    db.llm_settings.update_one(
        {
            "client_id": client_id,
            "app_name": app_name
        },
        {
            "$set": {
                "default_prompt": data.get("default_prompt"),
                "custom_prompt": data.get("custom_prompt"),
                "enabled": data.get("enabled", True)
            }
        },
        upsert=True
    )

    return jsonify({"status": "saved"})



@app.route("/llm-settings")
def llm_settings():
    client_id = session.get("client_id")

    applications = list(db.applications.find({"client_id": client_id}))

    return render_template("llm_settings.html",
                           applications=applications,
                           client_apps=applications)

# @app.route("/client/create_application", methods=["POST"])
# def create_application_llm():
#     client_id = session.get("client_id")
#     data = request.get_json()
#     app_name = data.get("app_name")
#
#     if not app_name:
#         return jsonify({"error": "App name required"}), 400
#
#     existing = db.applications.find_one({
#         "client_id": client_id,
#         "app_name": app_name
#     })
#
#     if existing:
#         return jsonify({"error": "Application already exists"}), 400
#
#     db.applications.insert_one({
#         "client_id": client_id,
#         "app_name": app_name,
#         "created_at": datetime.now()
#     })
#
#     return jsonify({"status": "created"})
#



@app.route("/client/update_booking_status", methods=["POST"])
def update_booking_status():

    data = request.get_json()
    booking_id = data.get("id")
    status = data.get("status")

    db.form_submissions.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {"status": status}}
    )

    return jsonify({"success": True})

@app.route("/api/form/<api_key>")
def get_form_by_api(api_key):

    app = db.client_apps.find_one({"api_key": api_key})

    if not app:
        return {"error": "Invalid API key"}, 401

    form = db.app_forms.find_one({
        "app_name": app["app_name"]
    })

    if not form:
        return {"fields": []}

    return {
        "app_name": app["app_name"],
        "fields": form.get("fields", [])
    }
@app.route("/download-html/<app_name>")
def download_html(app_name):

    form = db.app_forms.find_one({"app_name": app_name})

    if not form:
        return "Form not found"

    fields = form.get("fields", [])

    html = f"""
    <html>
    <head><title>{app_name} Form</title></head>
    <body>
    <h2>{app_name} Form</h2>

    <form action="{request.host_url}api/submit/YOUR_API_KEY" method="POST">
    """

    for field in fields:
        html += f"""
        <label>{field['label']}</label><br>
        <input type="{field['type']}" name="{field['name']}" placeholder="{field['label']}"><br><br>
        """

    html += """
        <button type="submit">Submit</button>
        </form>
        </body>
        </html>
    """

    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f"attachment;filename={app_name}.html"}
    )

@app.route("/test-db")
def test_db():
    return str(db.list_collection_names())




# ------------------ RUN ------------------
if __name__ == "__main__":
    if os.getenv("FLASK_ENV") == "production":
        from waitress import serve
        logger.info("Starting production server on port 5000...")
        serve(app, host="0.0.0.0", port=5000)
    else:
        app.run(port=5000, debug=True, use_reloader=False)
