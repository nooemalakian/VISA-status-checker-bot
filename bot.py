import requests
import os
import time
import json
import ddddocr
import gspread
import pandas as pd
from playwright.sync_api import sync_playwright
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

ss_folder = "app_status"
captcha_folder = "temp_captcha"
ss_error_folder = "error_logs"

os.makedirs(ss_folder, exist_ok=True)
os.makedirs(captcha_folder, exist_ok=True)
os.makedirs(ss_error_folder, exist_ok=True)

if os.path.exists("config.json"):
    with open("config.json","r") as f:
        config = json.load(f)
    TOKEN = config["BROWSERLESS_TOKEN"]
    DRIVE_FOLDER_ID = config["DRIVE_FOLDER_ID"]
    DRIVE_ERROR_FOLDER_ID = config["DRIVE_ERROR_FOLDER_ID"]
else:
    TOKEN = os.environ.get("BROWSERLESS_TOKEN")
    DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
    DRIVE_ERROR_FOLDER_ID = os.environ.get("DRIVE_ERROR_FOLDER_ID")

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

if os.path.exists("credentials.json"):
    creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
    gc = gspread.service_account(filename='credentials.json')
else:
    google_creds_raw = os.environ.get("GOOGLE_CREDENTIALS")
    if google_creds_raw:
        creds_dict = json.loads(google_creds_raw)
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        gc = gspread.authorize(creds)
    else:
        print("Error: Invalid Credentials")
        exit()

def upload_to_drive(file_local_path, file_name_in_drive, target_folder_id):
    print(f"Uploading {file_name_in_drive} to Drive...")
    try:
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {
            'name': file_name_in_drive,
            'parents': [target_folder_id]
        }
        media = MediaFileUpload(file_local_path, mimetype='image/png')
        
        uploaded_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()
        
        print(f"File successfully uploaded to Drive. File ID: {uploaded_file.get('id')}")
        return uploaded_file.get('id')
    except Exception as e:
        print(f"WARNING: Failed to upload to Google Drive. Error: {e}")
        return None

print("Fetching data from sheets...")

document = gc.open("VISA Status Checker DB")
sheet = document.get_worksheet(0)

records = sheet.get_all_records()
df = pd.DataFrame(records)

pending_row = df[df['STATUS'] == 'PENDING']

if pending_row.empty:
    print("No new ticket has been added to the sheet.")
    exit()

first_pending = pending_row.iloc[0]
num_row_sheets = int(first_pending.name) + 2

ticket_data = str(first_pending['TICKET NUMBER'])
ucc_data = str(first_pending['APPLICATION ID (UCC)'])
consulate_data = str(first_pending['APPLICATION COUNTRY + CONSULATE'])
ds160_data = str(first_pending['APPLICATION ID'])
passport_data = str(first_pending['PASSPORT NUMBER'])
surname_data = str(first_pending['FIRST 5 LETTERS OF SURNAME'])

print(f"New data has been found. Ticket: {ticket_data} is being processed in row {num_row_sheets}...")

