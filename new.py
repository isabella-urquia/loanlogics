import streamlit as st
import pandas as pd
import numpy as np
import os
import re
import requests
import hashlib
import textwrap
import uuid
import warnings
from collections import Counter
from io import BytesIO
from datetime import datetime
from fpdf import FPDF

# ============ CONFIG ============
OUTPUT_DIR = "usage_uploads"
CHUNK_DIR = os.path.join(OUTPUT_DIR, "chunks")
API_KEY = os.environ.get("TABS_API_KEY", "")
if not API_KEY:
    try:
        API_KEY = st.secrets["TABS_API_KEY"]
    except Exception:
        API_KEY = ""
API_URL_BASE = "https://integrators.prod.api.tabsplatform.com/v3/customers"
API_INVOICES_URL = "https://integrators.prod.api.tabsplatform.com/v3/invoices"
NETSUITE_API_BASE = "https://integrators.prod.api.tabsplatform.com"
# =================================

# Initialize session state variables
if "show_usage_download" not in st.session_state:
    st.session_state["show_usage_download"] = False
if "workflow_progress" not in st.session_state:
    st.session_state["workflow_progress"] = {
        'csv_uploaded': False,
        'pdfs_generated': False,
        'csv_mapping_created': False,
        'ready_for_upload': False
    }
if "current_pdf_step" not in st.session_state:
    st.session_state["current_pdf_step"] = 1
if "generated_pdfs" not in st.session_state:
    st.session_state["generated_pdfs"] = []
if "pdf_csv_data" not in st.session_state:
    st.session_state["pdf_csv_data"] = None

