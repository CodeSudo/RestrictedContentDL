import os
import json
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Allow HTTP redirect URIs since users will be pasting a localhost URL
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

SCOPES = ['https://www.googleapis.com/auth/drive.file']
CLIENT_SECRETS_FILE = 'credentials.json'
USER_TOKENS_FILE = 'user_tokens.json'

def load_user_tokens():
    if os.path.exists(USER_TOKENS_FILE):
        with open(USER_TOKENS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_user_token(user_id, creds_dict):
    tokens = load_user_tokens()
    tokens[str(user_id)] = creds_dict
    with open(USER_TOKENS_FILE, 'w') as f:
        json.dump(tokens, f)

def get_user_credentials(user_id):
    """Retrieve saved credentials for a specific Telegram user."""
    tokens = load_user_tokens()
    user_id_str = str(user_id)
    if user_id_str in tokens:
        creds = Credentials.from_authorized_user_info(tokens[user_id_str], SCOPES)
        # Auto-refresh if expired
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_user_token(user_id, json.loads(creds.to_json()))
        return creds
    return None

def generate_auth_url():
    """Generates the Google login link for the user."""
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri='http://localhost:8080/'
    )
    auth_url, _ = flow.authorization_url(prompt='consent')
    return auth_url

def authorize_user(user_id, auth_response_url):
    """Exchanges the localhost URL the user pastes for permanent tokens."""
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri='http://localhost:8080/'
    )
    flow.fetch_token(authorization_response=auth_response_url)
    creds = flow.credentials
    save_user_token(user_id, json.loads(creds.to_json()))

def get_or_create_folder(service):
    """Finds or creates the 'Telegram downloads' folder in the user's Drive."""
    folder_name = 'Telegram downloads'
    # Query Drive for a folder with this exact name
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])
    
    if not items:
        # Create it if it doesn't exist
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')
    return items[0].get('id')

def upload_to_drive_user(user_id, file_path):
    """Uploads a file directly to the user's personal Drive."""
    creds = get_user_credentials(user_id)
    if not creds:
        raise Exception("Authentication required")

    service = build('drive', 'v3', credentials=creds)
    folder_id = get_or_create_folder(service)
    
    file_metadata = {
        'name': os.path.basename(file_path),
        'parents': [folder_id]
    }
    
    media = MediaFileUpload(file_path, resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
    
    # We do NOT make permissions public here because it's their own Drive! They already have access.
    return file.get('webViewLink')