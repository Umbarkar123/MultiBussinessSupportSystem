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
from functools import wraps
from uuid import uuid4
from apscheduler.schedulers.background import BackgroundScheduler
import pytz


# 1. SETUP & CONFIG
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Standardize Timezone
IST = pytz.timezone('Asia/Kolkata')

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_key")
CORS(app)

# --- AUTH DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "role" not in session or session["role"] != "ADMIN":
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def client_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "role" not in session or session["role"] != "CLIENT":
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

# 2. LOAD ENVIRONMENT VARIABLES
MONGO_URI = os.getenv("MONGO_URI")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
RETELL_WEBHOOK = os.getenv("RETELL_WEBHOOK_URL")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

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

DEFAULT_FORM_FIELDS = [
    {"label": "Full Name", "name": "name", "type": "text", "required": True},
    {"label": "Phone Number", "name": "phone", "type": "tel", "required": True},
    {"label": "Email Address", "name": "email", "type": "email", "required": True},
    {"label": "Address / Location", "name": "address", "type": "textarea", "required": False},
    {"label": "Preferred Date", "name": "date", "type": "date", "required": False},
    {"label": "Service Details", "name": "query", "type": "textarea", "required": False}
]

# ------------------ MIGRATION ------------------
@app.route("/admin/migrate-forms")
@admin_required
def migrate_forms():
    
    # Ensure all apps from "applications" are in "client_apps"
    legacy_apps = list(db.applications.find())
    for app in legacy_apps:
        db.client_apps.update_one(
            {"client_id": app["client_id"], "app_name": app["app_name"]},
            {"$set": {"created_at": datetime.now(IST).replace(tzinfo=None)}},
            upsert=True
        )
    
    # Ensure all apps in "client_apps" have a form and slug
    all_apps = list(db.client_apps.find())
    created_count = 0
    for app in all_apps:
        app_id_str = str(app["_id"])
        slug = app.get("slug") or slugify(app.get("app_name", "app"))
        
        # Ensure slug exists in client_apps
        if not app.get("slug"):
            db.client_apps.update_one({"_id": app["_id"]}, {"$set": {"slug": slug}})
            
        existing_form = db.form_builders.find_one({
            "client_id": app["client_id"],
            "app_name": app["app_name"]
        })
        
        form_data = {
            "client_id": app["client_id"],
            "app_id": app_id_str,
            "app_name": app["app_name"],
            "slug": slug,
            "fields": existing_form.get("fields", DEFAULT_FORM_FIELDS) if existing_form else DEFAULT_FORM_FIELDS,
            "api_key": existing_form.get("api_key", secrets.token_hex(16)) if existing_form else secrets.token_hex(16),
            "updated_at": datetime.now(IST).replace(tzinfo=None)
        }
        
        if not existing_form:
            form_data["created_at"] = datetime.now(IST).replace(tzinfo=None)
            db.form_builders.insert_one(form_data)
            created_count += 1
        else:
            db.form_builders.update_one({"_id": existing_form["_id"]}, {"$set": form_data})
            
    return f"Migration complete. Synchronized {len(legacy_apps)} legacy apps. Created {created_count} missing forms."



# ------------------ TWILIO CONFIG ------------------
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
else:
    twilio_client = None
    logger.warning("Twilio credentials not set. Call features will be disabled.")

# RETELL_WEBHOOK is loaded from env above

def trigger_voice_call(booking_id, phone, app_name):
    """Refactor Twilio call logic into a reusable helper."""
    if not twilio_client:
        logger.warning("Twilio client not initialized. Cannot trigger call.")
        return False
        
    try:
        # Standardize phone format
        clean_phone = "".join(filter(str.isdigit, str(phone)))
        target_phone = "+91" + clean_phone if len(clean_phone) == 10 else ("+" + clean_phone if not str(phone).startswith("+") else str(phone))
        
        # Use BASE_URL if available, otherwise fallback to request.url_root
        active_base = BASE_URL if BASE_URL else (request.url_root.rstrip("/") if request else "")
        
        # Note: 'url' is for TwiML execution (Retell)
        # 'status_callback' is for status updates to OUR server
        
        call = twilio_client.calls.create(
            to=target_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{RETELL_WEBHOOK}?call_id={booking_id}&app_name={app_name}",
            status_callback=f"{active_base}/call-status", 
            status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
            status_callback_method='POST'
        )
        
        # Update DB to reflect call initiation - Support both ObjectId and string
        try:
            update_result = db.call_requests.update_one(
                {"_id": ObjectId(booking_id)},
                {"$set": {
                    "call_initiated": True, 
                    "call_initiated_at": datetime.now(IST).replace(tzinfo=None),
                    "twilio_sid": call.sid
                }}
            )
            if update_result.matched_count == 0:
                db.call_requests.update_one(
                    {"_id": booking_id},
                    {"$set": {
                        "call_initiated": True, 
                        "call_initiated_at": datetime.now(IST).replace(tzinfo=None),
                        "twilio_sid": call.sid
                    }}
                )
        except Exception as db_e:
            logger.warning(f"DB update fallback in trigger_voice_call: {db_e}")
            db.call_requests.update_one(
                {"_id": booking_id},
                {"$set": {
                    "call_initiated": True, 
                    "call_initiated_at": datetime.now(IST).replace(tzinfo=None),
                    "twilio_sid": call.sid
                }}
            )

        logger.info(f"✅ Successfully triggered call for {app_name} to {target_phone} (ID: {booking_id})")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to trigger call for {booking_id}: {e}")
        return False

@app.route("/call-status", methods=["POST"])
def call_status_webhook():
    """Webhook to handle Twilio call status updates."""
    call_sid = request.form.get("CallSid")
    call_status = request.form.get("CallStatus")
    
    logger.info(f"📞 Call Status Update: SID={call_sid}, Status={call_status}")
    
    if call_sid:
        db.call_requests.update_one(
            {"twilio_sid": call_sid},
            {"$set": {
                "call_status": call_status,
                "last_update": datetime.now(IST).replace(tzinfo=None)
            }}
        )
        
    return Response(status=200)

def check_scheduled_calls():
    """Background job to check and trigger scheduled calls."""
    with app.app_context():
        # IST FIX: Use IST now for comparison
        now = datetime.now(IST).replace(tzinfo=None)
        # Find APPROVED requests where call_time is now or in the past AND not initiated
        pending_calls = db.call_requests.find({
            "status": "APPROVED",
            "call_time": {"$lte": now},
            "call_initiated": {"$ne": True}
        })
        
        for call in pending_calls:
            call_id = str(call["_id"])
            phone = call.get("phone")
            app_name = call.get("app_name", "your request")
            
            logger.info(f"⏰ Triggering scheduled call for {call_id} (scheduled for {call.get('call_time')})")
            
            # No need to update status to CALLING - trigger_voice_call handles 'call_initiated'
            success = trigger_voice_call(call_id, phone, app_name)
            if not success:
                 db.call_requests.update_one({"_id": call["_id"]}, {"$set": {"call_error": "Failed to trigger scheduled call"}})

# Start Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_scheduled_calls, trigger="interval", minutes=1)
scheduler.start()

