/**
 * App shell: sidebar on desktop, slide-over drawer on mobile.
 *
 * Navigation is filtered by permission, so a staff user simply does not see
 * approvals or recalls. That is UX — the server enforces the same rules
 * independently on every request.
 *
 * Layout: the page itself never scrolls. The shell is exactly one viewport
 * tall and the main column is its own scroll container, so the sidebar stays
 * put while a 200-row stock table scrolls beside it. Scrolling the whole
 * document instead would carry the navigation off the top of the screen —
 * which is precisely when someone wants to click it.
 *
 * The sidebar collapses to an icon rail. The choice is remembered, because
 * having to re-collapse it on every visit is worse than not offering it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  ArrowLeftRight,
  Building2,
  ChartSpline,
  MessagesSquare,
  ClipboardList,
  Cog,
  Layers,
  LayoutDashboard,
  LogOut,
  Menu,
  Package,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  ScrollText,
  ShieldAlert,
  ShoppingCart,
  Siren,
  SlidersHorizontal,
  Timer,
  Truck,
  UsersRound,
  X,
} from "lucide-react";
import { useAuth } from "@/lib/auth";
import { useFeatures } from "@/lib/hooks";
import { cn } from "@/lib/format";
import { Button } from "./ui";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  permission?: string;
  /**
   * Hidden when an administrator has switched this capability off.
   *
   * Cosmetic only — the endpoint refuses independently, so a stale tab or a
   * pasted URL gets a 404 either way. This just stops the rail offering a
   * screen that would immediately fail.
   */
  feature?: string;
}

const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: "Overview",
    // Gated, unlike before: every figure on it comes from the stock endpoints.
    // A customer account is refused all three, so an ungated Dashboard offered
    // them a screen of em dashes as the first thing they saw.
    items: [
      {
        to: "/",
        label: "Dashboard",
        icon: LayoutDashboard,
        permission: "stock.view",
      },
    ],
  },
  {
    section: "Inventory",
    items: [
      { to: "/products", label: "Products", icon: Package, permission: "product.view" },
      { to: "/stock", label: "Stock", icon: Layers, permission: "stock.view" },
      { to: "/movements", label: "Movements", icon: ClipboardList, permission: "stock.view" },
    ],
  },
  {
    section: "Operations",
    items: [
      { to: "/purchase-orders", label: "Purchasing", icon: ShoppingCart, permission: "po.view" },
      { to: "/sales-orders", label: "Sales", icon: Truck, permission: "so.view" },
      { to: "/transfers", label: "Transfers", icon: ArrowLeftRight, permission: "transfer.view" },
      { to: "/adjustments", label: "Adjustments", icon: SlidersHorizontal, permission: "stock.view" },
    ],
  },
  {
    // Layer 2 reads the history the sections above write. Nothing here changes
    // stock, so it all sits behind one permission: ai.view.
    section: "Analysis",
    items: [
      // First because it is the least specialised: the other four answer a
      // question somebody already knew to ask. A speech bubble rather than a
      // sparkle — nothing here is magic, and the icon should not promise it.
      { to: "/ask", label: "Ask", icon: MessagesSquare, permission: "ai.view", feature: "features.nl_reporting" },
      { to: "/replenishment", label: "Replenishment", icon: RefreshCw, permission: "ai.view", feature: "features.reorder" },
      { to: "/forecast", label: "Demand forecast", icon: ChartSpline, permission: "ai.view", feature: "features.forecast" },
      { to: "/exceptions", label: "Exceptions", icon: Siren, permission: "ai.view", feature: "features.anomaly" },
      { to: "/lead-times", label: "Supplier lead times", icon: Timer, permission: "ai.view", feature: "features.leadtime" },
    ],
  },
  {
    section: "Compliance",
    items: [
      { to: "/recalls", label: "Batch Recalls", icon: ShieldAlert, permission: "stock.view" },
      { to: "/audit", label: "Audit trail", icon: ScrollText, permission: "audit.view" },
    ],
  },
  {
    section: "Setup",
    items: [
      { to: "/master-data", label: "Master data", icon: Building2, permission: "master.view" },
      { to: "/users", label: "Users", icon: UsersRound, permission: "user.manage" },
      { to: "/settings", label: "Settings", icon: Cog, permission: "settings.manage" },
    ],
  },
];

const ROLE_LABEL: Record<string, string> = {
  ADMIN: "Administrator",
  MANAGER: "Manager",
  STAFF: "Pharmacy Staff",
  CUSTOMER: "Customer",
};

