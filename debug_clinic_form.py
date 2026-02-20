
from app import app, db
from bson import ObjectId

with app.app_context():
    print("--- Searching for specific 'clinic' app ---")
    
    # 1. Find in client_apps
    client_app = db.client_apps.find_one({"app_name": "clinic"})
    if not client_app:
        # try case insensitive
        client_app = db.client_apps.find_one({"app_name": {"$regex": "^clinic$", "$options": "i"}})
        
    if client_app:
        print(f"✅ Found Client App: {client_app.get('app_name')}")
        print(f"   _id: {client_app.get('_id')} (Type: {type(client_app.get('_id'))})")
        print(f"   client_id: {client_app.get('client_id')}")
        print(f"   slug: {client_app.get('slug')}")
        
        app_id_str = str(client_app.get('_id'))
        
        # 2. Find in form_builders using app_id string
        print(f"\n--- Searching Form Builder by app_id string: '{app_id_str}' ---")
        form_by_str = db.form_builders.find_one({"app_id": app_id_str})
        if form_by_str:
            print(f"✅ Found Form (by string ID):")
            print(f"   _id: {form_by_str.get('_id')}")
            print(f"   app_name: {form_by_str.get('app_name')}")
        else:
            print("❌ NOT FOUND by string ID")

        # 3. Find in form_builders using ObjectId
        print(f"\n--- Searching Form Builder by ObjectId: {client_app.get('_id')} ---")
        form_by_obj = db.form_builders.find_one({"app_id": client_app.get('_id')})
        if form_by_obj:
            print(f"✅ Found Form (by ObjectId):")
            print(f"   _id: {form_by_obj.get('_id')}")
        else:
            print("❌ NOT FOUND by ObjectId")
            
        # 4. Find in form_builders by app_name
        print(f"\n--- Searching Form Builder by app_name: '{client_app.get('app_name')}' ---")
        form_by_name = db.form_builders.find_one({"app_name": client_app.get('app_name')})
        if form_by_name:
            print(f"✅ Found Form (by name):")
            print(f"   app_id stored: {form_by_name.get('app_id')} (Type: {type(form_by_name.get('app_id'))})")
        else:
            print("❌ NOT FOUND by name")

    else:
        print("❌ 'clinic' app not found in client_apps")
