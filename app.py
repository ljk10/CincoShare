import os
import pathlib
import csv
import requests
import uuid
import time
from datetime import datetime
from flask import Flask, session, abort, redirect, request, render_template, url_for
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from pip._vendor import cachecontrol
import google.auth.transport.requests
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "SuperSecretKey123"  # Change this for production

# --- CONFIGURATION (YOU MUST FILL THESE IN) ---

# 1. PASTE YOUR GOOGLE DRIVE FOLDER ID HERE
PARENT_FOLDER_ID = '1vczJrfJq_TemIeaaAoFiBptGeBedimE0' 

# 2. PASTE YOUR ADMIN EMAIL HERE
ADMIN_EMAIL = "louiskavalam4@gmail.com"

# 3. YOUR NGROK DOMAIN
MY_DOMAIN = "https://cincoshare.onrender.com"

# --- SETTINGS ---
CLIENT_SECRETS_FILE = os.path.join(pathlib.Path(__file__).parent, "client_secret.json")
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'mov', 'avi', 'mkv', 'heic', 'webp'}

# Allow HTTP (essential for localhost/tunnels)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
# Allow large uploads (1GB limit)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024 

# Fix for Flask/Ngrok
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# --- HELPERS ---

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_drive_service(user_creds_dict):
    """Builds the Drive Service using the Logged-in User's credentials"""
    creds = Credentials(**user_creds_dict)
    return build('drive', 'v3', credentials=creds)

def log_to_csv(name, email, filename):
    file_exists = os.path.isfile('admin_log.csv')
    with open('admin_log.csv', mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(['Timestamp', 'User Name', 'User Email', 'Filename'])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name, email, filename])

# --- ROUTES ---

@app.route("/")
def index():
    if "google_id" in session:
        return redirect("/dashboard")
    return render_template("login.html")

@app.route('/favicon.ico')
def favicon():
    return "", 204  # Silence the 404 error

@app.route("/login")
def login():
    redirect_uri = f"{MY_DOMAIN}/callback"
    flow = Flow.from_client_secrets_file(
        client_secrets_file=CLIENT_SECRETS_FILE,
        scopes=[
            "https://www.googleapis.com/auth/userinfo.profile", 
            "https://www.googleapis.com/auth/userinfo.email", 
            "https://www.googleapis.com/auth/drive.file",
            "openid"
        ],
        redirect_uri=redirect_uri 
    )
    authorization_url, state = flow.authorization_url(access_type='offline')
    session["state"] = state
    return redirect(authorization_url)

@app.route("/callback")
def callback():
    redirect_uri = f"{MY_DOMAIN}/callback"
    flow = Flow.from_client_secrets_file(
        client_secrets_file=CLIENT_SECRETS_FILE,
        scopes=[
            "https://www.googleapis.com/auth/userinfo.profile", 
            "https://www.googleapis.com/auth/userinfo.email", 
            "https://www.googleapis.com/auth/drive.file",
            "openid"
        ],
        redirect_uri=redirect_uri
    )
    flow.redirect_uri = redirect_uri
    authorization_response = request.url.replace('http:', 'https:')
    flow.fetch_token(authorization_response=authorization_response)

    credentials = flow.credentials
    session['credentials'] = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

    request_session = requests.session()
    cached_session = cachecontrol.CacheControl(request_session)
    token_request = google.auth.transport.requests.Request(session=cached_session)
    id_info = id_token.verify_oauth2_token(
        id_token=credentials.id_token,
        request=token_request,
        audience=credentials.client_id,
        clock_skew_in_seconds=60
    )

    session["google_id"] = id_info.get("sub")
    session["name"] = id_info.get("name")
    session["email"] = id_info.get("email")
    return redirect("/dashboard")

@app.route("/dashboard")
def dashboard():
    if "google_id" not in session:
        return redirect("/")
    return render_template("dashboard.html", name=session['name'])

@app.route("/upload", methods=['POST'])
def upload():
    if "google_id" not in session:
        return redirect("/")
    
    files = request.files.getlist('file')
    if not files or files[0].filename == '':
        return "No selected file"

    try:
        service = get_user_drive_service(session['credentials'])
        user_folder_name = f"{session['name']} ({session['email']})"
        
        # Check/Create Folder
        query = f"name='{user_folder_name}' and '{PARENT_FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        existing_files = results.get('files', [])

        if not existing_files:
            meta = {'name': user_folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [PARENT_FOLDER_ID]}
            folder = service.files().create(body=meta, fields='id').execute()
            folder_id = folder.get('id')
        else:
            folder_id = existing_files[0]['id']

        upload_count = 0
        
        for file in files:
            if file.filename:
                # Security Check
                if not allowed_file(file.filename):
                    print(f"Skipping {file.filename} (Invalid type)")
                    continue

                # Generate Safe & Unique Name
                original_name = secure_filename(file.filename)
                temp_filename = f"{uuid.uuid4()}_{original_name}"
                
                # Save Locally
                file.save(temp_filename) 
                
                try:
                    print(f"Uploading {original_name}...")
                    
                    file_metadata = {'name': original_name, 'parents': [folder_id]}
                    media = MediaFileUpload(temp_filename, resumable=True)
                    service.files().create(body=file_metadata, media_body=media).execute()
                    
                    # Explicitly release file handle
                    del media 

                    log_to_csv(session['name'], session['email'], original_name)
                    upload_count += 1

                except Exception as e:
                    print(f"Failed to upload {original_name}: {e}")
                
                finally:
                    # Retry Cleanup Logic
                    for i in range(5):
                        try:
                            if os.path.exists(temp_filename):
                                os.remove(temp_filename)
                            break
                        except PermissionError:
                            time.sleep(1)
                    
                    if os.path.exists(temp_filename):
                        print(f"Warning: Could not delete {temp_filename}")

        return redirect(url_for('dashboard', success=upload_count))

    except Exception as e:
        print("!!!!! UPLOAD ERROR !!!!!")
        import traceback
        traceback.print_exc()
        return f"Error: {e}"

@app.route("/admin")
def admin():
    if "email" not in session or session['email'] != ADMIN_EMAIL:
        return "Access Denied"
    
    logs = []
    if os.path.isfile('admin_log.csv'):
        with open('admin_log.csv', mode='r') as file:
            reader = csv.reader(file)
            logs = list(reader)
    return render_template("admin.html", logs=logs)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True, port=5000)