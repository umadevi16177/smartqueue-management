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

export type ActiveJourney = {
  journey_id: number;
  status: string;
  current_index: number;
  sequenced_tests_json: string;
  created_at: string;
  updated_at: string;
  telegram_chat_id: number;
  display_name: string | null;
  patient_identifier: string | null;   // hospital-issued
  sequence_number: number | null;      // bot-issued, registration order
  language: string;
  current_test: string | null;
  current_token?: string | null;
  steps: {
    step_index: number;
    test_code: string;
    queue_token: string | null;
    department_status: string;
    reserved_for_time: string | null;
    completed_at: string | null;
  }[];
};

export type RegisteredPatient = {
  patient_id: string;
  sequence_number: number;
  display_name: string;
};

export type UnclaimedPatient = {
  id: number;
  patient_identifier: string;
  sequence_number: number;
  display_name: string | null;
  created_at: string;
};

export const api = {
  health: () => request<{ status: string; hospital: string }>("/health"),
  listDepartments: () => request<BackendDepartment[]>("/departments"),
  patchDepartment: (code: string, patch: DepartmentPatch) =>
    request<BackendDepartment>(`/departments/${code}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  listActiveJourneys: () => request<ActiveJourney[]>("/journeys/active"),
  completeCurrentStep: (journeyId: number) =>
    request<{
      journey_id: number;
      completed_test: string | null;
      journey: ActiveJourney;
    }>(`/journeys/${journeyId}/complete-current`, { method: "POST" }),
  registerPatient: (name: string, patientId?: string) =>
    request<RegisteredPatient>("/patients", {
      method: "POST",
      body: JSON.stringify({
        name,
        ...(patientId ? { patient_id: patientId } : {}),
      }),
    }),
  listUnclaimedPatients: () => request<UnclaimedPatient[]>("/patients/unclaimed"),
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
