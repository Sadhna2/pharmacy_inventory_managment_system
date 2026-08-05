/**
 * Indian states and union territories, by the two-letter code this system uses.
 *
 * The code is what decides the GST split: same state as the warehouse means
 * CGST + SGST, different means IGST. A free-text field here would eventually
 * produce "Mh" and "MH" as two different states and quietly tax an order the
 * wrong way, so every place a state is chosen picks from this list.
 *
 * Ordered by name so the picker reads alphabetically.
 */

export interface State {
  code: string;
  name: string;
  /**
   * The statutory *numeric* code, which is what a GSTIN opens with.
   *
   * Two codes for one state, because they answer to different masters: the
   * two-letter one is what people type and what every `state_code` column
   * holds; the numeric one is the first two characters of every GSTIN issued
   * in that state. A branch's registration has to start with its own, so the
   * form can name the digits to expect instead of waiting for a refusal.
   *
   * Mirrors `STATE_CODES` in `api/app/services/gst.py`, which is the
   * authority — this copy exists only to label a field before submission.
   */
  gstPrefix: string;
}

export const STATES: State[] = [
  { code: "AN", name: "Andaman & Nicobar Islands", gstPrefix: "35" },
  { code: "AP", name: "Andhra Pradesh", gstPrefix: "37" },
  { code: "AR", name: "Arunachal Pradesh", gstPrefix: "12" },
  { code: "AS", name: "Assam", gstPrefix: "18" },
  { code: "BR", name: "Bihar", gstPrefix: "10" },
  { code: "CH", name: "Chandigarh", gstPrefix: "04" },
  { code: "CG", name: "Chhattisgarh", gstPrefix: "22" },
  { code: "DH", name: "Dadra & Nagar Haveli and Daman & Diu", gstPrefix: "26" },
  { code: "DL", name: "Delhi", gstPrefix: "07" },
  { code: "GA", name: "Goa", gstPrefix: "30" },
  { code: "GJ", name: "Gujarat", gstPrefix: "24" },
  { code: "HR", name: "Haryana", gstPrefix: "06" },
  { code: "HP", name: "Himachal Pradesh", gstPrefix: "02" },
  { code: "JK", name: "Jammu & Kashmir", gstPrefix: "01" },
  { code: "JH", name: "Jharkhand", gstPrefix: "20" },
  { code: "KA", name: "Karnataka", gstPrefix: "29" },
  { code: "KL", name: "Kerala", gstPrefix: "32" },
  { code: "LA", name: "Ladakh", gstPrefix: "38" },
  { code: "LD", name: "Lakshadweep", gstPrefix: "31" },
  { code: "MP", name: "Madhya Pradesh", gstPrefix: "23" },
  { code: "MH", name: "Maharashtra", gstPrefix: "27" },
  { code: "MN", name: "Manipur", gstPrefix: "14" },
  { code: "ML", name: "Meghalaya", gstPrefix: "17" },
  { code: "MZ", name: "Mizoram", gstPrefix: "15" },
  { code: "NL", name: "Nagaland", gstPrefix: "13" },
  { code: "OD", name: "Odisha", gstPrefix: "21" },
  { code: "PY", name: "Puducherry", gstPrefix: "34" },
  { code: "PB", name: "Punjab", gstPrefix: "03" },
  { code: "RJ", name: "Rajasthan", gstPrefix: "08" },
  { code: "SK", name: "Sikkim", gstPrefix: "11" },
  { code: "TN", name: "Tamil Nadu", gstPrefix: "33" },
  { code: "TS", name: "Telangana", gstPrefix: "36" },
  { code: "TR", name: "Tripura", gstPrefix: "16" },
  { code: "UP", name: "Uttar Pradesh", gstPrefix: "09" },
  { code: "UK", name: "Uttarakhand", gstPrefix: "05" },
  { code: "WB", name: "West Bengal", gstPrefix: "19" },
];

const BY_CODE = new Map(STATES.map((s) => [s.code, s.name]));

/** Falls back to the raw code so unknown data still renders something. */
export const stateName = (code: string): string => BY_CODE.get(code) ?? code;