const COLLAPSE_KEY = "pharmacy:nav-collapsed";

/* --------------------------------------------------------------- tooltip */

interface Tip {
  label: string;
  top: number;
}

/**
 * Labels for the collapsed rail, positioned against the viewport.
 *
 * Fixed rather than absolute because the nav is a scroll container: a tooltip
 * living inside it would be clipped at the rail's edge, which is exactly where
 * it needs to appear. One element is shared by every row — only one can be
 * hovered at a time.
 */
function RailTip({ tip }: { tip: Tip | null }) {
  if (!tip) return null;
  return (
    <div
      style={{ top: tip.top }}
      className={cn(
        "pointer-events-none fixed left-[4.25rem] z-50 -translate-y-1/2",
        "rounded-md bg-ink px-2 py-1 text-[12px] font-medium text-surface shadow-md",
        "whitespace-nowrap",
      )}
    >
      {tip.label}
    </div>
  );
}

/* ------------------------------------------------------------------- nav */

function NavLinks({
  collapsed,
  onNavigate,
  onTip,
}: {
  collapsed: boolean;
  onNavigate?: () => void;
  onTip?: (tip: Tip | null) => void;
}) {
  const { can } = useAuth();
  const features = useFeatures();
  const scroller = useRef<HTMLElement | null>(null);
  const [moreBelow, setMoreBelow] = useState(false);

  /**
   * Whether anything is hidden past the bottom edge.
   *
   * The rail deliberately has no visible scrollbar — it should read as a fixed
   * panel. That is fine until the destination list outgrows a short laptop
   * screen, at which point "no scrollbar" becomes "Master data does not
   * exist". A soft fade is the smallest honest signal: nothing moves, nothing
   * is added to the layout, and it only appears when there is genuinely more
   * below.
   */
  const measure = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    setMoreBelow(el.scrollHeight - el.scrollTop - el.clientHeight > 4);
  }, []);

  useEffect(() => {
    measure();
    const el = scroller.current;
    if (!el) return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure, collapsed]);

  const hover = (label: string) => ({
    onMouseEnter: (e: React.MouseEvent<HTMLElement>) => {
      if (!collapsed || !onTip) return;
      const r = e.currentTarget.getBoundingClientRect();
      onTip({ label, top: r.top + r.height / 2 });
    },
    onMouseLeave: () => onTip?.(null),
  });

  return (
    <div className="relative min-h-0 flex-1">
      {/* Sits above the rail's bottom edge, ignores the pointer, and fades in
          only when the list actually overflows. */}
      <div
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-x-0 bottom-0 z-10 h-8",
          "bg-gradient-to-t from-surface to-transparent",
          "transition-opacity duration-200",
          moreBelow ? "opacity-100" : "opacity-0",
        )}
      />
    <nav
      ref={scroller}
      onScroll={measure}
      className={cn(
        // no-scrollbar, not overflow-hidden: at 1366x768 the rail genuinely
        // does not fit, and clipping it would put Master data out of reach.
        // Gaps are tight on purpose. Seventeen destinations is a lot for a
        // flat rail, and every pixel of section spacing is a pixel of "Master
        // data" hanging below the fold on a shorter screen.
        "no-scrollbar h-full overflow-y-auto py-2",
        collapsed ? "space-y-2 px-2" : "space-y-3 px-3",
      )}
    >
      {NAV.map((group, groupIndex) => {
        const visible = group.items.filter((item) => {
          if (item.permission && !can(item.permission)) return false;
          // Absent while the flags are still loading counts as on, so the rail
          // does not flicker items in on every navigation.
          if (item.feature && features.data) {
            return features.data.enabled[item.feature] !== false;
          }
          return true;
        });
        if (visible.length === 0) return null;

        return (
          <div key={group.section}>
            {collapsed ? (
              // A section heading has nowhere to go at 64px wide. A hairline
              // keeps the grouping legible without pretending to be a label.
              // Nothing above the first group, so it needs no separator.
              groupIndex > 0 && <div className="mx-2 mb-2 border-t border-line" />
            ) : (
              <p className="mb-1 px-2.5 text-[10px] font-semibold tracking-widest text-ink-faint uppercase">
                {group.section}
              </p>
            )}
            {/* No gap between rows when expanded: each already carries 6px of
                  its own padding, so the 2px was invisible and cost 32px of
                  rail across seventeen items. */}
            <ul className={cn(collapsed ? "space-y-1" : "")}>
              {visible.map(({ to, label, icon: Icon }) => (
                <li key={to}>
                  <NavLink
                    to={to}
                    end={to === "/"}
                    onClick={onNavigate}
                    aria-label={collapsed ? label : undefined}
                    {...hover(label)}
                    className={({ isActive }) =>
                      cn(
                        "flex items-center rounded-lg text-sm font-medium transition-colors",
                        collapsed
                          ? "h-10 w-10 justify-center"
                          : "gap-2.5 px-2.5 py-1.5",
                        isActive
                          ? "bg-brand-soft text-brand"
                          : "text-ink-soft hover:bg-muted hover:text-ink",
                      )
                    }
                  >
                    <Icon className="size-4 shrink-0" />
                    {!collapsed && <span className="truncate">{label}</span>}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </nav>
    </div>
  );
}

/* -------------------------------------------------------------- identity */

function Identity({
  collapsed,
  onTip,
}: {
  collapsed: boolean;
  onTip?: (tip: Tip | null) => void;
}) {
  const { user, signOut } = useAuth();
  if (!user) return null;

  const role = ROLE_LABEL[user.role] ?? user.role;

  if (collapsed) {
    return (
      <div className="flex flex-col items-center gap-1 border-t border-line px-2 py-3">
        <div
          onMouseEnter={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            onTip?.({
              label: `${user.full_name} · ${role}`,
              top: r.top + r.height / 2,
            });
          }}
          onMouseLeave={() => onTip?.(null)}
          className="flex size-8 shrink-0 cursor-default items-center justify-center rounded-full bg-brand text-[13px] font-semibold text-white"
        >
          {user.full_name.charAt(0)}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={signOut}
          aria-label="Sign out"
          onMouseEnter={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            onTip?.({ label: "Sign out", top: r.top + r.height / 2 });
          }}
          onMouseLeave={() => onTip?.(null)}
          className="size-9 justify-center px-0"
        >
          <LogOut className="size-4" />
        </Button>
      </div>
    );
  }

  return (
    <div className="border-t border-line p-3">
      <div className="flex items-center gap-2.5 rounded-lg px-2 py-1.5">
        <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-brand text-[13px] font-semibold text-white">
          {user.full_name.charAt(0)}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium text-ink">
            {user.full_name}
          </p>
          <p className="truncate text-[11px] text-ink-faint">
            {role}
            {user.warehouse_name && ` · ${user.warehouse_name}`}
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={signOut}
          aria-label="Sign out"
          className="shrink-0 px-2"
        >
          <LogOut className="size-4" />
        </Button>
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- brand */

function Brand({
  collapsed = false,
  bordered = true,
}: {
  collapsed?: boolean;
  bordered?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex h-14 shrink-0 items-center gap-2.5",
        collapsed ? "justify-center px-2" : "px-5",
        bordered && "border-b border-line",
      )}
    >
      <div className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-brand">
        <span className="text-[13px] font-bold text-white">℞</span>
      </div>
      {!collapsed && (
        <div className="min-w-0">
          <p className="truncate text-[13px] leading-tight font-semibold tracking-tight">
            Pharmacy Inventory
          </p>
          <p className="text-[10px] leading-tight text-ink-faint">
            Multi-branch chain
          </p>
        </div>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- shell */

export function Shell() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [tip, setTip] = useState<Tip | null>(null);
  const { pathname } = useLocation();

  // Read once, lazily: touching localStorage on every render is wasteful, and
  // reading it after mount would flash the expanded rail on every page load.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === "1",
  );

  const toggleCollapsed = useCallback(() => {
    setCollapsed((value) => {
      localStorage.setItem(COLLAPSE_KEY, value ? "0" : "1");
      return !value;
    });
    setTip(null);
  }, []);

  // Close the drawer whenever the route changes.
  useEffect(() => setDrawerOpen(false), [pathname]);

  // A tooltip anchored to a row that has scrolled away is a floating label
  // pointing at nothing.
  useEffect(() => setTip(null), [collapsed, pathname]);

  // Escape closes the drawer, and the page behind it must not scroll while
  // the overlay is up.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawerOpen(false);
    document.addEventListener("keydown", onKey);
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
    };
  }, [drawerOpen]);

  return (
    // h-dvh + overflow-hidden: the shell owns the viewport and nothing outside
    // the main column scrolls.
    <div className="flex h-dvh overflow-hidden bg-canvas">
      {/* ------------------------------------------------- desktop sidebar */}
      <aside
        className={cn(
          "hidden shrink-0 flex-col overflow-hidden border-r border-line bg-surface lg:flex",
          // Only the width animates. Labels are simply clipped by the aside as
          // it narrows, which reads as the rail sliding shut rather than as
          // text disappearing.
          "transition-[width] duration-200 ease-out",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <Brand collapsed={collapsed} />
        <NavLinks collapsed={collapsed} onTip={setTip} />

        <div
          className={cn(
            "shrink-0 border-t border-line",
            collapsed ? "px-2 py-2" : "px-3 py-2",
          )}
        >
          <button
            onClick={toggleCollapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            onMouseEnter={(e) => {
              if (!collapsed) return;
              const r = e.currentTarget.getBoundingClientRect();
              setTip({ label: "Expand sidebar", top: r.top + r.height / 2 });
            }}
            onMouseLeave={() => setTip(null)}
            className={cn(
              // Styled as a nav row on purpose: it belongs to the same family
              // as everything above it, so it needs no visual language of
              // its own.
              "flex items-center rounded-lg text-sm font-medium",
              "text-ink-faint transition-colors hover:bg-muted hover:text-ink",
              collapsed ? "h-10 w-10 justify-center" : "w-full gap-2.5 px-2.5 py-2",
            )}
          >
            {collapsed ? (
              <PanelLeftOpen className="size-4 shrink-0" />
            ) : (
              <>
                <PanelLeftClose className="size-4 shrink-0" />
                <span className="truncate">Collapse</span>
              </>
            )}
          </button>
        </div>

        <Identity collapsed={collapsed} onTip={setTip} />
      </aside>

      <RailTip tip={tip} />

      {/* -------------------------------------------------- mobile drawer */}
      {drawerOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div
            className="absolute inset-0 bg-ink/25 backdrop-blur-[2px]"
            onClick={() => setDrawerOpen(false)}
            aria-hidden
          />
          <aside className="relative flex h-full w-[17rem] flex-col bg-surface shadow-2xl">
            <div className="flex items-center justify-between border-b border-line pr-2">
              <Brand bordered={false} />
              <button
                onClick={() => setDrawerOpen(false)}
                aria-label="Close navigation"
                className="rounded-md p-1.5 text-ink-faint hover:bg-muted hover:text-ink"
              >
                <X className="size-4" />
              </button>
            </div>
            {/* Never collapsed on mobile: the drawer is already hidden by
                default, so an icon rail would save nothing. */}
            <NavLinks collapsed={false} onNavigate={() => setDrawerOpen(false)} />
            <Identity collapsed={false} />
          </aside>
        </div>
      )}

      {/* --------------------------------------------------- main content */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-surface px-4 lg:hidden">
          <button
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation"
            className="-ml-1 rounded-md p-1.5 text-ink-soft hover:bg-muted hover:text-ink"
          >
            <Menu className="size-5" />
          </button>
          <div className="flex items-center gap-2">
            <div className="flex size-6 items-center justify-center rounded-md bg-brand">
              <span className="text-[11px] font-bold text-white">℞</span>
            </div>
            <span className="text-sm font-semibold tracking-tight">
              Pharmacy Inventory
            </span>
          </div>
        </header>

        {/* The one scrolling element on the page. */}
        {/*
          A flex column, and the centring wrapper stretches inside it.

          Every page here flows from the top and stops, so the wrapper's height
          never mattered — until Ask, which is a chat window: its composer has
          to sit on the bottom edge whether there is one answer or forty. That
          needs `h-full` to resolve, and a percentage height resolves against
          the parent's *height*, which an auto-height wrapper does not have. So
          the wrapper became a stretched flex item.

          `flex-1` with the default `min-height: auto` still grows past the
          viewport when a page is taller, so nothing that flows normally
          changes — only pages that ask for the full height now get it.
        */}
        <main className="flex min-w-0 flex-1 flex-col overflow-y-auto px-4 py-5 sm:px-6 sm:py-6 lg:px-8">
          <div className="mx-auto flex w-full max-w-[88rem] flex-1 flex-col">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-ink sm:text-[22px]">
          {title}
        </h1>
        {description && (
          <p className="mt-1 text-[13px] text-ink-soft sm:text-sm">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
