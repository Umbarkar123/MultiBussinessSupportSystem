from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()
client = MongoClient(os.getenv('MONGO_URI'))
for db_name in client.list_database_names():
    print(f"\nDB: {db_name}")
    d = client.get_database(db_name)
    for c in d.list_collection_names():
        count = d[c].count_documents({})
        if count > 0:
            print(f"  - {c}: {count}")

db = client.get_database() # default from URI

print(f"Total in call_requests: {db.call_requests.count_documents({})}")
print(f"Docs with string client_id: {db.call_requests.count_documents({'client_id': {'$type': 'string'}})}")
print(f"Docs with ObjectId client_id: {db.call_requests.count_documents({'client_id': {'$type': 'objectId'}})}")

# Also check for empty/missing client_id
print(f"Docs with missing client_id: {db.call_requests.count_documents({'client_id': {'$exists': False}})}")
print(f"Docs with null client_id: {db.call_requests.count_documents({'client_id': None})}")

print("\nAll collections and counts:")
for coll_name in db.list_collection_names():
    print(f"Collection: {coll_name} | Count: {db[coll_name].count_documents({})}")

# sample a few from likely candidates
candidates = ["submissions", "form_submissions", "bookings", "requests"]
for cand in candidates:
    if cand in db.list_collection_names():
        print(f"\nSample from {cand}:")
        for doc in db[cand].find().limit(2):
            print(doc)