# Suppress deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# PDF Generation Class
class LoanLogicsPDF(FPDF):
    def __init__(self, company):
        super().__init__()
        self.company = company
        self.set_auto_page_break(auto=False)
        self.set_margins(15, 15, 15)
        self.headers = ["Date", "Description", "Hours", "Total ($)"]
        self.col_widths = [30, 100, 25, 30]
        self.line_height = 5 * 1.55
        self.talent_counter = 0

    def add_talent_section(self, talent):
        self.add_page()
        if self.talent_counter == 0:
            self.set_font("helvetica", "B", 14)
            self.cell(0, 10, f"{self.company} - Hours Report", new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "B", 12)
        self.cell(0, 10, f"Talent: {talent}", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self.print_table_header()
        self.talent_counter += 1

    def print_table_header(self):
        self.set_font("helvetica", "B", 10)
        for i, h in enumerate(self.headers):
            self.cell(self.col_widths[i], 8, h, border="T")
        self.ln()

    def add_row(self, row):
        self.set_font("helvetica", "", 9)
        description = clean_description(row["description"])
        desc_lines = textwrap.wrap(description, width=60)
        num_lines = max(1, len(desc_lines))
        row_height = self.line_height * num_lines

        if self.get_y() + row_height > self.h - 15:
            self.add_page()
            self.print_table_header()

        x = self.get_x()
        y = self.get_y()

        # Date
        self.set_xy(x, y)
        self.set_font("helvetica", "", 9)
        self.cell(self.col_widths[0], row_height, format_date(row["date"]), border="T")

        # Description
        self.set_xy(x + self.col_widths[0], y)
        self.rect(x + self.col_widths[0], y, self.col_widths[1], row_height)
        for i, line in enumerate(desc_lines):
            self.set_xy(x + self.col_widths[0], y + i * self.line_height)
            self.set_font("helvetica", "", 9)
            self.cell(self.col_widths[1], self.line_height, line)

        # Hours
        self.set_xy(x + sum(self.col_widths[:2]), y)
        self.cell(self.col_widths[2], row_height, f"{row['Hours']:.2f}", border="T")

        # Total
        self.set_xy(x + sum(self.col_widths[:3]), y)
        self.cell(self.col_widths[3], row_height, f"${row['Company_Total_No_Currency ($)']:.2f}", border="T")

        self.set_y(y + row_height + 1)

    def add_totals(self, total_hours, total_amount):
        self.ln(3)
        self.set_font("helvetica", "B", 10)
        self.cell(sum(self.col_widths[:2]), 8, "Total", border="T")
        self.cell(self.col_widths[2], 8, f"{total_hours:.2f}", border="T")
        self.cell(self.col_widths[3], 8, f"${total_amount:.2f}", border="T")
        self.ln()

def extract_serial_code(filename):
    """Extract company ID from filename (last part before .pdf)"""
    try:
        base = os.path.splitext(filename)[0]
        parts = base.split("_")
        company_id = parts[-1] if parts else None
        
        # Handle edge cases
        if not company_id or company_id.lower() in ['nan', 'none', '']:
            return None
            
        # Validate that it looks like a UUID
        if is_valid_uuid(company_id):
            return company_id
        else:
            # Try to find a UUID in the filename parts
            for part in parts:
                if is_valid_uuid(part):
                    return part
            return None
            
    except Exception:
        return None

def is_valid_uuid(val):
    """Check if value is a valid UUID"""
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False

def format_date(date):
    """Format date string to YYYY-MM-DD format with error handling"""
    try:
        if pd.isna(date) or date is None:
            raise ValueError("Date is null or None")
        
        # Convert to datetime and format
        formatted_date = pd.to_datetime(date).strftime("%Y-%m-%d")
        return formatted_date
    except Exception as e:
        raise ValueError(f"Invalid date format: {date}. Error: {str(e)}")

def clean_description(desc):
    """Clean description text by removing empty lines and extra whitespace"""
    return "\n".join(line.strip() for line in str(desc).splitlines() if line.strip())

def fetch_event_types_from_api(api_token=None):
    """Fetch all event types from /v3/events/types API"""
    if not api_token:
        api_token = API_KEY
    if not api_token:
        return None
    
    try:
        api_base_url = "https://integrators.prod.api.tabsplatform.com/v3"
        headers = {
            'Authorization': api_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        url = f"{api_base_url}/events/types"
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success") and "payload" in data:
                event_types = data["payload"].get("data", [])
                # Create mapping: eventTypeId -> eventTypeName
                event_type_mapping = {et["id"]: et["name"] for et in event_types if "id" in et and "name" in et}
                return event_type_mapping
        return None
    except Exception as e:
        return None

def fetch_customer_events_from_api(customer_id, api_token=None, limit=1000):
    """Fetch events for a specific customer from /v3/events API"""
    if not api_token:
        api_token = API_KEY
    if not api_token:
        return None
    
    try:
        api_base_url = "https://integrators.prod.api.tabsplatform.com/v3"
        headers = {
            'Authorization': api_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Fetch events for this customer
        url = f"{api_base_url}/events?customerId={customer_id}&limit={limit}"
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success") and "payload" in data:
                events = data["payload"].get("data", [])
                # Extract unique eventTypeIds for this customer
                event_type_ids = list(set([e["eventTypeId"] for e in events if "eventTypeId" in e]))
                return event_type_ids
        return None
    except Exception as e:
        return None

def fetch_obligations_from_api(api_token=None, limit=1000, customer_id=None, page=None):
    """Fetch obligations from /v3/obligations API
    
    Args:
        api_token: API token for authentication
        limit: Maximum number of results per page
        customer_id: Optional customer ID to filter by (uses filter parameter)
        page: Optional page number for pagination
    
    Returns:
        Tuple of (obligations list, pagination info dict with totalItems, currentPage) or (None, None) on error
    """
    if not api_token:
        api_token = API_KEY
    if not api_token:
        return None, None
    
    try:
        api_base_url = "https://integrators.prod.api.tabsplatform.com/v3"
        headers = {
            'Authorization': api_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        if customer_id:
            # Use filter parameter to get obligations for specific customer
            url = f'{api_base_url}/obligations?filter=customerId:eq:"{customer_id}"&limit={limit}'
            if page:
                url += f"&page={page}"
        else:
            url = f"{api_base_url}/obligations?limit={limit}"
            if page:
                url += f"&page={page}"
        
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success") and "payload" in data:
                obligations = data["payload"].get("data", [])
                pagination_info = {
                    "totalItems": data["payload"].get("totalItems", 0),
                    "currentPage": data["payload"].get("currentPage", page or 1),
                    "totalPages": data["payload"].get("totalPages", 1)
                }
                return obligations, pagination_info
        return None, None
    except Exception as e:
        return None, None

def fetch_contracts_from_api(api_token=None, limit=1000):
    """Fetch contracts to map contractId to customerId with pagination"""
    if not api_token:
        api_token = API_KEY
    if not api_token:
        return None
    
    try:
        api_base_url = "https://integrators.prod.api.tabsplatform.com/v3"
        headers = {
            'Authorization': api_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        all_contracts = []
        page = 1
        
        while True:
            url = f"{api_base_url}/contracts?limit={limit}&page={page}"
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code != 200:
                break
                
            data = response.json()
            if not data.get("success") or "payload" not in data:
                break
                
            contracts = data["payload"].get("data", [])
            if not contracts:
                break
                
            all_contracts.extend(contracts)
            
            # Check if there are more pages
            payload = data["payload"]
            total_items = payload.get("totalItems", 0)
            current_page = payload.get("currentPage", page)
            total_pages = payload.get("totalPages", 1)
            
            # Check if we've fetched all items (more reliable than totalPages)
            if total_items > 0 and len(all_contracts) >= total_items:
                break
            
            # Also check if we got fewer than the limit (last page)
            if len(contracts) < limit:
                break
            
            # If totalPages seems wrong (we have more items than one page can hold), continue fetching
            if total_pages == 1 and total_items > limit:
                # Continue to next page
                pass
            elif current_page >= total_pages:
                break
                
            page += 1
            
            # Safety check
            if page > 100:
                break
        
        # Create mapping: contractId -> customerId (normalize customerId to string)
        contract_to_customer = {c["id"]: str(c.get("customerId")).strip() for c in all_contracts if "id" in c and c.get("customerId")}
        return contract_to_customer
    except Exception as e:
        return None

def build_customer_event_type_mapping(customer_ids, api_token=None, use_obligations=False):
    """Build mapping from customerId -> list of eventTypeNames using APIs
    
    Optimized: Fetches ALL obligations once, then groups by customer_id
    Uses /v3/obligations to get all obligations (with pagination)
    Uses /v3/events/types to map eventTypeId -> eventTypeName
    
    Args:
        customer_ids: List of customer IDs to map (used to filter results)
        api_token: API token for authentication
        use_obligations: Ignored - always uses /v3/obligations + /v3/events/types
    """
    if not api_token:
        api_token = API_KEY
    if not api_token:
        return {}
    
    # Step 1: Fetch all event types ONCE (eventTypeId -> eventTypeName) from /v3/events/types
    event_type_mapping = fetch_event_types_from_api(api_token)
    if not event_type_mapping:
        return {}
    
    # Normalize customer IDs to strings for comparison
    customer_ids_list = [str(cid).strip() for cid in customer_ids] if customer_ids else []
    customer_ids_set = set(customer_ids_list) if customer_ids_list else None
    
    # Fetch all obligations and contracts with pagination (efficient bulk approach)
    all_obligations = []
    
    # Step 1.5: Fetch contracts to map contractId -> customerId (obligations have contractId, not customerId)
    contract_to_customer = fetch_contracts_from_api(api_token)
    if not contract_to_customer:
        contract_to_customer = {}
    
    # Step 2: Fetch ALL obligations once (with pagination if needed)
    page = 1
    limit = 1000
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    try:
        total_items = None
        while True:
            status_text.text(f"Fetching obligations page {page}...")
            obligations, pagination_info = fetch_obligations_from_api(api_token, limit=limit, page=page)
            
            if not obligations:
                if page == 1:
                    # First page returned nothing - might be an error
                    st.warning("⚠️ No obligations returned from API. Check API key and permissions.")
                break
            
            all_obligations.extend(obligations)
            
            # Use pagination info if available
            if pagination_info:
                total_items = pagination_info.get("totalItems", 0)
                current_page = pagination_info.get("currentPage", page)
                total_pages = pagination_info.get("totalPages", 1)
                
                # Check if we've fetched all items (more reliable than totalPages which can be wrong)
                if total_items > 0 and len(all_obligations) >= total_items:
                    break
                
                # Also check if we got fewer than the limit (last page)
                if len(obligations) < limit:
                    break
                
                # If totalPages seems wrong (we have more items than one page can hold), continue fetching
                if total_pages == 1 and total_items > limit:
                    # Continue to next page
                    pass
                elif current_page >= total_pages:
                    break
            else:
                # Fallback: check if we got fewer than the limit (last page)
                if len(obligations) < limit:
                    break
            
            page += 1
            
            # Safety check to prevent infinite loops
            if page > 100:  # Max 100,000 obligations
                st.warning("⚠️ Reached maximum page limit (100), stopping pagination")
                break
            
            # Update progress
            if total_items:
                progress_bar.progress(min(len(all_obligations) / total_items, 1.0))
            else:
                progress_bar.progress(min(page / 50, 1.0))
    except Exception as e:
        st.error(f"❌ Error fetching obligations: {str(e)}")
        progress_bar.empty()
        status_text.empty()
        return {}
    finally:
        progress_bar.empty()
        status_text.empty()
    
    if not all_obligations:
        st.warning("⚠️ No obligations found in API")
        return {}
    
    # Step 3: Group obligations by customer_id and extract event types
    customer_to_event_types = {}
    obligations_without_contract = 0
    obligations_without_customer = 0
    obligations_without_billing = 0
    obligations_without_event_type = 0
    obligations_filtered_out = 0
    
    # Create a normalized lookup set for flexible matching
    # Store both original and normalized versions
    customer_ids_normalized = {}
    for cid in customer_ids_set:
        normalized = str(cid).strip().lower()
        customer_ids_normalized[normalized] = cid
    
    for obligation in all_obligations:
        # Try direct customerId field first
        customer_id = obligation.get("customerId") or obligation.get("customer_id") or obligation.get("CustomerId")
        
        # Debug: check if this obligation might be for our customer (before contract mapping)
        contract_id = obligation.get("contractId") or obligation.get("contract_id") or obligation.get("ContractId")
        
        # If no direct customerId, try mapping via contractId
        if not customer_id:
            if not contract_id:
                obligations_without_contract += 1
                continue
            
            # Map contractId to customerId
            customer_id = contract_to_customer.get(contract_id)
            if not customer_id:
                obligations_without_customer += 1
                continue
        
        # Normalize customer_id to string for comparison
        customer_id_normalized = str(customer_id).strip()
        
        # Try to match customer_id - check normalized (case-insensitive) match
        matched_customer_id = None
        if customer_ids_set:
            # Try exact match first
            if customer_id_normalized in customer_ids_set:
                matched_customer_id = customer_id_normalized
            else:
                # Try case-insensitive match
                customer_id_lower = customer_id_normalized.lower()
                if customer_id_lower in customer_ids_normalized:
                    matched_customer_id = customer_ids_normalized[customer_id_lower]
        
        if not matched_customer_id and customer_ids_set:
            obligations_filtered_out += 1
            continue
        
        # Use the matched customer_id (or original if no filtering)
        customer_id = matched_customer_id if matched_customer_id else customer_id_normalized
        
        billing_schedule = obligation.get("billingSchedule", {})
        if not billing_schedule:
            obligations_without_billing += 1
            continue
            
        event_type_id = billing_schedule.get("eventTypeId")
        if not event_type_id:
            obligations_without_event_type += 1
            continue
        
        if event_type_id not in event_type_mapping:
            continue
        
        event_type_name = event_type_mapping[event_type_id]
        
        if customer_id not in customer_to_event_types:
            customer_to_event_types[customer_id] = []
        
        if event_type_name not in customer_to_event_types[customer_id]:
            customer_to_event_types[customer_id].append(event_type_name)
    
    if not customer_to_event_types:
        debug_info = []
        if obligations_without_contract > 0:
            debug_info.append(f"{obligations_without_contract} without contractId")
        if obligations_without_customer > 0:
            debug_info.append(f"{obligations_without_customer} couldn't map to customer")
        if obligations_without_billing > 0:
            debug_info.append(f"{obligations_without_billing} without billingSchedule")
        if obligations_without_event_type > 0:
            debug_info.append(f"{obligations_without_event_type} without eventTypeId")
        if obligations_filtered_out > 0:
            debug_info.append(f"{obligations_filtered_out} filtered out (not in customer list)")
        
        debug_msg = f"⚠️ No customer event type mappings created from obligations"
        if debug_info:
            debug_msg += f" ({', '.join(debug_info)})"
        st.warning(debug_msg)
    
    return customer_to_event_types

def fetch_invoice_by_talent(company_id, talent_name, issue_date=None, api_token=None):
    """Find invoice ID by matching talent name to invoice line items"""
    if not company_id or company_id.lower() == "nan" or not is_valid_uuid(company_id):
        st.warning(f"Invalid company ID: {company_id}")
        return None
    
    if not talent_name or talent_name.strip() == "":
        st.warning(f"No talent name provided for matching")
        return None
    
    if not api_token:
        st.error("API key required for talent matching")
        return None
    
    try:
        # Get invoices from API
        headers = {
            'Authorization': api_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Use customer-specific endpoint with date filter if available
        url = f"{API_URL_BASE}/{company_id}/invoices"
        if issue_date:
            url += f"?issueDate={issue_date.strftime('%Y-%m-%d')}"
            
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            invoices = data.get('data', [])
            
            # Filter and find matching invoice
            for invoice in invoices:
                for line_item in invoice.get('line_items', []):
                    if talent_name.lower() in line_item.get('description', '').lower():
                        return invoice.get('id')
            
        st.warning(f"No invoice found matching talent '{talent_name}'")
        return None
        
    except Exception as e:
        st.error(f"Talent matching failed: {str(e)}")
        return None

def upload_pdf_attachment(customer_id, invoice_id, filepath, talent_name=None, api_key=None):
    """Upload PDF attachment to invoice via API"""
    try:
        if not api_key:
            st.error("API key not configured")
            return False
        
        # Construct API URL
        url = f"{API_URL_BASE}/{customer_id}/invoices/{invoice_id}/attachments"
        
        # Prepare headers
        headers = {
            "Authorization": api_key
        }
        
        # Modify filename if talent name provided
        filename = os.path.basename(filepath)
        if talent_name:
            name_without_ext = os.path.splitext(filename)[0]
            ext = os.path.splitext(filename)[1]
            filename = f"{name_without_ext}_{talent_name}{ext}"
        
        # Read file and upload
        with open(filepath, 'rb') as file:
            files = {
                'file': (filename, file, 'application/pdf')
            }
            
            response = requests.post(url, headers=headers, files=files, timeout=30)
            
            if response.status_code in [200, 201]:
                st.success(f"✅ Upload successful: {filename}")
                return True
            else:
                st.error(f"❌ Upload failed: {filename} (Status: {response.status_code})")
                return False
                
    except Exception as e:
        st.error(f"❌ Upload error: {str(e)}")
        return False

def upload_csv_attachment(customer_id, invoice_id, csv_bytes, filename, api_key=None):
    """Upload CSV attachment to invoice via API"""
    try:
        if not api_key:
            return False
        
        # Construct API URL
        url = f"{API_URL_BASE}/{customer_id}/invoices/{invoice_id}/attachments"
        
        # Prepare headers
        headers = {
            "Authorization": api_key
        }
        
        # Upload CSV bytes
        files = {
            'file': (filename, csv_bytes, 'text/csv')
        }
        
        response = requests.post(url, headers=headers, files=files, timeout=30)
        
        if response.status_code in [200, 201]:
            return True
        else:
            return False
                
    except Exception as e:
        return False

def fetch_all_invoices_for_cache(api_token):
    """Fetch all invoices from API for caching purposes"""
    try:
        api_base_url = "https://integrators.prod.api.tabsplatform.com/v3"
        headers = {
            'Authorization': api_token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        url = f"{api_base_url}/invoices"
        all_invoices = []
        page = 1
        limit = 1000
        
        st.info("🚀 Starting comprehensive invoice fetch...")
        
        # Create progress tracking
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        while True:
            params = {
                'limit': limit,
                'page': page
            }
            
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success') and 'payload' in data:
                    page_invoices = data['payload'].get('data', [])
                elif 'data' in data:
                    page_invoices = data.get('data', [])
                else:
                    page_invoices = []
                
                if not page_invoices:
                    break  # No more invoices
                
                all_invoices.extend(page_invoices)
                
                # Update progress
                progress = min(page / 50, 1.0)  # Assume max 50 pages
                progress_bar.progress(progress)
                status_text.text(f"📄 Fetched {len(all_invoices)} invoices (page {page})...")
                
                # Check pagination metadata
                total_pages = data.get('totalPages') or data.get('payload', {}).get('totalPages')
                current_page = data.get('currentPage') or data.get('payload', {}).get('currentPage')
                
                if total_pages and current_page:
                    if current_page >= total_pages:
                        break
                elif len(page_invoices) < limit:
                    break
                
                page += 1
                
                # Safety check
                if page > 100:  # Max 100,000 invoices
                    st.warning("⚠️ Reached maximum page limit (100), stopping pagination")
                    break
            else:
                st.error(f"API call failed with status {response.status_code}")
                break
        
        # Clear progress indicators
        progress_bar.empty()
        status_text.empty()
        
        if all_invoices:
            # Store in session state for caching
            cache_key = f"invoice_cache_{api_token[:10]}"
            st.session_state[cache_key] = all_invoices
            st.session_state[f"{cache_key}_timestamp"] = datetime.now().isoformat()
            
            # Also save to persistent file
            try:
                import json
                cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_token[:10]}.json")
                os.makedirs(_CACHE_DIR, exist_ok=True)
                with open(cache_file, 'w') as f:
                    json.dump({
                        'invoices': all_invoices,
                        'timestamp': datetime.now().isoformat()
                    }, f)
            except Exception as e:
                st.warning(f"Could not save cache to file: {str(e)}")
            
            st.success(f"✅ Successfully fetched and cached {len(all_invoices)} invoices across {page} pages")
            return all_invoices
        else:
            st.error("❌ No invoices fetched")
            return None
            
    except Exception as e:
        st.error(f"Failed to fetch invoices: {str(e)}")
        import traceback
        st.code(traceback.format_exc())
        return None

def find_invoice_by_date(customer_id, issue_date, api_token):
    """Find invoice ID by customer_id and issue_date using API with caching"""
    if not customer_id or str(customer_id).strip() == "":
        return None
    
    if not api_token:
        return None
    
    try:
        # Try to use cached invoices first
        cache_key = f"invoice_cache_{api_token[:10]}"
        cached_invoices = st.session_state.get(cache_key, [])
        
        # If no cache in session state, try to load from persistent file
        if not cached_invoices:
            try:
                import json
                # Ensure cache directory exists
                os.makedirs(_CACHE_DIR, exist_ok=True)
                cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_token[:10]}.json")
                if os.path.exists(cache_file):
                    with open(cache_file, 'r') as f:
                        cache_data = json.load(f)
                        cached_invoices = cache_data.get('invoices', [])
                        
                        # Always restore to session state if we loaded from file
                        if cached_invoices:
                            st.session_state[cache_key] = cached_invoices
            except Exception as e:
                # Silently fail - cache file might be corrupted
                pass
        
        if cached_invoices:
            # Use cached data for fast lookup
            invoices = cached_invoices
            
            # Debug: Count invoices for this customer
            customer_invoices = [inv for inv in invoices if inv.get('customerId', '') == customer_id]
            
            # Filter invoices for this customer and date
            valid_invoices = []
            for invoice in invoices:
                invoice_customer_id = invoice.get('customerId', '')
                invoice_date_str = invoice.get('issueDate', '')
                invoice_status = invoice.get('status', '').upper()
                invoice_source = invoice.get('source', '').upper()
                
                # Check customer match first
                if invoice_customer_id != customer_id:
                    continue
                
                # Check status and source
                if invoice_status == 'DELETED':
                    continue
                if invoice_source != 'TABS':
                    continue
                
                # If we have a specific date, filter by date
                if issue_date and invoice_date_str:
                    try:
                        # Convert issue_date to date object if it's not already
                        if isinstance(issue_date, str):
                            issue_date_obj = pd.to_datetime(issue_date).date()
                        elif hasattr(issue_date, 'date'):
                            issue_date_obj = issue_date.date()
                        else:
                            issue_date_obj = issue_date
                        
                        # Parse invoice date (handle both ISO format with T and without)
                        invoice_date = pd.to_datetime(invoice_date_str).date()
                        
                        if invoice_date == issue_date_obj:
                            valid_invoices.append(invoice)
                    except Exception as e:
                        # If date parsing fails, include the invoice anyway (like reference_code.py)
                        valid_invoices.append(invoice)
                else:
                    # No specific date, include all valid invoices
                    valid_invoices.append(invoice)
            
            if valid_invoices:
                # Sort by issue date (most recent first) and return the first one
                valid_invoices.sort(key=lambda x: x.get('issueDate', ''), reverse=True)
                selected_invoice = valid_invoices[0]
                invoice_id = selected_invoice.get('id')
                return invoice_id
        
        # If no cached data, return None (don't fetch from API here - that should be done via Refresh Cache button)
        # This matches the reference_code.py behavior - it doesn't fetch if cache is empty
        return None
        
    except Exception as e:
        return None

if "ns_to_tabs_cache" not in st.session_state:
    st.session_state["ns_to_tabs_cache"] = {}
if "uploaded_files" not in st.session_state:
    st.session_state["uploaded_files"] = {}
if "generated_files" not in st.session_state:
    st.session_state["generated_files"] = {}
    
    

# -------- Persistent cache helpers (avoid re-calling API across sessions) --------
# We persist the NetSuite→Tabs ID cache to disk and hydrate it at startup.
_CACHE_DIR = os.path.join(OUTPUT_DIR, "_session")
_NS_CACHE_FILE = os.path.join(_CACHE_DIR, "ns_to_tabs_cache.json")
# Try repo root first (for deployment), then fall back to cache dir
_CLIENT_MAPPINGS_FILE_REPO = os.path.join(os.path.dirname(__file__), "client_mappings.json")
_CLIENT_MAPPINGS_FILE = os.path.join(_CACHE_DIR, "client_mappings.json")

def _ensure_cache_dir_exists() -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
    except Exception:
        pass

def _load_ns_cache_from_disk() -> dict:
    try:
        if os.path.exists(_NS_CACHE_FILE):
            import json
            with open(_NS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}

def _save_ns_cache_to_disk(cache: dict) -> None:
    try:
        _ensure_cache_dir_exists()
        import json
        with open(_NS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass

def _load_client_mappings_from_disk() -> dict:
    """Load client mappings (parent_to_id, acct_to_tabs_id, etc.) from disk
    Tries repo root first (for deployment), then cache directory
    """
    import json
    # Try repo root first (for Streamlit Cloud deployment)
    for file_path in [_CLIENT_MAPPINGS_FILE_REPO, _CLIENT_MAPPINGS_FILE]:
        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return {
                            "parent_to_id": {str(k): str(v) for k, v in data.get("parent_to_id", {}).items()},
                            "acct_to_tabs_id": {str(k): str(v) for k, v in data.get("acct_to_tabs_id", {}).items()},
                            "acct_to_ns_id": {str(k): str(v) for k, v in data.get("acct_to_ns_id", {}).items()},
                            "acct_to_income_evt": {str(k): str(v) for k, v in data.get("acct_to_income_evt", {}).items()},
                            "acct_to_lbpa_evt": {str(k): str(v) for k, v in data.get("acct_to_lbpa_evt", {}).items()},
                            "acct_to_diff_name": {str(k): str(v) for k, v in data.get("acct_to_diff_name", {}).items()},
                            "acct_to_base_name": {str(k): str(v) for k, v in data.get("acct_to_base_name", {}).items()},
                        }
        except Exception:
            continue
    return {}

def _save_client_mappings_to_disk(mappings: dict) -> None:
    """Save client mappings to disk"""
    try:
        _ensure_cache_dir_exists()
        import json
        with open(_CLIENT_MAPPINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(mappings, f, ensure_ascii=False)
    except Exception:
        pass

# Hydrate session cache from disk once
try:
    disk_cache = _load_ns_cache_from_disk()
    if disk_cache:
        # Merge; keep existing session entries, add new ones from disk
        st.session_state["ns_to_tabs_cache"] = {**disk_cache, **st.session_state.get("ns_to_tabs_cache", {})}
except Exception:
    pass


def get_api_key() -> str:
    for k in ["ui_api_key", "ui_api_key_usage", "ui_api_key_attach"]:
        ui_key = str(st.session_state.get(k, "")).strip()
        if ui_key:
            return ui_key
    return API_KEY

_DEF_SESSION_DIR = os.path.join(OUTPUT_DIR, "_session")

def persist_upload(uploaded_file, key: str) -> None:
    """Store uploaded CSV content in memory (session_state) instead of disk.
    Saves bytes and a content hash for change detection, plus original filename.
    """
    if uploaded_file is None:
        return
    if hasattr(uploaded_file, "getvalue"):
        data_bytes = uploaded_file.getvalue()
        file_name = getattr(uploaded_file, "name", f"{key}.csv")
        new_hash = hashlib.md5(data_bytes).hexdigest()
        prev_hash = st.session_state.get(f"uploaded_{key}_hash")
        st.session_state["uploaded_files"][key] = {
            "bytes": data_bytes,
            "name": file_name,
            "hash": new_hash,
        }
        if new_hash != prev_hash:
            st.session_state[f"uploaded_{key}_hash"] = new_hash
            st.session_state["show_usage_download"] = False
    elif isinstance(uploaded_file, (str, os.PathLike)):
        try:
            with open(str(uploaded_file), "rb") as f:
                data_bytes = f.read()
            st.session_state["uploaded_files"][key] = {
                "bytes": data_bytes,
                "name": os.path.basename(str(uploaded_file)),
                "hash": hashlib.md5(data_bytes).hexdigest(),
            }
            st.session_state["show_usage_download"] = False
        except Exception:
            pass

# Load client mappings from disk at startup
try:
    if "client_mappings_loaded" not in st.session_state:
        disk_mappings = _load_client_mappings_from_disk()
        if disk_mappings:
            st.session_state["client_mappings"] = disk_mappings
            st.session_state["client_mappings_loaded"] = True
        else:
            st.session_state["client_mappings"] = {}
            st.session_state["client_mappings_loaded"] = True
except Exception:
    st.session_state["client_mappings"] = {}
    st.session_state["client_mappings_loaded"] = True

# ---------- header helpers ----------
_def_norm_regex = re.compile(r"[^a-z0-9]")

def normalize_name(name: str) -> str:
    return _def_norm_regex.sub("", str(name).strip().lower())

def find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    normalized_to_original = {normalize_name(c): c for c in df.columns}
    for cand in candidates:
        n = normalize_name(cand)
        if n in normalized_to_original:
            return normalized_to_original[n]
    return None
# ------------------------------------

# Custom field ID for Customer Number (stores NetSuite Account ID)
CUSTOMER_NUMBER_FIELD_ID = "edaefe3e-46b9-4212-957b-8140df8e2890"

def extract_account_id_from_entity_id(entity_id: str) -> str | None:
    """Extract the numeric Account ID from NetSuite entityId.
    Example: '8516 Regions Bank - NMS' -> '8516'
    Example: '8544 1st Security Bank of Washington - NMS' -> '8544'
    """
    if not entity_id:
        return None
    
    entity_id = str(entity_id).strip()
    # Extract the first numeric part (before the first space)
    match = re.match(r'^(\d+)', entity_id)
    if match:
        return match.group(1)
    return None


def get_customer_from_netsuite(tabs_customer_id: str, manufacturer_id: str = None, verbose: bool = False) -> dict | None:
    """Get customer from NetSuite API using TABS customer ID.
    The API endpoint expects TABS customer ID and manufacturer ID.
    Returns customer data with entityId if found, None otherwise.
    """
    if not tabs_customer_id or not get_api_key():
        return None
    
    api_key = get_api_key()
    headers = {
        "Authorization": api_key,  # Don't use f-string, use raw API key
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # Try to get manufacturer_id from environment or use default
    if not manufacturer_id:
        manufacturer_id = os.getenv("TABS_MANUFACTURER_ID", "388ca94d-6fa9-4121-9d2c-a37fb790382c")
    
    url = f"{NETSUITE_API_BASE}/v2/integrations/netsuite/merchant/{manufacturer_id}/customers/{tabs_customer_id}"
    
    if verbose:
        st.write(f"   🔍 Calling NetSuite API: {url}")
        st.write(f"   🔑 Using API key: {api_key[:20]}..." if api_key and len(api_key) > 20 else "   🔑 API key: (empty)")
        st.write(f"   📋 Headers: {list(headers.keys())}")
    
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Disable automatic redirects to avoid redirect loops
        res = requests.get(url, headers=headers, timeout=10, verify=False, allow_redirects=False)
        
        if verbose:
            st.write(f"   📊 Response status: {res.status_code}")
            st.write(f"   📄 Response headers: {dict(res.headers)}")
        
        # Handle redirects manually (only follow one redirect to avoid loops)
        if res.status_code in (301, 302, 303, 307, 308):
            redirect_url = res.headers.get('Location')
            if verbose:
                st.write(f"   🔄 Redirect detected to: {redirect_url}")
            if redirect_url:
                # If redirect URL is relative, make it absolute
                if redirect_url.startswith('/'):
                    from urllib.parse import urljoin
                    redirect_url = urljoin(url, redirect_url)
                # Follow redirect once
                res = requests.get(redirect_url, headers=headers, timeout=10, verify=False, allow_redirects=False)
                if verbose:
                    st.write(f"   📊 Redirect response status: {res.status_code}")
        
        if res.status_code == 200:
            try:
                data = res.json()
                if verbose:
                    st.write(f"   ✅ NetSuite API response received")
                    # Only show entityId in verbose to avoid huge output
                    entity_id = data.get("entityId") if isinstance(data, dict) else None
                    if entity_id:
                        st.write(f"   📍 Found entityId: {entity_id}")
                
                # The API returns entityId directly in the response (e.g., "8840 1st Source Bank - NMS")
                # Return the full data structure - caller will extract entityId
                return data
            except Exception as e:
                if verbose:
                    st.write(f"   ⚠️ Error parsing JSON response: {str(e)}")
                    st.write(f"   Raw response: {res.text[:500]}")
                return None
        elif res.status_code == 404:
            # 404 means customer is not linked to NetSuite for this manufacturer - this is expected for some customers
            if verbose:
                st.write(f"   ℹ️ Customer not linked to NetSuite (404) - skipping")
            return None
        elif res.status_code == 401 or res.status_code == 403:
            if verbose:
                st.write(f"   ⚠️ Authentication failed ({res.status_code}) for TABS customer {tabs_customer_id}")
                st.write(f"   Response: {res.text[:500]}")
            return None
        elif verbose:
            st.write(f"   ⚠️ NetSuite API returned {res.status_code} for TABS customer {tabs_customer_id}")
            st.write(f"   Response: {res.text[:500]}")
        return None
    except Exception as e:
        if verbose:
            st.write(f"   ⚠️ Error calling NetSuite API for TABS customer {tabs_customer_id}: {str(e)}")
        return None


def update_customer_custom_field(customer_id: str, field_id: str, value: str) -> bool:
    """Update a custom field on a TABS customer using PUT /v3/customers/{id}/custom-field
    Pass an empty string "" as value to clear/remove the custom field value.
    """
    if not customer_id or not field_id:
        return False
    
    # Allow empty string to clear the field value
    # Convert value to string, but allow empty strings
    value_str = str(value).strip() if value is not None else ""
    
    api_key = get_api_key()
    if not api_key:
        return False
    
    headers = {
        "Authorization": f"{api_key}",
        "Content-Type": "application/json"
    }
    
    # Use the custom-field endpoint (singular) as shown in API docs
    url = f"{API_URL_BASE}/{customer_id}/custom-field"
    
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # API expects an array of objects with manufacturerCustomFieldId and customFieldValue
        # Empty string should clear the field value
        payload = [
            {
                "manufacturerCustomFieldId": field_id,
                "customFieldValue": value_str
            }
        ]
        
        put_res = requests.put(url, headers=headers, json=payload, timeout=10, verify=False)
        
        if put_res.status_code in (200, 201, 204):
            return True
        else:
            st.write(f"Failed to update custom field for customer {customer_id}: {put_res.status_code} - {put_res.text}")
            return False
    except Exception as e:
        st.write(f"Error updating custom field for customer {customer_id}: {str(e)}")
        return False


def build_name_to_customer_mapping() -> dict[str, str]:
    """Build a mapping from customer name (normalized) to customer_id by fetching all customers"""
    mapping = {}
    api_key = get_api_key()
    if not api_key:
        st.write("⚠️ No API key available for building name mapping")
        return mapping
    
    headers = {
        "Authorization": f"{api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Fetch customers with pagination
        url = f"{API_URL_BASE}"
        page = 1  # API pages start at 1, not 0
        limit = 100
        
        while True:
            params = {"page": page, "limit": limit}
            res = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
            
            if res.status_code != 200:
                st.write(f"⚠️ API request failed with status {res.status_code}: {res.text[:200]}")
                break
            
            data = res.json()
            payload = data.get("payload", {})
            customers = payload.get("data", [])
            
            if not customers:
                if page == 1:
                    st.write("⚠️ No customers returned from API")
                break
            
            # Extract customer name and create normalized mapping
            for customer in customers:
                customer_id = str(customer.get("id", "")).strip()
                customer_name = str(customer.get("name", "")).strip()
                if customer_id and customer_name:
                    # Normalize name (same as normalize_name function)
                    normalized = normalize_name(customer_name)
                    mapping[normalized] = customer_id
                    # Also map the original name (lowercase)
                    mapping[customer_name.lower()] = customer_id
            
            # Check if there are more pages
            total_items = payload.get("totalItems", 0)
            current_page = payload.get("currentPage", page)
            if (current_page + 1) * limit >= total_items:
                break
            
            page += 1
            if page % 5 == 0:
                st.write(f"   Processed {page * limit} customers, built {len(mapping)} name mappings...")
            
    except Exception as e:
        st.write(f"Error building name to customer mapping: {str(e)}")
        import traceback
        st.write(f"Traceback: {traceback.format_exc()}")
    
    return mapping


def build_account_id_to_customer_mapping() -> dict[str, str]:
    """Build a mapping from AccountID (Customer Number custom field) to customer_id by fetching all customers"""
    mapping = {}
    api_key = get_api_key()
    if not api_key:
        return mapping
    
    headers = {
        "Authorization": f"{api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Fetch customers with pagination
        url = f"{API_URL_BASE}"
        page = 1  # API pages start at 1, not 0
        limit = 100
        
        while True:
            params = {"page": page, "limit": limit}
            res = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
            
            if res.status_code != 200:
                break
            
            data = res.json()
            payload = data.get("payload", {})
            customers = payload.get("data", [])
            
            if not customers:
                break
            
            # Extract AccountID from Customer Number custom field for each customer
            # Note: customFields might not be in the list response, so we fetch each customer individually
            for customer in customers:
                customer_id = str(customer.get("id", "")).strip()
                if not customer_id:
                    continue
                
                # Fetch individual customer to get customFields (they might not be in list response)
                try:
                    customer_url = f"{API_URL_BASE}/{customer_id}"
                    customer_res = requests.get(customer_url, headers=headers, timeout=10, verify=False)
                    if customer_res.status_code == 200:
                        customer_data = customer_res.json()
                        # Extract customer object from response
                        # Based on debug output: payload IS the customer object (has 'id', 'name', 'customFields', etc.)
                        customer_obj = None
                        if "payload" in customer_data:
                            payload = customer_data["payload"]
                            if isinstance(payload, dict):
                                # Payload itself is the customer object if it has customer-like keys
                                if "id" in payload or "name" in payload:
                                    customer_obj = payload
                                # Otherwise check if it has a "data" key
                                elif "data" in payload:
                                    data = payload["data"]
                                    if isinstance(data, list) and len(data) > 0:
                                        customer_obj = data[0]
                                    elif isinstance(data, dict):
                                        customer_obj = data
                            elif isinstance(payload, list) and len(payload) > 0:
                                customer_obj = payload[0]
                        else:
                            # No payload wrapper, data might be at root level
                            customer_obj = customer_data.get("data") or customer_data
                        
                        if customer_obj:
                            # customFields is a list, not a dict
                            custom_fields = customer_obj.get("customFields", [])
                            
                            # customFields is a list of field objects
                            # Each field object should have: manufacturerCustomFieldId and customFieldValue
                            if isinstance(custom_fields, list):
                                # Search for the Customer Number field in the list
                                for field in custom_fields:
                                    if isinstance(field, dict):
                                        # Try different possible field ID keys
                                        field_id = (
                                            field.get("manufacturerCustomFieldId") or 
                                            field.get("id") or 
                                            field.get("fieldId") or
                                            field.get("customFieldId")
                                        )
                                        if field_id == CUSTOMER_NUMBER_FIELD_ID:
                                            # Try different possible value keys
                                            field_value = (
                                                field.get("customFieldValue") or 
                                                field.get("value") or
                                                field.get("fieldValue")
                                            )
                                            if field_value and str(field_value).strip():
                                                account_id = re.sub(r"[^0-9]", "", str(field_value).strip())
                                                if account_id:
                                                    mapping[account_id] = customer_id
                                                    break
                            elif isinstance(custom_fields, dict):
                                # Fallback: if it's a dict (old format), try direct lookup
                                customer_number = custom_fields.get(CUSTOMER_NUMBER_FIELD_ID)
                                if customer_number:
                                    account_id = re.sub(r"[^0-9]", "", str(customer_number).strip())
                                    if account_id:
                                        mapping[account_id] = customer_id
                except Exception as e:
                    if len(mapping) == 0 and page == 1:
                        st.write(f"⚠️ Error fetching customer {customer_id}: {str(e)}")
                    continue
            
            # Check if there are more pages
            total_items = payload.get("totalItems", 0)
            current_page = payload.get("currentPage", page)
            if (current_page + 1) * limit >= total_items:
                break
            
            page += 1
            # Show progress
            if page % 5 == 0:
                st.write(f"   Processed {page * limit} customers, found {len(mapping)} with Customer Number custom field...")
            
    except Exception as e:
        st.write(f"Error building AccountID to customer mapping: {str(e)}")
        import traceback
        st.write(f"Traceback: {traceback.format_exc()}")
    
    return mapping


def get_customer_custom_field_value(customer_id: str, field_id: str) -> str | None:
    """Get the current value of a custom field for a customer"""
    if not customer_id or not field_id:
        return None
    
    api_key = get_api_key()
    if not api_key:
        return None
    
    headers = {
        "Authorization": f"{api_key}",
        "Content-Type": "application/json"
    }
    
    url = f"{API_URL_BASE}/{customer_id}"
    
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        res = requests.get(url, headers=headers, timeout=10, verify=False)
        if res.status_code == 200:
            data = res.json()
            payload = data.get("payload", {})
            customer_obj = payload if isinstance(payload, dict) and "id" in payload else None
            
            if customer_obj:
                custom_fields = customer_obj.get("customFields", [])
                if isinstance(custom_fields, list):
                    for field in custom_fields:
                        if isinstance(field, dict):
                            field_id_check = (
                                field.get("manufacturerCustomFieldId") or 
                                field.get("id") or 
                                field.get("fieldId")
                            )
                            if field_id_check == field_id:
                                return field.get("customFieldValue") or field.get("value")
    except Exception:
        pass
    
    return None


def sync_customer_number_from_entity_id(customer_obj: dict) -> bool:
    """Extract Account ID from customer's entityId and update the Customer Number custom field.
    This is called when a customer is fetched from NetSuite sync.
    Returns True if the field was updated, False otherwise.
    """
    if not customer_obj:
        return False
    
    customer_id = str(customer_obj.get("id") or "").strip()
    if not customer_id:
        return False
    
    # Check if the custom field already has a value
    current_value = get_customer_custom_field_value(customer_id, CUSTOMER_NUMBER_FIELD_ID)
    if current_value and str(current_value).strip():
        # Custom field already has a value, don't overwrite it
        return False
    
    # Get entityId from customer object
    entity_id = customer_obj.get("entityId") or customer_obj.get("entity_id")
    if not entity_id:
        return False
    
    # Extract the numeric Account ID from entityId (e.g., "8516" from "8516 Regions Bank - NMS")
    account_id = extract_account_id_from_entity_id(entity_id)
    if not account_id:
        return False
    
    # Update the custom field with the Account ID
    return update_customer_custom_field(customer_id, CUSTOMER_NUMBER_FIELD_ID, account_id)


def backfill_customer_number_from_entity_id(account_ids_to_find: set[str] | None = None) -> dict[str, int]:
    """Backfill Customer Number custom field for customers that have entityId but missing the custom field.
    If account_ids_to_find is provided, only processes customers whose entityId contains those AccountIDs.
    This can be run as a batch process to set custom fields for customers already synced from NetSuite.
    Returns a dict with counts: {'updated': X, 'skipped': Y, 'errors': Z}
    """
    results = {'updated': 0, 'skipped': 0, 'errors': 0}
    
    api_key = get_api_key()
    if not api_key:
        return results
    
    # Track which AccountIDs we've found (either updated or already had custom field)
    found_account_ids = set()
    
    headers = {
        "Authorization": f"{api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Fetch all customers with pagination
        url = f"{API_URL_BASE}"
        page = 1
        limit = 100
        
        while True:
            params = {"page": page, "limit": limit}
            res = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
            
            if res.status_code != 200:
                break
            
            data = res.json()
            payload = data.get("payload", {})
            customers = payload.get("data", [])
            
            if not customers:
                break
            
            # Process each customer
            for customer in customers:
                customer_id = str(customer.get("id", "")).strip()
                if not customer_id:
                    continue
                
                # Check if customer has entityId in list response
                entity_id = customer.get("entityId") or customer.get("entity_id")
                
                # If not in list response, fetch individual customer
                if not entity_id:
                    try:
                        customer_url = f"{API_URL_BASE}/{customer_id}"
                        customer_res = requests.get(customer_url, headers=headers, timeout=10, verify=False)
                        if customer_res.status_code == 200:
                            customer_data = customer_res.json()
                            payload_obj = customer_data.get("payload", {})
                            # Payload itself is the customer object
                            if isinstance(payload_obj, dict) and "id" in payload_obj:
                                entity_id = payload_obj.get("entityId") or payload_obj.get("entity_id")
                                
                                # Update customer object with fetched data for sync function
                                customer = payload_obj
                    except Exception as e:
                        pass
                
                if not entity_id:
                    results['skipped'] += 1
                    continue
                
                # Extract Account ID first to check if we should process this customer
                account_id = extract_account_id_from_entity_id(entity_id)
                if not account_id:
                    results['skipped'] += 1
                    continue
                
                # If we're looking for specific AccountIDs, only process those
                if account_ids_to_find is not None and account_id not in account_ids_to_find:
                    results['skipped'] += 1
                    continue
                
                # Check if custom field already has a value
                current_value = get_customer_custom_field_value(customer_id, CUSTOMER_NUMBER_FIELD_ID)
                if current_value and str(current_value).strip():
                    # Already has the field set, mark as found
                    found_account_ids.add(account_id)
                    results['skipped'] += 1
                    continue
                
                # Set custom field
                if sync_customer_number_from_entity_id(customer):
                    found_account_ids.add(account_id)
                    results['updated'] += 1
                else:
                    results['errors'] += 1
            
            # Stop early if we've found all AccountIDs we're looking for
            if account_ids_to_find and len(found_account_ids) >= len(account_ids_to_find):
                break
            
            # Check if there are more pages
            total_items = payload.get("totalItems", 0)
            current_page = payload.get("currentPage", page)
            if (current_page + 1) * limit >= total_items:
                break
            
            page += 1
            
    except Exception as e:
        st.write(f"Error in backfill process: {str(e)}")
        results['errors'] += 1
    
    return results


def resolve_tabs_id_from_ns(ns_external_id: str) -> str | None:
    ns_external_id = str(ns_external_id or "").strip()
    ns_external_id = ns_external_id.replace(".0", "")
    if not ns_external_id:
        return None
    cache = st.session_state.get("ns_to_tabs_cache", {})
    if ns_external_id in cache:
        return cache[ns_external_id]
    params_candidates = [
        {"externalId": ns_external_id, "limit": 1},
    ]
    api_key = get_api_key()
    headers = {"Authorization": f"{api_key}"}
    for params in params_candidates:
        try:
            url = f'{API_URL_BASE}?filter=externalIds.externalId:eq:"{ns_external_id}"'
            try:
                # Disable SSL verification - Note: In production, proper cert verification should be used
                res = requests.get(url, headers=headers, timeout=10, verify=False)
                # Suppress only the specific InsecureRequestWarning
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                
                if res.status_code >= 400:
                    pass
            except requests.exceptions.Timeout:
                pass
            except requests.exceptions.ConnectionError as e:
                pass
            except Exception as e:
                pass
            if res.status_code >= 400:
                continue
            data = res.json() if res.headers.get("content-type", "").startswith("application/json") else None
            if not data:
                continue
            items = (
                data.get("payload", {}).get("data")
                or data.get("data")
                or data.get("items")
                or []
            )
            if not items:
                continue
            # Prefer exact match on externalIds array (type NETSUITE and id equals)
            for cust in items:
                match = False
                ext_list = cust.get("externalIds") or cust.get("external_ids") or []
                for ext in ext_list or []:
                    ext_id = str(ext.get("id") or "").strip()
                    if ext_id == ns_external_id:
                        match = True
                        break
                if match:
                    tabs_id = str(cust.get("id") or "").strip()
                    if tabs_id:
                        cache[ns_external_id] = tabs_id
                        st.session_state["ns_to_tabs_cache"] = cache
                        # Persist to disk
                        _save_ns_cache_to_disk(cache)
                        # Sync Customer Number custom field from entityId if available
                        sync_customer_number_from_entity_id(cust)
                        return tabs_id
            # Fallback: first item when search hits
            cust = items[0]
            tabs_id = str(cust.get("id") or "").strip()
            if tabs_id:
                cache[ns_external_id] = tabs_id
                st.session_state["ns_to_tabs_cache"] = cache
                # Persist to disk
                _save_ns_cache_to_disk(cache)
                # Sync Customer Number custom field from entityId if available
                sync_customer_number_from_entity_id(cust)
                return tabs_id
        except Exception:
            continue
    return None


def detect_header_row(uploaded_clients):
    """Return a DataFrame for the mapping CSV, auto-detecting the header row.
    Looks for the first line that contains Acct#/AccountID and NetSuite/External ID.
    Works with CSVs that have banner/title rows above the real headers.
    """
    from io import StringIO
    raw = b""
    if hasattr(uploaded_clients, "read"):
        raw = uploaded_clients.read()
    elif isinstance(uploaded_clients, bytes):
        raw = uploaded_clients
    elif isinstance(uploaded_clients, (str, os.PathLike)):
        with open(uploaded_clients, "rb") as f:
            raw = f.read()
    else:
        raw = b""
    text = raw.decode("utf-8-sig", errors="ignore")
    # Split and find header index
    lines = [l for l in text.splitlines() if l is not None]
    header_idx = None
    for i, l in enumerate(lines[:500]):
        ll = l.lower()
        if ("acct#" in ll or "acct #" in ll or "accountid" in ll or "account id" in ll or "account number" in ll or "accountnumber" in ll or "acctno" in ll or "acct no" in ll) and ("netsuite" in ll or "external id" in ll):
            header_idx = i
            break
    if header_idx is None:
        # Fallback to first non-empty line
        header_idx = 0
    # Let pandas parse from the detected header row with automatic delimiter detection
    df_clients = pd.read_csv(StringIO(text), header=header_idx)
    # Clean column names and values
    df_clients.columns = [re.sub(r"\s+", " ", str(c)).strip().strip('"').strip("'") for c in df_clients.columns]
    df_clients = df_clients.apply(lambda x: x.astype(str).str.strip())
    return df_clients

def extract_mappings_from_clients(uploaded_clients):
    """Extract mappings from clients CSV file"""
    df_clients = detect_header_row(uploaded_clients)

    name_col = find_column(df_clients, ["name", "customer", "customername"]) 
    acc_name_col = find_column(df_clients, ["account name", "accountname"]) 
    name_with_prefix_col = find_column(df_clients, ["namewithprefix", "name with prefix"])
    id_col = find_column(df_clients, ["id", "tabs id", "tabs_customer_id", "tabscustomerid", "customerid", "customer id"])
    acctnum_col = find_column(df_clients, ["acct#", "acct #", "acctno", "acct no", "acct", "accountid", "account id", "accountnumber", "account number", "acctnum"]) 
    netsuite_id_col = find_column(df_clients, ["netsuite", "netsuite id", "netsuiteid", "ns id", "external id", "netsuite internal id"]) 
    diff_name_col = find_column(df_clients, ["account name", "name with prefix", "subsidiary", "subsidiary name"]) 
    rev_type_col = find_column(df_clients, ["rev. type", "rev type", "revenue type", "rev"]) 
    billing_type_col = find_column(df_clients, ["billing type", "billing", "bill type"]) 
    
    parent_to_id_raw: dict[str, str] = {}
    acct_to_tabs_id: dict[str, str] = {}
    acct_to_ns_id: dict[str, str] = {}
    acct_to_income_evt: dict[str, str] = {}
    acct_to_lbpa_evt: dict[str, str] = {}
    acct_to_diff_name: dict[str, str] = {}
    acct_to_base_name: dict[str, str] = {}
    
    base_names_series = df_clients[name_col] if name_col else pd.Series([], dtype=str)
    base_name_counts = Counter(str(x).strip() for x in base_names_series) if not base_names_series.empty else {}
    
    for _, r in df_clients.iterrows():
        tabs_id_val = str(r[id_col]).strip() if id_col else ""
        if name_col and tabs_id_val:
            parent_to_id_raw[str(r[name_col]).strip()] = tabs_id_val
            if name_with_prefix_col:
                alias = str(r[name_with_prefix_col]).strip()
                if alias:
                    parent_to_id_raw[alias] = tabs_id_val
        if acctnum_col and tabs_id_val:
            acct_key = re.sub(r"[^0-9]", "", str(r[acctnum_col]))
            if acct_key:
                acct_to_tabs_id[acct_key] = tabs_id_val
        if acctnum_col and netsuite_id_col:
            ns_val = str(r[netsuite_id_col]).strip()
            acct_key = re.sub(r"[^0-9]", "", str(r[acctnum_col]))
            if acct_key and ns_val:
                acct_to_ns_id[acct_key] = ns_val
        if acctnum_col and (name_col or acc_name_col):
            acct_key = re.sub(r"[^0-9]", "", str(r[acctnum_col]))
            base_name = str(r[name_col]).strip() if name_col else str(r[acc_name_col]).strip()
            if acct_key and base_name:
                acct_to_base_name[acct_key] = base_name
        if acctnum_col and diff_name_col:
            acct_key = re.sub(r"[^0-9]", "", str(r[acctnum_col]))
            dval = str(r[diff_name_col]).strip()
            base_name = str(r[name_col]).strip() if name_col else ""
            if acct_key and dval and base_name:
                if base_name_counts.get(base_name, 0) > 1 and normalize_name(dval) != normalize_name(base_name):
                    acct_to_diff_name[acct_key] = f"{base_name} - {dval}"
        if acctnum_col and (rev_type_col or billing_type_col):
            acct_key = re.sub(r"[^0-9]", "", str(r[acctnum_col]))
            rev_val = str(r[rev_type_col]).strip().lower() if rev_type_col else ""
            bill_val = str(r[billing_type_col]).strip().lower() if billing_type_col else ""
            evt = "Units" if "unit" in bill_val else ("Per Application" if bill_val else None)
            if acct_key and evt:
                if "income" in rev_val:
                    acct_to_income_evt[acct_key] = evt
                if "lbpa" in rev_val or "l b p a" in rev_val or "loanbeam per application" in rev_val:
                    acct_to_lbpa_evt[acct_key] = evt
    
    parent_to_id = {normalize_name(k): v for k, v in parent_to_id_raw.items()}
    
    return {
        "parent_to_id": parent_to_id,
        "acct_to_tabs_id": acct_to_tabs_id,
        "acct_to_ns_id": acct_to_ns_id,
        "acct_to_income_evt": acct_to_income_evt,
        "acct_to_lbpa_evt": acct_to_lbpa_evt,
        "acct_to_diff_name": acct_to_diff_name,
        "acct_to_base_name": acct_to_base_name,
    }

def transform_usage(uploaded_income, uploaded_lbpa, uploaded_customer, uploaded_clients=None, resolve_now: bool = False, usage_date=None, mappings=None, split_customers=None, api_key=None):
    """Transform usage data from income and LBPA files"""
    # Load mappings: use provided mappings, or extract from clients file, or load from disk
    if mappings:
        # Use provided mappings
        parent_to_id = mappings.get("parent_to_id", {})
        acct_to_tabs_id = mappings.get("acct_to_tabs_id", {})
        acct_to_ns_id = mappings.get("acct_to_ns_id", {})
        acct_to_income_evt = mappings.get("acct_to_income_evt", {})
        acct_to_lbpa_evt = mappings.get("acct_to_lbpa_evt", {})
        acct_to_diff_name = mappings.get("acct_to_diff_name", {})
        acct_to_base_name = mappings.get("acct_to_base_name", {})
    elif uploaded_clients:
        # Extract mappings from clients file
        mappings = extract_mappings_from_clients(uploaded_clients)
        # Save to disk for future use
        _save_client_mappings_to_disk(mappings)
        parent_to_id = mappings.get("parent_to_id", {})
        acct_to_tabs_id = mappings.get("acct_to_tabs_id", {})
        acct_to_ns_id = mappings.get("acct_to_ns_id", {})
        acct_to_income_evt = mappings.get("acct_to_income_evt", {})
        acct_to_lbpa_evt = mappings.get("acct_to_lbpa_evt", {})
        acct_to_diff_name = mappings.get("acct_to_diff_name", {})
        acct_to_base_name = mappings.get("acct_to_base_name", {})
    else:
        # Try to load from disk
        mappings = _load_client_mappings_from_disk()
        if mappings:
            parent_to_id = mappings.get("parent_to_id", {})
            acct_to_tabs_id = mappings.get("acct_to_tabs_id", {})
            acct_to_ns_id = mappings.get("acct_to_ns_id", {})
            acct_to_income_evt = mappings.get("acct_to_income_evt", {})
            acct_to_lbpa_evt = mappings.get("acct_to_lbpa_evt", {})
            acct_to_diff_name = mappings.get("acct_to_diff_name", {})
            acct_to_base_name = mappings.get("acct_to_base_name", {})
        else:
            # No mappings available - use empty dicts
            parent_to_id = {}
            acct_to_tabs_id = {}
            acct_to_ns_id = {}
            acct_to_income_evt = {}
            acct_to_lbpa_evt = {}
            acct_to_diff_name = {}
            acct_to_base_name = {}
            try:
                st.warning("No client mappings found. Proceeding without customer_id mapping.")
            except Exception:
                pass

    # Helper function to extract AccountID from CustomerNumber (handles floats like 8602.0 -> 8602)
    # Must be defined before process_usage since process_usage uses it
    def extract_account_id_from_customer_number(value):
        if pd.isna(value):
            return ""
        # Always convert to float first to handle both int and float inputs, then to int to remove decimal
        try:
            # Convert to float first (handles both numeric and string "8602.0")
            float_val = float(value)
            # Convert to int to remove decimal part (8602.0 -> 8602)
            int_val = int(float_val)
            return str(int_val)
        except (ValueError, TypeError):
            # If conversion fails, try string manipulation
            value_str = str(value).strip()
            # Remove .0 suffix if present
            if value_str.endswith('.0'):
                value_str = value_str[:-2]
            # Extract only numeric characters
            account_id = re.sub(r"[^0-9]", "", value_str)
            return account_id

    def process_usage(df: pd.DataFrame, event_type_name: str, qty_col_candidates: list[str]):
        df.columns = df.columns.str.strip()
        parent_col = find_column(df, ["customername", "accountname", "name"])
        # Prefer CustomerNumber over AccountID since that's what we map with
        acct_id_col = find_column(df, ["customernumber"])  # Try CustomerNumber first
        if not acct_id_col:
            acct_id_col = find_column(df, ["accountid", "acct#", "acct", "account number", "accountnumber"]) 
        datetime_col = find_column(df, ["submissiondate", "date", "createdon", "datetime"])
        qty_col = find_column(df, qty_col_candidates)
        if not parent_col:
            raise KeyError("Customer/Account name column missing")
        if not datetime_col:
            df["__datetime_fallback__"] = pd.Timestamp.today().normalize()
            datetime_col = "__datetime_fallback__"
        if not qty_col:
            raise KeyError("Quantity column not found")

        # Preserve original AccountName column from Income file BEFORE overwriting
        # This is the AccountName column that contains actual account names
        if "AccountName" in df.columns:
            # Preserve the AccountName column BEFORE we overwrite it
            df["__original_account_name__"] = df["AccountName"].copy()
        else:
            df["__original_account_name__"] = ""
        
        # Now set AccountName from parent_col (CustomerName)
        # Check if parent_col column is all NaN and try fallback to AccountName column if it exists
        if df[parent_col].isna().all() and "AccountName" in df.columns:
            # If CustomerName was all NaN, try using AccountName column directly
            account_name_col = find_column(df, ["accountname"])
            if account_name_col and not df[account_name_col].isna().all():
                df["AccountName"] = df[account_name_col]
            else:
                df["AccountName"] = df[parent_col]
        else:
            df["AccountName"] = df[parent_col]
        
        df["value"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0)
        # Always compute account id key if present
        # Use extract_account_id_from_customer_number if it's CustomerNumber column, otherwise use regex
        if acct_id_col:
            if acct_id_col.lower() == "customernumber":
                # Use the same extraction function we use for mapping
                df["__acct_key__"] = df[acct_id_col].apply(extract_account_id_from_customer_number)
            else:
                df["__acct_key__"] = df[acct_id_col].astype(str).str.replace(r"[^0-9]", "", regex=True)
        else:
            df["__acct_key__"] = ""
        # Don't map customer_id before grouping - we'll do it after grouping
        # This avoids issues with None values and ensures the mapping works on grouped data
        # Leave customer_id blank if AccountID mapping is not available.
        # IMPORTANT: Do not group by customer_id (it may be NaN and would drop all rows).
        # For Finastra rows, group by AccountID instead of __acct_key__ (CustomerNumber)
        # This ensures each Finastra AccountID gets its own row
        finastra_mask = df[parent_col].astype(str).str.contains("Finastra", case=False, na=False)
        
        # Create grouping key: use AccountID for Finastra, __acct_key__ for others
        df["__group_key__"] = df["__acct_key__"]  # Default to CustomerNumber
        if finastra_mask.any() and "AccountID" in df.columns:
            # For Finastra: use AccountID (normalized to numeric string) instead of CustomerNumber
            finastra_account_ids = df.loc[finastra_mask, "AccountID"].astype(str).str.replace(r"[^0-9]", "", regex=True)
            df.loc[finastra_mask, "__group_key__"] = finastra_account_ids
        
        group_keys = ["AccountName", "__group_key__"]
        
        agg_dict = {"value": "sum", datetime_col: "max"}
        # Preserve original account name if it exists
        if "__original_account_name__" in df.columns:
            agg_dict["__original_account_name__"] = "first"
        # Preserve AccountID column if it exists
        if "AccountID" in df.columns:
            agg_dict["AccountID"] = "first"
        # Preserve __acct_key__ (CustomerNumber) for customer_id mapping
        # This is needed because we use CustomerNumber to map to customer_id, not AccountID
        agg_dict["__acct_key__"] = "first"
        grouped = (
            df.groupby(group_keys, as_index=False)
              .agg(agg_dict)
        )
        
        # Map customer_id from AccountID using Customer Number custom field
        # For Finastra: use __acct_key__ (CustomerNumber = 9166) to map to customer_id
        # For others: also use __acct_key__ (CustomerNumber)
        acct_mapped = None
        if account_id_to_customer_from_custom_field and len(account_id_to_customer_from_custom_field) > 0:
            # Use __acct_key__ (CustomerNumber) for mapping, not __group_key__ (which is AccountID for Finastra)
            acct_mapped = grouped["__acct_key__"].map(account_id_to_customer_from_custom_field)
            
        # Use AccountID mapping only - missing customer_id values must remain missing
        # Always ensure customer_id column exists
        if acct_mapped is not None and len(acct_mapped) > 0 and acct_mapped.notna().any():
            grouped["customer_id"] = acct_mapped
        else:
            # If no mapping worked, create empty customer_id column with NaN
            grouped["customer_id"] = pd.NA

        grouped["event_type_name"] = event_type_name
        # Differentiator: will be set later for Finastra only
        grouped["differentiator"] = ""
        grouped.rename(columns={datetime_col: "datetime"}, inplace=True)
        # Use usage_date if provided, otherwise use the datetime from the file
        if usage_date is not None:
            # Format the usage_date as YYYY-MM-DD
            grouped["datetime"] = pd.to_datetime(usage_date).strftime("%Y-%m-%d")
        else:
            grouped["datetime"] = pd.to_datetime(grouped["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        # account_id always represents CustomerNumber (__acct_key__)
        grouped["account_id"] = grouped["__acct_key__"] if "__acct_key__" in grouped.columns else pd.NA
        
        # Store Finastra AccountID in separate column (from __group_key__ when it's Finastra)
        grouped["__finastra_account_id__"] = pd.NA
        if finastra_mask.any() and "__group_key__" in grouped.columns:
            # For Finastra rows, __group_key__ contains AccountID (normalized numeric string)
            # Map back to grouped dataframe to identify Finastra rows
            grouped_finastra_mask = grouped["AccountName"].astype(str).str.contains("Finastra", case=False, na=False)
            if grouped_finastra_mask.any():
                grouped.loc[grouped_finastra_mask, "__finastra_account_id__"] = grouped.loc[grouped_finastra_mask, "__group_key__"]
        
        # Include __original_account_name__ in return if it exists
        return_cols = ["customer_id", "AccountName", "event_type_name", "datetime", "value", "differentiator", "account_id"]
        if "__original_account_name__" in grouped.columns:
            return_cols.append("__original_account_name__")
        # Include AccountID if it exists
        if "AccountID" in grouped.columns:
            return_cols.append("AccountID")
        # Include __finastra_account_id__ if it exists (for Finastra differentiators)
        if "__finastra_account_id__" in grouped.columns:
            return_cols.append("__finastra_account_id__")
        return grouped[return_cols]

    income_df = pd.read_csv(uploaded_income)
    lbpa_df = pd.read_csv(uploaded_lbpa)
    
    # Build AccountID to customer_id mapping from customer file if provided
    account_id_to_customer_from_file = {}
    finastra_customer_id_from_file = None  # Hardcoded fallback for Finastra rows
    
    # Customer file is required
    try:
        customer_df = pd.read_csv(uploaded_customer)
        if "ID" in customer_df.columns and "Customer Number" in customer_df.columns:
            # Create mapping from Customer Number (AccountID) to ID (customer_id)
            # Also create mapping from AccountID + Name to customer_id for multi-account customers
            # Get Name column if available
            name_col = "Name" if "Name" in customer_df.columns else None
            
            # Only use customers with "- NMS" in their name
            if name_col:
                original_count = len(customer_df)
                customer_df = customer_df[customer_df[name_col].astype(str).str.contains("- NMS", case=False, na=False)]
            
            valid_rows = customer_df[["ID", "Customer Number"]].dropna()
            valid_rows = valid_rows[valid_rows["Customer Number"].astype(str).str.strip() != ""]
            
            for _, row in valid_rows.iterrows():
                # Extract numeric AccountID (remove all non-numeric characters)
                # Handle both string and numeric types
                customer_number_raw = row["Customer Number"]
                
                # If it's a float (like 4702.0), convert to int first to remove decimal
                if isinstance(customer_number_raw, (int, float)):
                    customer_number_str = str(int(customer_number_raw))
                else:
                    customer_number_str = str(customer_number_raw).strip()
                
                # Remove all non-numeric characters (shouldn't be needed if already numeric, but safe)
                account_id = re.sub(r"[^0-9]", "", customer_number_str)
                customer_id = str(row["ID"]).strip()
                
                if account_id and customer_id:
                    # Store AccountID -> customer_id mapping (for direct AccountID matches)
                    account_id_to_customer_from_file[account_id] = customer_id
            
            # Check for customers with multiple AccountIDs (same customer_id, different AccountIDs)
            customer_to_account_ids = {}
            for account_id, customer_id in account_id_to_customer_from_file.items():
                if customer_id not in customer_to_account_ids:
                    customer_to_account_ids[customer_id] = []
                customer_to_account_ids[customer_id].append(account_id)
            
            # Find Finastra customer_id for hardcoded fallback (must have "- NMS" in name)
            if "Name" in customer_df.columns:
                finastra_rows = customer_df[
                    customer_df["Name"].astype(str).str.contains("Finastra", case=False, na=False) &
                    customer_df["Name"].astype(str).str.contains("- NMS", case=False, na=False)
                ]
                if len(finastra_rows) > 0:
                    finastra_customer_id_from_file = str(finastra_rows.iloc[0]["ID"]).strip()
            
    except Exception as e:
        st.error(f"Error reading customer file: {str(e)}")
        raise
    
    # Extract AccountIDs from uploaded files to only backfill relevant customers
    account_ids_in_files = set()
    if "AccountID" in income_df.columns:
        income_account_ids = income_df["AccountID"].dropna().astype(str).str.replace(r"[^0-9]", "", regex=True)
        account_ids_in_files.update(income_account_ids[income_account_ids.str.strip() != ""].tolist())
    if "AccountID" in lbpa_df.columns:
        lbpa_account_ids = lbpa_df["AccountID"].dropna().astype(str).str.replace(r"[^0-9]", "", regex=True)
        account_ids_in_files.update(lbpa_account_ids[lbpa_account_ids.str.strip() != ""].tolist())
    
    # Check which AccountIDs from files are missing from customer mapping file
    if account_id_to_customer_from_file and account_ids_in_files:
        missing_account_ids = account_ids_in_files - set(account_id_to_customer_from_file.keys())
    
    # Build mapping from AccountID (Customer Number custom field) to customer_id
    # Use customer file mapping only (no API fetching)
    account_id_to_customer_from_custom_field = {}
    
    # If we have customer file mapping, use it as primary source
    if account_id_to_customer_from_file:
        account_id_to_customer_from_custom_field = account_id_to_customer_from_file.copy()
        # Customer mappings loaded silently

    # Build AccountID -> account name mapping for Finastra accounts from ORIGINAL dataframes
    # This ensures we capture all Finastra rows before any grouping/aggregation
    # Strategy: Group by AccountID and find the best account name for each AccountID
    account_id_to_name = {}
    
    # Build mapping from income_df (original, before processing)
    if "AccountID" in income_df.columns:
        # Find Finastra rows - check all possible name columns
        income_finastra_mask = pd.Series([False] * len(income_df))
        name_columns = []
        if "AccountName" in income_df.columns:
            name_columns.append("AccountName")
            income_finastra_mask = income_finastra_mask | income_df["AccountName"].astype(str).str.contains("Finastra", case=False, na=False)
        if "CustomerName" in income_df.columns:
            name_columns.append("CustomerName")
            income_finastra_mask = income_finastra_mask | income_df["CustomerName"].astype(str).str.contains("Finastra", case=False, na=False)
        
        if income_finastra_mask.any():
            finastra_income_rows = income_df[income_finastra_mask]
            
            # Group by AccountID to get unique account names per AccountID
            for account_id_raw, group in finastra_income_rows.groupby("AccountID"):
                account_id_raw = str(account_id_raw).strip()
                if not account_id_raw or account_id_raw.lower() in ["nan", "none", ""]:
                    continue
                account_id = re.sub(r"[^0-9]", "", account_id_raw)
                if not account_id:
                    continue
                
                # Try to find the best account name from available columns
                account_name = None
                for col in name_columns:
                    names = group[col].dropna().astype(str).str.strip()
                    names = names[names != ""]
                    if len(names) > 0:
                        # Prefer names with "Finastra - " format
                        finastra_names = [n for n in names if "Finastra - " in n]
                        if finastra_names:
                            account_name = finastra_names[0]
                            break
                        elif not account_name:
                            # Use first non-empty name as fallback
                            account_name = names.iloc[0]
                
                # Store mapping if we found a valid account name
                if account_name and account_name.lower() != "nan":
                    if account_id not in account_id_to_name or "Finastra - " in account_name:
                        account_id_to_name[account_id] = account_name
    
    # Build mapping from lbpa_df (original, before processing)
    if "AccountID" in lbpa_df.columns:
        # Find Finastra rows - check all possible name columns
        lbpa_finastra_mask = pd.Series([False] * len(lbpa_df))
        name_columns = []
        if "AccountName" in lbpa_df.columns:
            name_columns.append("AccountName")
            lbpa_finastra_mask = lbpa_finastra_mask | lbpa_df["AccountName"].astype(str).str.contains("Finastra", case=False, na=False)
        if "CustomerName" in lbpa_df.columns:
            name_columns.append("CustomerName")
            lbpa_finastra_mask = lbpa_finastra_mask | lbpa_df["CustomerName"].astype(str).str.contains("Finastra", case=False, na=False)
        
        if lbpa_finastra_mask.any():
            finastra_lbpa_rows = lbpa_df[lbpa_finastra_mask]
            
            # Group by AccountID to get unique account names per AccountID
            for account_id_raw, group in finastra_lbpa_rows.groupby("AccountID"):
                account_id_raw = str(account_id_raw).strip()
                if not account_id_raw or account_id_raw.lower() in ["nan", "none", ""]:
                    continue
                account_id = re.sub(r"[^0-9]", "", account_id_raw)
                if not account_id or account_id in account_id_to_name:  # Don't overwrite if already set
                    continue
                
                # Try to find the best account name from available columns
                account_name = None
                for col in name_columns:
                    names = group[col].dropna().astype(str).str.strip()
                    names = names[names != ""]
                    if len(names) > 0:
                        # Prefer names with "Finastra - " format
                        finastra_names = [n for n in names if "Finastra - " in n]
                        if finastra_names:
                            account_name = finastra_names[0]
                            break
                        elif not account_name:
                            # Use first non-empty name as fallback
                            account_name = names.iloc[0]
                
                # Store mapping if we found a valid account name
                if account_name and account_name.lower() != "nan":
                    if "Finastra - " in account_name:
                        account_id_to_name[account_id] = account_name
    
    # Process Income for both "Per Application" and "Units" separately
    income_upload_apps = process_usage(income_df, "Per Application",
                                       ["isinitialsubmission", "perapplication", "applicationcount"])
    income_upload_apps["ApplicationTypeName"] = "Income"

    income_upload_units = process_usage(income_df, "Units",
                                        ["unitsaspersubmission", "units", "unitcount"])
    income_upload_units["ApplicationTypeName"] = "Income"

    # Filter out rows with zero values before concatenation to avoid duplicates
    # Only include rows from income_upload_apps if they have value > 0
    income_upload_apps_filtered = income_upload_apps[income_upload_apps["value"] > 0].copy() if len(income_upload_apps) > 0 else income_upload_apps
    # Only include rows from income_upload_units if they have value > 0
    income_upload_units_filtered = income_upload_units[income_upload_units["value"] > 0].copy() if len(income_upload_units) > 0 else income_upload_units
    
    # Combine rows with the same AccountName + account_id before concatenation
    # This prevents duplicates when a customer appears in both apps and units processing
    # If a customer has the same AccountName and account_id in both, keep only one row (prefer "app")
    if len(income_upload_apps_filtered) > 0 and len(income_upload_units_filtered) > 0:
        # Create a key for matching: AccountName + account_id
        income_upload_apps_filtered["__match_key__"] = (
            income_upload_apps_filtered["AccountName"].astype(str) + "_" + 
            income_upload_apps_filtered["account_id"].astype(str)
        )
        income_upload_units_filtered["__match_key__"] = (
            income_upload_units_filtered["AccountName"].astype(str) + "_" + 
            income_upload_units_filtered["account_id"].astype(str)
        )
        
        # Find rows that appear in both (same AccountName + account_id)
        apps_keys = set(income_upload_apps_filtered["__match_key__"].unique())
        units_keys = set(income_upload_units_filtered["__match_key__"].unique())
        common_keys = apps_keys & units_keys
        
        if len(common_keys) > 0:
            # For common keys, remove from units (keep the "app" row)
            income_upload_units_filtered = income_upload_units_filtered[~income_upload_units_filtered["__match_key__"].isin(common_keys)]
        
        # Remove helper columns (all columns starting with "__" except __original_account_name__ which we preserve)
        helper_cols_apps = [col for col in income_upload_apps_filtered.columns if col.startswith("__") and col != "__original_account_name__"]
        helper_cols_units = [col for col in income_upload_units_filtered.columns if col.startswith("__") and col != "__original_account_name__"]
        if helper_cols_apps:
            income_upload_apps_filtered = income_upload_apps_filtered.drop(columns=helper_cols_apps, errors="ignore")
        if helper_cols_units:
            income_upload_units_filtered = income_upload_units_filtered.drop(columns=helper_cols_units, errors="ignore")
        
        # Rename __original_account_name__ to original_account_name to preserve it
        if "__original_account_name__" in income_upload_apps_filtered.columns:
            income_upload_apps_filtered = income_upload_apps_filtered.rename(columns={"__original_account_name__": "original_account_name"})
        if "__original_account_name__" in income_upload_units_filtered.columns:
            income_upload_units_filtered = income_upload_units_filtered.rename(columns={"__original_account_name__": "original_account_name"})
    
    # Concatenate Income apps and units (only rows with values > 0, duplicates removed)
    income_upload = pd.concat([income_upload_apps_filtered, income_upload_units_filtered], ignore_index=True)
    # Apply optional event type overrides from mapping (by account_id)
    if acct_to_income_evt:
        income_upload["event_type_name"] = income_upload["account_id"].map(acct_to_income_evt).fillna(income_upload["event_type_name"])

    lbpa_upload = process_usage(lbpa_df, "Units",
                                ["unitsaspersubmission", "units", "unitcount"])
    lbpa_upload["ApplicationTypeName"] = "LBPA"
    
    if acct_to_lbpa_evt:
        lbpa_upload["event_type_name"] = lbpa_upload["account_id"].map(acct_to_lbpa_evt).fillna(lbpa_upload["event_type_name"])
    
    # Set differentiators for Finastra rows in income_upload and lbpa_upload BEFORE concatenation
    def set_finastra_differentiators(df, account_id_to_name_map, df_name=""):
        """Set differentiators for Finastra rows in a dataframe"""
        finastra_mask = (
            df.get("CustomerName", pd.Series([""] * len(df))).astype(str).str.contains("Finastra", case=False, na=False) |
            df.get("AccountName", pd.Series([""] * len(df))).astype(str).str.contains("Finastra", case=False, na=False)
        )
        if not finastra_mask.any():
            return df
        
        finastra_rows = df[finastra_mask].copy()
        mapped_differentiators = pd.Series(index=finastra_rows.index, dtype=str)
        
        if account_id_to_name_map:
            # Use __finastra_account_id__ column only (no AccountID fallback)
            if "__finastra_account_id__" in finastra_rows.columns:
                finastra_account_ids = finastra_rows["__finastra_account_id__"].astype(str).str.replace(r"[^0-9]", "", regex=True)
                mapped_by_account_id = finastra_account_ids.map(account_id_to_name_map)
                mapped_differentiators = mapped_by_account_id.fillna("")
            else:
                # If __finastra_account_id__ is missing, leave differentiator empty
                mapped_differentiators = pd.Series(index=finastra_rows.index, dtype=str)
                mapped_differentiators[:] = ""
            
            df.loc[finastra_mask, "differentiator"] = mapped_differentiators
        else:
            # Fallback: use original_account_name if available (renamed from __original_account_name__)
            if "original_account_name" in df.columns:
                df.loc[finastra_mask, "differentiator"] = df.loc[finastra_mask, "original_account_name"].astype(str).str.strip()
        
        return df
    
    # Apply differentiators to both uploads
    income_upload = set_finastra_differentiators(income_upload, account_id_to_name, "income_upload")
    lbpa_upload = set_finastra_differentiators(lbpa_upload, account_id_to_name, "lbpa_upload")
    
    # Final combined usage (internal dataframe with account_id)
    combined_internal = pd.concat([income_upload, lbpa_upload], ignore_index=True)
    
    # Map event_type_name based on ApplicationTypeName
    # Income: "Per Application" -> "app", "Units" -> "unit"
    # LBPA: "Per Application" -> "LBPA app", "Units" -> "LBPA unit"
    income_mask = combined_internal["ApplicationTypeName"] == "Income"
    lbpa_mask = combined_internal["ApplicationTypeName"] == "LBPA"
    
    # Income mapping
    combined_internal.loc[income_mask & (combined_internal["event_type_name"] == "Per Application"), "event_type_name"] = "app"
    combined_internal.loc[income_mask & (combined_internal["event_type_name"] == "Units"), "event_type_name"] = "unit"
    
    # LBPA mapping
    combined_internal.loc[lbpa_mask & (combined_internal["event_type_name"] == "Per Application"), "event_type_name"] = "LBPA app"
    combined_internal.loc[lbpa_mask & (combined_internal["event_type_name"] == "Units"), "event_type_name"] = "LBPA unit"
    
    # Keep customer_id as nullable until final CSV export
    
    # Set customer_id in combined_internal using account_id mapping (authoritative source)
    # Initialize customer_id column if it doesn't exist
    if "customer_id" not in combined_internal.columns:
        combined_internal["customer_id"] = None
    
    # Map customer_id using account_id from customer file (primary method)
    if "account_id" in combined_internal.columns:
        if account_id_to_customer_from_file:
            account_ids_normalized = combined_internal["account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True)
            combined_internal["customer_id"] = account_ids_normalized.map(account_id_to_customer_from_file)
        
        # Then try Customer Number custom field mapping as fallback (for unmapped AccountIDs)
        if account_id_to_customer_from_custom_field and combined_internal["customer_id"].isna().any():
            unmapped_mask = combined_internal["customer_id"].isna() & combined_internal["account_id"].notna()
            if unmapped_mask.any():
                unmapped_account_ids = combined_internal.loc[unmapped_mask, "account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True)
                mapped = unmapped_account_ids.map(account_id_to_customer_from_custom_field)
                combined_internal.loc[unmapped_mask, "customer_id"] = mapped
    
    # Direct Finastra fallback: Apply Finastra customer_id to any Finastra rows that still don't have it
    if finastra_customer_id_from_file:
        # Check for Finastra rows by CustomerName or AccountName
        name_col = None
        if "CustomerName" in combined_internal.columns:
            name_col = "CustomerName"
        elif "AccountName" in combined_internal.columns:
            name_col = "AccountName"
        
        if name_col:
            finastra_mask = (
                combined_internal[name_col].astype(str).str.contains("Finastra", case=False, na=False) &
                combined_internal["customer_id"].isna()
            )
            if finastra_mask.any():
                combined_internal.loc[finastra_mask, "customer_id"] = finastra_customer_id_from_file
    
    valid_customer_mask = combined_internal["customer_id"].notna()
    
    # NOW calculate sums AFTER customer_id resolution
    # Sum UnitsAsPerSubmission and IsInitialSubmission directly from Income and LBPA files per customer_id
    # Use the account_id to customer_id mapping from combined_internal to map back to Income and LBPA files
    
    # Create a mapping from account_id to customer_id from combined_internal
    account_to_customer_mapping = {}
    if "account_id" in combined_internal.columns and "customer_id" in combined_internal.columns:
        valid_mapping_mask = (
            combined_internal["customer_id"].notna() &
            combined_internal["account_id"].notna()
        )
        mapping_df = combined_internal[valid_mapping_mask][["account_id", "customer_id"]].drop_duplicates()
        account_to_customer_mapping = dict(zip(
            mapping_df["account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True),
            mapping_df["customer_id"]
        ))
    
    # Map customer_id to Income and LBPA files using account_id
    income_df_with_customer = income_df.copy()
    lbpa_df_with_customer = lbpa_df.copy()
    
    # Initialize customer_id columns once (keep as nullable until final CSV export)
    if "customer_id" not in income_df_with_customer.columns:
        income_df_with_customer["customer_id"] = None
    if "customer_id" not in lbpa_df_with_customer.columns:
        lbpa_df_with_customer["customer_id"] = None
    
    # Map customer_id to Income file using CustomerNumber column - prefer Customer Number column from customer file
    if "CustomerNumber" in income_df_with_customer.columns:
        income_account_ids = income_df_with_customer["CustomerNumber"].apply(extract_account_id_from_customer_number)
        # First try customer file mapping (primary method - Customer Number column)
        # Since all historical AccountIDs are set and new ones will sync automatically, prioritize file mapping
        if account_id_to_customer_from_file:
            unique_income_account_ids = income_account_ids[income_account_ids != ""].unique()
            matching_account_ids = [aid for aid in unique_income_account_ids if aid in account_id_to_customer_from_file]
            income_df_with_customer["customer_id"] = income_account_ids.map(account_id_to_customer_from_file)
            # Count how many were actually matched
            income_matched = income_df_with_customer["customer_id"].notna().sum()
            income_total = len(income_df_with_customer)
        
        # Then try Customer Number custom field mapping as fallback (for unmapped AccountIDs)
        if account_id_to_customer_from_custom_field and income_df_with_customer["customer_id"].isna().any():
            unmapped_mask = income_df_with_customer["customer_id"].isna() & income_df_with_customer["CustomerNumber"].notna()
            if unmapped_mask.any():
                unmapped_account_ids = income_df_with_customer.loc[unmapped_mask, "CustomerNumber"].apply(extract_account_id_from_customer_number)
                mapped = unmapped_account_ids.map(account_id_to_customer_from_custom_field)
                income_df_with_customer.loc[unmapped_mask, "customer_id"] = mapped
        
        # Then try account_to_customer_mapping from combined_internal (only if CustomerNumber column exists)
        if account_to_customer_mapping and "CustomerNumber" in income_df_with_customer.columns:
            income_account_ids_for_mapping = income_df_with_customer["CustomerNumber"].apply(extract_account_id_from_customer_number)
            income_df_with_customer["customer_id"] = income_df_with_customer["customer_id"].fillna(
                income_account_ids_for_mapping.map(account_to_customer_mapping)
            )
    
    # Map customer_id to LBPA file using CustomerNumber column - prefer Customer Number column from customer file
    if "CustomerNumber" in lbpa_df_with_customer.columns:
        lbpa_account_ids = lbpa_df_with_customer["CustomerNumber"].apply(extract_account_id_from_customer_number)
        # First try customer file mapping (primary method - Customer Number column)
        # Since all historical AccountIDs are set and new ones will sync automatically, prioritize file mapping
        if account_id_to_customer_from_file:
            lbpa_df_with_customer["customer_id"] = lbpa_account_ids.map(account_id_to_customer_from_file)
            # Count how many were actually matched
            lbpa_matched = lbpa_df_with_customer["customer_id"].notna().sum()
            lbpa_total = len(lbpa_df_with_customer)
        
        # Then try Customer Number custom field mapping as fallback (for unmapped AccountIDs)
        if account_id_to_customer_from_custom_field and lbpa_df_with_customer["customer_id"].isna().any():
            unmapped_mask = lbpa_df_with_customer["customer_id"].isna() & lbpa_df_with_customer["CustomerNumber"].notna()
            if unmapped_mask.any():
                unmapped_account_ids = lbpa_df_with_customer.loc[unmapped_mask, "CustomerNumber"].apply(extract_account_id_from_customer_number)
                mapped = unmapped_account_ids.map(account_id_to_customer_from_custom_field)
                lbpa_df_with_customer.loc[unmapped_mask, "customer_id"] = mapped
        
        # Then try account_to_customer_mapping from combined_internal
        if account_to_customer_mapping:
            lbpa_df_with_customer["customer_id"] = lbpa_df_with_customer["customer_id"].fillna(
                lbpa_account_ids.map(account_to_customer_mapping)
            )
    
    income_valid_mask = income_df_with_customer["customer_id"].notna()
    
    lbpa_valid_mask = lbpa_df_with_customer["customer_id"].notna()
    
    # For Finastra customers, we need to group by customer_id + account_id (differentiator)
    # For other customers, group by customer_id only
    # First, identify Finastra customers
    income_finastra_mask = income_df_with_customer["CustomerName"].astype(str).str.contains("finastra", case=False, na=False)
    lbpa_finastra_mask = lbpa_df_with_customer["CustomerName"].astype(str).str.contains("finastra", case=False, na=False)
    
    # Create a grouping key: for Finastra use customer_id + account_id, for others just customer_id
    income_df_with_customer["__group_key__"] = income_df_with_customer["customer_id"]
    if "AccountID" in income_df_with_customer.columns:
        income_finastra_rows = income_df_with_customer[income_finastra_mask & income_valid_mask]
        if len(income_finastra_rows) > 0:
            income_df_with_customer.loc[income_finastra_mask & income_valid_mask, "__group_key__"] = (
                income_df_with_customer.loc[income_finastra_mask & income_valid_mask, "customer_id"].astype(str) + "_" +
                income_df_with_customer.loc[income_finastra_mask & income_valid_mask, "AccountID"].astype(str).str.replace(r"[^0-9]", "", regex=True)
            )
    
    lbpa_df_with_customer["__group_key__"] = lbpa_df_with_customer["customer_id"]
    if "AccountID" in lbpa_df_with_customer.columns:
        lbpa_finastra_rows = lbpa_df_with_customer[lbpa_finastra_mask & lbpa_valid_mask]
        if len(lbpa_finastra_rows) > 0:
            lbpa_df_with_customer.loc[lbpa_finastra_mask & lbpa_valid_mask, "__group_key__"] = (
                lbpa_df_with_customer.loc[lbpa_finastra_mask & lbpa_valid_mask, "customer_id"].astype(str) + "_" +
                lbpa_df_with_customer.loc[lbpa_finastra_mask & lbpa_valid_mask, "AccountID"].astype(str).str.replace(r"[^0-9]", "", regex=True)
            )
    
    # Sum UnitsAsPerSubmission and IsInitialSubmission from Income file per group_key
    customer_units_sums = {}
    customer_app_sums = {}
    
    if "UnitsAsPerSubmission" in income_df_with_customer.columns and income_valid_mask.any():
        # Convert to numeric, handling any non-numeric values
        income_df_with_customer["UnitsAsPerSubmission"] = pd.to_numeric(income_df_with_customer["UnitsAsPerSubmission"], errors="coerce").fillna(0)
        income_units = income_df_with_customer[income_valid_mask].groupby("__group_key__")["UnitsAsPerSubmission"].sum()
        customer_units_sums.update(income_units.to_dict())
    
    if "IsInitialSubmission" in income_df_with_customer.columns and income_valid_mask.any():
        # Convert to numeric, handling any non-numeric values
        income_df_with_customer["IsInitialSubmission"] = pd.to_numeric(income_df_with_customer["IsInitialSubmission"], errors="coerce").fillna(0)
        income_apps = income_df_with_customer[income_valid_mask].groupby("__group_key__")["IsInitialSubmission"].sum()
        customer_app_sums.update(income_apps.to_dict())
    
    # Sum UnitsAsPerSubmission and IsInitialSubmission from LBPA file per group_key
    if "UnitsAsPerSubmission" in lbpa_df_with_customer.columns and lbpa_valid_mask.any():
        # Convert to numeric, handling any non-numeric values
        lbpa_df_with_customer["UnitsAsPerSubmission"] = pd.to_numeric(lbpa_df_with_customer["UnitsAsPerSubmission"], errors="coerce").fillna(0)
        lbpa_units = lbpa_df_with_customer[lbpa_valid_mask].groupby("__group_key__")["UnitsAsPerSubmission"].sum()
        for group_key, value in lbpa_units.items():
            customer_units_sums[group_key] = customer_units_sums.get(group_key, 0) + value
    
    if "IsInitialSubmission" in lbpa_df_with_customer.columns and lbpa_valid_mask.any():
        # Convert to numeric, handling any non-numeric values
        lbpa_df_with_customer["IsInitialSubmission"] = pd.to_numeric(lbpa_df_with_customer["IsInitialSubmission"], errors="coerce").fillna(0)
        lbpa_apps = lbpa_df_with_customer[lbpa_valid_mask].groupby("__group_key__")["IsInitialSubmission"].sum()
        for group_key, value in lbpa_apps.items():
            customer_app_sums[group_key] = customer_app_sums.get(group_key, 0) + value
    
    # Initialize columns
    combined_internal["UnitsAsPerSubmission"] = 0
    combined_internal["IsInitialSubmission"] = 0
    
    # Set CustomerName if not already set (needed for group_key logic)
    if "CustomerName" not in combined_internal.columns:
        combined_internal["CustomerName"] = combined_internal.get("AccountName", "")
    
    # Create group_key in combined_internal for mapping
    combined_internal["__group_key__"] = combined_internal["customer_id"].astype(str)
    finastra_mask = combined_internal["CustomerName"].astype(str).str.contains("finastra", case=False, na=False)
    # For Finastra, use __finastra_account_id__ instead of account_id (which is CustomerNumber)
    if "__finastra_account_id__" in combined_internal.columns:
        finastra_with_account = finastra_mask & combined_internal["__finastra_account_id__"].notna()
        combined_internal.loc[finastra_with_account, "__group_key__"] = (
            combined_internal.loc[finastra_with_account, "customer_id"].astype(str) + "_" +
            combined_internal.loc[finastra_with_account, "__finastra_account_id__"].astype(str).str.replace(r"[^0-9]", "", regex=True)
        )
    
    # Map sums to all rows using group_key
    if customer_units_sums and valid_customer_mask.any():
        mapped_units = combined_internal.loc[valid_customer_mask, "__group_key__"].map(customer_units_sums).fillna(0)
        combined_internal.loc[valid_customer_mask, "UnitsAsPerSubmission"] = mapped_units
    
    if customer_app_sums and valid_customer_mask.any():
        mapped_apps = combined_internal.loc[valid_customer_mask, "__group_key__"].map(customer_app_sums).fillna(0)
        combined_internal.loc[valid_customer_mask, "IsInitialSubmission"] = mapped_apps
    
    # Update value column based on event_type_name:
    # - If event_type_name contains "unit" → value = UnitsAsPerSubmission
    # - If event_type_name contains "app" → value = IsInitialSubmission
    evt_lower = combined_internal["event_type_name"].astype(str).str.lower()
    unit_mask = evt_lower.str.contains("unit")
    app_mask = evt_lower.str.contains("app")
    
    # Only update rows with valid customer_id
    if valid_customer_mask.any():
        # For unit events, set value to UnitsAsPerSubmission
        if unit_mask.any():
            combined_internal.loc[unit_mask & valid_customer_mask, "value"] = (
                combined_internal.loc[unit_mask & valid_customer_mask, "UnitsAsPerSubmission"]
            )
        
        # For app events, set value to IsInitialSubmission
        if app_mask.any():
            combined_internal.loc[app_mask & valid_customer_mask, "value"] = (
                combined_internal.loc[app_mask & valid_customer_mask, "IsInitialSubmission"]
            )
    
    # Set CustomerName if not already set (should be set during aggregation)
    if "CustomerName" not in combined_internal.columns:
        combined_internal["CustomerName"] = combined_internal.get("AccountName", "")
    
    # Differentiators for Finastra rows are already set before concatenation
    # Just ensure non-Finastra rows have empty differentiator
    # Only update if differentiator is not already set (preserve what was set by set_finastra_differentiators)
    finastra_mask = (
        combined_internal["CustomerName"].astype(str).str.contains("Finastra", case=False, na=False) |
        combined_internal.get("AccountName", pd.Series([""] * len(combined_internal))).astype(str).str.contains("Finastra", case=False, na=False)
    )
    
    # Only set differentiator for Finastra rows that don't already have one
    # This preserves the differentiators set by set_finastra_differentiators which uses __finastra_account_id__
    if finastra_mask.any():
        finastra_rows_mask = finastra_mask & (
            combined_internal["differentiator"].isna() | 
            (combined_internal["differentiator"].astype(str).str.strip() == "")
        )
        
        if finastra_rows_mask.any() and account_id_to_name:
            # Fill in missing differentiators using __finastra_account_id__ only (no AccountID fallback)
            finastra_rows = combined_internal[finastra_rows_mask].copy()
            if "__finastra_account_id__" in finastra_rows.columns:
                finastra_account_ids = finastra_rows["__finastra_account_id__"].astype(str).str.replace(r"[^0-9]", "", regex=True)
                mapped_differentiators = finastra_account_ids.map(account_id_to_name)
            else:
                # If __finastra_account_id__ is missing, leave differentiator empty
                mapped_differentiators = pd.Series(index=finastra_rows.index, dtype=str)
                mapped_differentiators[:] = ""
            
            # Fill in any unmapped ones using original_account_name as fallback (renamed from __original_account_name__)
            unmapped_mask = mapped_differentiators.isna() | (mapped_differentiators == "")
            if unmapped_mask.any() and "original_account_name" in finastra_rows.columns:
                unmapped_rows = finastra_rows[unmapped_mask]
                for idx in unmapped_rows.index:
                    original_name = str(unmapped_rows.loc[idx, "original_account_name"]).strip()
                    if original_name and original_name.lower() != "nan":
                        mapped_differentiators.loc[idx] = original_name
            
            # Set differentiators for Finastra rows that don't have one
            combined_internal.loc[finastra_rows_mask, "differentiator"] = mapped_differentiators.fillna("")
        elif "original_account_name" in combined_internal.columns:
            # Fallback: use original_account_name if account_id_to_name not available (renamed from __original_account_name__)
            combined_internal.loc[finastra_rows_mask, "differentiator"] = (
                combined_internal.loc[finastra_rows_mask, "original_account_name"].astype(str)
            )
    
    # Set differentiator to empty for non-Finastra customers (only if not already set)
    non_finastra_mask = ~finastra_mask & (
        combined_internal["differentiator"].isna() | 
        (combined_internal["differentiator"].astype(str).str.strip() == "")
    )
    combined_internal.loc[non_finastra_mask, "differentiator"] = ""
    
    # Initialize invoice column
    combined_internal["invoice"] = ""
    
    # Handle split invoices if enabled
    if split_customers and len(split_customers) > 0:
        # Apply split numbering to selected customers (by CustomerName)
        for customer_name in split_customers:
            # Get all rows for this customer by matching CustomerName
            customer_mask = combined_internal["CustomerName"].astype(str).str.strip() == str(customer_name).strip()
            customer_data = combined_internal[customer_mask]
            
            if len(customer_data) > 0:
                # Get all row indices for this customer (don't group by date)
                customer_indices = customer_data.index.tolist()
                
                # Apply numbering: blank, 1, 2, 3... for all rows of this customer
                for i, idx in enumerate(customer_indices):
                    if i == 0:
                        combined_internal.loc[idx, "invoice"] = ""  # First row blank
                    else:
                        combined_internal.loc[idx, "invoice"] = str(i)  # Subsequent rows: 1, 2, 3...

    # Order/output columns to match Tabs expected headers
    upload_cols = [
        "customer_id",
        "CustomerName",
        "event_type_name",
        "datetime",
        "ApplicationTypeName",
        "UnitsAsPerSubmission",
        "IsInitialSubmission",
        "value",
        "differentiator",
        "invoice",
    ]
    
    # Validate and correct event_type_name using API mapping
    # Use provided API key or fall back to global API_KEY
    validation_api_key = api_key if api_key else API_KEY
    
    if validation_api_key and validation_api_key.strip():
        # Only validate rows with valid customer IDs from income/LBPA files
        validation_mask = valid_customer_mask.copy()
        
        # Get unique customer IDs from rows that will be validated
        unique_customer_ids = combined_internal.loc[validation_mask, "customer_id"].dropna().unique()
        unique_customer_ids = [str(cid).strip() for cid in unique_customer_ids if str(cid).strip()]
        
        if len(unique_customer_ids) > 0:
            # Build customer -> event type names mapping for all customers
            # Use /v3/obligations?filter=customerId:eq:"{customer_id}" + /v3/events/types
            customer_event_types = build_customer_event_type_mapping(unique_customer_ids, validation_api_key, use_obligations=True)
            
            if customer_event_types:
                # Validate and correct event_type_name for each row
                # Only use these 4 event type names: "app", "unit", "LBPA app", "LBPA unit"
                # Map API event types to these standard names
                corrections_made = 0
                unmatched_count = 0
                
                # Mapping from API event types to standard event type names
                api_to_standard = {
                    "app": "app",
                    "per application": "app",
                    "unit": "unit",
                    "units": "unit",
                    "lbpa app": "LBPA app",
                    "lbpa application": "LBPA app",
                    "lbpa unit": "LBPA unit",
                    "lbpa units": "LBPA unit",
                }
                
                rows_to_validate = combined_internal[validation_mask]
                
                # Build correction mapping: customer_id -> correct event type(s)
                customer_corrections = {}
                
                for customer_id in unique_customer_ids:
                    if customer_id in customer_event_types:
                        valid_event_types = customer_event_types[customer_id]
                        
                        # Map API event types to standard names
                        api_to_standard_map = {
                            "app": "app",
                            "per application": "app",
                            "unit": "unit",
                            "units": "unit",
                            "lbpa app": "LBPA app",
                            "lbpa application": "LBPA app",
                            "lbpa unit": "LBPA unit",
                            "lbpa units": "LBPA unit",
                        }
                        
                        # Find which standard event types this customer has in the API
                        customer_standard_types = set()
                        for et in valid_event_types:
                            et_lower = et.lower()
                            if et_lower in api_to_standard_map:
                                customer_standard_types.add(api_to_standard_map[et_lower])
                        
                        # Determine correct event types for Income and LBPA
                        income_event_type = None
                        lbpa_event_type = None
                        
                        has_app = "app" in customer_standard_types
                        has_unit = "unit" in customer_standard_types
                        has_lbpa_app = "LBPA app" in customer_standard_types
                        has_lbpa_unit = "LBPA unit" in customer_standard_types
                        
                        # For Income: use what customer has (prefer unit if only one, otherwise use what's set)
                        if has_unit and not has_app:
                            income_event_type = "unit"
                        elif has_app and not has_unit:
                            income_event_type = "app"
                        elif has_app and has_unit:
                            # Customer has both - will be determined per row
                            income_event_type = None
                        
                        # For LBPA: use what customer has
                        if has_lbpa_unit and not has_lbpa_app:
                            lbpa_event_type = "LBPA unit"
                        elif has_lbpa_app and not has_lbpa_unit:
                            lbpa_event_type = "LBPA app"
                        elif has_lbpa_app and has_lbpa_unit:
                            lbpa_event_type = None
                        
                        customer_corrections[customer_id] = {
                            "income": income_event_type,
                            "lbpa": lbpa_event_type,
                            "has_app": has_app,
                            "has_unit": has_unit,
                            "has_lbpa_app": has_lbpa_app,
                            "has_lbpa_unit": has_lbpa_unit,
                        }
                
                # Apply corrections to all rows
                for idx, row in rows_to_validate.iterrows():
                    customer_id = str(row.get("customer_id", "")).strip()
                    current_event_type = str(row.get("event_type_name", "")).strip()
                    application_type = str(row.get("ApplicationTypeName", "")).strip()
                    
                    if customer_id in customer_corrections:
                        correction_info = customer_corrections[customer_id]
                        correct_event_type = None
                        
                        if application_type == "Income":
                            # If customer only has one option, use it
                            if correction_info["income"]:
                                correct_event_type = correction_info["income"]
                            elif correction_info["has_app"] and correction_info["has_unit"]:
                                # Customer has both - check data to determine which one to use
                                units_value = row.get("UnitsAsPerSubmission", 0)
                                app_value = row.get("IsInitialSubmission", 0)
                                
                                # If current event type is valid, keep it
                                if current_event_type.lower() in ["app", "unit"]:
                                    correct_event_type = current_event_type
                                # Otherwise, prefer unit if UnitsAsPerSubmission > 0, otherwise prefer unit over app
                                elif units_value and units_value > 0:
                                    correct_event_type = "unit"
                                elif app_value and app_value > 0:
                                    correct_event_type = "app"
                                else:
                                    # Default to unit when both are available (unit is more common)
                                    correct_event_type = "unit"
                            else:
                                # Customer has neither - mark as unmatched
                                correct_event_type = None
                                
                        elif application_type == "LBPA":
                            # If customer only has one option, use it
                            if correction_info["lbpa"]:
                                correct_event_type = correction_info["lbpa"]
                            elif correction_info["has_lbpa_app"] and correction_info["has_lbpa_unit"]:
                                # Customer has both - check data to determine which one to use
                                units_value = row.get("UnitsAsPerSubmission", 0)
                                app_value = row.get("IsInitialSubmission", 0)
                                
                                # If current event type is valid, keep it
                                if current_event_type.lower() in ["lbpa app", "lbpa unit"]:
                                    correct_event_type = current_event_type
                                # Otherwise, prefer LBPA unit if UnitsAsPerSubmission > 0
                                elif units_value and units_value > 0:
                                    correct_event_type = "LBPA unit"
                                elif app_value and app_value > 0:
                                    correct_event_type = "LBPA app"
                                else:
                                    # Default to LBPA unit when both are available
                                    correct_event_type = "LBPA unit"
                            else:
                                # Customer has neither - mark as unmatched
                                correct_event_type = None
                        
                        # Apply the correct event type
                        if correct_event_type:
                            if current_event_type != correct_event_type:
                                combined_internal.loc[idx, "event_type_name"] = correct_event_type
                                corrections_made += 1
                        else:
                            unmatched_count += 1
                    else:
                        unmatched_count += 1
                
                # Show summary - store for combined message later
                total_validated = len(combined_internal[validation_mask])
                validated_count = total_validated - unmatched_count
                # Store event type validation stats for combined message
                event_type_corrections = corrections_made
                event_type_validated = validated_count
                event_type_unmatched = unmatched_count
            else:
                # No customer_event_types from validation, initialize empty
                customer_event_types = {}
                event_type_corrections = 0
                event_type_validated = 0
                event_type_unmatched = 0
        else:
            # No unique customer IDs, initialize empty
            customer_event_types = {}
            event_type_corrections = 0
            event_type_validated = 0
            event_type_unmatched = 0
    else:
        # No API key provided, initialize empty
        customer_event_types = {}
        event_type_corrections = 0
        event_type_validated = 0
        event_type_unmatched = 0
    
    # Generating usage file...
    # Separate unmapped rows (customer_id is missing AND CustomerName is still numeric/account ID)
    # Derive customer_id_missing as the inverse of valid_customer_mask
    customer_id_missing = ~valid_customer_mask
    customer_name_is_numeric = combined_internal["CustomerName"].astype(str).str.match(r'^\d+$', na=False)
    unmapped_mask = customer_id_missing & customer_name_is_numeric
    
    # Show combined summary of mapping results
    total_rows = len(combined_internal)
    mapped_rows = total_rows - customer_id_missing.sum()
    unmapped_rows = customer_id_missing.sum()
    
    # Build combined message
    message_parts = []
    if mapped_rows > 0:
        message_parts.append(f"{mapped_rows:,} of {total_rows:,} rows mapped to customer IDs")
    if event_type_validated > 0:
        message_parts.append(f"{event_type_validated:,} event types validated")
    
    if message_parts:
        combined_message = "✅ " + ", ".join(message_parts)
        st.success(combined_message)
    
    # Also check for rows with invalid customer_id (missing)
    # Derive invalid_customer_mask as the inverse of valid_customer_mask
    invalid_customer_mask = ~valid_customer_mask
    
    # Clear event_type_name for rows without customer_id (can't be validated against API)
    combined_internal.loc[customer_id_missing, "event_type_name"] = ""
    
    # Create separate DataFrames for mapped and unmapped rows
    # Only include rows with valid customer_id in the main output
    unmapped_df = combined_internal[unmapped_mask].copy()
    
    # Separate rows with missing customer_id (regardless of CustomerName status)
    missing_customer_id_df = combined_internal[customer_id_missing].copy()
    
    mapped_df = combined_internal[valid_customer_mask].copy()  # Use valid_customer_mask instead of ~unmapped_mask
    
    # Group by customer_id and differentiator to combine multiple rows per customer
    # This ensures Finastra accounts (with different differentiators) remain separate
    if len(mapped_df) > 0:
        # Determine aggregation strategy for each column
        agg_dict = {}
        for col in upload_cols:
            if col in mapped_df.columns:
                if col == "value":
                    # Value will be recalculated based on event type after grouping
                    agg_dict[col] = "sum"  # Temporary, will be overwritten
                elif col in ["UnitsAsPerSubmission", "IsInitialSubmission"]:
                    # These are already aggregated per customer/account, so use "first" or "max" to avoid doubling
                    # We'll recalculate them properly after grouping
                    agg_dict[col] = "first"
                elif col == "event_type_name":
                    # For event_type_name, if customer has multiple rows, keep the first one
                    # (The validation should have already corrected them to match API)
                    agg_dict[col] = "first"
                else:
                    # Keep first value for other text columns
                    agg_dict[col] = "first"
        
        # Group by customer_id and differentiator to keep Finastra accounts separate
        # Use differentiator if available, otherwise just customer_id
        if "differentiator" in mapped_df.columns:
            group_keys = ["customer_id", "differentiator"]
            
        else:
            group_keys = ["customer_id"]
        
        # Group and aggregate - this will combine "app" and "unit" rows but keep Finastra accounts separate
        combined = mapped_df.groupby(group_keys, as_index=False).agg(agg_dict)[upload_cols]
        
        # Recalculate UnitsAsPerSubmission and IsInitialSubmission from the original grouped data
        # to avoid double-counting when combining "app" and "unit" rows
        if "customer_id" in combined.columns:
            for idx, row in combined.iterrows():
                customer_id = row["customer_id"]
                differentiator = row.get("differentiator", "")
                
                # Find matching rows in mapped_df
                if differentiator:
                    matching_mask = (mapped_df["customer_id"] == customer_id) & (mapped_df["differentiator"] == differentiator)
                else:
                    matching_mask = mapped_df["customer_id"] == customer_id
                
                matching_rows = mapped_df[matching_mask]
                
                # Get the values from the first row (they should all be the same since they're already aggregated)
                if len(matching_rows) > 0:
                    first_row = matching_rows.iloc[0]
                    combined.loc[idx, "UnitsAsPerSubmission"] = first_row.get("UnitsAsPerSubmission", 0)
                    combined.loc[idx, "IsInitialSubmission"] = first_row.get("IsInitialSubmission", 0)
        
        # Set value based on event_type_name:
        # - "app" events use IsInitialSubmission
        # - "unit" events use UnitsAsPerSubmission
        if "event_type_name" in combined.columns and "IsInitialSubmission" in combined.columns and "UnitsAsPerSubmission" in combined.columns:
            # Convert to numeric
            combined["IsInitialSubmission"] = pd.to_numeric(combined["IsInitialSubmission"], errors="coerce").fillna(0)
            combined["UnitsAsPerSubmission"] = pd.to_numeric(combined["UnitsAsPerSubmission"], errors="coerce").fillna(0)
            
            # Set value based on event type
            app_mask = combined["event_type_name"].str.lower().isin(["app", "lbpa app"])
            unit_mask = combined["event_type_name"].str.lower().isin(["unit", "lbpa unit"])
            
            combined.loc[app_mask, "value"] = combined.loc[app_mask, "IsInitialSubmission"]
            combined.loc[unit_mask, "value"] = combined.loc[unit_mask, "UnitsAsPerSubmission"]
    else:
        combined = pd.DataFrame(columns=upload_cols)
    unmapped_output = unmapped_df[upload_cols] if len(unmapped_df) > 0 else pd.DataFrame(columns=upload_cols)
    
    
    # Create CSV for rows with missing customer_id
    missing_customer_id_output = missing_customer_id_df[upload_cols] if len(missing_customer_id_df) > 0 else pd.DataFrame(columns=upload_cols)
    
    missing_customer_id_csv_bytes = missing_customer_id_output.to_csv(index=False).encode("utf-8") if len(missing_customer_id_output) > 0 else b""

    # Prepare in-memory CSV bytes
    combined_csv_bytes = combined.to_csv(index=False).encode("utf-8")
    combined_internal_csv_bytes = combined_internal.to_csv(index=False).encode("utf-8")
    unmapped_csv_bytes = unmapped_output.to_csv(index=False).encode("utf-8") if len(unmapped_output) > 0 else b""

    # Store in session_state for later tabs/downloads
    st.session_state["generated_files"]["usage_combined"] = {
        "name": "LoanLogics_upload_All.csv",
        "bytes": combined_csv_bytes,
    }
    st.session_state["generated_files"]["usage_internal"] = {
        "name": "LoanLogics_upload_All_internal.csv",
        "bytes": combined_internal_csv_bytes,
    }
    if len(unmapped_output) > 0:
        st.session_state["generated_files"]["usage_unmapped"] = {
            "name": "LoanLogics_upload_Unmapped.csv",
            "bytes": unmapped_csv_bytes,
        }
        st.session_state["unmapped_count"] = len(unmapped_output)
        # Store unmapped DataFrame for preview
        st.session_state["unmapped_preview_df"] = unmapped_output.copy()
    else:
        # Clear unmapped files if no unmapped rows
        if "usage_unmapped" in st.session_state.get("generated_files", {}):
            del st.session_state["generated_files"]["usage_unmapped"]
        st.session_state["unmapped_count"] = 0
        if "unmapped_preview_df" in st.session_state:
            del st.session_state["unmapped_preview_df"]
    
    # Store missing customer_id CSV
    if len(missing_customer_id_output) > 0:
        st.session_state["generated_files"]["usage_missing_customer_id"] = {
            "name": "LoanLogics_upload_Missing_Customer_ID.csv",
            "bytes": missing_customer_id_csv_bytes,
        }
        st.session_state["missing_customer_id_count"] = len(missing_customer_id_output)
        # Store missing customer_id DataFrame for preview
        st.session_state["missing_customer_id_preview_df"] = missing_customer_id_output.copy()
    else:
        # Clear missing customer_id files if no missing rows
        if "usage_missing_customer_id" in st.session_state.get("generated_files", {}):
            del st.session_state["generated_files"]["usage_missing_customer_id"]
        st.session_state["missing_customer_id_count"] = 0
        if "missing_customer_id_preview_df" in st.session_state:
            del st.session_state["missing_customer_id_preview_df"]

    # Store original dataframes for later split CSV generation with all columns
    st.session_state["original_income_df"] = income_df.copy()
    st.session_state["original_lbpa_df"] = lbpa_df.copy()

    return income_upload, lbpa_df, combined_csv_bytes, combined_internal_csv_bytes


def generate_split_csvs_with_all_columns(income_df, lbpa_df, usage_df):
    """Generate split CSVs with all original columns from Income and LBPA files, grouped by customer_id.
    Uses the Usage CSV (which has customer_id) to join back to original dataframes."""
    
    # Extract customer_id mapping from usage_df
    # The usage_df has CustomerName (not AccountName) and customer_id columns
    if usage_df is None or len(usage_df) == 0:
        return []
    
    # Check what column name is used in usage_df for the customer name
    usage_name_col = None
    if "CustomerName" in usage_df.columns:
        usage_name_col = "CustomerName"
    elif "AccountName" in usage_df.columns:
        usage_name_col = "AccountName"
    
    # Get mappings from usage_df - account_id → customer_id only
    account_id_to_customer_id = {}
    # Also create reverse mapping: customer_id -> CustomerName (for filename)
    customer_id_to_name = {}
    
    if "account_id" in usage_df.columns and "customer_id" in usage_df.columns:
        # Create account_id -> customer_id mapping (like reference code uses UUID)
        usage_acct_mapping = usage_df[["account_id", "customer_id"]].drop_duplicates()
        usage_acct_mapping["__acct_key__"] = usage_acct_mapping["account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True)
        account_id_to_customer_id = dict(zip(usage_acct_mapping["__acct_key__"], usage_acct_mapping["customer_id"]))
    
    if usage_name_col and "customer_id" in usage_df.columns:
        # Reverse mapping for filename generation only (not for matching)
        usage_mapping = usage_df[[usage_name_col, "customer_id"]].drop_duplicates()
        customer_id_to_name = dict(zip(usage_mapping["customer_id"].astype(str), usage_mapping[usage_name_col].astype(str)))
    
    def add_customer_id_from_usage(df, df_name):
        """Add customer_id to dataframe by joining with usage_df mapping via account_id only.
        This function does not create new customer mappings - it only uses account_id → customer_id from usage_df."""
        df = df.copy()
        df.columns = df.columns.str.strip()
        
        # Find account_id column in original Income/LBPA file
        acct_id_col = find_column(df, ["accountid", "acct#", "acct", "account number", "accountnumber"])
        
        # Only use account_id matching (usage_df is the single source of truth)
        if acct_id_col and account_id_to_customer_id:
            df["__acct_key__"] = df[acct_id_col].astype(str).str.replace(r"[^0-9]", "", regex=True)
            df["customer_id"] = df["__acct_key__"].map(account_id_to_customer_id)
        else:
            df["customer_id"] = None
        
        return df
    
    # Add customer_id to both dataframes using the usage mapping
    income_with_id = add_customer_id_from_usage(income_df, "income")
    lbpa_with_id = add_customer_id_from_usage(lbpa_df, "lbpa")
    
    # Combine both dataframes
    combined_all = pd.concat([income_with_id, lbpa_with_id], ignore_index=True)
    
    # Generate split CSVs grouped by customer_id
    results = []
    if len(combined_all) == 0:
        return results
    
    # Remove helper columns before generating CSVs (drop all columns starting with "__" except __original_account_name__ which we preserve)
    helper_columns = [col for col in combined_all.columns if col.startswith("__") and col != "__original_account_name__"]
    columns_to_keep = [col for col in combined_all.columns if col not in helper_columns]
    
    # Rename __original_account_name__ to original_account_name to preserve it
    if "__original_account_name__" in combined_all.columns:
        combined_all = combined_all.rename(columns={"__original_account_name__": "original_account_name"})
        if "original_account_name" not in columns_to_keep:
            columns_to_keep.append("original_account_name")
    # Ensure customer_id is included
    if "customer_id" not in columns_to_keep:
        columns_to_keep.append("customer_id")
    
    for customer_id, group in combined_all.groupby("customer_id"):
        group = group.sort_values("SubmissionDate" if "SubmissionDate" in group.columns else group.columns[0])
        if pd.isna(customer_id) or str(customer_id).strip() == "":
            continue
        
        # Create one file per customer (no splitting)
        # Get customer name for filename
        customer_name = customer_id_to_name.get(str(customer_id), "Unknown")
        # Clean customer name for filesystem: remove special chars, replace spaces with underscores
        safe_name = re.sub(r'[<>:"/\\|?*]', '', customer_name)  # Remove invalid filename chars
        safe_name = re.sub(r'\s+', '_', safe_name.strip())  # Replace spaces with underscores
        safe_name = safe_name[:50] if len(safe_name) > 50 else safe_name  # Limit length
        
        filename = f"{safe_name}_{customer_id}.csv"
        # Only include original columns + customer_id (exclude helper columns)
        group_clean = group[[col for col in columns_to_keep if col in group.columns]]
        split_csv_bytes = group_clean.to_csv(index=False).encode("utf-8")
        results.append({"name": filename, "bytes": split_csv_bytes})
    return results

def generate_chunks(combined_df, max_rows_per_chunk=900):
    results = []
    for customer_id, group in combined_df.groupby("customer_id"):
        group = group.sort_values("datetime")
        if pd.isna(customer_id) or str(customer_id).strip() == "":
            continue
        chunks = [group[i:i + max_rows_per_chunk] for i in range(0, len(group), max_rows_per_chunk)]
        for idx, chunk in enumerate(chunks, start=1):
            suffix = f"_part{idx}" if len(chunks) > 1 else ""
            filename = f"tabs_upload_{customer_id}{suffix}.csv"
            chunk_bytes = chunk.to_csv(index=False).encode("utf-8")
            results.append({"name": filename, "bytes": chunk_bytes})
    return results


# --- Streamlit UI ---
st.set_page_config(page_title="LoanLogics Usage Automation", layout="wide")
st.title("LoanBeam Usage and Invoice Attachment Workflow")

usage_tab, chunk_tab = st.tabs(["Usage Transformation", "Invoice Attachment"])

tab_names = [
    "📁 Step 1: Upload CSV",
    "📄 Step 2: Generate PDFs", 
    "📋 Step 3: Create CSV Mapping",
    "🚀 Step 4: Bulk Upload"
]

with usage_tab:
    st.subheader("Generate Usage File")
    
    # Multi-file uploader
    uploaded_files_list = st.file_uploader("Upload CSV Files", type="csv", accept_multiple_files=True, key="all_files")
    
    # Store uploaded files in session state
    if uploaded_files_list:
        # Initialize file assignments if not exists
        if "file_assignments" not in st.session_state:
            st.session_state["file_assignments"] = {}
        
        # Get list of file names
        file_names = [f.name for f in uploaded_files_list]
        
        # File type selectors
        st.markdown("**Assign file types:**")
        col1, col2, col3 = st.columns(3)
        
        with col1:
            income_file_name = st.selectbox(
                "Income File",
                options=[""] + file_names,
                key="income_file_selector",
                help="Select which file contains Income transaction data"
            )
        
        with col2:
            lbpa_file_name = st.selectbox(
                "LBPA File",
                options=[""] + file_names,
                key="lbpa_file_selector",
                help="Select which file contains LBPA transaction data"
            )
        
        with col3:
            customer_file_name = st.selectbox(
                "Customer File",
                options=[""] + file_names,
                key="customer_file_selector",
                help="Select which file contains customer mapping data (must have 'ID' and 'Customer Number' columns)"
            )
        
        # Store assignments
        if income_file_name:
            st.session_state["file_assignments"]["income"] = income_file_name
        if lbpa_file_name:
            st.session_state["file_assignments"]["lbpa"] = lbpa_file_name
        if customer_file_name:
            st.session_state["file_assignments"]["customer"] = customer_file_name
        
        # Find and persist the selected files
        for uploaded_file in uploaded_files_list:
            if income_file_name and uploaded_file.name == income_file_name:
                persist_upload(uploaded_file, "income")
            if lbpa_file_name and uploaded_file.name == lbpa_file_name:
                persist_upload(uploaded_file, "lbpa")
            if customer_file_name and uploaded_file.name == customer_file_name:
                persist_upload(uploaded_file, "customer")
    
    # Show previews
    uploaded_files = st.session_state.get("uploaded_files", {})
    
    if uploaded_files.get("income", {}).get("bytes"):
        st.markdown("**Income File Preview:**")
        with st.expander("Preview Income File", expanded=False):
            try:
                income_df = pd.read_csv(BytesIO(uploaded_files["income"]["bytes"]))
                st.caption(f"Rows: {len(income_df):,} | Columns: {len(income_df.columns)}")
                st.dataframe(income_df, use_container_width=True)
            except Exception as e:
                st.error(f"Could not preview Income file: {e}")
    
    if uploaded_files.get("lbpa", {}).get("bytes"):
        st.markdown("**LBPA File Preview:**")
        with st.expander("Preview LBPA File", expanded=False):
            try:
                lbpa_df = pd.read_csv(BytesIO(uploaded_files["lbpa"]["bytes"]))
                st.caption(f"Rows: {len(lbpa_df):,} | Columns: {len(lbpa_df.columns)}")
                st.dataframe(lbpa_df, use_container_width=True)
            except Exception as e:
                st.error(f"Could not preview LBPA file: {e}")
    
    if uploaded_files.get("customer", {}).get("bytes"):
        st.markdown("**Customer File Preview:**")
        with st.expander("Preview Customer File", expanded=False):
            try:
                customer_df = pd.read_csv(BytesIO(uploaded_files["customer"]["bytes"]))
                st.caption(f"Rows: {len(customer_df):,} | Columns: {len(customer_df.columns)}")
                st.dataframe(customer_df, use_container_width=True)
                
                # Check if required columns exist
                required_cols = ["ID", "Customer Number"]
                missing_cols = [col for col in required_cols if col not in customer_df.columns]
                if missing_cols:
                    st.warning(f"⚠️ Missing required columns: {', '.join(missing_cols)}")
                else:
                    # Show mapping stats
                    valid_mappings = customer_df[["ID", "Customer Number"]].dropna()
                    valid_mappings = valid_mappings[valid_mappings["Customer Number"].astype(str).str.strip() != ""]
            except Exception as e:
                st.error(f"Could not preview Customer file: {e}")

    # Client mappings are automatically loaded from disk at startup
    current_mappings = st.session_state.get("client_mappings", {})
    if not current_mappings:
        # Try to load from disk if not in session state
        disk_mappings = _load_client_mappings_from_disk()
        if disk_mappings:
            st.session_state["client_mappings"] = disk_mappings
            current_mappings = disk_mappings

    # Split invoice configuration
    st.markdown("---")
    enable_split_invoices = st.checkbox("Enable Split Invoices")
    
    split_customers = []
    if enable_split_invoices:
        # Get unique customer names from the customer file for selection
        if uploaded_files.get("customer", {}).get("bytes"):
            try:
                customer_df = pd.read_csv(BytesIO(uploaded_files["customer"]["bytes"]))
                name_col = "Name" if "Name" in customer_df.columns else None
                if name_col:
                    unique_customer_names = customer_df[name_col].dropna().unique()
                    unique_customer_names = [str(name).strip() for name in unique_customer_names if str(name).strip() and str(name).lower() != 'nan']
                    
                    if len(unique_customer_names) > 0:
                        split_customers = st.multiselect(
                            "Select customers for split invoices",
                            options=unique_customer_names,
                            help="Select which customers should have split invoice numbering applied"
                        )
                    else:
                        st.warning("No valid customer names found in customer file")
                else:
                    st.warning("Customer Name column not found in customer file")
            except Exception as e:
                st.warning(f"Could not load customer names for selection: {e}")

    # Usage date picker
    st.markdown("---")
    usage_date = st.date_input(
        "Select Usage Date",
        value=None,
        help="This date will be used to populate the datetime column for all usage records"
    )
    
    # API Key input for event type validation
    st.markdown("---")
    api_key_input = st.text_input(
        "API Key (For event type validation)",
        type="password",
        help="Enter your Tabs Platform API key to automatically validate and correct event type names against the API",
        value=API_KEY if API_KEY else ""
    )
    # Use provided API key or fall back to environment/secrets
    event_validation_api_key = api_key_input if api_key_input else API_KEY
    
    # Store API key in session state so get_api_key() can find it
    if event_validation_api_key:
        st.session_state["ui_api_key_usage"] = event_validation_api_key

    if st.button("Generate Usage CSV"):
        up = st.session_state.get("uploaded_files", {})
        missing = []
        if not up.get("income", {}).get("bytes"):
            missing.append("Income")
        if not up.get("lbpa", {}).get("bytes"):
            missing.append("LBPA")
        if not up.get("customer", {}).get("bytes"):
            missing.append("Customer File")

        if not missing:
            if not usage_date:
                st.error("Please select a usage date")
            else:
                with st.spinner("Running transformation..."):
                    # Use stored mappings if available
                    stored_mappings = st.session_state.get("client_mappings")
                    # Get customer file (required)
                    uploaded_customer = BytesIO(up["customer"]["bytes"])
                    
                    income_df, lbpa_df, combined_csv, combined_internal_csv = transform_usage(
                        BytesIO(up["income"]["bytes"]),
                        BytesIO(up["lbpa"]["bytes"]),
                        uploaded_customer,
                        uploaded_clients=None,
                        resolve_now=True,  # Always enabled - AccountIDs will be set if API key is provided
                        usage_date=usage_date,
                        mappings=stored_mappings if stored_mappings else None,
                        split_customers=split_customers if enable_split_invoices else [],
                        api_key=event_validation_api_key,
                    )
                st.session_state["show_usage_download"] = True
        else:
            st.error(f"Missing: {', '.join(missing)}")

    if st.session_state.get("show_usage_download") and st.session_state.get("generated_files", {}).get("usage_combined"):
        st.write()
        st.subheader("Generated Usage CSV")
        
        # Show preview of generated Usage CSV
        with st.expander("Preview", expanded=False):
            try:
                usage_csv_bytes = st.session_state["generated_files"]["usage_combined"]["bytes"]
                usage_df = pd.read_csv(BytesIO(usage_csv_bytes))
                st.caption(f"Rows: {len(usage_df):,} | Columns: {len(usage_df.columns)}")
                st.dataframe(usage_df, use_container_width=True)
            except Exception as e:
                st.error(f"Could not preview Usage CSV: {e}")
        
        # Show missing customer_id CSV download if any missing customer_id rows exist
        if st.session_state.get("generated_files", {}).get("usage_missing_customer_id"):
            missing_customer_id_count = st.session_state.get("missing_customer_id_count", 0)
            st.warning(f"⚠️ {missing_customer_id_count} rows have missing customer_id and have been separated into a separate CSV file.")
            
            # Show missing customer_id preview under the warning message
            if st.session_state.get("missing_customer_id_preview_df") is not None:
                missing_customer_id_df = st.session_state["missing_customer_id_preview_df"]
                
                with st.expander("Missing Customer ID Rows Preview", expanded=False):
                    st.caption(f"Rows: {len(missing_customer_id_df):,} | Columns: {len(missing_customer_id_df.columns)}")
                    st.dataframe(missing_customer_id_df, use_container_width=True)
        
        # Show unmapped CSV download if any unmapped rows exist (subset of missing customer_id)
        if st.session_state.get("generated_files", {}).get("usage_unmapped"):
            unmapped_count = st.session_state.get("unmapped_count", 0)
            st.warning(f"⚠️ {unmapped_count} rows could not be mapped to customers (unmapped account IDs) and have been separated into a separate CSV file.")
            
            # Show unmapped preview under the warning message
            if st.session_state.get("unmapped_preview_df") is not None:
                with st.expander("⚠️ Unmapped Rows Preview", expanded=False):
                    unmapped_df = st.session_state["unmapped_preview_df"]
                    st.caption(f"Rows: {len(unmapped_df):,} | Columns: {len(unmapped_df.columns)}")
                    st.dataframe(unmapped_df, use_container_width=True)
            
            st.download_button(
                "Download Unmapped Rows CSV",
                data=st.session_state["generated_files"]["usage_unmapped"]["bytes"],
                file_name=st.session_state["generated_files"]["usage_unmapped"]["name"],
                key="dl_unmapped_latest",
            )

with chunk_tab:
    st.subheader("Invoice Attachment Workflow")
    
    # Step selector
    invoice_steps = [
        "Step 1: Generate Split CSVs",
        "Step 2: Invoice Mapping",
        "Step 3: Bulk Upload"
    ]
    
    selected_step = st.radio(
        "Select Step:",
        options=invoice_steps,
        index=st.session_state.get("current_invoice_step", 0),
        horizontal=True,
        key="invoice_step_tabs"
    )
    
    st.session_state["current_invoice_step"] = invoice_steps.index(selected_step)
    current_step = st.session_state["current_invoice_step"]
    
    if current_step == 0:  # Step 1: Generate Split CSVs
        st.subheader("Generate Split CSVs")
        
        st.info("This step creates one CSV file per customer from the original Income and LBPA data uploaded in the 'Usage Transformation' tab. Each CSV contains all original columns and is grouped by customer ID.")
        
        # Check if Usage CSV was generated first
        if not st.session_state.get("show_usage_download"):
            st.warning("⚠️ Please generate Usage CSV in the 'Usage Transformation' tab first. This will ensure the original Income and LBPA data is available for creating split CSVs with all columns.")
        else:
            # Get the generated files
            generated_files = st.session_state.get("generated_files", {})
            
            if not generated_files.get("usage_combined"):
                st.warning("⚠️ Usage CSV not found. Please generate Usage CSV in the 'Usage Transformation' tab first.")
            else:
                # Get original Income and LBPA data from session state
                uploaded_files_check = st.session_state.get("uploaded_files", {})
                
                if not uploaded_files_check.get("income", {}).get("bytes") or not uploaded_files_check.get("lbpa", {}).get("bytes"):
                    st.warning("⚠️ Original Income/LBPA data not found. Please generate Usage CSV in the 'Usage Transformation' tab first.")
                else:
                    # Read the generated usage CSV to get customer mappings
                    usage_csv_bytes = generated_files["usage_combined"]["bytes"]
                    usage_df = pd.read_csv(BytesIO(usage_csv_bytes))
                    
                    # Read original Income and LBPA files
                    income_df = pd.read_csv(BytesIO(uploaded_files_check["income"]["bytes"]))
                    lbpa_df = pd.read_csv(BytesIO(uploaded_files_check["lbpa"]["bytes"]))
                    
                    # Show download buttons if split CSVs already exist
                    if st.session_state.get("split_csvs_ready"):
                        split_csvs = st.session_state.get("invoice_split_csvs", [])
                        col_header1, col_header2 = st.columns([3, 1])
                        with col_header1:
                            st.success(f"✅ {len(split_csvs)} split CSV files ready")
                        with col_header2:
                            if st.button("🔄 Regenerate", key="regenerate_split_csvs", help="Regenerate all split CSVs"):
                                st.session_state["split_csvs_ready"] = False
                                st.session_state["invoice_split_csvs"] = []
                                st.rerun()
                        
                        # Show summary
                        summary_data = []
                        for split_csv in split_csvs:
                            csv_df = pd.read_csv(BytesIO(split_csv["bytes"]))
                            summary_data.append({
                                "Filename": split_csv["name"],
                                "Rows": len(csv_df),
                                "Customer ID": csv_df["customer_id"].iloc[0] if "customer_id" in csv_df.columns and len(csv_df) > 0 else "N/A"
                            })
                        
                        summary_df = pd.DataFrame(summary_data)
                        st.dataframe(summary_df, use_container_width=True)
                        
                        # Add search and download all functionality
                        st.markdown("---")
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            search_term = st.text_input("Search split CSVs by filename or customer ID", 
                                                       placeholder="Type to filter...", 
                                                       key="search_split_csvs",
                                                       label_visibility="visible")
                        with col2:
                            # Download all as ZIP
                            import zipfile
                            zip_buffer = BytesIO()
                            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                                for split_csv in split_csvs:
                                    zip_file.writestr(split_csv["name"], split_csv["bytes"])
                            zip_buffer.seek(0)
                            st.write("")  # Add spacing to align with input field
                            st.download_button(
                                "Download All as ZIP",
                                data=zip_buffer.getvalue(),
                                file_name="all_split_csvs.zip",
                                mime="application/zip",
                                key="dl_all_split_csvs_zip",
                                use_container_width=True
                            )
                        
                        # Filter split CSVs based on search
                        filtered_csvs = split_csvs
                        if search_term:
                            search_lower = search_term.lower()
                            filtered_csvs = [
                                csv for csv in split_csvs 
                                if search_lower in csv["name"].lower() or 
                                search_lower in str(summary_df[summary_df["Filename"] == csv["name"]]["Customer ID"].iloc[0] if len(summary_df[summary_df["Filename"] == csv["name"]]) > 0 else "").lower()
                            ]
                        
                        # Add individual download buttons for each split CSV
                        st.subheader(f"Download Individual Split CSVs ({len(filtered_csvs)} of {len(split_csvs)})")
                        if len(filtered_csvs) == 0:
                            st.info("No files match your search.")
                        else:
                            for i, split_csv in enumerate(filtered_csvs):
                                col1, col2 = st.columns([3, 1])
                                with col1:
                                    st.write(f"{i+1}. {split_csv['name']}")
                                with col2:
                                    st.download_button(
                                        "Download",
                                        data=split_csv["bytes"],
                                        file_name=split_csv["name"],
                                        mime="text/csv",
                                        key=f"dl_split_csv_existing_{i}"
                                    )
                    
                    # Only show Generate button if split CSVs don't exist yet
                    if not st.session_state.get("split_csvs_ready"):
                        st.markdown("---")
                        if st.button("Generate Split CSVs", type="primary"):
                            with st.spinner("Generating split CSVs..."):
                                split_csvs = generate_split_csvs_with_all_columns(income_df, lbpa_df, usage_df)
                            
                            if split_csvs:
                                st.session_state["invoice_split_csvs"] = split_csvs
                                st.session_state["split_csvs_ready"] = True
                                
                                # Show summary
                                summary_data = []
                                for split_csv in split_csvs:
                                    csv_df = pd.read_csv(BytesIO(split_csv["bytes"]))
                                    summary_data.append({
                                        "Filename": split_csv["name"],
                                        "Rows": len(csv_df),
                                        "Customer ID": csv_df["customer_id"].iloc[0] if "customer_id" in csv_df.columns and len(csv_df) > 0 else "N/A"
                                    })
                                
                                summary_df = pd.DataFrame(summary_data)
                                st.success(f"✅ Generated {len(split_csvs)} split CSV files")
                                st.dataframe(summary_df, use_container_width=True)
                                
                                # Add search and download all functionality
                                st.markdown("---")
                                col1, col2 = st.columns([3, 1])
                                with col1:
                                    search_term_new = st.text_input("Search split CSVs by filename or customer ID", 
                                                                   placeholder="Type to filter...", 
                                                                   key="search_split_csvs_new",
                                                                   label_visibility="visible")
                                with col2:
                                    # Download all as ZIP
                                    import zipfile
                                    zip_buffer = BytesIO()
                                    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                                        for split_csv in split_csvs:
                                            zip_file.writestr(split_csv["name"], split_csv["bytes"])
                                    zip_buffer.seek(0)
                                    st.write("")  # Add spacing to align with input field
                                    st.download_button(
                                        "Download All as ZIP",
                                        data=zip_buffer.getvalue(),
                                        file_name="all_split_csvs.zip",
                                        mime="application/zip",
                                        key="dl_all_split_csvs_zip_new",
                                        use_container_width=True
                                    )
                                
                                # Filter split CSVs based on search
                                filtered_csvs_new = split_csvs
                                if search_term_new:
                                    search_lower = search_term_new.lower()
                                    filtered_csvs_new = [
                                        csv for csv in split_csvs 
                                        if search_lower in csv["name"].lower() or 
                                        search_lower in str(summary_df[summary_df["Filename"] == csv["name"]]["Customer ID"].iloc[0] if len(summary_df[summary_df["Filename"] == csv["name"]]) > 0 else "").lower()
                                    ]
                                
                                # Add individual download buttons for each split CSV
                                st.subheader(f"Download Individual Split CSVs ({len(filtered_csvs_new)} of {len(split_csvs)})")
                                if len(filtered_csvs_new) == 0:
                                    st.info("No files match your search.")
                                else:
                                    for i, split_csv in enumerate(filtered_csvs_new):
                                        col1, col2 = st.columns([3, 1])
                                        with col1:
                                            st.write(f"{i+1}. {split_csv['name']}")
                                        with col2:
                                            st.download_button(
                                                "Download",
                                                data=split_csv["bytes"],
                                                file_name=split_csv["name"],
                                                mime="text/csv",
                                                key=f"dl_split_csv_{i}"
                                            )
                            else:
                                st.warning("⚠️ No split CSVs created. Check that customer IDs are properly mapped.")
    
    elif current_step == 1:  # Step 2: Invoice Mapping
        st.subheader("Invoice Mapping")
        
        st.info("Map each split CSV to the correct invoice using the Tabs API. Enter your API key and select the invoice issue date.")
        
        # Check if split CSVs are ready
        if not st.session_state.get("split_csvs_ready"):
            st.warning("⚠️ Please complete Step 1 first (Generate Split CSVs)")
        else:
            # API Key input
            api_key_attach = st.text_input("Tabs API Key", type="password", key="ui_api_key_attach", 
                                         value=st.session_state.get("invoice_api_key", ""),
                                         help="Enter your Tabs API key for invoice mapping")
            
            if api_key_attach:
                st.session_state["invoice_api_key"] = api_key_attach
            
            # Smart caching system for API invoices
            st.markdown("---")
            st.subheader("📋 Invoice Cache Management")
            
            # Check if we have cached invoices (with better persistence)
            cache_key = f"invoice_cache_{api_key_attach[:10]}"
            
            # Try to get from session state first
            cached_invoices = st.session_state.get(cache_key, [])
            cache_timestamp = st.session_state.get(f"{cache_key}_timestamp", None)
            
            # If no cache in session state, try to load from file
            if not cached_invoices:
                try:
                    import json
                    cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_key_attach[:10]}.json")
                    os.makedirs(_CACHE_DIR, exist_ok=True)
                    if os.path.exists(cache_file):
                        with open(cache_file, 'r') as f:
                            cache_data = json.load(f)
                            cached_invoices = cache_data.get('invoices', [])
                            cache_timestamp_str = cache_data.get('timestamp')
                            if cache_timestamp_str:
                                cache_timestamp = datetime.fromisoformat(cache_timestamp_str)
                        
                        # Restore to session state
                        st.session_state[cache_key] = cached_invoices
                        st.session_state[f"{cache_key}_timestamp"] = cache_timestamp
                        st.success(f"✅ Loaded {len(cached_invoices)} invoices from persistent cache")
                except Exception as e:
                    st.warning(f"Could not load persistent cache: {e}")
                    cached_invoices = []
                    cache_timestamp = None
            
            col1, col2, col3 = st.columns([2, 1, 1])
            
            with col1:
                if cached_invoices:
                    cache_age = datetime.now() - cache_timestamp if cache_timestamp else None
                    if cache_age:
                        age_hours = cache_age.total_seconds() / 3600
                        st.success(f"✅ Cache: {len(cached_invoices)} invoices cached ({age_hours:.1f} hours ago)")
                    else:
                        st.success(f"✅ Cache: {len(cached_invoices)} invoices cached")
                else:
                    st.warning("⚠️ No invoice cache found")
                    st.info("💡 Click 'Refresh Cache' to fetch all invoices from API (one-time setup)")
            
            with col2:
                if st.button("🔄 Refresh Cache", help="Fetch fresh invoices from API"):
                    if not api_key_attach:
                        st.error("⚠️ Please enter API key first")
                    else:
                        with st.spinner("Fetching all invoices from API (this may take a few minutes)..."):
                            all_invoices = fetch_all_invoices_for_cache(api_key_attach)
                            if all_invoices:
                                # Save to session state
                                st.session_state[cache_key] = all_invoices
                                st.session_state[f"{cache_key}_timestamp"] = datetime.now()
                                
                                # Also save to file for persistence
                                try:
                                    import json
                                    cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_key_attach[:10]}.json")
                                    os.makedirs(_CACHE_DIR, exist_ok=True)
                                    cache_data = {
                                        'invoices': all_invoices,
                                        'timestamp': datetime.now().isoformat(),
                                        'count': len(all_invoices)
                                    }
                                    with open(cache_file, 'w') as f:
                                        json.dump(cache_data, f)
                                    st.success(f"✅ Cached {len(all_invoices)} invoices successfully! (Saved to file)")
                                except Exception as e:
                                    st.success(f"✅ Cached {len(all_invoices)} invoices successfully! (File save failed: {e})")
                                
                                st.rerun()
                            else:
                                st.error("❌ Failed to fetch invoices")
            
            with col3:
                if st.button("🗑️ Clear Cache", help="Clear cached invoices"):
                    # Clear from session state
                    if cache_key in st.session_state:
                        del st.session_state[cache_key]
                    if f"{cache_key}_timestamp" in st.session_state:
                        del st.session_state[f"{cache_key}_timestamp"]
                    
                    # Also clear from file
                    try:
                        cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_key_attach[:10]}.json")
                        os.makedirs(_CACHE_DIR, exist_ok=True)
                        if os.path.exists(cache_file):
                            os.remove(cache_file)
                        st.success("✅ Cache cleared! (Both memory and file)")
                    except Exception as e:
                        st.success(f"✅ Cache cleared! (File removal failed: {e})")
                    
                    st.rerun()
            
            # Show cache recommendations
            if cached_invoices and cache_timestamp:
                cache_age = datetime.now() - cache_timestamp
                age_hours = cache_age.total_seconds() / 3600
                if age_hours > 24:
                    st.warning("⚠️ Cache is older than 24 hours. Consider refreshing for new invoices.")
                elif age_hours > 6:
                    st.info("ℹ️ Cache is older than 6 hours. New invoices may not be included.")
                else:
                    st.info("✅ Cache is fresh and up-to-date.")
            
            st.markdown("---")
            
            # Option to upload mapping CSV (takes priority over generated mapping)
            uploaded_mapping = st.file_uploader("Upload Invoice Mapping CSV (optional)", type=["csv"], key="upload_mapping_csv")
            
            if uploaded_mapping is not None:
                # User uploaded a CSV - use it instead of generating mapping
                try:
                    mapping_df = pd.read_csv(uploaded_mapping)
                    required_cols = ["split_csv_filename", "customer_id", "invoice_id"]
                    if all(col in mapping_df.columns for col in required_cols):
                        st.session_state["invoice_mapping"] = mapping_df
                        st.session_state["invoice_mapping_ready"] = True
                        st.success(f"✅ Loaded {len(mapping_df)} mappings from uploaded CSV")
                        st.dataframe(mapping_df, use_container_width=True)
                    else:
                        st.error(f"CSV must have columns: {', '.join(required_cols)}")
                except Exception as e:
                    st.error(f"Error reading CSV: {str(e)}")
            else:
                # No file uploaded - generate mapping via API
                st.markdown("---")
                
                # Invoice issue date selector
                issue_date = st.date_input(
                    "Select Invoice Issue Date",
                    value=datetime.now().date(),
                    help="Select the issue date for the invoices you want to map to"
                )
                
                if st.button("Map Invoices to Split CSVs", type="primary"):
                    if not api_key_attach:
                        st.error("⚠️ Please enter API key first")
                    elif not st.session_state.get("split_csvs_ready"):
                        st.error("⚠️ No split CSVs found. Please complete Step 1 first.")
                    else:
                        split_csvs = st.session_state.get("invoice_split_csvs", [])
                        with st.spinner("Mapping invoices..."):
                            mapping_results = []
                            
                            for split_csv in split_csvs:
                                csv_df = pd.read_csv(BytesIO(split_csv["bytes"]))
                                customer_id = None
                                
                                # Try to get customer_id from the CSV
                                if "customer_id" in csv_df.columns:
                                    customer_ids = csv_df["customer_id"].dropna().unique()
                                    # Validate: each split CSV must contain exactly one unique customer_id
                                    if len(customer_ids) == 0:
                                        mapping_results.append({
                                            "split_csv_filename": split_csv["name"],
                                            "customer_id": "N/A",
                                            "invoice_id": "Not Found",
                                            "status": "Failed",
                                            "reason": "No customer ID in CSV"
                                        })
                                        continue
                                    elif len(customer_ids) > 1:
                                        mapping_results.append({
                                            "split_csv_filename": split_csv["name"],
                                            "customer_id": f"Multiple: {', '.join(map(str, customer_ids[:3]))}",
                                            "invoice_id": "Not Found",
                                            "status": "Failed",
                                            "reason": f"CSV contains {len(customer_ids)} unique customer IDs (must be exactly 1)"
                                        })
                                        continue
                                    else:
                                        customer_id = str(customer_ids[0]).strip()
                                
                                if not customer_id:
                                    mapping_results.append({
                                        "split_csv_filename": split_csv["name"],
                                        "customer_id": "N/A",
                                        "invoice_id": "Not Found",
                                        "status": "Failed",
                                        "reason": "No customer ID in CSV"
                                    })
                                    continue
                                
                                # Look up invoice for this customer
                                invoice_id = find_invoice_by_date(customer_id, issue_date, api_key_attach)
                                
                                if invoice_id:
                                    mapping_results.append({
                                        "split_csv_filename": split_csv["name"],
                                        "customer_id": customer_id,
                                        "invoice_id": invoice_id,
                                        "status": "Success"
                                    })
                                else:
                                    mapping_results.append({
                                        "split_csv_filename": split_csv["name"],
                                        "customer_id": customer_id,
                                        "invoice_id": "Not Found",
                                        "status": "Failed",
                                        "reason": "No matching invoice found"
                                    })
                            
                            mapping_df = pd.DataFrame(mapping_results)
                            st.session_state["invoice_mapping"] = mapping_df
                            st.session_state["invoice_mapping_ready"] = True
                            
                            # Show results
                            success_count = (mapping_df["status"] == "Success").sum()
                            st.success(f"✅ Mapped {success_count}/{len(mapping_df)} split CSVs to invoices")
                            
                            # Show successful mappings
                            successful_mappings = mapping_df[mapping_df["status"] == "Success"]
                            if len(successful_mappings) > 0:
                                st.dataframe(successful_mappings[["split_csv_filename", "customer_id", "invoice_id"]], use_container_width=True)
                            
                            # Show unmapped files
                            unmapped = mapping_df[mapping_df["status"] == "Failed"]
                            if len(unmapped) > 0:
                                st.warning(f"⚠️ {len(unmapped)} Split CSVs Requiring Attention")
                                st.dataframe(unmapped, use_container_width=True)
    
    elif current_step == 2:  # Step 3: Bulk Upload
        st.subheader("Bulk Upload CSV Attachments")
        
        st.info("Upload all mapped CSV files as attachments to their corresponding invoices.")
        
        # Check if mapping is ready
        mapping_ready = False
        mapping_df = None
        
        # Use mapping from Step 2
        if not st.session_state.get("invoice_mapping_ready"):
            st.warning("⚠️ Please complete Step 2 first (Invoice Mapping)")
            mapping_ready = False
        else:
            mapping_df = st.session_state.get("invoice_mapping")
            mapping_ready = True
        
        if mapping_ready and mapping_df is not None:
            if not st.session_state.get("split_csvs_ready"):
                st.warning("⚠️ Split CSVs not found. Please complete Step 1 first (Generate Split CSVs)")
            else:
                split_csvs = st.session_state.get("invoice_split_csvs", [])
                # Filter to only include successful mappings (status == "Success" and invoice_id != "Not Found")
                successful_mappings = mapping_df[
                    (mapping_df["status"] == "Success") & 
                    (mapping_df["invoice_id"] != "Not Found") &
                    (mapping_df["invoice_id"].notna())
                ].copy()
                
                failed_count = len(mapping_df) - len(successful_mappings)
                if failed_count > 0:
                    st.warning(f"⚠️ {failed_count} split CSV(s) were not mapped to invoices and will be excluded from upload")
                
                st.info(f"📋 Ready to upload {len(successful_mappings)} split CSVs to invoices")
                
                # Create lookup dict for split CSVs
                split_csvs_dict = {split_csv["name"]: split_csv for split_csv in split_csvs}
            
                # Show preview (only successful mappings)
                st.subheader("Upload Preview")
                preview_df = successful_mappings.copy()
                preview_df["split_csv_size"] = preview_df["split_csv_filename"].map(
                    lambda x: len(pd.read_csv(BytesIO(split_csvs_dict[x]["bytes"]))) if x in split_csvs_dict else 0
                )
                preview_df["split_csv_exists"] = preview_df["split_csv_filename"].map(
                    lambda x: "Yes" if x in split_csvs_dict else "No"
                )
                st.dataframe(preview_df, use_container_width=True)
                
                # Check for missing split CSVs
                missing_csvs = preview_df[preview_df["split_csv_exists"] == "No"]["split_csv_filename"].tolist()
                if missing_csvs:
                    st.warning(f"⚠️ {len(missing_csvs)} split CSV(s) not found: {', '.join(missing_csvs[:5])}{'...' if len(missing_csvs) > 5 else ''}")
                
                api_key = st.session_state.get('invoice_api_key', '')
                
                if not api_key:
                    st.warning("⚠️ Please enter API key in Step 2")
                else:
                    # Add test mode option
                    test_mode = st.checkbox("🧪 Test Mode: Upload only one row from the first split CSV", value=False)
                    
                    if st.button("Start Bulk Upload", type="primary"):
                        try:
                            with st.spinner("Uploading CSV attachments..."):
                                upload_results = []
                                progress_bar = st.progress(0)
                                
                                # Limit to first row if test mode is enabled (only use successful mappings)
                                rows_to_process = successful_mappings.head(1) if test_mode else successful_mappings
                                
                                for idx, row in rows_to_process.iterrows():
                                    split_csv_name = row["split_csv_filename"]
                                    customer_id = row["customer_id"]
                                    invoice_id = row["invoice_id"]
                                    
                                    if split_csv_name not in split_csvs_dict:
                                        upload_results.append({
                                            "split_csv": split_csv_name,
                                            "status": "Failed",
                                            "reason": "Split CSV not found"
                                        })
                                        continue
                                    
                                    split_csv_bytes = split_csvs_dict[split_csv_name]["bytes"]
                                    
                                    # Validate: each split CSV must contain exactly one unique customer_id
                                    try:
                                        validation_df = pd.read_csv(BytesIO(split_csv_bytes))
                                        if "customer_id" in validation_df.columns:
                                            unique_customer_ids = validation_df["customer_id"].dropna().unique()
                                            if len(unique_customer_ids) == 0:
                                                upload_results.append({
                                                    "split_csv": split_csv_name,
                                                    "status": "Failed",
                                                    "reason": "CSV contains no customer IDs"
                                                })
                                                continue
                                            elif len(unique_customer_ids) > 1:
                                                upload_results.append({
                                                    "split_csv": split_csv_name,
                                                    "status": "Failed",
                                                    "reason": f"CSV contains {len(unique_customer_ids)} unique customer IDs (must be exactly 1): {', '.join(map(str, unique_customer_ids[:3]))}"
                                                })
                                                continue
                                    except Exception as e:
                                        upload_results.append({
                                            "split_csv": split_csv_name,
                                            "status": "Failed",
                                            "reason": f"Error validating CSV: {str(e)}"
                                        })
                                        continue
                                    
                                    # In test mode, create a CSV with only the first row
                                    if test_mode:
                                        try:
                                            test_df = pd.read_csv(BytesIO(split_csv_bytes))
                                            if len(test_df) > 0:
                                                # Keep only the first row
                                                test_df = test_df.head(1)
                                                # Create new filename with "_test" suffix
                                                test_filename = split_csv_name.replace(".csv", "_test.csv")
                                                split_csv_bytes = test_df.to_csv(index=False).encode("utf-8")
                                                split_csv_name = test_filename
                                            else:
                                                upload_results.append({
                                                    "split_csv": split_csv_name,
                                                    "status": "Failed",
                                                    "reason": "CSV is empty"
                                                })
                                                continue
                                        except Exception as e:
                                            upload_results.append({
                                                "split_csv": split_csv_name,
                                                "status": "Failed",
                                                "reason": f"Error reading CSV: {str(e)}"
                                            })
                                            continue
                                    
                                    # Upload CSV as attachment to invoice
                                    success = upload_csv_attachment(
                                        customer_id,
                                        invoice_id,
                                        split_csv_bytes,
                                        split_csv_name,
                                        api_key
                                    )
                                    
                                    upload_results.append({
                                        "split_csv": split_csv_name,
                                        "customer_id": customer_id,
                                        "invoice_id": invoice_id,
                                        "status": "Success" if success else "Failed",
                                        "reason": "" if success else "Upload failed"
                                    })
                                    
                                    progress_bar.progress((idx + 1) / len(rows_to_process))
                                
                                results_df = pd.DataFrame(upload_results)
                                st.session_state["upload_results"] = results_df
                                
                                success_count = (results_df["status"] == "Success").sum()
                                progress_bar.empty()
                                
                                if test_mode:
                                    st.success(f"✅ Test upload complete! {success_count}/{len(results_df)} successful")
                                else:
                                    st.success(f"✅ Upload complete! {success_count}/{len(results_df)} successful")
                                
                        except Exception as e:
                            st.error(f"Error during bulk upload: {str(e)}")
                            import traceback
                            st.code(traceback.format_exc())
    
