// Backend API client. Runs through the Vite proxy in dev (vite.config.ts)
// and against the same origin in prod.

export type BackendAvailability = "open" | "maintenance" | "closed";

export type BackendDepartment = {
  code: string;
  queue_length: number;
  estimated_wait_minutes: number;
  availability: BackendAvailability;
  updated_at: string;
};

export type DepartmentPatch = {
  queue_length?: number;
  estimated_wait_minutes?: number;
  availability?: BackendAvailability;
};

const API_BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body?.detail ?? JSON.stringify(body);
    } catch {
      detail = await res.text();
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string; hospital: string }>("/health"),
  listDepartments: () => request<BackendDepartment[]>("/departments"),
  patchDepartment: (code: string, patch: DepartmentPatch) =>
    request<BackendDepartment>(`/departments/${code}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  metrics: () =>
    request<{
      journey: {
        completed_journeys: number;
        avg_journey_minutes: number | null;
        longest_journey_minutes: number | null;
        delay_points: { test_code: string; avg_gap_minutes: number }[];
      };
      feedback: {
        sentiment_counts: Record<string, number>;
        priority_counts: Record<string, number>;
        avg_rating: number | null;
        top_tags: [string, number][];
      };
    }>("/metrics"),
};
