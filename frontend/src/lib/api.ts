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

export type RevisionState =
  | 'applicable'
  | 'already_applied'
  | 'version_conflict'
  | 'missing_record'
  | 'duplicate_row'
  | 'invalid_row';

export type RevisionStrategy = 'all_or_nothing' | 'per_row';

export interface RevisionRow {
  row_number: number;
  line_code: string | null;
  state: RevisionState;
  detail: string | null;
  issues: string[];
  expected_lock_version: number | null;
  current_lock_version: number | null;
  new_lock_version: number | null;
  current_observed_delta_m: number | null;
  current_distance_m: number | null;
  proposed_observed_delta_m: number | null;
  proposed_distance_m: number | null;
  audit_event_id: number | null;
}

export interface RevisionPreview {
  project_id: number;
  total_rows: number;
  applicable_rows: number;
  blocking_rows: number;
  ready_to_apply: boolean;
  counts: Record<RevisionState, number>;
  rows: RevisionRow[];
}

export interface RevisionConfirmResult {
  project_id: number;
  strategy: RevisionStrategy;
  applied: boolean;
  total_rows: number;
  applied_rows: number;
  already_applied_rows?: number;
  blocking_rows: number;
  counts?: Record<string, number>;
  detail?: string;
  receipts: RevisionRow[];
}

export async function uploadRevisions<T extends RevisionPreview | RevisionConfirmResult>(
  projectId: number,
  action: 'preview' | 'confirm',
  file: File,
  strategy: RevisionStrategy = 'all_or_nothing'
): Promise<T> {
  const form = new FormData();
  form.append('file', file);
  if (action === 'confirm') form.append('strategy', strategy);
  const response = await fetch(`/api/projects/${projectId}/observations/revisions/${action}`, {
    method: 'POST',
    body: form,
  });
  if (!response.ok) {
    let message = `${response.status}`;
    try {
      const body = await response.json();
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  return response.json();
}
