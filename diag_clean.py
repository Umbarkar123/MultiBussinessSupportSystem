import os
import certifi
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(
    MONGO_URI,
    server_api=ServerApi('1'),
    tlsCAFile=certifi.where()
)

db_name = "voice_agent_db"
if "/" in MONGO_URI.split("@")[-1]:
     extracted = MONGO_URI.split("@")[-1].split("/")[1].split("?")[0]
     if extracted: db_name = extracted

db = client.get_database(db_name)
print(f"Connected to DB: {db_name}")

colls = db.list_collection_names()
print(f"Collections found: {len(colls)}")
for c in sorted(colls):
    count = db[c].count_documents({})
    print(f"  - {c}: {count}")
    if count > 0 and c in ["call_requests", "submissions", "form_submissions", "booking_data"]:
        print(f"    Sample from {c}:")
        for doc in db[c].find().limit(1):
            print(f"    {doc}")
