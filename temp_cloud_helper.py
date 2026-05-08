import os
import requests

def upload_to_temp_cloud(file_path):
    """Uploads a file to GoFile (Temp Cloud) and returns the download link."""
    try:
        # 1. Get the best server available
        server_res = requests.get("https://api.gofile.io/servers").json()
        if server_res['status'] != 'ok':
            raise Exception("GoFile API is currently unavailable.")
            
        server = server_res['data']['servers'][0]['name']
        upload_url = f"https://{server}.gofile.io/contents/uploadfile"
        
        # 2. Upload the file
        with open(file_path, 'rb') as f:
            files = {'file': f}
            upload_res = requests.post(upload_url, files=files).json()
            
        if upload_res['status'] == 'ok':
            return upload_res['data']['downloadPage']
        else:
            raise Exception(upload_res.get('status', 'Unknown upload error'))
            
    except Exception as e:
        raise Exception(f"Temp Cloud Upload Error: {str(e)}")