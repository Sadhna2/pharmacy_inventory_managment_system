"""Generate the Colab notebook that benchmarks Unlimited-OCR.

    python3 bench/ocr/colab/make_notebook.py

The notebook is written rather than hand-edited because the escaping in .ipynb
JSON is miserable and every edit risks a corrupt file. Change the cells here.

WHY THIS RUNS ON COLAB AND NOT THE LAPTOP
-----------------------------------------
Unlimited-OCR is a 3B vision-language model whose only documented path is CUDA
12.9. The deployment target for this project is a 2 GB ARM t4g with no GPU, so
the model can never be the shipped OCR — this notebook exists to produce a
score for the comparison table, not a component.

TWO PROMPTS, BOTH SAVED RAW
---------------------------
The model is a *document parser*: its native output is markdown, not the JSON
schema the Gemini runner gets for free. So each invoice is run twice — once in
native mode, once asking for JSON — and BOTH raw strings are saved before any
parsing. Parsing is then a local, free, repeatable step. Getting the parser
wrong should cost nothing; re-running 50 pages on a borrowed GPU should not
have to happen twice.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent

# The four traps, identical to bench/ocr/run_gemini.py, so that a difference in
# the score reflects the model and not a kinder prompt.
PROMPT_JSON = r'''Read this Indian pharmaceutical distributor tax invoice and return ONLY JSON.

{"invoice_number":"","invoice_date":"YYYY-MM-DD","supplier_name":"","supplier_gstin":"",
 "drug_licence_no":"",
 "lines":[{"sn":1,"product_name":"","pack":"","batch_no":"","expiry_date":"YYYY-MM-DD",
           "quantity":0,"free_quantity":0,"mrp":0,"rate":0,"discount_pct":0}],
 "totals":{"taxable_amount":0,"cgst":0,"sgst":0,"igst":0,"grand_total":0}}

Rules:
1. Expiry is a MONTH. `08/27` means good through 2027-08-31, the LAST day.
   February 2028 ends on the 29th.
2. The expiry year is printed on the page. In `08-27` the `27` is the year
   2027, never a day. Never reuse the invoice's year.
3. Copy batch numbers character by character. Never change O to 0 or I to 1.
4. MRP is the printed ceiling price; rate is what the buyer pays and is lower.
5. Free goods (`10+1`, a FREE column) go in free_quantity, not quantity.
6. Every column belongs to one field. A narrow column wraps onto two lines --
   `1x200MD` becomes `1x20` above `0MD`. Both halves stay in their own column.
   Never carry characters sideways from the column next door.
Output JSON only, no markdown fence, no commentary.'''


def code(src: str) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": src.strip("\n").splitlines(keepends=True)}


def md(src: str) -> dict:
    return {"cell_type": "markdown", "metadata": {},
            "source": src.strip("\n").splitlines(keepends=True)}


CELLS = [
    md("""
# Unlimited-OCR on the pharmacy invoice benchmark

Scores Baidu's `Unlimited-OCR` (3B VLM, CUDA-only) against the same 50
synthetic Indian pharma invoices that `gemini-3.1-flash-lite` scored **88%
clean** on.

**Before running:** Runtime → Change runtime type → **T4 GPU**.

Run the cells in order. Cell 5 is a single-invoice smoke test — read its
output before launching all 50, because it tells you whether the model will
emit JSON at all or only markdown.
"""),

    md("## 1. Confirm the GPU\n\nA T4 is Turing, which has **no bfloat16**. "
       "The repo's example uses bfloat16, so the dtype is chosen from the "
       "card's compute capability rather than copied from the README."),
    code('''
import subprocess, torch
print(subprocess.run(["nvidia-smi","--query-gpu=name,memory.total",
                      "--format=csv,noheader"], capture_output=True, text=True).stdout.strip())
assert torch.cuda.is_available(), "No GPU. Runtime > Change runtime type > T4 GPU."
CAP = torch.cuda.get_device_capability()
DTYPE = torch.bfloat16 if CAP[0] >= 8 else torch.float16
print(f"compute capability {CAP[0]}.{CAP[1]}  ->  {DTYPE}")
print("bfloat16 needs Ampere (8.0+); on a T4 we use float16 instead.")
'''),

    md("## 2. Dependencies\n\nColab's preinstalled torch is kept — installing "
       "the repo's pinned `torch==2.10.0` risks a CUDA mismatch with the "
       "driver Colab actually has, and costs several minutes. Only the small "
       "pure-Python deps are pinned."),
    code('''
%pip install -q transformers==4.57.1 einops==0.8.2 addict==2.4.0 easydict==1.13 \\
                pymupdf==1.27.2.2 Pillow accelerate
import transformers; print("transformers", transformers.__version__)
print("torch", __import__("torch").__version__)
'''),

    md("## 3. Upload the invoices\n\nUpload `invoices_300dpi.zip` "
       "(50 PNGs, ~18 MB) from `bench/ocr/colab/`."),
    code('''
import zipfile, pathlib
from google.colab import files
up = files.upload()
name = next(iter(up))
zipfile.ZipFile(name).extractall("/content/invoices")
PNGS = sorted(pathlib.Path("/content/invoices").rglob("inv_*.png"))
print(f"{len(PNGS)} invoices ready")
assert len(PNGS) == 50, f"expected 50, got {len(PNGS)}"
'''),

    md("## 4. Load the model\n\n~6 GB download on Colab's connection. "
       "Flash-attention is not requested: it needs Ampere and would fail on a T4."),
    code('''
import torch, time
from transformers import AutoModel, AutoTokenizer

MODEL = "baidu/Unlimited-OCR"
t0 = time.time()
tok = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
try:
    model = AutoModel.from_pretrained(
        MODEL, trust_remote_code=True, use_safetensors=True,
        torch_dtype=DTYPE, attn_implementation="eager")
except TypeError:
    # Older remote code may not accept attn_implementation.
    model = AutoModel.from_pretrained(
        MODEL, trust_remote_code=True, use_safetensors=True, torch_dtype=DTYPE)
model = model.eval().cuda()
print(f"loaded in {time.time()-t0:.0f}s")
print(f"params {sum(p.numel() for p in model.parameters())/1e9:.2f}B")
print(f"VRAM   {torch.cuda.memory_allocated()/1e9:.1f} GB")
'''),

    md("""## 5. Smoke test — READ THIS OUTPUT

