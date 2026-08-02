export type TrackingMode = "NONE" | "LOT" | "LOT_EXPIRY" | "SERIAL";
export type StockStatus =
  | "AVAILABLE"
  | "QUARANTINE"
  | "DAMAGED"
  | "IN_TRANSIT"
  | "RETURNED_PENDING";
export type DrugSchedule = "OTC" | "G" | "H" | "H1" | "X";
export type StorageCondition = "AMBIENT" | "COOL" | "COLD_CHAIN" | "FROZEN";
export type DocumentStatus =
  | "DRAFT"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "PARTIALLY_RECEIVED"
  | "RECEIVED"
  | "IN_TRANSIT"
  | "ALLOCATED"
  | "PICKED"
  | "SHIPPED"
  | "COMPLETED"
  | "CANCELLED";

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

export interface User {
  id: number;
  email: string;
  full_name: string;
  role: "ADMIN" | "MANAGER" | "STAFF" | "CUSTOMER";
  permissions: string[];
  warehouse_id: number | null;
  warehouse_name: string | null;
  is_active: boolean;
}

export interface LoginResponse {
  access_token: string;
  expires_in: number;
  user: User;
}

export type SourcingPolicy = "VIA_CENTRAL" | "DIRECT" | "EITHER";

export interface Product {
  id: number;
  sku: string;
  name: string;
  description: string | null;
  category_id: number | null;
  category_name: string | null;
  uom_id: number;
  uom_code: string | null;
  tracking_mode: TrackingMode;
  composition: string | null;
  manufacturer: string | null;
  pack_size: string | null;
  drug_schedule: DrugSchedule;
  storage_condition: StorageCondition;
  is_prescription_required: boolean;
  hsn_code: string | null;
  gst_rate: string;
  barcode: string | null;
  reorder_point: string;
  safety_stock_days: number;
  sourcing_policy: SourcingPolicy;
  mrp: string | null;
  is_active: boolean;
  qty_on_hand: string | null;
  qty_available: string | null;
}

export interface Category {
  id: number;
  name: string;
  parent_id: number | null;
  is_active: boolean;
  product_count: number;
}

export interface Uom {
  id: number;
  code: string;
  name: string;
  product_count: number;
}

export interface Warehouse {
  id: number;
  code: string;
  name: string;
  is_central: boolean;
  state_code: string;
  address: string | null;
  is_active: boolean;
}

export interface Bin {
  id: number;
  warehouse_id: number;
  code: string;
  zone: string | null;
  is_cold_chain: boolean;
  is_quarantine: boolean;
  is_active: boolean;
}

export interface Supplier {
  id: number;
  code: string;
  name: string;
  gstin: string | null;
  state_code: string;
  contact_person: string | null;
  phone: string | null;
  email: string | null;
  address: string | null;
  payment_terms_days: number;
  is_active: boolean;
}

export interface Customer {
  id: number;
  code: string;
  name: string;
  is_institutional: boolean;
  gstin: string | null;
  state_code: string;
  phone: string | null;
  email: string | null;
  address: string | null;
  credit_limit: string;
  is_active: boolean;
}

export interface Balance {
  product_id: number;
  sku: string;
  product_name: string;
  warehouse_id: number;
  warehouse_name: string;
  bin_id: number | null;
  bin_code: string | null;
  lot_id: number | null;
  lot_code: string | null;
  expiry_date: string | null;
  /** The batch's own printed price; falls back to the product's. */
  mrp: string | null;
  status: StockStatus;
  qty_on_hand: string;
  qty_reserved: string;
  qty_available: string;
}

export interface Movement {
  id: number;
  movement_type: string;
  product_id: number;
  sku: string | null;
  product_name: string | null;
  warehouse_id: number;
  warehouse_name: string | null;
  bin_id: number | null;
  lot_id: number | null;
  lot_code: string | null;
  status: StockStatus;
  quantity: string;
  unit_cost: string | null;
  reference_type: string | null;
  reference_id: number | null;
  /** Set once a later entry corrects this one — a row can only be reversed once. */
  reversed_by_id: number | null;
  occurred_at: string;
  created_by: number;
  created_by_name: string | null;
  notes: string | null;
}

export interface StockSummary {
  total_skus: number;
  total_units: string;
  below_reorder_point: number;
  expiring_30_days: number;
  expired_on_hand: number;
  quarantined_units: string;
  in_transit_units: string;
  stock_value: string | null;
}

export interface ExpiringStock {
  product_id: number;
  sku: string;
  product_name: string;
  warehouse_id: number;
  warehouse_name: string;
  lot_id: number;
  lot_code: string;
  expiry_date: string;
  qty_on_hand: string;
  days_to_expiry: number;
}

export interface Lot {
  id: number;
  product_id: number;
  lot_code: string;
  mfg_date: string | null;
  expiry_date: string | null;
  supplier_id: number | null;
  received_at: string;
  days_to_expiry: number | null;
}

export interface TaxLine {
  taxable_value: string;
  gst_rate: string;
  cgst_amount: string;
  sgst_amount: string;
  igst_amount: string;
  line_total: string;
}