with sync_playwright() as p:
    endpoint_url = f"wss://chrome.browserless.io?token={TOKEN}&stealth"
    
    print("Connecting to browser...")
    browser = p.chromium.connect_over_cdp(endpoint_url)
    
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()

    print("Connecting to CEAC...")
    page.goto("https://ceac.state.gov/CEACStatTracker/Status.aspx")
    time.sleep(3)
    print("The data is getting fetch. Please wait...")

    page.select_option("#Visa_Application_Type", label="NONIMMIGRANT VISA (NIV)")
    page.wait_for_load_state("networkidle")
    time.sleep(4)

    clean_city = consulate_data.split(",")[-1].strip().upper()
    option = page.locator("#Location_Dropdown option").filter(has_text=clean_city)
    page.select_option("#Location_Dropdown", value=option.first.get_attribute("value"))
    page.wait_for_load_state("networkidle")
    time.sleep(3)

    page.fill("#Visa_Case_Number", ds160_data)
    page.fill("#Passport_Number", passport_data)
    page.fill("#Surname", surname_data)
    time.sleep(2)

    captcha_success = False
    max_retries = 3

    ocr = ddddocr.DdddOcr(show_ad=False)

    for attempt in range(1, max_retries + 1):
        print(f"Attempt {attempt} from {max_retries} to solve the captcha...")

        captcha_name = os.path.join(captcha_folder, f"temp_captcha_{attempt}.png")

        id_captcha_pic = "#c_status_ctl00_contentplaceholder1_defaultcaptcha_CaptchaImage"
        page.wait_for_selector(id_captcha_pic)
        page.locator(id_captcha_pic).screenshot(path=captcha_name)

        with open(captcha_name, "rb") as f:
            img_bytes = f.read()

        captcha_text = ocr.classification(img_bytes)

        page.fill("#Captcha", captcha_text)
        page.click("#ctl00_ContentPlaceHolder1_imgFolder")

        time.sleep(4)

        error_captcha = page.locator("text=The code entered does not match").is_visible()

        if not error_captcha:
            print("Captcha successfully solved. Moving on to the next step...")
            captcha_success = True
            break
        else: 
            print(f"Captcha attempt {attempt} failed. Retrying...")
            page.fill("#Captcha", "")


    try:
        if captcha_success:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            ss_name = f"{ucc_data}_app_status_{timestamp}.png"
            ss_local_path = os.path.join(ss_folder, ss_name)
            page.screenshot(path=ss_local_path, full_page=True)
            print(f"Local screenshot saved as {ss_local_path}")

            upload_to_drive(ss_local_path, ss_name, DRIVE_FOLDER_ID)
            try:
                page.wait_for_selector("#ctl00_ContentPlaceHolder1_ucApplicationStatusView_lblStatus", timeout=5000)
                status_element = page.locator("#ctl00_ContentPlaceHolder1_ucApplicationStatusView_lblStatus")
                ceac_status_text = status_element.text_content().strip().upper()
            except Exception:
                ceac_status_text = "UNKNOWN (COULD NOT READ STATUS TEXT)"
            print(f"U.S. Visa Status: {ceac_status_text}")

            excel_status = "PROCESSED"
        else:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            ss_name = f"{ucc_data}_app_error_{timestamp}.png"
            ss_local_path = os.path.join(ss_error_folder, ss_name)
            page.screenshot(path=ss_local_path, full_page=True)
            print(f"Captcha failed after multiple attempts. Evidence saved on the logs as {ss_local_path}")

            upload_to_drive(ss_local_path, ss_name, DRIVE_ERROR_FOLDER_ID)

            ceac_status_text = "ERROR_CAPTCHA_FAILED"
            excel_status = "FAILURE TO PROCESS"

        print("Connecting to n8n...")
        
        try:
            with open(ss_local_path,"rb") as image_file:
                file_bytes = image_file.read()
            
            url_test_n8n = "https://visa-ga.up.railway.app/webhook-test/2b3fd5e2-8584-4a5f-90b2-fef17590170b"
            
            payload = {
                "ucc_data": ucc_data,
                "ticket_number": ticket_data,
                "ceac_status": ceac_status_text
            }

            files = {
                "file": (ss_name, file_bytes, "image/png")
            }

            response = requests.post(url_test_n8n, data=payload, files=files)
            print (f"Data successfully sent to n8n. Response code: {response.status_code}")
        except Exception as e:
            print(f"Failed to trigger n8n webhook. Error{e}")

        sheet.update_cell(num_row_sheets, 7, excel_status)
        print(f"Status has been changed to '{excel_status}' in the spreadsheet.")

    except Exception as catastrophic_error:
        print(f"An error occured during execution: {catastrophic_error}")
    
        try:
            ss_name = f"{ucc_data}_error.png"
            ss_local_path = os.path.join(ss_error_folder, ss_name)
            page.screenshot(path=ss_local_path, full_page=True)
            upload_to_drive(ss_local_path, ss_name, DRIVE_ERROR_FOLDER_ID)
            with open(ss_local_path,"rb") as image_file:
                file_bytes = image_file.read()
        except Exception:
            file_bytes = b""
            ss_name = "no_image.png"
        print("Alerting n8n of failure...")
        
        try:
            url_test_n8n = "https://visa-ga.up.railway.app/webhook-test/2b3fd5e2-8584-4a5f-90b2-fef17590170b"
            
            payload = {
                "ucc_data": ucc_data,
                "ticket_number": ticket_data,
                "ceac_status": "ERROR_SYSTEM_CRASH"
            }

            files = {
                "file": (ss_name, file_bytes, "image/png")
            }

            response = requests.post(url_test_n8n, data=payload, files=files)
            print (f"Error sent to n8n. Evidence saved on the logs.")

        except Exception as e:
            print(f"Error: Could not connect to n8n. {e}")
        
        try:
            sheet.update_cell(num_row_sheets,7,"FAILURE TO PROCESS")
        except Exception:
            pass
        
    try:
        if 'ss_local_path' in locals() and os.path.exists(ss_local_path):
            os.remove(ss_local_path)

        if os.path.exists(captcha_folder):
            for archive in os.listdir(captcha_folder):
                archive_path = os.path.join(captcha_folder, archive)
                if os.path.exists(archive_path):
                    os.remove(archive_path)
    except Exception as e:
            print(f"Cleanup warning:{e}")
    
    browser.close()