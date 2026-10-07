import { useState } from 'react';
import {
  apiUpload,
  type RevisionPreview,
  type RevisionReceipt,
  type RevisionRow
} from '../lib/api';

const STATUS_LABELS: Record<string, string> = {
  applicable: '可应用',
  version_conflict: '版本冲突',
  missing_record: '缺失记录',
  duplicate_line: '重复行',
  invalid_row: '无效行',
  applied: '已应用',
  blocked_by_strategy: '整单驳回'
};

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function RowTable({ rows }: { rows: RevisionRow[] }) {
  return (
    <table className="residual-table revision-table">
      <thead>
        <tr>
          <th>行</th>
          <th>测段</th>
          <th>结果</th>
          <th>当前版本</th>
          <th>文件版本</th>
          <th>新高差 (m)</th>
          <th>新长度 (m)</th>
          <th>说明</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.row} className={`revision-${row.status}`}>
            <td>{row.row}</td>
            <td>{row.line_code ?? '—'}</td>
            <td>{statusLabel(row.status)}</td>
            <td>{row.current?.lock_version ?? row.current_lock_version ?? '—'}</td>
            <td>{row.requested?.lock_version ?? '—'}</td>
            <td>{row.requested ? row.requested.observed_delta_m.toFixed(6) : '—'}</td>
            <td>{row.requested ? row.requested.distance_m.toFixed(3) : '—'}</td>
            <td className="revision-detail">
              {row.status === 'applied'
                ? `锁版本 ${row.lock_version_before} → ${row.lock_version_after}`
                : (row.detail ?? '')}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function RevisionPanel({ projectId, onMessage }: { projectId: number; onMessage: (text: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [strategy, setStrategy] = useState<'all_or_nothing' | 'per_row'>('all_or_nothing');
  const [preview, setPreview] = useState<RevisionPreview | null>(null);
  const [receipt, setReceipt] = useState<RevisionReceipt | null>(null);
  const [busy, setBusy] = useState(false);

  function formData(): FormData {
    const form = new FormData();
    form.append('file', file!);
    form.append('strategy', strategy);
    return form;
  }

  async function run<T>(path: string, label: string): Promise<T | null> {
    if (!file) {
      onMessage('请先选择测段修订 CSV 文件');
      return null;
    }
    setBusy(true);
    try {
      return await apiUpload<T>(path, formData());
    } catch (error) {
      onMessage(`${label}失败：${(error as Error).message}`);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function doPreview() {
    const result = await run<RevisionPreview>(`/api/projects/${projectId}/revisions/preview`, '预览');
    if (!result) return;
    setPreview(result);
    setReceipt(null);
    const c = result.counts;
    onMessage(
      `预览 ${result.total_rows} 行：可应用 ${c.applicable ?? 0}，版本冲突 ${c.version_conflict ?? 0}，` +
        `缺失 ${c.missing_record ?? 0}，重复 ${c.duplicate_line ?? 0}，无效 ${c.invalid_row ?? 0}`
    );
  }

  async function doConfirm() {
    const result = await run<RevisionReceipt>(`/api/projects/${projectId}/revisions/confirm`, '确认');
    if (!result) return;
    setReceipt(result);
    onMessage(
      result.draft_changed
        ? `已应用 ${result.applied_count} 行，跳过 ${result.skipped_count} 行（审计事件已写入）`
        : `草稿未变更：${result.skipped_count} 行未应用`
    );
  }

  return (
    <section className="card">
      <h2>测段修订（CSV 预览 / 确认）</h2>
      <p className="revision-hint">
        CSV 列：line_code, lock_version, observed_delta_m, distance_m。确认时按当前草稿重新校验，
        仅通过乐观锁审计路径写入，旧任务快照不受影响。
      </p>
      <div className="toolbar">
        <label>
          修订文件
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setPreview(null);
              setReceipt(null);
            }}
          />
        </label>
        <label>
          应用策略
          <select value={strategy} onChange={(event) => setStrategy(event.target.value as typeof strategy)}>
            <option value="all_or_nothing">全成功（任一行失败则整单不应用）</option>
            <option value="per_row">逐行（可应用行写入，其余跳过）</option>
          </select>
        </label>
        <button onClick={doPreview} disabled={!file || busy}>
          预览修订
        </button>
        <button onClick={doConfirm} disabled={!file || busy} className="primary">
          确认应用
        </button>
      </div>

      {preview && !receipt && (
        <>
          <h3>预览结果</h3>
          <RowTable rows={preview.rows} />
        </>
      )}
      {receipt && (
        <>
          <h3>应用回执（{receipt.strategy === 'all_or_nothing' ? '全成功' : '逐行'}）</h3>
          <RowTable rows={receipt.rows} />
        </>
      )}
    </section>
  );
}
