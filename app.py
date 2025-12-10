import streamlit as st
import requests
import pandas as pd
import jwt
import time
import uuid
import os
import re
from io import BytesIO
import google.generativeai as genai

# ================= CONFIGURATION =================
# ⚠️ REPLACE THIS WITH YOUR ACTUAL GOOGLE API KEY
GOOGLE_API_KEY = "PASTE_YOUR_GOOGLE_GEMINI_KEY_HERE"

# EPIC CONFIGURATION
CLIENT_ID = "2914e8ac-a781-47b2-928b-404916f6e8d2" 
KEY_ID = "my-key-1"
TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"
FHIR_BASE_URL = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"

# ================= HELPER FUNCTIONS =================
def configure_ai():
    """Sets up the Google Gemini Model"""
    if "GEMINI_KEY" in st.secrets:
        genai.configure(api_key=st.secrets["GEMINI_KEY"])
    else:
        genai.configure(api_key=GOOGLE_API_KEY)
    return genai.GenerativeModel('gemini-1.5-flash')

def get_epic_token():
    """Generates the secure token to talk to Epic"""
    if not os.path.exists("private_key.pem"):
        st.error("❌ 'private_key.pem' not found in this folder. Please put it next to this script.")
        return None

    with open("private_key.pem", "rb") as f:
        key = f.read()
    
    now = int(time.time())
    jwt_token = jwt.encode(
        {"iss": CLIENT_ID, "sub": CLIENT_ID, "aud": TOKEN_URL, "jti": str(uuid.uuid4()), "exp": now+240},
        key, algorithm='RS384', headers={"kid": KEY_ID}
    )
    
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": jwt_token
    })
    
    if resp.status_code != 200:
        st.error(f"Failed to get Token: {resp.text}")
        return None
        
    return resp.json().get('access_token')

def safe_get_json(url, headers):
    try:
        r = requests.get(url, headers=headers)
        return r.json() if r.status_code == 200 else {}
    except:
        return {}

def get_patient_data(mrn, token):
    """Fetches raw data from Epic FHIR"""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    # Get Patient
    pt_resp = safe_get_json(f"{FHIR_BASE_URL}/Patient?identifier={mrn}", headers)
    if not pt_resp.get('total'): 
        return None, None, [], [], [], []

    pid = pt_resp['entry'][0]['resource']['id']
    name = pt_resp['entry'][0]['resource']['name'][0]['text']

    # Helper to clean text
    def clean(t): return str(t).replace('\n', ' ').strip()[:300]

    # Fetch Details
    list_devs, list_conds, list_procs, list_imgs = [], [], [], []

    # Devices
    devs = safe_get_json(f"{FHIR_BASE_URL}/Device?patient={pid}", headers)
    for e in devs.get('entry', []):
        d_name = e['resource'].get('deviceName', [{}])[0].get('name') or "Unknown Device"
        list_devs.append(clean(d_name))

    # Conditions (Active only)
    conds = safe_get_json(f"{FHIR_BASE_URL}/Condition?patient={pid}", headers)
    for e in conds.get('entry', []):
        if e['resource'].get('clinicalStatus', {}).get('coding', [{}])[0].get('code') == 'active':
            c_name = e['resource'].get('code', {}).get('text') or "Unknown Condition"
            list_conds.append(clean(c_name))

    # Surgeries
    procs = safe_get_json(f"{FHIR_BASE_URL}/Procedure?patient={pid}&status=completed", headers)
    for e in procs.get('entry', []):
        p_name = e['resource'].get('code', {}).get('text') or "Unknown Procedure"
        p_date = e['resource'].get('performedPeriod', {}).get('start') or ""
        list_procs.append(f"{clean(p_name)} ({p_date})")

    # Imaging
    rpts = safe_get_json(f"{FHIR_BASE_URL}/DiagnosticReport?patient={pid}", headers)
    for e in rpts.get('entry', []):
        cat = str(e['resource'].get('category', [{}])[0].get('text')).lower()
        if 'radiology' in cat or 'imaging' in cat:
            study = e['resource'].get('code', {}).get('text') or "Study"
            list_imgs.append(clean(study))

    return pid, name, list_devs, list_conds, list_procs, list_imgs

