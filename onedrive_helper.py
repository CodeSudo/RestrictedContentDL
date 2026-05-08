import os
import json
import requests
from config import PyroConf

TOKEN_FILE = 'onedrive_tokens.json'
REDIRECT_URI = 'http://localhost:8080/'
SCOPES = 'Files.ReadWrite offline_access'
AUTH_URL = 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'
TOKEN_URL = 'https://login.microsoftonline.com/common/oauth2/v2.0/token'

def load_tokens():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_token(user_id, token_data):
    tokens = load_tokens()
    tokens[str(user_id)] = token_data
    with open(TOKEN_FILE, 'w') as f:
        json.dump(tokens, f)

def get_onedrive_auth_url(user_id):
    return f"{AUTH_URL}?client_id={PyroConf.ONEDRIVE_CLIENT_ID}&response_type=code&redirect_uri={REDIRECT_URI}&scope={SCOPES}"

def authorize_onedrive_user(user_id, auth_response_url):
    from urllib.parse import urlparse, parse_qs
    parsed_url = urlparse(auth_response_url)
    code = parse_qs(parsed_url.query).get('code')
    if not code: raise Exception("Auth code not found in URL.")
        
    data = {
        'client_id': PyroConf.ONEDRIVE_CLIENT_ID,
        'client_secret': PyroConf.ONEDRIVE_CLIENT_SECRET,
        'code': code[0],
        'redirect_uri': REDIRECT_URI,
        'grant_type': 'authorization_code'
    }
    res = requests.post(TOKEN_URL, data=data).json()
    if 'access_token' not in res: raise Exception(res.get('error_description'))
    save_token(user_id, res)

def get_valid_token(user_id):
    tokens = load_tokens()
    user_data = tokens.get(str(user_id))
    if not user_data: return None
        
    data = {
        'client_id': PyroConf.ONEDRIVE_CLIENT_ID,
        'client_secret': PyroConf.ONEDRIVE_CLIENT_SECRET,
        'refresh_token': user_data['refresh_token'],
        'grant_type': 'refresh_token'
    }
    res = requests.post(TOKEN_URL, data=data).json()
    if 'access_token' in res:
        if 'refresh_token' not in res: res['refresh_token'] = user_data['refresh_token']
        save_token(user_id, res)
        return res['access_token']
    return None

def upload_to_onedrive_user(user_id, file_path, custom_folder=None):
    token = get_valid_token(user_id)
    if not token: raise Exception("Auth required")
    headers = {"Authorization": f"Bearer {token}"}
    
    file_name = os.path.basename(file_path)
    folder = custom_folder if custom_folder else "Telegram downloads"
    
    # Create Upload Session
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{folder}/{file_name}:/createUploadSession"
    session = requests.post(url, headers=headers).json()
    if 'uploadUrl' not in session: raise Exception(f"Session Error: {session}")
    
    upload_url = session['uploadUrl']
    size = os.path.getsize(file_path)
    chunk_size = 327680 * 10 
    
    with open(file_path, "rb") as f:
        start = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk: break
            end = start + len(chunk) - 1
            h = {"Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{size}"}
            requests.put(upload_url, headers=h, data=chunk)
            start += len(chunk)

    meta = requests.get(f"https://graph.microsoft.com/v1.0/me/drive/root:/{folder}/{file_name}", headers=headers).json()
    return meta.get('webUrl')