export interface PurchaseOrderLine extends TaxLine {
  id: number;
  product_id: number;
  sku: string | null;
  product_name: string | null;
  /** Lets the receiving form know which lines need a batch number. */
  tracking_mode: TrackingMode | null;
  qty_ordered: string;
  qty_received: string;
  unit_price: string;
}

export interface PurchaseOrder {
  id: number;
  po_number: string;
  supplier_id: number;
  supplier_name: string | null;
  warehouse_id: number;
  warehouse_name: string | null;
  status: DocumentStatus;
  order_date: string;
  expected_date: string | null;
  notes: string | null;
  created_by: number;
  approved_by: number | null;
  subtotal: string;
  tax_total: string;
  round_off: string;
  grand_total: string;
  is_interstate: boolean;
  place_of_supply: string | null;
  lines: PurchaseOrderLine[];
}

export interface SalesOrderLine extends TaxLine {
  id: number;
  product_id: number;
  sku: string | null;
  product_name: string | null;
  qty_ordered: string;
  qty_shipped: string;
  unit_price: string;
}

export interface SalesOrder {
  id: number;
  so_number: string;
  customer_id: number;
  customer_name: string | null;
  warehouse_id: number;
  warehouse_name: string | null;
  status: DocumentStatus;
  order_date: string;
  notes: string | null;
  subtotal: string;
  tax_total: string;
  round_off: string;
  grand_total: string;
  is_interstate: boolean;
  place_of_supply: string | null;
  lines: SalesOrderLine[];
}

export interface Allocation {
  product_id: number;
  sku: string | null;
  product_name: string | null;
  lot_id: number | null;
  lot_code: string | null;
  expiry_date: string | null;
  quantity: string;
  /** Printed on the batch FEFO picked — not necessarily the order's price. */
  mrp: string | null;
}

export interface Shipment {
  id: number;
  shipment_number: string;
  sales_order_id: number;
  shipped_at: string;
  shipped_by: number;
  lines: Allocation[];
}

export interface TransferLine {
  id: number;
  product_id: number;
  sku: string | null;
  product_name: string | null;
  lot_id: number | null;
  lot_code: string | null;
  quantity: string;
  qty_received: string;
}

export interface Transfer {
  id: number;
  transfer_number: string;
  from_warehouse_id: number;
  from_warehouse_name: string | null;
  to_warehouse_id: number;
  to_warehouse_name: string | null;
  status: DocumentStatus;
  dispatched_at: string | null;
  received_at: string | null;
  notes: string | null;
  lines: TransferLine[];
}

export interface AdjustmentLine {
  id: number;
  product_id: number;
  sku: string | null;
  product_name: string | null;
  lot_id: number | null;
  quantity: string;
}

export interface Adjustment {
  id: number;
  adjustment_number: string;
  warehouse_id: number;
  reason_code: string;
  status: DocumentStatus;
  created_by: number;
  approved_by: number | null;
  notes: string | null;
  lines: AdjustmentLine[];
}

export interface RecallImpact {
  recall_id: number;
  lot_code: string;
  product_sku: string;
  product_name: string;
  expiry_date: string | null;
  total_quarantined: string;
  locations: {
    warehouse_id: number;
    warehouse_name: string;
    qty_quarantined: string;
  }[];
  customers: {
    customer_id: number;
    customer_name: string;
    quantity: string;
    shipment_numbers: string[];
  }[];
}

export interface Recall {
  id: number;
  lot_id: number;
  lot_code: string | null;
  product_sku: string | null;
  reason: string;
  regulator_ref: string | null;
  status: "INITIATED" | "QUARANTINED" | "CLOSED";
  initiated_at: string;
  closed_at: string | null;
  qty_quarantined: string;
}

export interface AuditEntry {
  id: number;
  action: string;
  entity_type: string;
  entity_id: number | null;
  actor_user_id: number | null;
  actor_name: string | null;
  actor_email: string | null;
  /** Field values either side of the change. Null when nothing was modified. */
  before_json: Record<string, unknown> | null;
  after_json: Record<string, unknown> | null;
  ip: string | null;
  request_id: string | null;
  created_at: string;
}

export interface AuditFacets {
  actions: string[];
  entity_types: string[];
  actors: { id: number; full_name: string; email: string }[];
}

export interface Role {
  id: number;
  code: "ADMIN" | "MANAGER" | "STAFF" | "CUSTOMER";
  name: string;
  description: string | null;
  permissions: string[];
}

export interface ManagedUser {
  id: number;
  email: string;
  full_name: string;
  role_id: number;
  role_code: "ADMIN" | "MANAGER" | "STAFF" | "CUSTOMER";
  role_name: string;
  warehouse_id: number | null;
  warehouse_name: string | null;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
}

/* --------------------------------------------------------- Layer 2: analysis */