Runs one invoice in both modes. Two things to check:

- does `document parsing.` return a usable table?
- does the JSON prompt return parseable JSON, or does the model ignore it?

If JSON mode fails, that is a finding, not a blocker — the raw markdown is
saved either way and parsed locally afterwards.
"""),
    code(f'''
PROMPT_JSON = """{PROMPT_JSON}"""

def run(img_path, prompt):
    """The repo documents infer(tokenizer, prompt=..., image_file=...);
    fall back through the variants other releases have used."""
    for kwargs in ({{"prompt": prompt, "image_file": str(img_path)}},
                   {{"prompt": prompt, "image": str(img_path)}}):
        try:
            with torch.no_grad():
                return str(model.infer(tok, **kwargs))
        except TypeError:
            continue
    with torch.no_grad():
        return str(model.infer(tok, str(img_path), prompt))

import time
p = PNGS[0]
for label, prompt in (("NATIVE", "<image>document parsing."),
                      ("JSON", "<image>" + PROMPT_JSON)):
    t0 = time.time()
    out = run(p, prompt)
    print(f"===== {{label}}  ({{time.time()-t0:.1f}}s, {{len(out)}} chars) =====")
    print(out[:1800])
    print()
'''),

    md("## 6. Run all 50\n\nBoth modes per invoice, raw strings saved before "
       "any parsing. A failure on one invoice is recorded and the run continues."),
    code('''
import json, pathlib, time, traceback
OUT = pathlib.Path("/content/out"); OUT.mkdir(exist_ok=True)
(OUT/"raw").mkdir(exist_ok=True)

began, failures = time.time(), []
for i, p in enumerate(PNGS, 1):
    rec = {"invoice_id": p.stem}
    for key, prompt in (("native", "<image>document parsing."),
                        ("json", "<image>" + PROMPT_JSON)):
        try:
            t0 = time.time()
            rec[key] = run(p, prompt)
            rec[f"{key}_seconds"] = round(time.time()-t0, 1)
        except Exception as exc:
            rec[key] = ""
            rec[f"{key}_error"] = f"{type(exc).__name__}: {exc}"
            failures.append((p.stem, key))
    (OUT/"raw"/f"{p.stem}.json").write_text(json.dumps(rec, indent=1))
    print(f"  {i:>2}/50  {p.stem}  native {rec.get('native_seconds','-')}s  "
          f"json {rec.get('json_seconds','-')}s", flush=True)

print(f"\\ndone in {(time.time()-began)/60:.1f} min")
if failures:
    print(f"{len(failures)} calls failed: {failures[:10]}")
'''),

    md("## 7. Download\n\nUnzip into `bench/ocr/out/raw/unlimited-ocr/`, then "
       "run `python bench/ocr/parse_unlimited.py` and `score.py` locally. "
       "Parsing happens off the GPU so it can be fixed and repeated for free."),
    code('''
import shutil
from google.colab import files
shutil.make_archive("/content/unlimited_ocr_raw", "zip", "/content/out")
files.download("/content/unlimited_ocr_raw.zip")
'''),
]

nb = {
    "nbformat": 4, "nbformat_minor": 0,
    "metadata": {
        "colab": {"provenance": [], "gpuType": "T4"},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
        "accelerator": "GPU",
    },
    "cells": CELLS,
}

dest = HERE / "unlimited_ocr_benchmark.ipynb"
dest.write_text(json.dumps(nb, indent=1))
print(f"wrote {dest}  ({dest.stat().st_size/1024:.0f} KB, {len(CELLS)} cells)")