@app.route('/')
def home():
    if "role" not in session:
        return redirect('/login')
    
    role = session.get("role")
    if role == "ADMIN":
        return redirect('/super-dashboard')
    elif role == "CLIENT":
        return redirect('/analytics')
    else:
        return redirect('/calls')

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
    data["created_at"] = datetime.now(IST).replace(tzinfo=None)
    data["user_id"] = user_email
    data["client_id"] = client_id

    # ===== Insert ONCE and get call id =====
    result = collection.insert_one(data)
    # call_id = str(result.inserted_id)

    # ===== Twilio call using dynamic phone (DISABLED PER USER REQUEST - SMS ONLY ON APPROVAL) =====
    # phone_number = data.get("phone")
    # if phone_number and twilio_client:
    #     try:
    #         twilio_client.calls.create(
    #             to=phone_number,
    #             from_=TWILIO_PHONE_NUMBER,
    #             url=f"{RETELL_WEBHOOK}?call_id={call_id}"
    #         )
    #     except Exception as e:
    #         logger.error(f"Error initiating Twilio call: {e}")

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
# ------------------ CONTEXT PROCESSOR ------------------
@app.context_processor
def inject_user_profile():
    """Injects user profile data into all templates."""
    if "user" not in session:
        return {"profile": None}

    user_email = session.get("user")
    role = session.get("role")
    
    profile = {}
    
    if role == "CLIENT":
        client = db.clients.find_one({"email": user_email})
        if client:
            profile = {
                "name": client.get("name", "Client"),
                "email": client.get("email"),
                "phone": client.get("phone", ""),
                "business_name": client.get("business_name", "My Business"),
                "gst": client.get("gst", "N/A"),
                "role": "Client"
            }
    elif role == "ADMIN":
        admin = db.admin.find_one({"email": user_email})
        if admin:
            profile = {
                "name": admin.get("name", "Admin"),
                "email": admin.get("email"),
                "role": "Super Admin",
                "business_name": "ConnexHub System",
                "gst": "N/A"
            }
    else:
        # Regular User
        user = db.user.find_one({"email": user_email})
        if user:
            profile = {
                "name": user.get("name", "User"),
                "email": user.get("email"),
                "phone": user.get("phone", ""),
                "role": "User"
            }

    # Ensure defaults if empty
    if not profile:
        profile = {"name": "Unknown", "email": user_email, "role": role}

    return {"profile": profile}


# ------------------ ACCOUNT ROUTES ------------------

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_email = session.get("user")
    role = session.get("role")
    
    collection = None
    if role == "CLIENT":
        collection = db.clients
    elif role == "ADMIN":
        collection = db.admin
    else:
        collection = db.user

    if request.method == "POST":
        name = request.form.get("name")
        phone = request.form.get("phone")
        
        update_data = {"name": name}
        if phone:
            update_data["phone"] = phone
            
        collection.update_one({"email": user_email}, {"$set": update_data})
        return redirect("/profile")

    data = collection.find_one({"email": user_email})
    return render_template("profile.html", data=data, role=role)

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password")
        new_pw = request.form.get("new_password")
        
        user_email = session.get("user")
        role = session.get("role")
        
        collection = None
        if role == "CLIENT": collection = db.clients
        elif role == "ADMIN": collection = db.admin
        else: collection = db.user
        
        user = collection.find_one({"email": user_email, "password": current_pw})
        
        if not user:
            return render_template("change_password.html", error="Incorrect current password")
            
        collection.update_one({"email": user_email}, {"$set": {"password": new_pw}})
        return render_template("change_password.html", success="Password updated successfully")
        
    return render_template("change_password.html")

