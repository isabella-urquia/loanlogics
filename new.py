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
            st.success(f"✅ Successfully fetched {len(all_invoices)} invoices across {page} pages")
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
                cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_token[:10]}.json")
                if os.path.exists(cache_file):
                    with open(cache_file, 'r') as f:
                        cache_data = json.load(f)
                        cached_invoices = cache_data.get('invoices', [])
                        cache_timestamp_str = cache_data.get('timestamp')
                        
                        # Check TTL (1 hour = 3600 seconds)
                        if cache_timestamp_str:
                            try:
                                if isinstance(cache_timestamp_str, (int, float)):
                                    cache_time = cache_timestamp_str
                                else:
                                    cache_time = datetime.fromisoformat(cache_timestamp_str).timestamp()
                                
                                current_time = datetime.now().timestamp()
                                age_seconds = current_time - cache_time
                                
                                if age_seconds < 3600:  # Less than 1 hour old
                                    # Restore to session state
                                    st.session_state[cache_key] = cached_invoices
                                else:
                                    # Cache expired - still use it but warn user might want to refresh
                                    st.session_state[cache_key] = cached_invoices
                            except Exception:
                                # If timestamp parsing fails, use cache anyway
                                st.session_state[cache_key] = cached_invoices
                        else:
                            # No timestamp, use cache anyway
                            st.session_state[cache_key] = cached_invoices
            except Exception:
                pass
        
        if cached_invoices:
            # Use cached data for fast lookup
            invoices = cached_invoices
            
            # Filter invoices for this customer and date
            valid_invoices = []
            for invoice in invoices:
                invoice_customer_id = invoice.get('customerId', '')
                invoice_date_str = invoice.get('issueDate', '')
                
                # Check customer match and status
                if (invoice_customer_id == customer_id and 
                    invoice.get('status', '').upper() != 'DELETED' and 
                    invoice.get('source', '').upper() == 'TABS'):
                    
                    # If we have a specific date, filter by date
                    if issue_date and invoice_date_str:
                        try:
                            if 'T' in invoice_date_str:
                                invoice_date = pd.to_datetime(invoice_date_str).date()
                            else:
                                invoice_date = pd.to_datetime(invoice_date_str).date()
                            
                            if invoice_date == issue_date:
                                valid_invoices.append(invoice)
                        except:
                            # If date parsing fails, include the invoice anyway
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
            else:
                # Cache exists but no matching invoices found
                return None
        
        # If no cached data at all, try to fetch and cache
        all_invoices = fetch_all_invoices_for_cache(api_token)
        
        if all_invoices:
            # Cache the results for future use
            st.session_state[cache_key] = all_invoices
            
            # Also save to persistent cache file
            try:
                import json
                _ensure_cache_dir_exists()
                cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_token[:10]}.json")
                cache_data = {
                    'invoices': all_invoices,
                    'timestamp': datetime.now().isoformat(),
                    'count': len(all_invoices)
                }
                with open(cache_file, 'w') as f:
                    json.dump(cache_data, f)
            except Exception:
                pass
            
            # Now filter the freshly fetched invoices
            invoices = all_invoices
            valid_invoices = []
            for invoice in invoices:
                invoice_customer_id = invoice.get('customerId', '')
                invoice_date_str = invoice.get('issueDate', '')
                
                if (invoice_customer_id == customer_id and 
                    invoice.get('status', '').upper() != 'DELETED' and 
                    invoice.get('source', '').upper() == 'TABS'):
                    
                    if issue_date and invoice_date_str:
                        try:
                            if 'T' in invoice_date_str:
                                invoice_date = pd.to_datetime(invoice_date_str).date()
                            else:
                                invoice_date = pd.to_datetime(invoice_date_str).date()
                            
                            if invoice_date == issue_date:
                                valid_invoices.append(invoice)
                        except:
                            valid_invoices.append(invoice)
                    else:
                        valid_invoices.append(invoice)
            
            if valid_invoices:
                valid_invoices.sort(key=lambda x: x.get('issueDate', ''), reverse=True)
                selected_invoice = valid_invoices[0]
                invoice_id = selected_invoice.get('id')
                return invoice_id
        
        # If no cached data or no match found, return None
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


def resolve_tabs_id_from_ns(ns_external_id: str) -> str | None:
    ns_external_id = str(ns_external_id or "").strip()
    ns_external_id = ns_external_id.replace(".0", "")
    print(ns_external_id)
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
            print(f"\nMaking request to: {url}")
            print(f"Headers: {headers}")
            try:
                # Disable SSL verification - Note: In production, proper cert verification should be used
                res = requests.get(url, headers=headers, timeout=10, verify=False)
                # Suppress only the specific InsecureRequestWarning
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                
                # print(f"Response Status Code: {res.status_code}")
                # print(f"Response Headers: {dict(res.headers)}")
                # try:
                #     print(f"Response Content: {res.json()}")
                # except:
                #     print(f"Raw Response Content: {res.text}")
                if res.status_code >= 400:
                    print(f"Error: Request failed with status code {res.status_code}")
            except requests.exceptions.Timeout:
                print("Error: Request timed out after 10 seconds")
            except requests.exceptions.ConnectionError as e:
                print(f"Error: Connection failed - {str(e)}")
            except Exception as e:
                print(f"Error: Unexpected error - {str(e)}")
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
                        return tabs_id
            # Fallback: first item when search hits
            cust = items[0]
            tabs_id = str(cust.get("id") or "").strip()
            if tabs_id:
                cache[ns_external_id] = tabs_id
                st.session_state["ns_to_tabs_cache"] = cache
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

