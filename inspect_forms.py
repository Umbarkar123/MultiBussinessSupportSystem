
import sys
import os
sys.path.append(os.getcwd())
from app import app, db

with app.app_context():
    print("--- DUMPING FORM BUILDERS ---")
    forms = list(db.form_builders.find({}))
    for f in forms:
        print(f"ID: {f.get('_id')}")
        print(f"  App Name: '{f.get('app_name')}'")
        print(f"  Client ID: '{f.get('client_id')}' (Type: {type(f.get('client_id'))})")
        print(f"  App ID: {f.get('app_id')}")
        print(f"  Slug: {f.get('slug')}")
        print("-" * 20)