@app.route("/business-info")
@login_required
def business_info():
    if session.get("role") != "CLIENT":
        return redirect("/profile")
        
    client = db.clients.find_one({"email": session.get("user")})
    return render_template("business_info.html", client=client)

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_email = session.get("user")
    role = session.get("role")
    client_id = session.get("client_id")

    collection = None
    if role == "CLIENT": collection = db.clients
    elif role == "ADMIN": collection = db.admin
    else: collection = db.user

    user_data = collection.find_one({"email": user_email})

    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "change_password":
            current_pw = request.form.get("current_password")
            new_pw = request.form.get("new_password")
            confirm_pw = request.form.get("confirm_password")
            
            if new_pw != confirm_pw:
                return render_template("profile_settings.html", user=user_data, error="Passwords do not match", active_tab="security")
                
            if user_data.get("password") != current_pw:
                return render_template("profile_settings.html", user=user_data, error="Incorrect current password", active_tab="security")
                
            collection.update_one({"email": user_email}, {"$set": {"password": new_pw}})
            return render_template("profile_settings.html", user=user_data, success="Password updated successfully", active_tab="security")

        if action == "update_preferences":
            language = request.form.get("language")
            timezone = request.form.get("timezone")
            date_format = request.form.get("date_format")
            
            update_data = {
                "language": language,
                "timezone": timezone,
                "date_format": date_format
            }
            collection.update_one({"email": user_email}, {"$set": update_data})
            user_data = collection.find_one({"email": user_email}) # Refresh data
            return render_template("profile_settings.html", user=user_data, success="Preferences updated", active_tab="preferences")

        if action == "delete_account":
            # Just a placeholder/logging for now as requested
            logger.warning(f"Account deletion requested for: {user_email}")
            return render_template("profile_settings.html", user=user_data, error="Please contact support to delete your account", active_tab="danger")

    # For GET: Fetch representative API key if client
    api_key = "No API Key found"
    if role == "CLIENT" and client_id:
        form = db.form_builders.find_one({"client_id": client_id})
        if form:
            api_key = form.get("api_key", "N/A")

    return render_template("profile_settings.html", user=user_data, api_key=api_key, role=role)

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
            
            # TRACK LAST LOGIN
            user_col.update_one({"email": user["email"]}, {"$set": {"last_login": datetime.utcnow()}})
            
            logger.info(f"User login successful: {user['email']}")
            return redirect("/dashboard")

        # 2) Check CLIENT collection
        client = db["clients"].find_one({"email": email, "password": password})
        if client:
            session["user"] = client["email"]
            session["role"] = "CLIENT"
            session["client_id"] = str(client["_id"])
            
            # TRACK LAST LOGIN
            db["clients"].update_one({"_id": client["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
            
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
            
            # TRACK LAST LOGIN
            admin_col.update_one({"email": admin["email"]}, {"$set": {"last_login": datetime.utcnow()}})
            
            logger.info(f"Admin login successful: {admin['email']}")
            return redirect("/super-dashboard")


        return render_template("login.html", error="Invalid credentials")

    return render_template("login.html")

@app.route("/dashboard")
@login_required
def dashboard():
    role = session.get("role")
    client_id = session.get("client_id")
    
    query = {}
    if role != "ADMIN":
        query = {"client_id": client_id}

    total = collection.count_documents(query)
    pending = collection.count_documents({**query, "status": "PENDING"})
    approved = collection.count_documents({**query, "status": "APPROVED"})
    rejected = collection.count_documents({**query, "status": "REJECTED"})

    recent = list(
        collection.find(query)
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
@login_required
def calls():
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
            "created_at": ""
        }
        
        # Format created_at timestamp
        if call.get("created_at"):
            formatted["created_at"] = call["created_at"].strftime("%Y-%m-%d %H:%M")
        
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
@client_required
def analytics():
    client_id = session.get("client_id")

    total = collection.count_documents({"client_id": client_id})
    confirmed = collection.count_documents({"client_id": client_id, "status": "APPROVED"})
    rejected = collection.count_documents({"client_id": client_id, "status": "REJECTED"})
    pending = collection.count_documents({"client_id": client_id, "status": "PENDING"})

    recent = list(
        collection.find({"client_id": client_id})
        .sort("created_at", -1)
        .limit(10)
    )

    # 1. Trend Data (Last 7 Days)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    pipeline = [
        {"$match": {
            "client_id": client_id,
            "created_at": {"$gte": seven_days_ago}
        }},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]
    trend_raw = list(collection.aggregate(pipeline))
    
    trend_labels = []
    trend_values = []
    for i in range(7):
        d = (datetime.utcnow() - timedelta(days=6-i)).strftime("%Y-%m-%d")
        trend_labels.append(d)
        val = next((x["count"] for x in trend_raw if x["_id"] == d), 0)
        trend_values.append(val)

    # 2. App Distribution
    app_pipeline = [
        {"$match": {"client_id": client_id}},
        {"$group": {"_id": "$app_name", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    app_dist_raw = list(collection.aggregate(app_pipeline))
    app_labels = [str(x["_id"] or "Default") for x in app_dist_raw]
    app_values = [x["count"] for x in app_dist_raw]

    return render_template(
        "analytics.html",
        total=total,
        confirmed=confirmed,
        cancelled=rejected,
        pending=pending,
        recent=recent,
        trend_labels=trend_labels,
        trend_values=trend_values,
        app_labels=app_labels,
        app_values=app_values
    )




import secrets

@app.route("/api-key", methods=["GET", "POST"])
@admin_required
def api_key_page():
    clients = list(db["clients"].find())

    # ===== GENERATE KEY =====
    if request.method == "POST":
        client_id = request.form.get("client_id")
        scope = request.form.get("scope") # "ALL_APPS" or "SINGLE_APP"
        app_name = request.form.get("app_name")

        key = "sk_" + secrets.token_hex(16)

        new_key_data = {
            "client_id": client_id,
            "api_key": key,
            "created_at": datetime.utcnow(),
            "scope": scope,
            "status": "active"
        }

        if scope == "SINGLE_APP" and app_name:
            new_key_data["app_name"] = app_name
        else:
            new_key_data["scope"] = "ALL_APPS" # Ensure default

        api_col.insert_one(new_key_data)

        return redirect("/api-key")

    # ===== SHOW KEYS =====
    raw_keys = list(api_col.find().sort("created_at", -1))
    keys = []

    # Get all apps to pass for dynamic dropdown
    all_apps = list(db.client_apps.find({}, {"_id": 0, "client_id": 1, "app_name": 1}))

    for k in raw_keys:
        cid = str(k["client_id"]).strip()
        client = db["clients"].find_one({"_id": cid})

        k["client_name"] = client["company_name"] if client else "Unknown"
        k["client_email"] = client["email"] if client else "-"
        
        # Legacy handling
        if "scope" not in k:
            k["scope"] = "LEGACY"
        
        k["status"] = k.get("status", "active")
        
        keys.append(k)

    return render_template(
        "api_key.html",
        clients=clients,
        keys=keys,
        all_apps=all_apps
    )

@app.route("/regenerate-key/<id>")
@admin_required
def regenerate_key(id):
    new_key = "sk_" + secrets.token_hex(16)

    api_col.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {
                "api_key": new_key,
                "created_at": datetime.utcnow()
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
    scope = api.get("scope", "ALL_APPS") # Default to all for legacy or explicit
    scoped_app = api.get("app_name")

    # Fetch call data
    query = {"client_id": client_id}
    
    # Enforce scope if it's a single app key
    if scope == "SINGLE_APP" and scoped_app:
        query["app_name"] = scoped_app

    calls = list(
        collection.find(
            query,
            {"_id": 0}   # hide mongo id
        ).sort("created_at", -1)
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
@admin_required
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

        last_call = calls.find({"client_id": cid}).sort("created_at", -1).limit(1)
        last_activity = ""
        for x in last_call:
            last_activity = x["created_at"]

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

        last_call = calls.find({"user_id": email}).sort("created_at", -1).limit(1)
        last_activity = ""
        for x in last_call:
            last_activity = x["created_at"]

        user_list.append({
            "email": email,
            "total": total,
            "pending": pending,
            "approved": approved,
            "last_activity": last_activity
        })

    # ---------------- RECENT CALLS ----------------
    recent = list(calls.find().sort("created_at", -1).limit(10))

    # ---------------- 📊 ANALYTICS DATA (NEW) ----------------
    
    # 1. System Calls Trend (Last 7 Days)
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    trend_pipeline = [
        {"$match": {"created_at": {"$gte": seven_days_ago}}},
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                "count": {"$sum": 1}
            }
        },
        {"$sort": {"_id": 1}}
    ]
    trend_data = list(calls.aggregate(trend_pipeline))

    # 2. Status Distribution
    status_dist = {
        "approved": approved_calls,
        "pending": pending_calls,
        "rejected": calls.count_documents({"status": "REJECTED"})
    }

    # 3. Client Activity (For horizontal bar chart)
    client_activity_pipeline = [
        {"$group": {"_id": "$client_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 8}
    ]
    raw_activity = list(calls.aggregate(client_activity_pipeline))
    client_activity = []
    for item in raw_activity:
        client_id_val = item.get("_id")
        client_doc = None
        if client_id_val and len(str(client_id_val)) == 24:
            try:
                client_doc = db["clients"].find_one({"_id": ObjectId(client_id_val)})
            except:
                pass
        
        client_name = client_doc.get("company_name", "Unknown") if client_doc else "Direct/User"
        client_activity.append({"name": client_name, "count": item["count"]})

    # 4. System Insights
    top_client_name = "N/A"
    if client_list:
        top_c = max(client_list, key=lambda x: x['total'])
        if top_c['total'] > 0:
            top_client_name = top_c['company']

    peak_day = "N/A"
    if trend_data:
        peak = max(trend_data, key=lambda x: x['count'])
        peak_day = peak['_id']

    avg_calls = round(total_calls / total_clients, 1) if total_clients > 0 else 0
    approval_rate = round((approved_calls / total_calls * 100), 1) if total_calls > 0 else 0

    system_insights = {
        "top_client": top_client_name,
        "peak_day": peak_day,
        "avg_calls": avg_calls,
        "approval_rate": approval_rate
    }

    # 5. Recent Activity Log (Interleaving new clients and calls)
    recent_clients = list(db["clients"].find().sort("_id", -1).limit(5))
    activity_log = []
    for c in recent_clients:
        t = c.get("_id").generation_time if hasattr(c.get("_id"), "generation_time") else datetime.utcnow()
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        activity_log.append({
            "type": "CLIENT_ADDED",
            "title": f"New client: {c.get('company_name')}",
            "time": t
        })
    for call in recent[:8]:
        t = call.get("created_at")
        if t and t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        title = f"Call for {call.get('name')} ({call.get('status')}) - {call.get('app_name', call.get('service', 'N/A'))}"
        if call.get("call_time"):
            st = call.get("call_time").strftime('%H:%M')
            title += f" [Scheduled: {st}]"
            
        activity_log.append({
            "type": "CALL",
            "title": title,
            "time": t
        })
    activity_log.sort(key=lambda x: x['time'] if x['time'] else datetime.min, reverse=True)

    return render_template(
        "super_dashboard.html",
        total_clients=total_clients,
        total_users=total_users,
        total_calls=total_calls,
        pending_calls=pending_calls,
        approved_calls=approved_calls,
        active_clients=active_clients,
        clients=client_list,
        users=user_list,
        recent=recent,
        trend_data=trend_data,
        status_dist=status_dist,
        client_activity=client_activity,
        system_insights=system_insights,
        activity_log=activity_log[:10]
    )
# ------------------ ADMIN : CLIENTS MANAGEMENT ------------------
@app.route("/manage-clients")
@admin_required
def manage_clients():

    # Summary Stats
    total_clients = db["clients"].count_documents({})
    active_clients = len(db["call_requests"].distinct("client_id")) # Simple metric for active
    total_apps = db.client_apps.count_documents({})
    total_forms = db.form_builders.count_documents({})

    raw_clients = list(db["clients"].find())
    enriched_clients = []
    
    for c in raw_clients:
        cid_str = str(c["_id"])
        
        # Stats for this client
        app_count = db.client_apps.count_documents({"client_id": cid_str})
        form_count = db.form_builders.count_documents({"client_id": cid_str})
        total_calls = db.call_requests.count_documents({"client_id": cid_str})
        
        # Last Activity
        last_req = db.call_requests.find_one({"client_id": cid_str}, sort=[("created_at", -1)])
        last_activity = last_req["created_at"] if last_req and "created_at" in last_req else None
        
        # Applications list for detail view
        client_apps = list(db.client_apps.find({"client_id": cid_str}))
        for app in client_apps:
            # Check if form exists for this app
            fb = db.form_builders.find_one({"client_id": cid_str, "app_name": app["app_name"]})
            app["has_form"] = True if fb else False

        enriched_clients.append({
            "id": cid_str,
            "company_name": c.get("company_name", "N/A"),
            "email": c.get("email", "N/A"),
            "phone": c.get("phone", "N/A"),
            "name": c.get("name", "N/A"),
            "created_at": c.get("_id").generation_time if hasattr(c.get("_id"), "generation_time") else None,
            "app_count": app_count,
            "form_count": form_count,
            "total_calls": total_calls,
            "last_activity": last_activity,
            "apps": client_apps,
            "status": "Active" if app_count > 0 or last_activity else "Inactive"
        })

    return render_template(
        "manage_clients.html", 
        clients=enriched_clients,
        stats={
            "total": total_clients,
            "active": active_clients,
            "apps": total_apps,
            "forms": total_forms
        }
    )


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
@admin_required
def system_analytics():

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
        {"$match": {"created_at": {"$gte": seven_days_ago}}},
        {
            "$group": {
                "_id": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}
                },
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    calls_per_day = list(collection.aggregate(pipeline_days))

    # ===== RECENT ACTIVITY =====
    recent = list(collection.find().sort("created_at", -1).limit(10))

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
@client_required
def booking_data():

    client_id = session.get("client_id")

    # GET ALL DATA FROM call_requests ONLY
    # Support both string and ObjectId if necessary, though login standardizes to string
    query = {"client_id": client_id}
    
    all_data = list(
        db.call_requests.find(query)
        .sort("created_at", -1)
    )
    
    # Debug: If still empty, try matching as ObjectId just in case legacy data exists
    if not all_data:
        try:
            oid_query = {"client_id": ObjectId(client_id)}
            all_data = list(db.call_requests.find(oid_query).sort("created_at", -1))
        except:
            pass
    
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
        # Requested Time (Submission Time) -> Always use created_at
        if d.get("created_at"):
            d["time"] = d["created_at"].strftime("%Y-%m-%d %I:%M %p")
        else:
            d["time"] = "Unknown"

        # Scheduled Call Time -> Preserved in d['call_time'] from DB
        # No change needed for call_time as template uses it directly

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
@admin_required
def admin_calls():

    client_id = request.args.get("client")
    app_filter = request.args.get("app")

    if not client_id:
        # ... (previous logic for listing clients)
        raw_clients = list(db["clients"].find({"role": "CLIENT"}))
        enriched_clients = []
        
        # Calculate summary stats
        total_clients = len(raw_clients)
        total_calls = db["call_requests"].count_documents({})
        active_clients = len(db["call_requests"].distinct("client_id"))

        for c in raw_clients:
            cid = str(c["_id"])
            app_count = db.client_apps.count_documents({"client_id": cid})
            client_calls = db.call_requests.count_documents({"client_id": cid})
            form = db.form_builders.find_one({"client_id": cid})
            app_name = form["app_name"] if form else None

            enriched_clients.append({
                "_id": cid,
                "company_name": c.get("company_name", "N/A"),
                "email": c.get("email", "N/A"),
                "app_count": app_count,
                "total_calls": client_calls,
                "app_name": app_name,
                "status": "Active" if client_calls > 0 or app_count > 0 else "Inactive"
            })

        return render_template(
            "admin_calls_clients.html", 
            clients=enriched_clients,
            stats={
                "total_clients": total_clients,
                "total_calls": total_calls,
                "active_clients": active_clients
            }
        )

    # VIEW CALLS FOR SPECIFIC CLIENT
    client = None
    if client_id:
        try:
            from bson import ObjectId
            # Try finding by ObjectId first
            if len(str(client_id)) == 24:
                client = db["clients"].find_one({"_id": ObjectId(client_id)})
        except:
            pass
        
        if not client:
            # Fallback to string ID check
            client = db["clients"].find_one({"_id": client_id})
        
        if not client:
            # Check secondary 'client_id' field for legacy/custom mappings
            client = db["clients"].find_one({"client_id": client_id})

    if not client:
        return "Client not found", 404

    # Fetch unique applications for this client
    apps = db["call_requests"].distinct("app_name", {"client_id": client_id})
    apps = [a for a in apps if a]

    query = {"client_id": client_id}
    if app_filter:
        query["app_name"] = app_filter

    calls = list(db["call_requests"].find(query).sort("created_at", -1))

    return render_template(
        "admin_calls_list.html",
        calls=calls,
        client=client,
        client_id=client_id,
        apps=apps,
        app_filter=app_filter
    )



from bson import ObjectId

@app.route("/update-status/<id>/<status>")
def update_status(id, status):
    if "role" not in session or session["role"] != "CLIENT":
        return redirect("/login")

    # Fetch booking details for notification
    booking = db.call_requests.find_one({"_id": ObjectId(id)})
    
    db.call_requests.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"status": status}}
    )

    # Send Notification if status is APPROVED or REJECTED
    if booking and status in ["APPROVED", "REJECTED"]:
        phone = booking.get("phone")
        app_name = booking.get("app_name", "your request")
        user_name = booking.get("name", booking.get("user_name", "Customer"))
        send_status_sms(phone, status, app_name, user_name)

        # Trigger Call if Approved
        if status == "APPROVED" and phone and twilio_client:
            try:
                clean_phone = "".join(filter(str.isdigit, str(phone)))
                target_phone = "+91" + clean_phone if len(clean_phone) == 10 else ("+" + clean_phone if not str(phone).startswith("+") else str(phone))
                
                twilio_client.calls.create(
                    to=target_phone,
                    from_=TWILIO_PHONE_NUMBER,
                    url=f"{RETELL_WEBHOOK}?call_id={id}&app_name={app_name}",
                )
                
                # Update DB to reflect call initiation
                db.call_requests.update_one(
                    {"_id": ObjectId(id)},
                    {"$set": {
                        "call_initiated": True, 
                        "call_initiated_at": datetime.utcnow()
                    }}
                )

                logger.info(f"✅ Triggered status-update call to {target_phone}")
            except Exception as e:
                logger.error(f"❌ Failed to trigger status-update call: {e}")

    return redirect("/analytics")

# ---------------- APPLICATION LIST ----------------
@app.route("/client/applications")
@client_required
def client_applications():
    client_id = session.get("client_id")

    # Check if client has any apps
    raw_apps = list(db.client_apps.find({"client_id": client_id}))
    
    # Auto-initialize default apps for NEW clients
    if not raw_apps:
        logger.info(f"🆕 New client detected ({client_id}). Initializing default apps...")
        defaults = ["Hotel", "Restaurant"]
        for app_name in defaults:
            slug = slugify(app_name)
            app_result = db.client_apps.insert_one({
                "client_id": client_id,
                "app_name": app_name,
                "slug": slug,
                "created_at": datetime.utcnow(),
                "submissions": 0
            })
            # Also create default form
            db.form_builders.insert_one({
                "client_id": client_id,
                "app_id": str(app_result.inserted_id),
                "app_name": app_name,
                "slug": slug,
                "fields": DEFAULT_FORM_FIELDS,
                "api_key": secrets.token_hex(16),
                "created_at": datetime.utcnow()
            })
        # Re-fetch after initialization
        raw_apps = list(db.client_apps.find({"client_id": client_id}))

    # Process for template
    client_apps = []
    for app in raw_apps:
        app["_id"] = str(app["_id"])
        # Add submission count
        app["submissions"] = db.call_requests.count_documents({
            "client_id": client_id,
            "app_name": app["app_name"]
        })
        client_apps.append(app)

    return render_template(
        "client_applications.html",
        client_apps=client_apps,
        client_id=client_id,
        client={"client_id": client_id}
    )


# ---------------- CREATE APPLICATION ----------------
import re

def slugify(text):
    return re.sub(r'[\W_]+', '-', text.lower()).strip('-')

@app.route("/client/create_application", methods=["POST"])
@client_required
def create_application():
    client_id = session.get("client_id")

    data = request.get_json()
    app_name = data.get("app_name")

    if not app_name:
        return jsonify({"error": "App name required"})

    existing = db.client_apps.find_one({
        "client_id": client_id,
        "app_name": app_name
    })

    if existing:
        return jsonify({"error": "App already exists"})

    slug = slugify(app_name)
    
    # Insert Application
    app_result = db.client_apps.insert_one({
        "client_id": client_id,
        "app_name": app_name,
        "slug": slug,
        "created_at": datetime.utcnow(),
        "submissions": 0
    })
    
    app_id = str(app_result.inserted_id)

    # Automatically Create Default Form
    db.form_builders.insert_one({
        "client_id": client_id,
        "app_id": app_id,
        "app_name": app_name,
        "slug": slug,
        "fields": DEFAULT_FORM_FIELDS,
        "api_key": secrets.token_hex(16),
        "created_at": datetime.now(IST).replace(tzinfo=None)
    })

    return jsonify({"status": "created", "app_id": app_id})

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
@client_required
def application_dashboard(app_name):
    client_id = session.get("client_id")

    # Verify app exists for this client
    app_data = db.client_apps.find_one({"app_name": app_name, "client_id": client_id})
    
    if not app_data:
        default_apps = ["Hotel", "Restaurant"]
        if app_name in default_apps:
            # Auto-initialize default app for this client
            slug = slugify(app_name)
            app_data = {
                "client_id": client_id,
                "app_name": app_name,
                "slug": slug,
                "created_at": datetime.now(IST).replace(tzinfo=None)
            }
            db.client_apps.insert_one(app_data)
        else:
            return "Application not found", 404

    # 1. Total Submissions (Unified)
    submissions_count = db.call_requests.count_documents({
        "app_name": app_name,
        "client_id": client_id
    })

    # 2. Recent Submissions
    recent_submissions = list(db.call_requests.find({
        "app_name": app_name,
        "client_id": client_id
    }).sort("created_at", -1).limit(5))

    # 3. 7-Day Trend
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    pipeline = [
        {"$match": {
            "app_name": app_name,
            "client_id": client_id,
            "created_at": {"$gte": seven_days_ago}
        }},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]
    trend_raw = list(db.call_requests.aggregate(pipeline))
    
    # Fill gaps for trend
    trend_labels = []
    trend_values = []
    for i in range(7):
        d = (datetime.utcnow() - timedelta(days=6-i)).strftime("%Y-%m-%d")
        trend_labels.append(d)
        val = next((x["count"] for x in trend_raw if x["_id"] == d), 0)
        trend_values.append(val)

    # Fetch Form Configuration for Preview
    form_cfg = db.form_builders.find_one({"app_name": app_name, "client_id": client_id})
    form_fields = form_cfg.get("fields", []) if form_cfg else []

    return render_template(
        "app_dashboard.html",
        app_name=app_name,
        submissions=submissions_count,
        recent_submissions=recent_submissions,
        trend_labels=trend_labels,
        trend_values=trend_values,
        client_id=client_id,
        form_fields=form_fields
    )



# ---------------- FORM BUILDER ----------------
@app.route("/client/application/<app_name>")
@client_required
def open_form_builder(app_name):

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

@app.route("/client/application/<app_name>/template")
@client_required
def get_app_template(app_name):
    # Try finding specific template for this app type
    template = db.form_templates.find_one({"app_type": app_name.lower()})
    
    if not template:
        # Fallback to very basic fields if no template exists
        fields = [
            {"label": "Full Name", "name": "name", "type": "text", "placeholder": "Enter your name"},
            {"label": "Phone Number", "name": "phone", "type": "tel", "placeholder": "Enter your phone"}
        ]
    else:
        fields = template.get("fields", [])
        
    return jsonify({"fields": fields})

# ---------------- SAVE FORM ----------------
import secrets

@app.route("/client/save_form/<app_name>", methods=["POST"])
@client_required
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
                # ENSURE THESE FIELDS ARE ALWAYS SAVED
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







# PUBLIC FORM (slug-based access)
@app.route("/form/<app_id>/<slug>")
def dynamic_public_form(app_id, slug):
    try:
        from bson import ObjectId
        # 1. Try exact app_id string
        form = db.form_builders.find_one({"app_id": app_id})
        
        # 2. Try ObjectId if string failed
        if not form:
            try:
                form = db.form_builders.find_one({"app_id": ObjectId(app_id)})
            except:
                pass
        
        # 3. Fallback: Try by app_id as if it were app_name (sometimes passed during dev)
        if not form:
            form = db.form_builders.find_one({"app_name": app_id})
            
        # 4. Fallback: Try case-insensitive app_name
        if not form:
             form = db.form_builders.find_one({"app_name": {"$regex": f"^{app_id}$", "$options": "i"}})
             
    except Exception as e:
        logger.error(f"Error finding form: {e}")
        form = None

    if not form:
        return "Form not found", 404

    return render_template(
        "client_open_form.html",
        form=form,
        fields=form.get("fields", []),
        app_name=form.get("app_name"),
        api_key=form.get("api_key")
    )

# Legacy route for backward compatibility
@app.route("/form/<client_id>/<app_name>")
def legacy_public_form_v2(client_id, app_name):
    from bson import ObjectId
    import re
    import secrets

    # Normalize inputs
    c_id_input = str(client_id).strip()
    a_name_input = str(app_name).strip()
    
    logger.info(f"🔍 DEBUG VIEW FORM: client='{c_id_input}', app='{a_name_input}'")

    # 1. FIND CLIENT APPLICATION (Case-insensitive)
    query = {
        "client_id": c_id_input,
        "app_name": {"$regex": f"^{re.escape(a_name_input)}$", "$options": "i"}
    }
    
    # Try finding the app first
    app_data = db.client_apps.find_one(query)
        
    # Try ObjectId conversion for client_id if not found
    if not app_data and len(c_id_input) == 24:
        try:
             query["client_id"] = ObjectId(c_id_input)
             app_data = db.client_apps.find_one(query)
        except: pass

    if not app_data:
        logger.error(f"❌ Application not found: {c_id_input}/{a_name_input}")
        return "Application not found for this client", 404

    # Use found data for reliable lookup
    real_app_name = app_data["app_name"]
    real_client_id = app_data["client_id"] # could be string or objectid
    
    logger.info(f"✅ Found App: {real_app_name} (Client: {real_client_id})")

    # 2. FIND FORM BUILDER WITH CASE-INSENSITIVE MATCH
    # Use the same regex strategy to find the form
    form = db.form_builders.find_one({
        "client_id": real_client_id,
        "app_name": {"$regex": f"^{re.escape(real_app_name)}$", "$options": "i"}
    })

    # 3. AUTO-CREATE IF MISSING
    if not form:
        logger.warning(f"⚠️ Form missing for {real_app_name}. Auto-creating default...")
        slug = app_data.get("slug") or re.sub(r'[\W_]+', '-', real_app_name.lower()).strip('-')
        
        new_form = {
            "client_id": real_client_id,
            "app_id": str(app_data["_id"]),
            "app_name": real_app_name,
            "slug": slug,
            "fields": DEFAULT_FORM_FIELDS,
            "api_key": secrets.token_hex(16),
            "created_at": datetime.now(IST).replace(tzinfo=None)
        }
        db.form_builders.insert_one(new_form)
        form = new_form
        logger.info("✅ Auto-created form successfully")

    # 4. VALIDATE FIELDS
    fields = form.get("fields", [])
    if not fields:
        return "Form is not configured yet", 404

    # 5. RENDER
    return render_template(
        "client_open_form.html",
        form=form,
        fields=fields,
        app_name=real_app_name,
        api_key=form.get("api_key")
    )

@app.route("/form/<app_name>")
def legacy_public_form(app_name):
    # Try to find by app_name case-insensitive
    form = db.form_builders.find_one({"app_name": {"$regex": f"^{app_name}$", "$options": "i"}})
    
    # If not found, try by app_id (in case the link was generated with ID)
    if not form:
        form = db.form_builders.find_one({"app_id": app_name})
        
    if form:
        # Redirect to the v2 legacy route which now handles rendering
        return redirect(f"/form/{form['client_id']}/{form['app_name']}")
    
    return "Form not found"

@app.route("/client/application/<app_name>/preview")
@client_required
def preview_application(app_name):
        
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
# ---------------- SUBMISSIONS ----------------
@app.route("/client/application/<app_name>/submissions")
@client_required
def view_submissions(app_name):
    client_id = session.get("client_id")
    app_data = db.client_apps.find_one({"app_name": app_name, "client_id": client_id})

    if not app_data:
        return "Application not found"

    submissions = list(db.call_requests.find({
        "app_name": app_name,
        "client_id": client_id
    }).sort("created_at", -1))

    return render_template(
        "submission.html",
        app_name=app_name,
        submissions=submissions
    )

# ---------------- SETTINGS ----------------
@app.route("/client/application/<app_name>/settings", methods=["GET", "POST"])
@client_required
def application_settings(app_name):
    client_id = session.get("client_id")
    # get application from DB
    app_data = db.client_apps.find_one({"app_name": app_name, "client_id": client_id})

    if not app_data:
        return "Application not found"

    # ---------- SAVE SETTINGS ----------
    if request.method == "POST":
        updated_name = request.form.get("app_name")
        active = True if request.form.get("status") == "on" else False
        public_form = True if request.form.get("public_form") == "on" else False
        llm_enabled = True if request.form.get("llm_enabled") == "on" else False

        db.client_apps.update_one(
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

        return redirect(url_for("application_settings", app_name=updated_name))

    # ---------- LOAD PAGE ----------
    return render_template("app_settings.html", app=app_data)

from flask import jsonify

@app.route("/client/application/<app_name>/delete", methods=["DELETE"])
@client_required
def delete_application(app_name):
    client_id = session.get("client_id")

    # delete from main apps collection
    db.client_apps.delete_one({
        "app_name": app_name,
        "client_id": client_id
    })

    # delete form config
    db.form_builders.delete_many({
        "app_name": app_name,
        "client_id": client_id
    })

    # delete submissions (unified call_requests)
    db.call_requests.delete_many({
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
    
    # Try to find client_id for this app
    form_config = db.form_builders.find_one({"app_name": app_name})
    client_id = form_config.get("client_id") if form_config else session.get("client_id")

    # Standardize data for unified "Booking Details" view
    data["app_name"] = app_name
    data["client_id"] = client_id
    data["status"] = "PENDING"
    # IST FIX: Use IST for submission timestamp
    data["created_at"] = datetime.now(IST).replace(tzinfo=None)
    
    # Try to extract name, phone, and time from dynamic form fields
    extracted_name = "Unknown"
    extracted_phone = "N/A"
    extracted_time = ""
    call_time = None

    for k, v in data.items():
        low_k = k.lower()
        if not data.get("name") and "name" in low_k:
            extracted_name = v
            data["name"] = v
        if not data.get("phone") and ("phone" in low_k or "mobile" in low_k or "contact" in low_k):
            extracted_phone = v
            data["phone"] = v
        if not data.get("time") and ("time" in low_k or "booking" in low_k or "slot" in low_k):
            extracted_time = v
            data["time"] = v
        
        # Detect Scheduled Call Time
        if "preferred_call_time" in low_k and v:
            try:
                # Expecting format from datetime-local input: YYYY-MM-DDTHH:MM
                call_time = datetime.strptime(v, "%Y-%m-%dT%H:%M")
                data["call_time"] = call_time
            except Exception as e:
                logger.error(f"Error parsing call_time {v}: {e}")

    # BASIC YEAR FIX: If year is 0026 (or < 2000), fix it
    if call_time and call_time.year < 2000:
        now_year = datetime.now(IST).year
        if call_time.year == 26:
             call_time = call_time.replace(year=2026)
        else:
             call_time = call_time.replace(year=now_year)
        logger.info(f"🔧 Fixed invalid year for call_time in submit_form. New: {call_time}")

    # Insert into unified collection
    db.call_requests.insert_one(data)

    # Notify Client
    if client_id:
        notify_client_sms(client_id, app_name, extracted_name, extracted_phone)

    return render_template("success.html", message="Your request has been submitted successfully!")


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
@client_required
def integration_page():
    client_id = session.get("client_id")

    apps = list(db.form_builders.find({"client_id": client_id}))

    return render_template("integration.html", apps=apps)

from bson import ObjectId



def send_status_sms(phone, status, app_name, user_name="Customer"):
    """Sends a professional, personalized Twilio SMS."""
    if not twilio_client or not phone:
        logger.warning("Twilio not configured or phone missing - skipping SMS.")
        return

    # Standardize phone format (+91 for India if exactly 10 digits)
    clean_phone = "".join(filter(str.isdigit, str(phone)))
    if len(clean_phone) == 10:
        target_phone = "+91" + clean_phone
    elif clean_phone.startswith("91") and len(clean_phone) == 12:
        target_phone = "+" + clean_phone
    elif not str(phone).startswith("+"):
        target_phone = "+" + clean_phone
    else:
        target_phone = str(phone)

    # Impressive, personalized message
    if status == "APPROVED":
        msg_body = f"High five, {user_name}! 🚀 Your request for '{app_name}' has been APPROVED. We are excited to move forward with you! - ConnexHub"
    else:
        msg_body = f"Hello {user_name}, thank you for your interest in '{app_name}'. Your request has been REJECTED at this time. We wish you the best! - ConnexHub"
    
    try:
        response = twilio_client.messages.create(
            body=msg_body,
            from_=TWILIO_PHONE_NUMBER,
            to=target_phone
        )
        logger.info(f"✅ Enhanced SMS SID: {response.sid} sent to {target_phone} for {user_name}")
    except Exception as e:
        logger.error(f"❌ Twilio Error sending SMS to {target_phone}: {e}")
        logger.error(traceback.format_exc())

def notify_client_sms(client_id, app_name, user_name, user_phone):
    """Notifies the business owner (Client) about a new booking request."""
    if not twilio_client:
        return

    # Try identification by Email, then direct String ID, then ObjectId
    client = db.clients.find_one({"email": str(client_id)})
    if not client:
        client = db.clients.find_one({"_id": str(client_id)})
    if not client and len(str(client_id)) == 24:
        try:
            client = db.clients.find_one({"_id": ObjectId(client_id)})
        except:
            pass

    if not client:
        logger.warning(f"⚠️ Could not find client in DB with ID: {client_id}")
        return

    client_phone = client.get("phone") or client.get("number")

    if not client_phone:
        logger.warning(f"Could not find phone or number for client {client_id}")
        return
    
    # Standardize phone format
    clean_phone = "".join(filter(str.isdigit, str(client_phone)))
    if len(clean_phone) == 10:
        target_phone = "+91" + clean_phone
    elif not str(client_phone).startswith("+"):
        target_phone = "+" + clean_phone
    else:
        target_phone = client_phone

    msg_body = f"ConnexHub Alert: ⚡ You have a new booking request from {user_name} ({user_phone}) for {app_name}. Visit your dashboard to approve it!"

    try:
        response = twilio_client.messages.create(
            body=msg_body,
            from_=TWILIO_PHONE_NUMBER,
            to=target_phone
        )
        logger.info(f"✅ Client SMS SID: {response.sid} notified of new request from {user_name}")
    except Exception as e:
        logger.error(f"❌ Twilio Error notifying client {client_id} at {target_phone}: {e}")
        logger.error(traceback.format_exc())

# APPROVE
@app.route("/approve/<id>")
@client_required
def approve_booking(id):
    # Robust lookup supporting both ObjectId and string
    try:
        obj_id = ObjectId(id)
        booking = db.call_requests.find_one({"_id": obj_id})
    except:
        booking = db.call_requests.find_one({"_id": id})
        
    if not booking:
        logger.warning(f"⚠️ Booking not found for approval: {id}")
        return redirect(url_for("booking_data"))

    # Update status
    db.call_requests.update_one(
        {"_id": booking["_id"]},
        {"$set": {"status": "APPROVED"}}
    )

    phone = booking.get("phone")
    app_name = booking.get("app_name", "your request")
    user_name = booking.get("name", booking.get("user_name", "Customer"))

    # Send Notification
    send_status_sms(phone, "APPROVED", app_name, user_name)

    # IST FIX: Use IST now
    call_time = booking.get("call_time", None)
    now_ist = datetime.now(IST).replace(tzinfo=None)
    
    if call_time and call_time > now_ist:
        logger.info(f"Booking {id} approved but scheduled for {call_time}. Skipping immediate call.")
    else:
        if phone:
            trigger_voice_call(str(booking["_id"]), phone, app_name)

    return redirect(url_for("booking_data"))


# REJECT
@app.route("/reject/<id>")
@client_required
def reject_booking(id):
    try:
        obj_id = ObjectId(id)
        booking = db.call_requests.find_one({"_id": obj_id})
    except:
        booking = db.call_requests.find_one({"_id": id})
        
    if not booking:
        return redirect(url_for("booking_data"))

    db.call_requests.update_one(
        {"_id": booking["_id"]},
        {"$set": {"status": "REJECTED"}}
    )

    # Send Notification
    phone = booking.get("phone")
    app_name = booking.get("app_name", "your request")
    user_name = booking.get("name", booking.get("user_name", "Customer"))
    send_status_sms(phone, "REJECTED", app_name, user_name)

    return redirect(url_for("booking_data"))



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
    time_label = ""
    time_field_name = ""
    
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

        # Check for time
        if "time" in label_text or "time" in field_name or "slot" in label_text or "booking" in label_text:
            time_label = field.get("label")
            time_field_name = field.get("name")

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

    extracted_time = ""
    if time_label and time_label in data:
        extracted_time = data.get(time_label)
    elif time_field_name and time_field_name in data:
        extracted_time = data.get(time_field_name)
    
    # Fallback to common field names if labels didn't work
    if not user_name:
        for key in ["name", "Name", "full_name", "Full Name", "fullname", "user_name", "username"]:
            if key in data:
                user_name = data[key]
                break

    # Extract call time if present
    call_time = None
    pref_call_time = data.get("preferred_call_time")
    if pref_call_time:
        try:
            call_time = datetime.fromisoformat(pref_call_time)
        except:
            try:
                # Handle common format YYYY-MM-DD HH:MM
                call_time = datetime.strptime(pref_call_time, "%Y-%m-%d %H:%M")
            except:
                logger.warning(f"Could not parse preferred_call_time: {pref_call_time}")

    # BASIC YEAR FIX: If year is 0026 (or < 2000), fix it to current year or next year
    if call_time and call_time.year < 2000:
        now_year = datetime.now(IST).year
        # If user meant 2026, but it came as 0026, add 2000
        if call_time.year == 26:
             call_time = call_time.replace(year=2026)
        else:
             call_time = call_time.replace(year=now_year)
        logger.info(f"🔧 Fixed invalid year for call_time. New: {call_time}")
    
    if not phone_num:
        for key in ["phone", "Phone", "phone_number", "Phone Number", "mobile", "Mobile", "phone number"]:
            if key in data:
                phone_num = data[key]
                logger.info(f"✅ Found phone via fallback key: '{key}' = '{phone_num}'")
                break

    if not extracted_time:
        for key in ["time", "booking", "slot", "booking time", "preferred time"]:
            for dk in data.keys():
                if key in dk.lower():
                    extracted_time = data[dk]
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
        "time": extracted_time,
        "data": data,  # All form fields
        "ai_reply": ai_reply,
        "query": ai_reply[:100] if ai_reply else "",  # Short summary for table view
        "status": "PENDING",
        "call_time": call_time,
        "time_str": datetime.now(IST).strftime("%H:%M"),
        "created_at": datetime.now(IST).replace(tzinfo=None),
        "createdAt": datetime.now(IST).replace(tzinfo=None)  # For backward compatibility
    }
    call_req_result = db.call_requests.insert_one(call_request)
    call_id = str(call_req_result.inserted_id)

    # Notify Client
    notify_client_sms(client_id, app_name, display_name, display_phone)

    # ==========================================
    # 🔥 STEP 4: TRIGGER VOICE CALL (DISABLED PER USER REQUEST - SMS ONLY ON APPROVAL)
    # ==========================================
    call_initiated = False
    call_error = None
    
    # if phone_num and twilio_client:
    #     try:
    #         # Ensure phone has country code
    #         if not phone_num.startswith('+'):
    #             phone_num = '+91' + phone_num  # Default to India
    #         
    #         twilio_client.calls.create(
    #             to=phone_num,
    #             from_=TWILIO_PHONE_NUMBER,
    #             url=f"{RETELL_WEBHOOK}?call_id={call_id}&app_name={app_name}",
    #         )
    #         
    #          # Track call initiation WITHOUT changing status (keep as PENDING for approval)
    #         db.call_requests.update_one(
    #             {"_id": call_req_result.inserted_id},
    #             {"$set": {"call_initiated": True, "call_initiated_at": datetime.now(IST).replace(tzinfo=None)}}
    #         )
    #         
    #         call_initiated = True
    #         logger.info(f"✅ Triggered automated call for {app_name} to {phone_num}")
    #         
    #     except Exception as e:
    #         call_error = str(e)
    #         logger.error(f"❌ Failed to trigger automated call: {e}")
    #         
    #         # Track call failure WITHOUT changing status
    #         db.call_requests.update_one(
    #             {"_id": call_req_result.inserted_id},
    #             {"$set": {"call_initiated": False, "call_error": call_error}}
    #         )
    # elif not phone_num:
    #     logger.warning(f"⚠️ No valid phone number provided for {app_name}")
    # elif not twilio_client:
    #     logger.warning(f"⚠️ Twilio client not initialized - calls disabled")

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
@client_required
def save_form_builder():

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
@client_required
def api_page(app_name):

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
@client_required
def regen_key(app_name):
    new_key = secrets.token_hex(16)

    db.form_builders.update_one(
        {"app_name": app_name, "client_id": session["client_id"]},
        {"$set": {"api_key": new_key}}
    )
    return {"status":"ok"}


@app.route("/revoke_key/<app_name>")
@client_required
def revoke_key_api(app_name):
    db.form_builders.update_one(
        {"app_name": app_name, "client_id": session["client_id"]},
        {"$set": {"api_key": None}}
    )
    return {"status":"revoked"}


@app.route("/client/application/<app_name>/open")
@client_required
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
@client_required
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
@client_required
def llm_settings():
    client_id = session.get("client_id")

    # Fetch from correct collection
    applications = list(db.client_apps.find({"client_id": client_id}))

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
#         "created_at": datetime.now(IST).replace(tzinfo=None)
#     })
#
#     return jsonify({"status": "created"})
#



@app.route("/client/update_booking_status", methods=["POST"])
@client_required
def update_booking_status():
    data = request.get_json()
    booking_id = data.get("id")
    status = data.get("status")

    # Fetch booking details for notification
    booking = db.call_requests.find_one({"_id": ObjectId(booking_id)})

    db.call_requests.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {"status": status}}
    )

    # Send Notification if status is APPROVED or REJECTED
    if booking and status in ["APPROVED", "REJECTED"]:
        phone = booking.get("phone")
        app_name = booking.get("app_name", "your request")
        user_name = booking.get("name", booking.get("user_name", "Customer"))
        send_status_sms(phone, status, app_name, user_name)

        # Trigger Call if Approved
        if status == "APPROVED" and phone and twilio_client:
            try:
                clean_phone = "".join(filter(str.isdigit, str(phone)))
                target_phone = "+91" + clean_phone if len(clean_phone) == 10 else ("+" + clean_phone if not str(phone).startswith("+") else str(phone))
                
                twilio_client.calls.create(
                    to=target_phone,
                    from_=TWILIO_PHONE_NUMBER,
                    url=f"{RETELL_WEBHOOK}?call_id={booking_id}&app_name={app_name}",
                )
                logger.info(f"✅ Triggered AJAX-update call to {target_phone}")
            except Exception as e:
                logger.error(f"❌ Failed to trigger AJAX-update call: {e}")

    return jsonify({"success": True})



@app.route("/debug-forms")
def debug_forms():
    forms = list(db.form_builders.find({}))
    result = []
    for f in forms:
        result.append({
            "id": str(f.get("_id")),
            "app_name": f.get("app_name"),
            "client_id": str(f.get("client_id")),
            "client_id_type": str(type(f.get("client_id"))),
            "app_id": str(f.get("app_id")),
            "slug": f.get("slug")
        })
    return jsonify(result)

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
# ------------------ ADMIN : CLIENT OPERATIONS (Iteration 2) ------------------

@app.route("/client/<client_id>/applications")
def client_shortcut_apps(client_id):
    return redirect(f"/admin/calls?client={client_id}")

@app.route("/client/<client_id>/forms")
def client_shortcut_forms(client_id):
    # For matching user request "View Forms", we'll redirect to a filtered view
    return redirect(f"/admin/calls?client={client_id}")

@app.route("/client/<client_id>/dashboard")
def client_shortcut_dashboard(client_id):
    return redirect("/system-analytics")

@app.route("/admin/delete-client/<client_id>")
@admin_required
def delete_client(client_id):
    
    try:
        from bson import ObjectId
        # 1. Delete client record
        db["clients"].delete_one({"_id": ObjectId(client_id)})
        # 2. Delete related apps, forms, calls
        db.client_apps.delete_many({"client_id": client_id})
        db.form_builders.delete_many({"client_id": client_id})
        db.call_requests.delete_many({"client_id": client_id})
        
        return redirect("/manage-clients")
    except Exception as e:
        print(f"Error deleting client: {e}")
        return redirect("/manage-clients")

@app.route("/admin/create-client", methods=["GET", "POST"])
@admin_required
def create_client():

    if request.method == "POST":
        company = request.form.get("company_name")
        email = request.form.get("email")
        password = request.form.get("password", "temp123")
        name = request.form.get("name", "")
        number = request.form.get("number", "")
        
        if db["clients"].find_one({"email": email}):
            return "Email already exists", 400
            
        db["clients"].insert_one({
            "company_name": company,
            "email": email,
            "password": password,
            "role": "CLIENT",
            "name": name,
            "number": number,
            "phone": number # Keep both for compatibility
        })
        return redirect("/manage-clients")
        
    return redirect("/manage-clients")

@app.route("/admin/export-clients")
@admin_required
def export_clients():

    import csv
    import io
    
    clients = list(db["clients"].find())
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Client ID', 'Company', 'Email', 'Role'])
    
    for c in clients:
        writer.writerow([str(c['_id']), c.get('company_name'), c.get('email'), c.get('role')])
        
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=clients_export.csv"}
    )

if __name__ == "__main__":
    if os.getenv("FLASK_ENV") == "production":
        from waitress import serve
        logger.info("Starting production server on port 5000...")
        serve(app, host="0.0.0.0", port=5000)
    else:
        app.run(port=5000, debug=True, use_reloader=False)