def analyze_with_ai(model, name, devs, conds, procs, imgs):
    """Sends the data to Gemini for analysis"""
    
    history_str = "Patient's Clinical History (FHIR Data):\n" + "-" * 40 + "\n"
    if devs: history_str += "DEVICES:\n" + "\n".join([f"- {d}" for d in devs]) + "\n"
    if conds: history_str += "CONDITIONS:\n" + "\n".join([f"- {c}" for c in conds]) + "\n"
    if procs: history_str += "SURGERIES:\n" + "\n".join([f"- {p}" for p in procs]) + "\n"
    if imgs:  history_str += "IMAGING:\n" + "\n".join([f"- {i}" for i in imgs]) + "\n"

    # Truncate if too long
    if len(history_str) > 28000:
        history_str = history_str[:28000] + "\n...[TRUNCATED]"

    prompt = f"""
    You are an MRI safety expert.
    Patient: {name}
    {history_str}
    
    Determine MRI Safety Status (Safe, Conditional, Unsafe) and Risk Level (Low, Mod, High).
    Provide key findings and specific recommendations.
    
    OUTPUT FORMAT:
    **MRI Safety Status:** [Status]
    **Risk Level:** [Level]
    **Analysis:** [Full detailed analysis]
    """
    
    try:
        resp = model.generate_content(prompt)
        return resp.text
    except Exception as e:
        return f"AI Error: {str(e)}"

# ================= STREAMLIT UI =================
st.set_page_config(page_title="MRI Safety Assistant", layout="wide")
st.title("🧲 MRI Safety Assistant (Epic + Gemini)")
st.markdown("Directly fetching data from FHIR and analyzing with Google Gemini.")

# Initialize AI
try:
    model = configure_ai()
    ai_ready = True
except:
    st.error("Google API Key missing. Please edit the code to add your key.")
    ai_ready = False

mrn_input = st.text_area("Enter MRNs (comma-separated)", placeholder="203715, 203716", height=100)

if st.button("Analyze Patients") and ai_ready:
    if not mrn_input:
        st.warning("Please enter an MRN.")
    else:
        # 1. Get Token
        with st.status("Authenticating with Epic...") as status:
            token = get_epic_token()
            
            if token:
                status.update(label="Authentication Successful!", state="complete")
                
                mrn_list = [x.strip() for x in mrn_input.split(",") if x.strip()]
                results = []
                progress_bar = st.progress(0)
                
                # 2. Loop through patients
                for i, mrn in enumerate(mrn_list):
                    st.write(f"🔎 Analyzing **{mrn}**...")
                    
                    # A. Fetch Data
                    pid, name, devs, conds, procs, imgs = get_patient_data(mrn, token)
                    
                    if not pid:
                        st.error(f"Patient {mrn} not found.")
                        continue
                        
                    # B. AI Analysis
                    ai_report = analyze_with_ai(model, name, devs, conds, procs, imgs)
                    
                    # C. Extract Status (Simple Regex)
                    status_val = "Unknown"
                    risk_val = "Unknown"
                    try:
                        status_match = re.search(r"\*\*MRI Safety Status:\*\*\s*(.*)", ai_report)
                        if status_match: status_val = status_match.group(1).strip()
                        risk_match = re.search(r"\*\*Risk Level:\*\*\s*(.*)", ai_report)
                        if risk_match: risk_val = risk_match.group(1).strip()
                    except: pass
                    
                    results.append({
                        "MRN": mrn,
                        "Name": name,
                        "Safety Status": status_val,
                        "Risk Level": risk_val,
                        "Full Analysis": ai_report,
                        "Devices": " | ".join(devs),
                        "Conditions": " | ".join(conds)
                    })
                    
                    progress_bar.progress((i + 1) / len(mrn_list))
                
                # 3. Display Results
                if results:
                    st.success("Analysis Complete!")
                    df = pd.DataFrame(results)
                    st.dataframe(df)
                    
                    # 4. Excel Download
                    output = BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                        df.to_excel(writer, index=False, sheet_name='Report')
                    
                    st.download_button(
                        label="📥 Download Excel Report",
                        data=output.getvalue(),
                        file_name="mri_safety_report.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
            else:
                status.update(label="Authentication Failed", state="error")