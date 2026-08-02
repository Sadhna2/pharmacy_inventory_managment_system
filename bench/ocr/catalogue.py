"""Source material for synthetic distributor invoices.

Everything here is modelled on what a real Indian pharma distributor invoice
actually carries — the abbreviations, the pack notations, the licence numbers.
The point is not to make pretty documents; it is to reproduce the specific
things that break OCR on this document class:

  * expiry printed as MM/YY, so "08/27" must become 2027-08-31, and "8/27",
    "08-27" and "AUG27" all mean the same date
  * batch numbers mixing letters and digits where O/0 and I/1 are ambiguous
  * a `Free` column — scheme goods that arrive but are not invoiced, and which
    must still be received into stock
  * `Rate` (what we pay) sitting next to `MRP` (what the customer may pay),
    which a model that has only seen retail receipts will happily confuse
"""

# --- products ---------------------------------------------------------------
# (display name, pack, hsn, gst_rate, mrp_range, typical abbreviation)
PRODUCTS = [
    ("PARACETAMOL 650MG TAB", "10x10", "3004", 12, (28, 45), "PCM-650 TAB"),
    ("AMOXYCILLIN 500MG CAP", "10x10", "3004", 12, (85, 140), "AMOXY-500 CAP"),
    ("AZITHROMYCIN 500MG TAB", "1x3", "3004", 12, (95, 165), "AZITHRO-500"),
    ("PANTOPRAZOLE 40MG TAB", "10x15", "3004", 12, (110, 180), "PANTOP-40 TAB"),
    ("METFORMIN 500MG TAB", "10x15", "3004", 12, (32, 58), "METFOR-500"),
    ("GLIMEPIRIDE 2MG TAB", "10x10", "3004", 12, (68, 96), "GLIMI-2 TAB"),
    ("ATORVASTATIN 10MG TAB", "10x10", "3004", 12, (72, 118), "ATORVA-10"),
    ("TELMISARTAN 40MG TAB", "10x10", "3004", 12, (88, 145), "TELMA-40 TAB"),
    ("AMLODIPINE 5MG TAB", "10x10", "3004", 12, (26, 48), "AMLO-5 TAB"),
    ("CETIRIZINE 10MG TAB", "10x10", "3004", 12, (18, 34), "CETZINE-10"),
    ("MONTELUKAST 10MG TAB", "10x10", "3004", 12, (105, 175), "MONTEK-10"),
    ("SALBUTAMOL INHALER", "1x200MD", "3004", 12, (145, 235), "ASTHALIN INH"),
    ("INSULIN GLARGINE 100IU", "1x3ML", "3004", 5, (680, 940), "GLARGINE 3ML"),
    ("HUMAN INSULIN 40IU", "1x10ML", "3004", 5, (155, 240), "ACTRAPID 10ML"),
    ("ORS SACHET ORANGE", "1x21.8G", "3004", 5, (18, 28), "ORS-O SACHET"),
    ("VITAMIN D3 60K IU", "1x4", "3004", 12, (48, 82), "D3-60K SACHET"),
    ("CALCIUM + VIT D3 TAB", "10x15", "3004", 12, (95, 155), "CALCI-D TAB"),
    ("IRON + FOLIC ACID TAB", "10x10", "3004", 12, (42, 68), "FEFOL TAB"),
    ("PANTOPRAZOLE INJ 40MG", "1x1", "3004", 12, (32, 58), "PANTOP INJ"),
    ("CEFTRIAXONE 1GM INJ", "1x1", "3004", 12, (48, 92), "CEFTRI-1G INJ"),
    ("DICLOFENAC GEL 30G", "1x30G", "3004", 12, (68, 105), "DICLO GEL"),
    ("POVIDONE IODINE 100ML", "1x100ML", "3004", 12, (78, 122), "BETADINE 100"),
    ("ABSORBENT COTTON 500G", "1x500G", "3005", 12, (155, 235), "COTTON ROLL"),
    ("SURGICAL GLOVES M", "1x50", "4015", 12, (285, 420), "GLOVES-M 50S"),
    ("DIGITAL THERMOMETER", "1x1", "9025", 18, (145, 260), "DIGI THERMO"),
    ("N95 MASK", "1x20", "6307", 5, (240, 380), "N95 MASK 20S"),
    ("GLUCOMETER STRIPS", "1x50", "3822", 12, (620, 890), "GLUCO STRIP 50"),
    ("COUGH SYRUP 100ML", "1x100ML", "3004", 12, (88, 132), "COUGH SYP 100"),
    ("ANTACID SUSPENSION", "1x200ML", "3004", 12, (105, 158), "ANTACID SUSP"),
    ("OMEPRAZOLE 20MG CAP", "10x10", "3004", 12, (55, 88), "OMEZ-20 CAP"),
]

