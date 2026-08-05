/**
 * API client.
 *
 * The access token lives in memory only — never localStorage, which is an XSS
 * token-theft vector. The refresh token is an httpOnly cookie the browser
 * sends automatically and JavaScript can never read.
 *
 * On a 401 the client silently refreshes once and replays the request. A
 * single in-flight refresh is shared, so ten parallel requests hitting an
 * expired token produce one refresh, not ten.
 */

export interface ApiProblem {
  type: string;
  title: string;
  status: number;
  detail: string;
  request_id?: string;
  errors?: { field: string; message: string }[];
}

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ApiProblem;

  constructor(status: number, problem: ApiProblem) {
    super(problem.detail || problem.title);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }

  /** Field-level messages, for inline form errors. */
  get fieldErrors(): Record<string, string> {
    return Object.fromEntries(
      (this.problem.errors ?? []).map((e) => [e.field, e.message]),
    );
  }
}

let accessToken: string | null = null;
let refreshPromise: Promise<boolean> | null = null;
let onAuthLost: (() => void) | null = null;

export const setAccessToken = (token: string | null) => {
  accessToken = token;
};
export const getAccessToken = () => accessToken;
export const setAuthLostHandler = (fn: () => void) => {
  onAuthLost = fn;
};

async function refreshAccessToken(): Promise<boolean> {
  refreshPromise ??= (async () => {
    try {
      const res = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) return false;
      const body = await res.json();
      accessToken = body.access_token;
      return true;
    } catch {
      return false;
    } finally {
      // Cleared on the next tick so concurrent callers all see this result.
      setTimeout(() => {
        refreshPromise = null;
      }, 0);
    }
  })();
  return refreshPromise;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

async function request<T>(
  path: string,
  { method = "GET", body, signal }: RequestOptions = {},
  isRetry = false,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const res = await fetch(path, {
    method,
    headers,
    credentials: "include",
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 401 && !isRetry && !path.includes("/auth/")) {
    if (await refreshAccessToken()) {
      return request<T>(path, { method, body, signal }, true);
    }
    onAuthLost?.();
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const parsed = text ? JSON.parse(text) : null;

  if (!res.ok) {
    throw new ApiError(
      res.status,
      parsed ?? {
        type: "about:blank",
        title: "Request failed",
        status: res.status,
        detail: res.statusText,
      },
    );
  }
  return parsed as T;
}

/**
 * A GET whose body is not JSON — at present only the printable tax invoice.
 *
 * It exists because `window.open("/api/v1/...")` cannot work: a top-level
 * navigation carries no Authorization header, so the new tab renders a 401
 * every time. The document has to be fetched like any other authenticated
 * request and then handed to the browser.
 *
 * Deliberately not folded into `request`, which parses JSON unconditionally
 * and would throw on the first `<`.
 */
async function getText(path: string, isRetry = false): Promise<string> {
  const headers: Record<string, string> = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const res = await fetch(path, { headers, credentials: "include" });

  if (res.status === 401 && !isRetry) {
    if (await refreshAccessToken()) return getText(path, true);
    onAuthLost?.();
  }

  const text = await res.text();
  if (!res.ok) {
    // An error here is problem+json even though success is HTML, but a proxy
    // or gateway can still return a plain HTML error page — so parsing is
    // allowed to fail without masking the real status.
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = null;
    }
    throw new ApiError(
      res.status,
      parsed ?? {
        type: "about:blank",
        title: "Request failed",
        status: res.status,
        detail: res.statusText,
      },
    );
  }
  return text;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) =>
    request<T>(path, { signal }),
  getText,
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  refresh: refreshAccessToken,
};

/** Build a query string, dropping empty values so URLs stay clean. */
export function qs(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const str = search.toString();
  return str ? `?${str}` : "";
}