export interface LeadTimeStats {
  supplier_id: number;
  supplier_name: string;
  deliveries: number;
  /** The typical wait. Plan dates on this. */
  median_days: number;
  /** Nine in ten land by here. Size safety cover on this. */
  p90_days: number;
  mean_days: number;
  std_dev: number;
  min_days: number;
  max_days: number;
  on_time_rate: number;
  /** Recent third minus oldest third, in days. Positive means slowing. */
  trend_days: number;
  reliable: boolean;
  verdict: string;
}

export interface SupplierDelivery {
  po_id: number;
  po_number: string;
  ordered: string;
  promised: string | null;
  received: string;
  days: number;
  late_by: number | null;
}

export interface SupplierProduct {
  product_id: number;
  sku: string;
  product_name: string;
  receipts: number;
  units: number;
}

export interface LeadTimeList {
  as_of: string;
  lookback_days: number;
  suppliers: LeadTimeStats[];
}

export interface LeadTimeDetail {
  as_of: string;
  lookback_days: number;
  stats: LeadTimeStats;
  expected_date: string;
  plan_for_date: string;
  safety_days: number;
  products: SupplierProduct[];
  deliveries: SupplierDelivery[];
}

export interface Anomaly {
  key: string;
  kind: "consumption" | "shrinkage" | "write_off" | "after_hours" | "repeat_loss";
  severity: "high" | "medium" | "low";
  occurred_at: string;
  product_id: number | null;
  product_name: string;
  sku: string;
  warehouse_id: number;
  warehouse_name: string;
  quantity: number;
  /** Rupees at cost. Zero where the finding is not a loss. */
  value: number;
  /** Robust z-score; 0 for the rule-based detectors. */
  score: number;
  explanation: string;
  baseline: Record<string, unknown>;
  movement_ids: number[];
}

export interface AnomalyReport {
  lookback_days: number;
  summary: {
    total: number;
    high: number;
    medium: number;
    low: number;
    by_kind: Record<string, number>;
    value_at_risk: number;
  };
  anomalies: Anomaly[];
}

export interface ForecastAccuracy {
  method: string;
  /** Mean absolute error, in units. */
  mae: number;
  /** Total error over total actual — 0.18 means off by 18% of the volume. */
  wape: number;
  hit_rate: number;
}

export interface DemandForecast {
  product_id: number;
  sku: string;
  product_name: string;
  warehouse_id: number;
  warehouse_name: string;
  method: string;
  confidence: "high" | "medium" | "low";
  start: string;
  daily: number[];
  lower: number[];
  upper: number[];
  total: number;
  daily_mean: number;
  accuracy: ForecastAccuracy;
  alternatives: ForecastAccuracy[];
  history_days: number;
  stockout_days: number;
}

export interface ForecastList {
  horizon_days: number;
  generated_for: string;
  forecasts: DemandForecast[];
}

export interface ReorderSourcing {
  supplier_id: number | null;
  supplier_name: string;
  lead_time_days: number;
  lead_time_sd: number;
  p90_days: number;
  /** True when measured from real deliveries rather than the supplier's quote. */
  measured: boolean;
  unit_cost: number;
  moq: number;
  pack_qty: number;
  via_central: boolean;
}

export interface Recommendation {
  key: string;
  product_id: number;
  sku: string;
  product_name: string;
  warehouse_id: number;
  warehouse_name: string;
  on_hand: number;
  on_order: number;
  position: number;
  drafted_qty: number;
  draft_po_numbers: string[];
  daily_demand: number;
  forecast_confidence: "high" | "medium" | "low";
  forecast_method: string;
  lead_time_days: number;
  safety_stock: number;
  reorder_point: number;
  order_up_to: number;
  suggested_qty: number;
  days_of_cover: number;
  stockout_date: string | null;
  urgency: "stockout" | "critical" | "soon" | "ok";
  service_level: "critical" | "high" | "standard";
  sourcing: ReorderSourcing;
  estimated_cost: number;
  reason: string;
  workings: Record<string, unknown>;
}

export interface DraftOrder {
  supplier_id: number;
  supplier_name: string;
  warehouse_id: number;
  warehouse_name: string;
  lines: number;
  units: number;
  estimated_cost: number;
  items: Recommendation[];
}

export interface ReorderReport {
  generated_for: string;
  horizon_days: number;
  summary: {
    total: number;
    stockout: number;
    critical: number;
    soon: number;
    estimated_cost: number;
    lines: number;
  };
  recommendations: Recommendation[];
  draft_orders: DraftOrder[];
}

export interface Tunable {
  key: string;
  label: string;
  help: string;
  kind: "bool" | "int" | "float" | "time";
  group: string;
  unit: string | null;
  minimum: number | null;
  maximum: number | null;
  default: unknown;
  value: unknown;
  /** True when an administrator has moved it off the shipped default. */
  is_overridden: boolean;
}

export interface FeatureFlag {
  key: string;
  label: string;
  description: string;
  is_enabled: boolean;
  /** False for capabilities that are designed but not built. */
  is_implemented: boolean;
  updated_at: string | null;
}

export interface SettingsGroup {
  key: string;
  label: string;
  tunables: Tunable[];
}

export interface AppSettings {
  features: FeatureFlag[];
  groups: SettingsGroup[];
}