# --- distributors -----------------------------------------------------------
# (name, city, state_code, gstin_prefix, style of address block)
DISTRIBUTORS = [
    ("SHREE PHARMA DISTRIBUTORS", "Mumbai", "27", "27AAACS", "Bhiwandi, Thane"),
    ("MEDLINK HEALTHCARE PVT LTD", "Pune", "27", "27AABCM", "Bhosari MIDC, Pune"),
    ("GUJARAT MEDICO AGENCIES", "Ahmedabad", "24", "24AAFCG", "Naroda GIDC"),
    ("KRISHNA DRUG HOUSE", "Nagpur", "27", "27AAHCK", "Itwari, Nagpur"),
    ("APEX PHARMA TRADERS", "Surat", "24", "24AACCA", "Ring Road, Surat"),
    ("SANJEEVANI MEDICAL STORES", "Nashik", "27", "27AADCS", "Satpur, Nashik"),
    ("NOVA HEALTHCARE SUPPLIES", "Bengaluru", "29", "29AAGCN", "Peenya, Bengaluru"),
    ("RELIEF PHARMA AGENCIES", "Indore", "23", "23AABCR", "Sanwer Road, Indore"),
    ("UNITY DRUG DISTRIBUTORS", "Hyderabad", "36", "36AACCU", "Balanagar, Hyd"),
    ("SIDDHI VINAYAK MEDICOS", "Mumbai", "27", "27AAKCS", "Ghatkopar West"),
]

MANUFACTURERS = [
    "SUN PHARMA", "CIPLA LTD", "DR REDDYS LAB", "MANKIND PHARMA",
    "TORRENT PHARMA", "ALKEM LABS", "LUPIN LTD", "ZYDUS HEALTHCARE",
    "GLENMARK PHARMA", "USV PVT LTD", "INTAS PHARMA", "ABBOTT INDIA",
]

# --- format variation -------------------------------------------------------

#: Layout skeletons. Each is a distinct visual document, not a restyle.
LAYOUTS = ["classic", "boxed", "minimal", "dense", "twocol"]

#: Typeface families a print shop in India would actually use.
FONTS = [
    ("'Times New Roman', Times, serif", "serif"),
    ("Arial, Helvetica, sans-serif", "sans"),
    ("'Arial Narrow', Arial, sans-serif", "condensed"),
    ("'Courier New', Courier, monospace", "mono"),
    ("Verdana, Geneva, sans-serif", "wide"),
]

#: How tax is presented. All three are common and each hides the numbers
#: somewhere different.
TAX_STYLES = [
    "split_columns",   # CGST% CGST₹ SGST% SGST₹ per line
    "single_column",   # one GST% column, tax only in the footer summary
    "footer_summary",  # no tax on lines at all, HSN-wise table at the bottom
]

#: Column sets. A model that hardcodes column order fails on half of these.
COLUMN_SETS = [
    ["sn", "product", "pack", "batch", "exp", "qty", "free", "mrp", "rate", "amount"],
    ["sn", "product", "batch", "exp", "pack", "qty", "rate", "mrp", "disc", "amount"],
    ["product", "pack", "hsn", "batch", "exp", "qty", "free", "rate", "disc", "amount"],
    ["sn", "product", "hsn", "batch", "exp", "qty", "mrp", "rate", "amount"],
    ["sn", "product", "pack", "batch", "exp", "qty", "free", "rate", "amount"],
]

#: Date renderings. "08/27" and "AUG-27" mean the same month.
EXPIRY_FORMATS = ["mm/yy", "mm-yy", "mon-yy", "mm/yyyy"]
INVOICE_DATE_FORMATS = ["dd/mm/yyyy", "dd-mm-yyyy", "dd.mm.yyyy", "dd-Mon-yyyy"]

#: Scan degradation, applied after rendering.
NOISE_PROFILES = [
    "clean",       # born-digital PDF, best case
    "scan_light",  # flatbed scan, slight grey cast
    "scan_heavy",  # photocopy of a photocopy
    "photo",       # phone camera: skew, uneven light, JPEG artifacts
]
