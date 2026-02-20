
import sys
import os
sys.path.append(os.getcwd())
from app import app, db
from bson import ObjectId

with app.app_context():
    print("--- Searching for 'clinic' form ---")
    
    # Check by app_name
    forms = list(db.form_builders.find({"app_name": "clinic"}))
    if not forms:
        forms = list(db.form_builders.find({"app_name": {"$regex": "^clinic$", "$options": "i"}}))
        
    for f in forms:
        print(f"Found Form:")
        print(f"  _id: {f.get('_id')}")
        print(f"  app_name: '{f.get('app_name')}'")
        print(f"  client_id: '{f.get('client_id')}' (Type: {type(f.get('client_id'))})")
        print(f"  app_id: {f.get('app_id')}")
        print(f"  slug: {f.get('slug')}")