def transform_usage(uploaded_income, uploaded_lbpa, uploaded_clients=None, resolve_now: bool = False, usage_date=None, mappings=None):
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

    def process_usage(df: pd.DataFrame, event_type_name: str, qty_col_candidates: list[str]):
        df.columns = df.columns.str.strip()
        parent_col = find_column(df, ["customername", "accountname", "name"])
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
        if acct_id_col:
            df["__acct_key__"] = df[acct_id_col].astype(str).str.replace(r"[^0-9]", "", regex=True)
        else:
            df["__acct_key__"] = ""
        # Prefer mapping by AccountID if available
        if acct_id_col and acct_to_tabs_id:
            df["customer_id"] = df["__acct_key__"].map(acct_to_tabs_id)
        else:
            df["__join_key__"] = df["AccountName"].astype(str).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
            df["customer_id"] = df["__join_key__"].map(parent_to_id)
        # Do NOT call APIs in the Usage tab; leave customer_id blank if only NetSuite ID exists.
        # IMPORTANT: Do not group by customer_id (it may be NaN and would drop all rows).
        group_keys = ["AccountName", "__acct_key__"]
        agg_dict = {"value": "sum", datetime_col: "max"}
        # Preserve original account name if it exists
        if "__original_account_name__" in df.columns:
            agg_dict["__original_account_name__"] = "first"
        grouped = (
            df.groupby(group_keys, as_index=False)
              .agg(agg_dict)
        )
        # Map customer_id after grouping when available from mapping (name or acct)
        grouped["__join_key__"] = grouped["AccountName"].astype(str).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
        name_mapped = grouped["__join_key__"].map(parent_to_id) if 'parent_to_id' in locals() or 'parent_to_id' in globals() else None
        acct_mapped = grouped["__acct_key__"].map(acct_to_tabs_id) if 'acct_to_tabs_id' in locals() else None
        if acct_mapped is not None:
            grouped["customer_id"] = acct_mapped
        if name_mapped is not None:
            grouped["customer_id"] = grouped.get("customer_id").fillna(name_mapped) if "customer_id" in grouped.columns else name_mapped

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
        grouped["account_id"] = grouped["__acct_key__"]
        # Include __original_account_name__ in return if it exists
        return_cols = ["customer_id", "AccountName", "event_type_name", "datetime", "value", "differentiator", "account_id"]
        if "__original_account_name__" in grouped.columns:
            return_cols.append("__original_account_name__")
        return grouped[return_cols]

    income_df = pd.read_csv(uploaded_income)
    lbpa_df = pd.read_csv(uploaded_lbpa)

    income_upload = process_usage(income_df, "Per Application",
                                  ["isinitialsubmission", "perapplication", "applicationcount"])
    income_upload["ApplicationTypeName"] = "Income"
    # Apply optional event type overrides from mapping (by account_id)
    if acct_to_income_evt:
        income_upload["event_type_name"] = income_upload["account_id"].map(acct_to_income_evt).fillna(income_upload["event_type_name"])

    lbpa_upload = process_usage(lbpa_df, "Units",
                                ["unitsaspersubmission", "units", "unitcount"])
    lbpa_upload["ApplicationTypeName"] = "LBPA"
    if acct_to_lbpa_evt:
        lbpa_upload["event_type_name"] = lbpa_upload["account_id"].map(acct_to_lbpa_evt).fillna(lbpa_upload["event_type_name"])

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

    # Optional: resolve Tabs IDs now using NetSuite external IDs via API
    if resolve_now and get_api_key():
        # Build acct -> NS map from clients file
        # Reuse acct_to_ns_id built earlier in this function
        missing_mask = combined_internal["customer_id"].isna() | (combined_internal["customer_id"].astype(str).str.strip() == "")
        if missing_mask.any():
            acct_keys = combined_internal.loc[missing_mask, "account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True)
            ns_series = acct_keys.map(acct_to_ns_id)
            unique_ns = sorted(x for x in ns_series.dropna().unique().tolist() if str(x).strip())
            ns_to_tabs: dict[str, str] = {}
            for ns in unique_ns:
                tabs_id = resolve_tabs_id_from_ns(ns)
                if tabs_id:
                    ns_to_tabs[str(ns)] = tabs_id
            if ns_to_tabs:
                combined_internal.loc[missing_mask, "customer_id"] = ns_series.map(ns_to_tabs)
    
    # Optional: resolve Tabs IDs now using NetSuite external IDs via API
    if resolve_now and get_api_key():
        # Build acct -> NS map from clients file
        # Reuse acct_to_ns_id built earlier in this function
        missing_mask = combined_internal["customer_id"].isna() | (combined_internal["customer_id"].astype(str).str.strip() == "")
        if missing_mask.any():
            acct_keys = combined_internal.loc[missing_mask, "account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True)
            ns_series = acct_keys.map(acct_to_ns_id)
            unique_ns = sorted(x for x in ns_series.dropna().unique().tolist() if str(x).strip())
            ns_to_tabs: dict[str, str] = {}
            for ns in unique_ns:
                tabs_id = resolve_tabs_id_from_ns(ns)
                if tabs_id:
                    ns_to_tabs[str(ns)] = tabs_id
            if ns_to_tabs:
                combined_internal.loc[missing_mask, "customer_id"] = ns_series.map(ns_to_tabs)
    
    # Ensure customer_id is populated and string type AFTER resolution
    combined_internal["customer_id"] = combined_internal["customer_id"].astype(str)
    valid_customer_mask = (
        (combined_internal["customer_id"] != "nan") &
        (combined_internal["customer_id"] != "None") &
        (combined_internal["customer_id"].str.strip() != "")
    )
    
    # NOW calculate sums AFTER customer_id resolution
    # Sum UnitsAsPerSubmission and IsInitialSubmission directly from Income and LBPA files per customer_id
    # Use the account_id to customer_id mapping from combined_internal to map back to Income and LBPA files
    
    # Create a mapping from account_id to customer_id from combined_internal
    account_to_customer_mapping = {}
    if "account_id" in combined_internal.columns and "customer_id" in combined_internal.columns:
        valid_mapping_mask = (
            combined_internal["customer_id"].notna() &
            (combined_internal["customer_id"].astype(str).str.strip() != "") &
            (combined_internal["customer_id"].astype(str).str.strip().str.lower() != "nan") &
            combined_internal["account_id"].notna()
        )
        mapping_df = combined_internal[valid_mapping_mask][["account_id", "customer_id"]].drop_duplicates()
        account_to_customer_mapping = dict(zip(
            mapping_df["account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True),
            mapping_df["customer_id"].astype(str)
        ))
    
    # Map customer_id to Income and LBPA files using account_id
    income_df_with_customer = income_df.copy()
    lbpa_df_with_customer = lbpa_df.copy()
    
    # Map customer_id to Income file using account_id from combined_internal mapping
    if "AccountID" in income_df_with_customer.columns:
        income_account_ids = income_df_with_customer["AccountID"].astype(str).str.replace(r"[^0-9]", "", regex=True)
        if account_to_customer_mapping:
            income_df_with_customer["customer_id"] = income_account_ids.map(account_to_customer_mapping)
        else:
            # Fallback: try using acct_to_tabs_id if available
            if acct_to_tabs_id:
                income_df_with_customer["customer_id"] = income_account_ids.map(acct_to_tabs_id)
            else:
                income_df_with_customer["customer_id"] = None
    
    # Map customer_id to LBPA file using account_id from combined_internal mapping
    if "AccountID" in lbpa_df_with_customer.columns:
        lbpa_account_ids = lbpa_df_with_customer["AccountID"].astype(str).str.replace(r"[^0-9]", "", regex=True)
        if account_to_customer_mapping:
            lbpa_df_with_customer["customer_id"] = lbpa_account_ids.map(account_to_customer_mapping)
        else:
            # Fallback: try using acct_to_tabs_id if available
            if acct_to_tabs_id:
                lbpa_df_with_customer["customer_id"] = lbpa_account_ids.map(acct_to_tabs_id)
            else:
                lbpa_df_with_customer["customer_id"] = None
    
    # If customer_id still missing, try mapping by customer name using parent_to_id
    if "CustomerName" in income_df_with_customer.columns:
        if "customer_id" not in income_df_with_customer.columns or income_df_with_customer["customer_id"].isna().any():
            income_df_with_customer["__join_key__"] = income_df_with_customer["CustomerName"].astype(str).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
            name_mapped = income_df_with_customer["__join_key__"].map(parent_to_id)
            if "customer_id" not in income_df_with_customer.columns:
                income_df_with_customer["customer_id"] = name_mapped
            else:
                income_df_with_customer["customer_id"] = income_df_with_customer["customer_id"].fillna(name_mapped)
    
    if "CustomerName" in lbpa_df_with_customer.columns:
        if "customer_id" not in lbpa_df_with_customer.columns or lbpa_df_with_customer["customer_id"].isna().any():
            lbpa_df_with_customer["__join_key__"] = lbpa_df_with_customer["CustomerName"].astype(str).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
            name_mapped = lbpa_df_with_customer["__join_key__"].map(parent_to_id)
            if "customer_id" not in lbpa_df_with_customer.columns:
                lbpa_df_with_customer["customer_id"] = name_mapped
            else:
                lbpa_df_with_customer["customer_id"] = lbpa_df_with_customer["customer_id"].fillna(name_mapped)
    
    # Convert customer_id to string and filter valid ones
    if "customer_id" in income_df_with_customer.columns:
        income_df_with_customer["customer_id"] = income_df_with_customer["customer_id"].astype(str)
    else:
        income_df_with_customer["customer_id"] = None
    
    if "customer_id" in lbpa_df_with_customer.columns:
        lbpa_df_with_customer["customer_id"] = lbpa_df_with_customer["customer_id"].astype(str)
    else:
        lbpa_df_with_customer["customer_id"] = None
    
    income_valid_mask = (
        income_df_with_customer["customer_id"].notna() &
        (income_df_with_customer["customer_id"] != "nan") &
        (income_df_with_customer["customer_id"] != "None") &
        (income_df_with_customer["customer_id"].str.strip() != "")
    )
    
    lbpa_valid_mask = (
        lbpa_df_with_customer["customer_id"].notna() &
        (lbpa_df_with_customer["customer_id"] != "nan") &
        (lbpa_df_with_customer["customer_id"] != "None") &
        (lbpa_df_with_customer["customer_id"].str.strip() != "")
    )
    
    # For Finastra customers, we need to group by customer_id + account_id (differentiator)
    # For other customers, group by customer_id only
    # First, identify Finastra customers
    income_finastra_mask = income_df_with_customer["CustomerName"].astype(str).str.strip().str.lower() == "finastra"
    lbpa_finastra_mask = lbpa_df_with_customer["CustomerName"].astype(str).str.strip().str.lower() == "finastra"
    
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
    finastra_mask = combined_internal["CustomerName"].astype(str).str.strip().str.lower() == "finastra"
    if "account_id" in combined_internal.columns:
        finastra_with_account = finastra_mask & combined_internal["account_id"].notna()
        combined_internal.loc[finastra_with_account, "__group_key__"] = (
            combined_internal.loc[finastra_with_account, "customer_id"].astype(str) + "_" +
            combined_internal.loc[finastra_with_account, "account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True)
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
    
    # Set differentiator for Finastra customers only
    # Use __original_account_name__ directly (it already contains "Finastra - {account name}")
    finastra_mask = combined_internal["CustomerName"].astype(str).str.strip().str.lower() == "finastra"
    
    # For Finastra rows, use the original AccountName from Income file directly
    # (it already contains "Finastra - {account name}" so no need to prepend "Finastra - ")
    if "__original_account_name__" in combined_internal.columns:
        # Use original AccountName from Income file AccountName column directly
        income_finastra_mask = finastra_mask & (combined_internal["ApplicationTypeName"] == "Income")
        if income_finastra_mask.any():
            combined_internal.loc[income_finastra_mask, "differentiator"] = (
                combined_internal.loc[income_finastra_mask, "__original_account_name__"].astype(str)
            )
        # For LBPA rows with Finastra, use AccountName (which should be the customer name)
        lbpa_finastra_mask = finastra_mask & (combined_internal["ApplicationTypeName"] == "LBPA")
        if lbpa_finastra_mask.any():
            combined_internal.loc[lbpa_finastra_mask, "differentiator"] = (
                "Finastra - " + combined_internal.loc[lbpa_finastra_mask, "AccountName"].astype(str)
            )
    else:
        # Fallback: use AccountName if __original_account_name__ not available
        combined_internal.loc[finastra_mask, "differentiator"] = (
            "Finastra - " + combined_internal.loc[finastra_mask, "AccountName"].astype(str)
        )
    
    # Set differentiator to empty for non-Finastra customers
    combined_internal.loc[~finastra_mask, "differentiator"] = ""

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
    ]
    
    # Separate unmapped rows (customer_id is missing AND CustomerName is still numeric/account ID)
    customer_id_missing = combined_internal["customer_id"].isna() | (combined_internal["customer_id"].astype(str).str.strip() == "")
    customer_name_is_numeric = combined_internal["CustomerName"].astype(str).str.match(r'^\d+$', na=False)
    unmapped_mask = customer_id_missing & customer_name_is_numeric
    
    # Also check for rows with invalid customer_id (not just missing, but also "nan", "None", empty)
    invalid_customer_mask = (
        combined_internal["customer_id"].isna() |
        (combined_internal["customer_id"].astype(str).str.strip() == "") |
        (combined_internal["customer_id"].astype(str).str.strip().str.lower() == "nan") |
        (combined_internal["customer_id"].astype(str).str.strip() == "None")
    )
    
    # Create separate DataFrames for mapped and unmapped rows
    # Only include rows with valid customer_id in the main output
    unmapped_df = combined_internal[unmapped_mask].copy()
    mapped_df = combined_internal[valid_customer_mask].copy()  # Use valid_customer_mask instead of ~unmapped_mask
    
    combined = mapped_df[upload_cols]
    unmapped_output = unmapped_df[upload_cols] if len(unmapped_df) > 0 else pd.DataFrame(columns=upload_cols)

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

    # Store original dataframes for later split CSV generation with all columns
    st.session_state["original_income_df"] = income_df.copy()
    st.session_state["original_lbpa_df"] = lbpa_df.copy()

    return income_upload, lbpa_df, combined_csv_bytes, combined_internal_csv_bytes


def generate_split_csvs_with_all_columns(income_df, lbpa_df, usage_df, max_rows_per_split_csv=900):
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
    
    # Get mappings from usage_df - try account_id first, then CustomerName
    account_id_to_customer_id = {}
    customername_to_customer_id = {}
    # Also create reverse mapping: customer_id -> CustomerName (for filename)
    customer_id_to_name = {}
    
    if "account_id" in usage_df.columns and "customer_id" in usage_df.columns:
        # Create account_id -> customer_id mapping (like reference code uses UUID)
        usage_acct_mapping = usage_df[["account_id", "customer_id"]].drop_duplicates()
        usage_acct_mapping["__acct_key__"] = usage_acct_mapping["account_id"].astype(str).str.replace(r"[^0-9]", "", regex=True)
        account_id_to_customer_id = dict(zip(usage_acct_mapping["__acct_key__"], usage_acct_mapping["customer_id"]))
    
    if usage_name_col and "customer_id" in usage_df.columns:
        usage_mapping = usage_df[[usage_name_col, "customer_id"]].drop_duplicates()
        # Create both exact and normalized name mappings
        for name, cust_id in zip(usage_mapping[usage_name_col].astype(str), usage_mapping["customer_id"]):
            customername_to_customer_id[name] = cust_id
            # Also create normalized version for matching
            normalized_name = normalize_name(name)
            if normalized_name not in customername_to_customer_id:
                customername_to_customer_id[normalized_name] = cust_id
        # Reverse mapping for filename generation
        customer_id_to_name = dict(zip(usage_mapping["customer_id"].astype(str), usage_mapping[usage_name_col].astype(str)))
    
    def add_customer_id_from_usage(df, df_name):
        """Add customer_id to dataframe by joining with usage_df mapping"""
        df = df.copy()
        df.columns = df.columns.str.strip()
        
        # Find AccountName/CustomerName column in original Income/LBPA file
        parent_col = find_column(df, ["customername", "accountname", "name"])
        acct_id_col = find_column(df, ["accountid", "acct#", "acct", "account number", "accountnumber"])
        
        if parent_col:
            df["__original_name__"] = df[parent_col].astype(str)
            df["__original_name__"] = df["__original_name__"].fillna("Unknown")
        else:
            df["__original_name__"] = "Unknown"
        
        # Try account_id matching first (most reliable, like reference code)
        if acct_id_col and account_id_to_customer_id:
            df["__acct_key__"] = df[acct_id_col].astype(str).str.replace(r"[^0-9]", "", regex=True)
            df["customer_id"] = df["__acct_key__"].map(account_id_to_customer_id)
        
        # Fill missing with name-based matching (try exact first, then normalized)
        if customername_to_customer_id:
            # Try exact match first
            name_mapped = df["__original_name__"].map(customername_to_customer_id)
            if "customer_id" in df.columns:
                df["customer_id"] = df["customer_id"].fillna(name_mapped)
            else:
                df["customer_id"] = name_mapped
            
            # If still missing, try normalized name matching
            missing_mask = df["customer_id"].isna()
            if missing_mask.any():
                df.loc[missing_mask, "__normalized_name__"] = df.loc[missing_mask, "__original_name__"].apply(normalize_name)
                normalized_mapped = df.loc[missing_mask, "__normalized_name__"].map(customername_to_customer_id)
                df.loc[missing_mask, "customer_id"] = df.loc[missing_mask, "customer_id"].fillna(normalized_mapped)
        
        if "customer_id" not in df.columns:
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
    
    # Remove helper columns before generating CSVs (keep only original columns + customer_id)
    helper_columns = ["__original_name__", "__acct_key__", "__normalized_name__", "__join_key__", "__original_account_name__"]
    columns_to_keep = [col for col in combined_all.columns if col not in helper_columns]
    # Ensure customer_id is included
    if "customer_id" not in columns_to_keep:
        columns_to_keep.append("customer_id")
    
    for customer_id, group in combined_all.groupby("customer_id"):
        group = group.sort_values("SubmissionDate" if "SubmissionDate" in group.columns else group.columns[0])
        if pd.isna(customer_id) or str(customer_id).strip() == "":
            continue
        split_csvs = [group[i:i + max_rows_per_split_csv] for i in range(0, len(group), max_rows_per_split_csv)]
        for idx, split_csv in enumerate(split_csvs, start=1):
            suffix = f"_part{idx}" if len(split_csvs) > 1 else ""
            
            # Get customer name for filename
            customer_name = customer_id_to_name.get(str(customer_id), "Unknown")
            # Clean customer name for filesystem: remove special chars, replace spaces with underscores
            safe_name = re.sub(r'[<>:"/\\|?*]', '', customer_name)  # Remove invalid filename chars
            safe_name = re.sub(r'\s+', '_', safe_name.strip())  # Replace spaces with underscores
            safe_name = safe_name[:50] if len(safe_name) > 50 else safe_name  # Limit length
            
            filename = f"{safe_name}_{customer_id}{suffix}.csv"
            # Only include original columns + customer_id (exclude helper columns)
            split_csv_clean = split_csv[[col for col in columns_to_keep if col in split_csv.columns]]
            split_csv_bytes = split_csv_clean.to_csv(index=False).encode("utf-8")
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
    income_file = st.file_uploader("Upload Income Transaction Data", type="csv", key="income")
    
    if income_file is not None:
        persist_upload(income_file, "income")
    
    # Show preview directly under Income uploader
    uploaded_files = st.session_state.get("uploaded_files", {})
    if uploaded_files.get("income", {}).get("bytes"):
        with st.expander("Preview", expanded=False):
            try:
                income_df = pd.read_csv(BytesIO(uploaded_files["income"]["bytes"]))
                st.caption(f"Rows: {len(income_df):,} | Columns: {len(income_df.columns)}")
                st.dataframe(income_df, use_container_width=True)
            except Exception as e:
                st.error(f"Could not preview Income file: {e}")
    
    lbpa_file = st.file_uploader("Upload LBPA Transaction Data", type="csv", key="lbpa")
    
    if lbpa_file is not None:
        persist_upload(lbpa_file, "lbpa")
    
    # Show preview directly under LBPA uploader
    if uploaded_files.get("lbpa", {}).get("bytes"):
        with st.expander("Preview", expanded=False):
            try:
                lbpa_df = pd.read_csv(BytesIO(uploaded_files["lbpa"]["bytes"]))
                st.caption(f"Rows: {len(lbpa_df):,} | Columns: {len(lbpa_df.columns)}")
                st.dataframe(lbpa_df, use_container_width=True)
            except Exception as e:
                st.error(f"Could not preview LBPA file: {e}")

    # Client mappings are automatically loaded from disk at startup
    # Show mappings status
    current_mappings = st.session_state.get("client_mappings", {})
    if not current_mappings:
        # Try to load from disk if not in session state
        disk_mappings = _load_client_mappings_from_disk()
        if disk_mappings:
            st.session_state["client_mappings"] = disk_mappings
            current_mappings = disk_mappings
    
    if current_mappings and current_mappings.get("acct_to_ns_id"):
        mapping_count = len(current_mappings.get("acct_to_ns_id", {}))
        st.info(f"✅ Client mappings loaded ({mapping_count} NetSuite ID mappings available)")
    else:
        st.warning("⚠️ No client mappings found. Please ensure client_mappings.json exists in usage_uploads/_session/")

    resolve_now = st.checkbox("Retrieve Tabs Customer IDs (requires API key)")
    if resolve_now:
        st.text_input("Tabs API Key", type="password", key="ui_api_key_usage", placeholder="Enter Tabs API key")

    # Usage date picker
    st.markdown("---")
    usage_date = st.date_input(
        "Select Usage Date",
        value=None,
        help="This date will be used to populate the datetime column for all usage records"
    )

    if st.button("Generate Usage CSV"):
        up = st.session_state.get("uploaded_files", {})
        missing = []
        if not up.get("income", {}).get("bytes"):
            missing.append("Income")
        if not up.get("lbpa", {}).get("bytes"):
            missing.append("LBPA")

        if not missing:
            if not usage_date:
                st.error("Please select a usage date")
            else:
                with st.spinner("Running transformation..."):
                    # Use stored mappings if available
                    stored_mappings = st.session_state.get("client_mappings")
                    income_df, lbpa_df, combined_csv, combined_internal_csv = transform_usage(
                        BytesIO(up["income"]["bytes"]),
                        BytesIO(up["lbpa"]["bytes"]),
                        uploaded_clients=None,
                        resolve_now=resolve_now,
                        usage_date=usage_date,
                        mappings=stored_mappings if stored_mappings else None,
                    )
                st.success("Transformation complete!")
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
        
        st.download_button(
            "Download Usage CSV",
            data=st.session_state["generated_files"]["usage_combined"]["bytes"],
            file_name=st.session_state["generated_files"]["usage_combined"]["name"],
            key="dl_usage_latest",
        )
        
        # Show unmapped CSV download if any unmapped rows exist
        if st.session_state.get("generated_files", {}).get("usage_unmapped"):
            unmapped_count = st.session_state.get("unmapped_count", 0)
            st.warning(f"⚠️ {unmapped_count} rows could not be mapped to customers and have been separated into a separate CSV file.")
            
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
        
        # Check if original data is available
        has_original_data = (
            st.session_state.get("original_income_df") is not None and
            st.session_state.get("original_lbpa_df") is not None
        )
        
        if not has_original_data:
            st.warning("⚠️ Please generate Usage CSV in the 'Usage Transformation' tab first. This will ensure the original Income and LBPA data is available for creating split CSVs with all columns.")
        
        # Generate split CSVs
        if has_original_data:
            if st.button("Generate Split CSVs", type="primary"):
                try:
                    with st.spinner("Creating split CSVs..."):
                        # Get original Income and LBPA dataframes from session state
                        income_df = st.session_state.get("original_income_df")
                        lbpa_df = st.session_state.get("original_lbpa_df")
                        
                        # Get Usage CSV (which has customer_id) - use generated or uploaded
                        usage_df = st.session_state.get("invoice_usage_csv")
                        if usage_df is None and st.session_state.get("generated_files", {}).get("usage_combined"):
                            usage_csv_bytes = st.session_state["generated_files"]["usage_combined"]["bytes"]
                            usage_df = pd.read_csv(BytesIO(usage_csv_bytes))
                        
                        if usage_df is None or len(usage_df) == 0:
                            st.error("⚠️ Usage CSV not found. Please generate Usage CSV in the 'Usage Transformation' tab first.")
                        elif income_df is None or lbpa_df is None:
                            st.error("⚠️ Original Income/LBPA data not found. Please generate Usage CSV in the 'Usage Transformation' tab first.")
                        else:
                            split_csvs = generate_split_csvs_with_all_columns(
                                income_df, 
                                lbpa_df, 
                                usage_df,
                                max_rows_per_split_csv=999999  # One CSV per customer, no splitting
                            )
                            
                            if len(split_csvs) == 0:
                                st.warning(f"⚠️ No split CSVs created. Check that customer_id mapping is working correctly.")
                            else:
                                st.session_state["invoice_split_csvs"] = split_csvs
                                st.session_state["invoice_split_csvs_ready"] = True
                                st.success(f"✅ Created {len(split_csvs)} split CSV files with all original columns")
                except Exception as e:
                    st.error(f"Error creating split CSVs: {str(e)}")
                    import traceback
                    st.code(traceback.format_exc())
        
        # Display download options if split CSVs are ready (outside button handler so they persist)
        if st.session_state.get("invoice_split_csvs_ready"):
            split_csvs = st.session_state.get("invoice_split_csvs", [])
            if split_csvs:
                # Show split CSV summary
                split_csv_summary = pd.DataFrame([
                    {"Filename": split_csv["name"], "Size (rows)": len(pd.read_csv(BytesIO(split_csv["bytes"])))}
                    for split_csv in split_csvs
                ])
                st.dataframe(split_csv_summary, use_container_width=True)
                
                st.markdown("---")
                st.subheader("Download Options")
                
                # Option to download individual files
                with st.expander("Download Individual CSVs"):
                    cols = st.columns(min(3, len(split_csvs)))
                    for idx, split_csv in enumerate(split_csvs):
                        with cols[idx % len(cols)]:
                            st.download_button(
                                label=split_csv['name'],
                                data=split_csv["bytes"],
                                file_name=split_csv["name"],
                                mime="text/csv",
                                key=f"download_split_csv_{idx}"
                            )
                
                # Download all option
                import zipfile
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for split_csv in split_csvs:
                        zip_file.writestr(split_csv["name"], split_csv["bytes"])
                zip_buffer.seek(0)
                st.download_button(
                    "Download All Split CSVs (ZIP)",
                    data=zip_buffer.getvalue(),
                    file_name="all_split_csvs.zip",
                    mime="application/zip",
                    key="download_all_split_csvs"
                )
    
    elif current_step == 1:  # Step 2: Invoice Mapping
        st.subheader("Invoice Mapping")
        
        if not st.session_state.get("invoice_split_csvs_ready"):
            st.warning("⚠️ Please complete Step 1 first (Generate Split CSVs)")
        else:
            split_csvs = st.session_state.get("invoice_split_csvs", [])
            st.info(f"📋 {len(split_csvs)} split CSV files ready for invoice mapping")
                
            # API Configuration
            st.subheader("API Configuration")
            api_key = st.text_input(
                "Tabs API Key",
                type="password",
                help="Enter your TABS API key for invoice lookup",
                value=st.session_state.get('invoice_api_key', ''),
                key="invoice_api_key_input"
            )
                
            if api_key:
                st.session_state['invoice_api_key'] = api_key
            
            # Smart caching system for API invoices (matching reference_code)
            if api_key:
                st.subheader("Invoice Cache Management")
                
                # Check if we have cached invoices (with better persistence)
                cache_key = f"invoice_cache_{api_key[:10]}"
                
                # Try to get from session state first
                cached_invoices = st.session_state.get(cache_key, [])
                cache_timestamp = st.session_state.get(f"{cache_key}_timestamp", None)
                
                # If no cache in session state, try to load from file
                if not cached_invoices:
                    try:
                        import json
                        cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_key[:10]}.json")
                        if os.path.exists(cache_file):
                            with open(cache_file, 'r') as f:
                                cache_data = json.load(f)
                                cached_invoices = cache_data.get('invoices', [])
                                cache_timestamp_str = cache_data.get('timestamp')
                                if cache_timestamp_str:
                                    try:
                                        if isinstance(cache_timestamp_str, (int, float)):
                                            cache_timestamp = datetime.fromtimestamp(cache_timestamp_str)
                                        else:
                                            cache_timestamp = datetime.fromisoformat(cache_timestamp_str)
                                    except Exception:
                                        cache_timestamp = None
                                    
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
                        # Clear existing cache first
                        if cache_key in st.session_state:
                            del st.session_state[cache_key]
                        if f"{cache_key}_timestamp" in st.session_state:
                            del st.session_state[f"{cache_key}_timestamp"]
                        
                        # Also clear from file
                        try:
                            cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_key[:10]}.json")
                            if os.path.exists(cache_file):
                                os.remove(cache_file)
                        except Exception:
                            pass
                        
                        with st.spinner("Fetching all invoices from API (this may take a few minutes)..."):
                            all_invoices = fetch_all_invoices_for_cache(api_key)
                            if all_invoices:
                                # Save to session state
                                st.session_state[cache_key] = all_invoices
                                st.session_state[f"{cache_key}_timestamp"] = datetime.now()
                                
                                # Also save to file for persistence
                                try:
                                    import json
                                    _ensure_cache_dir_exists()
                                    cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_key[:10]}.json")
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
                            cache_file = os.path.join(_CACHE_DIR, f"invoice_cache_{api_key[:10]}.json")
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
        
        # Date picker for issue date
        st.subheader("Invoice Issue Date")
        issue_date = st.date_input(
            "Select the issue date for invoice lookup:",
            value=datetime.today().date(),
            help="This date will be used to find matching invoices",
            key="invoice_issue_date"
        )
        
        if st.button("Map Invoices to Split CSVs", type="primary"):
            if not api_key:
                st.error("Please provide API key")            
            else:
                try:
                    mapping_data = []
                    problematic_split_csvs = []
                    
                    st.info(f"📋 Processing {len(split_csvs)} split CSV files...")
                    
                    for i, split_csv in enumerate(split_csvs, 1):
                        split_csv_df = pd.read_csv(BytesIO(split_csv["bytes"]))
                        
                        # Get unique customer IDs from split CSV
                        customer_ids = split_csv_df["customer_id"].dropna().unique()
                        
                        st.write(f"📄 Processing {i}/{len(split_csvs)}: {split_csv['name']}")
                        
                        if len(customer_ids) == 0:
                            st.warning(f"   ⚠️ No customer IDs found in split CSV")
                            problematic_split_csvs.append({
                                "split_csv_filename": split_csv["name"],
                                "customer_id": "N/A",
                                "issue_date": issue_date.strftime("%Y-%m-%d"),
                                "issue": "No customer IDs found"
                            })
                            st.write("---")
                            continue
                        
                        # For now, use first customer_id (each split CSV should be per customer)
                        customer_id = customer_ids[0]
                        st.write(f"   Customer ID: {customer_id}")
                        
                        # Find invoice ID by customer and issue date
                        st.write(f"   Looking up invoice for date: {issue_date.strftime('%Y-%m-%d')}")
                        
                        invoice_id = find_invoice_by_date(customer_id, issue_date, api_key)
                        
                        if not invoice_id:
                            st.warning(f"   ⚠️ No matching invoice found for customer {customer_id} on {issue_date}")
                            problematic_split_csvs.append({
                                "split_csv_filename": split_csv["name"],
                                "customer_id": customer_id,
                                "issue_date": issue_date.strftime("%Y-%m-%d"),
                                "issue": f"No matching invoice found for customer {customer_id} on {issue_date}"
                            })
                            st.error(f"   ❌ No invoice ID found")
                        else:
                            st.success(f"   ✅ Invoice ID: {invoice_id}")
                            mapping_data.append({
                                "split_csv_filename": split_csv["name"],
                                "customer_id": customer_id,
                                "invoice_id": invoice_id,
                                "issue_date": issue_date.strftime("%Y-%m-%d")
                            })
                        
                        st.write("---")
                        
                    # Show results summary after processing all split CSVs
                    if mapping_data:
                        mapping_df = pd.DataFrame(mapping_data)
                        st.session_state["invoice_mapping"] = mapping_df
                        st.session_state["invoice_mapping_ready"] = True
                        
                        st.success(f"✅ **Invoice mapping completed!**")
                        st.info(f"**Results:** {len(mapping_df)} split CSVs successfully mapped, {len(problematic_split_csvs)} split CSVs need attention")
                        st.dataframe(mapping_df, use_container_width=True)
                        
                        # Download button for successful mappings
                        if len(mapping_df) > 0:
                            csv_bytes = mapping_df.to_csv(index=False).encode('utf-8')
                            st.download_button(
                                "Download Invoice Mapping CSV",
                                data=csv_bytes,
                                file_name="invoice_mapping_successful.csv",
                                mime="text/csv"
                            )
                    else:
                        st.error("No valid invoice mappings could be created")
                    
                    # Create problematic split CSVs DataFrame and store in session state
                    if problematic_split_csvs:
                        problematic_df = pd.DataFrame(problematic_split_csvs)
                        st.session_state["invoice_problematic_split_csvs"] = problematic_df
                    else:
                        st.session_state["invoice_problematic_split_csvs"] = None
                    
                except Exception as e:
                    st.error(f"Error creating invoice mapping: {str(e)}")
                    import traceback
                    st.code(traceback.format_exc())
        
        # Display problematic split CSVs if any (matching reference_code pattern)
        if 'invoice_problematic_split_csvs' in st.session_state and st.session_state.invoice_problematic_split_csvs is not None:
            df_problematic = st.session_state.invoice_problematic_split_csvs
            
            if len(df_problematic) > 0:
                st.markdown("---")
                st.subheader("⚠️ Split CSVs Requiring Attention")
                st.warning(f"**{len(df_problematic)} split CSVs could not be mapped to invoices**")
                st.dataframe(df_problematic, use_container_width=True)
                
                # Download button for problematic split CSVs
                problematic_csv_bytes = df_problematic.to_csv(index=False).encode('utf-8')
                st.download_button(
                    "Download Unmapped Split CSVs CSV",
                    data=problematic_csv_bytes,
                    file_name="invoice_mapping_problematic.csv",
                    mime="text/csv"
                )
    
    elif current_step == 2:  # Step 3: Bulk Upload
        st.subheader("Bulk Upload CSV Attachments")
        
        if not st.session_state.get("invoice_mapping_ready"):
            st.warning("⚠️ Please complete Step 2 first (Invoice Mapping)")
        else:
            mapping_df = st.session_state.get("invoice_mapping")
            split_csvs = st.session_state.get("invoice_split_csvs", [])
            
            st.info(f"📋 Ready to upload {len(mapping_df)} split CSVs to invoices")
            
            # Create lookup dict for split CSVs
            split_csvs_dict = {split_csv["name"]: split_csv for split_csv in split_csvs}
            
            # Show preview
            st.subheader("Upload Preview")
            preview_df = mapping_df.copy()
            preview_df["split_csv_size"] = preview_df["split_csv_filename"].map(
                lambda x: len(pd.read_csv(BytesIO(split_csvs_dict[x]["bytes"]))) if x in split_csvs_dict else 0
            )
            st.dataframe(preview_df, use_container_width=True)
            
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
                            
                            # Limit to first row if test mode is enabled
                            rows_to_process = mapping_df.head(1) if test_mode else mapping_df
                            
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
