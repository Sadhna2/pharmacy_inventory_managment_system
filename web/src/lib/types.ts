/**
 * The API contract, derived from the backend rather than restated by hand.
 *
 * Every type below is an alias onto `schema.d.ts`, which is GENERATED from the
 * live OpenAPI document by `npm run gen:api`. Nothing here describes a shape;
 * it only gives the generated shapes the names this codebase already uses.
 *
 * Why it works this way
 * ---------------------
 * These types used to be written out by hand, which meant the same field was
 * declared twice — once in a Pydantic model and once here — with nothing
 * connecting the two. Rename `qty_received` on the server and this file kept
 * saying `qty_received`, so TypeScript went on checking against a copy that was
 * no longer true, the build passed, and the column rendered blank in the
 * browser. The failure surfaced in front of a person, not in CI.
 *
 * Now a server-side rename changes `schema.d.ts`, the alias below changes with
 * it, and every call site still using the old name fails to compile. CI
 * regenerates the file and fails if it differs from the committed one
 * (`.github/workflows/ci.yml`), so the two cannot drift apart silently.
 *
 * Adding a field? Change the Pydantic model, run `npm run gen:api`, commit the
 * regenerated `schema.d.ts`. Do not hand-edit either file.
 */
import type { components } from "./schema";

type S = components["schemas"];

// --- enumerations -----------------------------------------------------------
// These arrive as real unions rather than `string` because the backend declares
// them as Python enums or Literals. A plain `str` on the server would widen to
// `string` here and quietly cost every exhaustiveness check in the app.

export type TrackingMode = S["TrackingMode"];
export type StockStatus = S["StockStatus"];
export type DrugSchedule = S["DrugSchedule"];
export type StorageCondition = S["StorageCondition"];
export type DocumentStatus = S["DocumentStatus"];
export type SourcingPolicy = S["SourcingPolicy"];
export type MovementType = S["MovementType"];
export type RecallStatus = S["RecallStatus"];

/** The four role codes. Backed by a `Literal` in `core/permissions.py`. */
export type RoleCode = S["UserOut"]["role"];

// --- envelope ---------------------------------------------------------------

/**
 * The pagination envelope.
 *
 * Hand-written because it is generic, and FastAPI emits a separate concrete
 * schema per item type (`Page_ProductOut_`, `Page_MovementOut_`, …) which
 * cannot be expressed as one generic alias. The shape is asserted against a
 * generated one at the bottom of this file, so it is still drift-checked.
 */
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

// --- identity ---------------------------------------------------------------

export type User = S["UserOut"];
export type LoginResponse = S["LoginResponse"];
export type Role = S["RoleOut"];
export type ManagedUser = S["UserListOut"];

// --- master data ------------------------------------------------------------

export type Product = S["ProductOut"];
export type Category = S["CategoryOut"];
export type Uom = S["UomOut"];
export type Warehouse = S["WarehouseOut"];
export type Bin = S["BinOut"];
export type Supplier = S["SupplierOut"];
export type Customer = S["CustomerOut"];

// --- stock ------------------------------------------------------------------

export type Balance = S["BalanceOut"];
export type Movement = S["MovementOut"];
export type StockSummary = S["StockSummary"];
export type ExpiringStock = S["ExpiringStockOut"];
export type Lot = S["LotOut"];

// --- documents --------------------------------------------------------------

/**
 * The GST fields every priced line carries.
 *
 * Derived from a purchase-order line rather than declared, so the tax shape
 * cannot drift from the one the server actually sends.
 */
export type TaxLine = Pick<
  S["POLineOut"],
  | "taxable_value"
  | "gst_rate"
  | "cgst_amount"
  | "sgst_amount"
  | "igst_amount"
  | "line_total"
>;

export type PurchaseOrderLine = S["POLineOut"];
export type PurchaseOrder = S["PurchaseOrderOut"];
export type GoodsReceipt = S["GoodsReceiptOut"];
export type GoodsReceiptLine = S["GRNLineOut"];
export type SalesOrderLine = S["SOLineOut"];
export type SalesOrder = S["SalesOrderOut"];
export type Allocation = S["AllocationOut"];
export type Shipment = S["ShipmentOut"];
export type TransferLine = S["TransferLineOut"];
export type Transfer = S["TransferOut"];
export type AdjustmentLine = S["AdjustmentLineOut"];
export type Adjustment = S["AdjustmentOut"];

// --- recalls and audit ------------------------------------------------------

export type Recall = S["RecallOut"];
export type RecallImpact = S["RecallImpactOut"];
export type LocationImpact = S["LocationImpactOut"];
export type CustomerImpact = S["CustomerImpactOut"];
export type AuditEntry = S["AuditLogOut"];
export type AuditFacets = S["AuditFacetsOut"];
export type AuditActor = S["AuditActorOut"];

// --- analysis (Layer 2) -----------------------------------------------------

export type LeadTimeStats = S["LeadTimeStatsOut"];
export type LeadTimeList = S["LeadTimeOut"];
export type LeadTimeDetail = S["LeadTimeDetailOut"];
export type SupplierDelivery = S["SupplierDeliveryOut"];
export type SupplierProduct = S["SupplierProductOut"];

export type Anomaly = S["AnomalyOut"];
export type AnomalyReport = S["AnomalyReportOut"];
export type AnomalySummary = S["AnomalySummaryOut"];

export type DemandForecast = S["ForecastOut"];
export type ForecastList = S["ForecastListOut"];
export type ForecastAccuracy = S["AccuracyOut"];

export type Recommendation = S["RecommendationOut"];
export type ReorderReport = S["ReorderReportOut"];
export type ReorderSummary = S["ReorderSummaryOut"];
export type ReorderSourcing = S["SourcingOut"];
export type DraftOrder = S["DraftOrderOut"];

// --- settings ---------------------------------------------------------------

export type Tunable = S["TunableOut"];
export type FeatureFlag = S["FeatureOut"];
export type SettingsGroup = S["SettingsGroupOut"];
export type AppSettings = S["SettingsOut"];

// --- request bodies ---------------------------------------------------------
// Exported so a form's payload is checked against what the endpoint accepts,
// not merely against what the developer remembered it accepts.

export type ProductIn = S["ProductIn"];
export type ProductUpdate = S["ProductUpdate"];
export type PurchaseOrderIn = S["PurchaseOrderIn"];
export type GoodsReceiptIn = S["GoodsReceiptIn"];
export type SalesOrderIn = S["SalesOrderIn"];
export type TransferIn = S["TransferIn"];
export type AdjustmentIn = S["AdjustmentIn"];
export type MovementIn = S["MovementIn"];
export type LotIn = S["LotIn"];
export type RecallIn = S["RecallIn"];
export type SettingsUpdateIn = S["SettingsUpdateIn"];
export type UserCreate = S["UserCreate"];
export type UserUpdate = S["UserUpdate"];

// --- drift guards -----------------------------------------------------------
// Compile-time only; these produce no JavaScript.

/** Fails to compile if `A` and `B` are not the same type. */
type Exact<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
type Assert<T extends true> = T;

/**
 * The generic `Page<T>` above is the one shape in this file not taken straight
 * from the schema. This pins it to a generated one, so a change to the
 * pagination envelope on the server breaks the build here rather than going
 * unnoticed until a table renders empty.
 */
export type _PageEnvelopeMatches = Assert<
  Exact<Page<Product>, S["Page_ProductOut_"]>
>;
