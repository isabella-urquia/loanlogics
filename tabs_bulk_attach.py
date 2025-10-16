import os
import pandas as pd
import requests

# ========= CONFIGURATION =========
API_KEY = "YOUR_TABS_API_KEY"
API_URL_BASE = "https://api.tabs.dev/v3/customers"
OUTPUT_DIR = "output_usage_chunks"
MAPPING_FILE = "invoice_mapping.csv"
# =================================


def bulk_attach():
    mapping = pd.read_csv(MAPPING_FILE)

    for _, row in mapping.iterrows():
        cust_id = str(row["customer_id"])
        invoice_id = str(row["invoice_id"])
        csv_path = os.path.join(OUTPUT_DIR, f"tabs_upload_{cust_id}.csv")

        if not os.path.exists(csv_path):
            print(f"⚠️ Skipping {cust_id} — no CSV found")
            continue

        with open(csv_path, "rb") as f:
            res = requests.post(
                f"{API_URL_BASE}/{cust_id}/invoices/{invoice_id}/attachments",
                headers={"Authorization": f"Bearer {API_KEY}"},
                files={"file": (os.path.basename(csv_path), f, "text/csv")},
            )

        if res.status_code in (200, 201):
            print(f"✅ Attached {csv_path} to invoice {invoice_id}")
        else:
            print(f"❌ Failed for {cust_id} ({res.status_code}): {res.text}")


if __name__ == "__main__":
    print("\n🔹 Starting bulk attachment process")
    bulk_attach()
    print("\n✅ All done!")
