import os
from pymongo import MongoClient
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client.get_database()

print("--- CLIENTS ---")
for c in db.clients.find():
    print(f"ID: {c.get('_id')}, Email: {c.get('email')}, Phone: {c.get('phone')}, Business: {c.get('business_name')}")

print("\n--- RECENT CALL REQUESTS ---")
for r in db.call_requests.find().sort("created_at", -1).limit(3):
    print(f"ID: {r.get('_id')}, Name: {r.get('name')}, Phone: {r.get('phone')}, Status: {r.get('status')}, ClientID: {r.get('client_id')}")

print("\n--- FORM BUILDERS ---")
for f in db.form_builders.find():
    print(f"App: {f.get('app_name')}, ClientID: {f.get('client_id')}")
