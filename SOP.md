# LoanBeam Usage and Invoice Attachment Workflow - User Guide

## Overview
This tool helps you transform usage data from Income and LBPA transaction files into the format required by Tabs Platform, and then attach CSV files to invoices in bulk.

## Prerequisites
- Access to the Streamlit web application
- Your Income transaction CSV file
- Your LBPA transaction CSV file
- Your Customer Mapping CSV file (with account numbers, customer names, and Tabs customer IDs)
- Tabs Platform API key (for invoice mapping and bulk upload)

---

## Workflow Overview

The tool has two main tabs:

1. **Usage Transformation** - Transform your Income and LBPA data into Tabs-compatible format
2. **Invoice Attachment** - Create split CSVs and attach them to invoices

---

## Tab 1: Usage Transformation

### Step 1: Upload Your Files

1. **Upload Income Transaction Data**
   - Click "Browse files" under "Upload Income Transaction Data"
   - Select your Income CSV file
   - You'll see a preview showing the number of rows and columns

2. **Upload LBPA Transaction Data**
   - Click "Browse files" under "Upload LBPA Transaction Data"
   - Select your LBPA CSV file
   - You'll see a preview showing the number of rows and columns

3. **Customer Mapping CSV** (Auto-loaded)
   - The customer mapping file is automatically loaded from the session directory
   - If you see "✅ Customer Mapping CSV loaded from session directory", the file is ready
   - If you need to upload a different mapping file, it will appear as an uploader
   - Your mapping file should contain:
     - Account numbers (Acct# column)
     - Customer names
     - Tabs customer IDs (optional, but recommended)

### Step 2: Map NetSuite IDs (Optional)

If you have NetSuite IDs in your mapping file that need to be resolved to Tabs customer IDs:

1. Check the box "Map NetSuite IDs to Tabs Customer IDs (requires API key)"
2. Enter your Tabs API key in the password field
3. Click "Generate Usage CSV"

**Note:** If you already have Tabs customer IDs in your mapping file, you can skip this step.

### Step 3: Generate Usage CSV

1. Click the **"Generate Usage CSV"** button
2. Wait for the transformation to complete (you'll see a spinner)
3. Once complete, you'll see a success message
4. Click the **"Download Usage CSV"** button to download your transformed file

### What the Output Contains

The generated Usage CSV includes:
- Customer ID (Tabs customer UUID)
- Customer Name
- Event Type (e.g., "Per Application", "Units")
- Date/Time
- Application Type (Income or LBPA)
- Units per Submission
- Initial Submissions
- Value
- Differentiator (for customers with subsidiaries)

---

## Tab 2: Invoice Attachment

This tab has three steps that you'll complete in order.

### Step 1: Generate Split CSVs

**Purpose:** Creates one CSV file per customer from your original Income and LBPA data. Each CSV contains all original columns and is grouped by customer ID.

**Prerequisites:**
- You must have completed Tab 1 (Usage Transformation) first
- This ensures the original data is available

**Instructions:**

1. The system will automatically check if you have the required data
2. If you see a warning, go back to Tab 1 and generate the Usage CSV first
3. Click the **"Generate Split CSVs"** button
4. Wait for processing to complete
5. You'll see a summary table showing:
   - Filename for each split CSV
   - Number of rows in each file

**Download Options:**

- **Individual CSVs:** Expand the "Download Individual CSVs" section to download specific files
- **All at Once:** Click "Download All Split CSVs (ZIP)" to download everything in a ZIP file

### Step 2: Invoice Mapping

**Purpose:** Maps each split CSV to the correct invoice using the Tabs API.

**Prerequisites:**
- Step 1 must be completed
- You need your Tabs API key

**Instructions:**

1. **Enter API Key**
   - Enter your Tabs API key in the "Tabs API Key" field
   - The key will be saved for this session

2. **Manage Invoice Cache** (Recommended First-Time Setup)
   - The system uses caching to speed up invoice lookups
   - Click **"🔄 Refresh Cache"** to fetch all invoices from the API
     - This may take a few minutes the first time
     - The cache is saved and will persist between sessions
   - You'll see the cache status showing:
     - Number of invoices cached
     - How old the cache is
   - **Cache Recommendations:**
     - ✅ Fresh (< 6 hours): Cache is up-to-date
     - ℹ️ Moderate (6-24 hours): New invoices may not be included
     - ⚠️ Old (> 24 hours): Consider refreshing for new invoices

3. **Select Invoice Issue Date**
   - Use the date picker to select the issue date for invoices
   - This date will be used to find matching invoices for each customer

4. **Map Invoices**
   - Click **"Map Invoices to Split CSVs"** button
   - The system will process each split CSV:
     - It looks up the customer ID from the CSV
     - Finds the matching invoice for that customer on the selected date
     - Maps the CSV to the invoice ID
   - You'll see progress messages for each file
   - After completion, you'll see:
     - A summary table showing successful mappings
     - A download button for the mapping CSV

**Handling Unmapped Files:**

If some split CSVs couldn't be mapped to invoices:
- You'll see a warning section titled "⚠️ Split CSVs Requiring Attention"
- Review the table to see which files had issues
- Common issues:
  - No customer ID found in the CSV
  - No matching invoice found for that customer/date
- Download the problematic CSV for review
- Fix the issues and regenerate if needed

### Step 3: Bulk Upload

**Purpose:** Uploads all mapped CSV files as attachments to their corresponding invoices.

**Prerequisites:**
- Step 2 must be completed
- You need your Tabs API key (entered in Step 2)

**Instructions:**

1. Review the upload preview table showing:
   - Split CSV filename
   - Customer ID
   - Invoice ID
   - Number of rows in each CSV

2. Click **"Start Bulk Upload"** button

3. Watch the progress bar as files are uploaded

4. After completion, you'll see:
   - Success count (how many uploaded successfully)
   - A results table showing status for each file
   - Files that failed will show a reason

**Troubleshooting Upload Failures:**

If some uploads fail:
- Check the "reason" column in the results table
- Common issues:
  - Invalid API key
  - Network connectivity issues
  - Invoice ID no longer exists
- Retry failed uploads individually if needed

---

## Common Issues and Solutions

### Issue: "Missing: Income/LBPA/Clients"
**Solution:** Make sure you've uploaded all three required files before clicking "Generate Usage CSV"

### Issue: "No split CSVs created"
**Solution:** 
- Ensure you've generated the Usage CSV first
- Check that customer IDs are properly mapped in your mapping file
- Verify that account numbers match between your files

### Issue: "No matching invoice found"
**Solution:**
- Verify the invoice issue date is correct
- Check that invoices exist for the selected date
- Ensure customer IDs match between your data and Tabs
- Try refreshing the invoice cache

### Issue: "Cache is very old"
**Solution:** Click "🔄 Refresh Cache" to fetch the latest invoices from the API

### Issue: "Upload failed"
**Solution:**
- Verify your API key is correct
- Check your internet connection
- Ensure the invoice IDs are still valid
- Review the error message for specific details

---

## Tips for Best Results

1. **Keep Your Mapping File Updated**
   - Ensure customer IDs are current
   - Include account numbers for accurate matching

2. **Refresh Cache Regularly**
   - If you're adding new invoices, refresh the cache to include them
   - Cache is valid for 1 hour, but can be refreshed manually anytime

3. **Verify Dates**
   - Double-check the invoice issue date matches your invoices
   - Use the exact date format expected by Tabs

4. **Review Unmapped Files**
   - Always check the "Unmapped Split CSVs" section
   - These files need attention before proceeding to upload

5. **Test with Small Batch First**
   - If processing many files, test with a few first
   - Verify the results before processing the full batch

---

## Support

If you encounter issues not covered in this guide:
1. Check the error messages displayed in the application
2. Review the troubleshooting section above
3. Contact your system administrator with:
   - Screenshot of the error
   - Steps you were taking when the error occurred
   - Any relevant file names or customer IDs

---

## Quick Reference Checklist

### Usage Transformation:
- [ ] Upload Income CSV
- [ ] Upload LBPA CSV
- [ ] Verify Customer Mapping CSV is loaded (auto-loaded from session directory)
- [ ] (Optional) Enter API key for NetSuite ID mapping
- [ ] Click "Generate Usage CSV"
- [ ] Download the generated file

### Invoice Attachment:
- [ ] Complete Usage Transformation first
- [ ] Click "Generate Split CSVs"
- [ ] Enter Tabs API key
- [ ] (First time) Click "Refresh Cache"
- [ ] Select invoice issue date
- [ ] Click "Map Invoices to Split CSVs"
- [ ] Review and download mapping CSV
- [ ] Review any unmapped files
- [ ] Click "Start Bulk Upload"
- [ ] Verify upload results

---

*Last Updated: November 2024*

