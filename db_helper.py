import json
import os
from config import PyroConf  # <-- Import your config

DB_FILE = 'users.json'

def load_db():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w') as f:
            json.dump({}, f)
        return {}
    with open(DB_FILE, 'r') as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def get_user_role(user_id):
    # Check against the variable from your config file!
    if user_id == PyroConf.SUPER_ADMIN_ID:
        return "admin"
    db = load_db()
    return db.get(str(user_id), "user")

def set_user_role(user_id, role):
    db = load_db()
    db[str(user_id)] = role
    save_db(db)

def get_all_users():
    return load_db()