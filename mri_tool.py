import streamlit as st
import requests
import pandas as pd
import json
from io import BytesIO

# --- Configuration ---
API_URL = "https://us-central1-emory-radiology-asssistant.cloudfunctions.net/mri-safety-check"
HEADERS = {"Content-Type": "application/json"}

def fetch_patient_data(mrn):
    """Calls the API for a single MRN."""
    try:
        payload = {"mrn": mrn.strip()}
        response = requests.post(API_URL, json=payload, headers=HEADERS)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e), "patient_info": {"mrn": mrn}}

def parse_data_to_row(data):
    """Parses the complex nested JSON into a flat dictionary for the spreadsheet."""
    
    # Handle error cases where API failed
    if "error" in data:
        return {
            "MRN": data.get("patient_info", {}).get("mrn", "Unknown"),
            "Status": "API Error",
            "Summary": data["error"]
        }

    # 1. Extract High-Level Info
    assessment = data.get("mri_safety_assessment", {})
    patient = data.get("patient_info", {})
    details = data.get("analysis_details", {})

    # 2. Parse Devices/Findings (The complex part)
    # We loop through findings to create a clean string of what is actually in the patient
    findings_list = details.get("individual_findings", [])
    devices_found = []
    
    for item in findings_list:
        if item.get("has_concern"):
            # Dig into the deeply nested resource to get the exact device name if available
            resource = item.get("item_data", {}).get("resource", {})
            device_names = resource.get("deviceName", [])
            
            # Fallback logic to get a name
            if device_names:
                name = device_names[0]['name']
            else:
                name = item.get("description", "Unknown Device")[:50] + "..." # Truncate if too long
                
            model = resource.get("modelNumber", "N/A")
            devices_found.append(f"• {name} (Model: {model}) - {item.get('concern_level', '').upper()} risk")

    # 3. Format Lists into Strings (for Excel cells)
    concerns_str = "\n".join(assessment.get("concerns", []))
    recs_str = "\n".join(assessment.get("recommendations", []))
    devices_str = "\n".join(devices_found)

    # 4. Return the Flat Row
    return {
        "MRN": patient.get("mrn", "").replace("🏥 ", ""), # Cleaning emojis if preferred
        "Name": patient.get("name", "").replace("👤 ", ""),
        "DOB": patient.get("dob", "").replace("📅 ", ""),
        "Gender": patient.get("gender", "").replace("⚧ ", ""),
        "Safety Status": assessment.get("status", ""),
        "Risk Level": assessment.get("risk", ""),
        "Devices/Implants Found": devices_str,
        "Clinical Summary": assessment.get("summary", ""),
        "Key Concerns": concerns_str,
        "Technologist Recommendations": recs_str,
        "Timestamp": data.get("timestamp", "")
    }

# --- Streamlit UI ---
st.set_page_config(page_title="MRI Safety Batch Processor", layout="wide")
st.title("🧲 MRI Safety Batch Checker")
st.markdown("Enter patient MRNs to generate a safety triage spreadsheet.")

# Input Area
mrn_input = st.text_area("Enter MRNs (comma-separated)", placeholder="203715, 203716, 203717", height=100)

if st.button("Analyze Patients"):
    if not mrn_input:
        st.warning("Please enter at least one MRN.")
    else:
        # 1. Prepare List
        mrn_list = [x.strip() for x in mrn_input.split(",") if x.strip()]
        st.write(f"Processing {len(mrn_list)} patients...")
        
        # 2. Progress Bar
        progress_bar = st.progress(0)
        results = []
        
        # 3. Loop and Fetch
        for i, mrn in enumerate(mrn_list):
            data = fetch_patient_data(mrn)
            flat_row = parse_data_to_row(data)
            results.append(flat_row)
            progress_bar.progress((i + 1) / len(mrn_list))
            
        # 4. Create DataFrame
        df = pd.DataFrame(results)
        
        # 5. Display Preview
        st.success("Analysis Complete!")
        st.dataframe(df)
        
        # 6. Excel Download Logic
        # We use BytesIO to create the file in memory
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Safety Report')
            
            # Auto-adjust column widths (Optional polish)
            workbook = writer.book
            worksheet = writer.sheets['Safety Report']
            text_wrap_format = workbook.add_format({'text_wrap': True, 'valign': 'top'})
            
            # Apply wrapping to long text columns
            for col_num, col_name in enumerate(df.columns):
                if col_name in ["Devices/Implants Found", "Clinical Summary", "Key Concerns", "Technologist Recommendations"]:
                    worksheet.set_column(col_num, col_num, 50, text_wrap_format)
                else:
                    worksheet.set_column(col_num, col_num, 20)

        processed_data = output.getvalue()

        st.download_button(
            label="📥 Download Excel Report",
            data=processed_data,
            file_name="mri_safety_batch_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )