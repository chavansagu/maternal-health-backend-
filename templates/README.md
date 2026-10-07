# Pregnant Women Bulk Upload Template

## 📋 Template File
- **File:** `pregnant_women_bulk_upload_template.csv`
- **Format:** Excel (.xlsx, .xls) or CSV
- **Encoding:** UTF-8

## 📊 Column Details

| Column Name | Required | Type | Description | Example |
|------------|----------|------|-------------|---------|
| full_name | ✅ Yes | Text | Full name of pregnant woman | Priya Sharma |
| mobile_number | ✅ Yes | Text | 10-digit mobile number | 9876543210 |
| abha_id | ❌ No | Text | ABHA ID (14 digits with hyphens) | 12-3456-7890-1234 |
| rch_id | ❌ No | Text | RCH ID | RCH001 |
| husband_name | ❌ No | Text | Husband's name | Rajesh Sharma |
| age | ❌ No | Number | Age in years | 25 |
| address | ❌ No | Text | Full address | 123 Main Street Ward 1 |
| ward_id | ✅ Yes | Number | Ward ID (must exist in database) | 1 |
| hpr_id | ❌ No | Text | Healthcare Professional Registry ID | HPR001 |
| blood_group | ❌ No | Text | Blood group | O+, A+, B+, AB+, O-, A-, B-, AB- |

## ✅ Validation Rules

1. **Duplicate Check:**
   - ABHA ID must be unique
   - Mobile number must be unique

2. **Required Fields:**
   - full_name
   - mobile_number
   - ward_id

3. **Data Format:**
   - Mobile: 10 digits
   - Age: Positive number
   - Ward ID: Must exist in database

## 📝 Sample Data

```csv
full_name,mobile_number,abha_id,rch_id,husband_name,age,address,ward_id,hpr_id,blood_group
Priya Sharma,9876543210,12-3456-7890-1234,RCH001,Rajesh Sharma,25,123 Main Street Ward 1,1,HPR001,O+
Anita Verma,9876543211,12-3456-7890-1235,RCH002,Suresh Verma,28,456 Park Road Ward 2,2,HPR002,A+
```

## 🚀 How to Use

1. Download template: `pregnant_women_bulk_upload_template.csv`
2. Fill in your data (keep column headers)
3. Save as Excel (.xlsx) or CSV
4. Upload via API: `POST /api/v1/pregnant-women/bulk-upload`
5. Check response for success/failed/duplicate counts

## ⚠️ Important Notes

- Keep column headers exactly as shown
- Don't add extra columns
- Empty optional fields are allowed
- Duplicates will be skipped (not inserted)
- Errors will be logged per row
