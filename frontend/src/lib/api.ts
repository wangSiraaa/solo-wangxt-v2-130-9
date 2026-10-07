const API_BASE = '';

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  return response.json();
}

export interface Stage {
  name: string;
  status: string;
  attempt: number;
  detail: Record<string, unknown>;
}

export interface Job {
  id: number;
  status: string;
  current_stage: string;
  generation_key: string;
  snapshot_version: number;
  input_summary: Record<string, number | string>;
  algorithm: Record<string, unknown>;
  diagnostics: Record<string, any>;
  stages: Stage[];
}

export interface ResidualRow {
  line_code: string;
  observed_delta_m: number;
  adjusted_delta_m: number | null;
  correction_m: number | null;
  residual: number | null;
}

export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  // No Content-Type header: the browser sets the multipart boundary.
  const response = await fetch(`${API_BASE}${path}`, { method: 'POST', body: form });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  return response.json();
}

export type RevisionRowStatus =
  | 'applicable'
  | 'version_conflict'
  | 'missing_record'
  | 'duplicate_line'
  | 'invalid_row'
  | 'applied'
  | 'blocked_by_strategy';

export interface RevisionRow {
  row: number;
  line_code: string | null;
  status: RevisionRowStatus;
  detail?: string;
  observation_id?: number;
  current?: { lock_version: number; observed_delta_m: number; distance_m: number };
  requested?: { lock_version: number; observed_delta_m: number; distance_m: number };
  current_lock_version?: number;
  lock_version_before?: number;
  lock_version_after?: number;
}

export interface RevisionPreview {
  project_id: number;
  total_rows: number;
  counts: Record<string, number>;
  rows: RevisionRow[];
}

export interface RevisionReceipt {
  project_id: number;
  strategy: 'all_or_nothing' | 'per_row';
  applied_count: number;
  skipped_count: number;
  draft_changed: boolean;
  rows: RevisionRow[];
}
