import { useState } from 'react';
import {
  uploadRevisions,
  type RevisionConfirmResult,
  type RevisionPreview,
  type RevisionRow,
  type RevisionStrategy
} from '../lib/api';

const STATE_LABEL: Record<RevisionRow['state'], string> = {
  applicable: '可应用',
  already_applied: '已是该值（幂等跳过）',
  version_conflict: '版本冲突',
  missing_record: '缺失记录',
  duplicate_row: '重复行',
  invalid_row: '非法行'
};

const STATE_CLASS: Record<RevisionRow['state'], string> = {
  applicable: 'rev-applicable',
  already_applied: 'rev-applied',
  version_conflict: 'rev-block',
  missing_record: 'rev-block',
  duplicate_row: 'rev-block',
  invalid_row: 'rev-block'
};

interface Props {
  projectId: number;
  onApplied?: () => void;
}

export function RevisionPanel({ projectId, onApplied }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<RevisionPreview | null>(null);
  const [confirm, setConfirm] = useState<RevisionConfirmResult | null>(null);
  const [strategy, setStrategy] = useState<RevisionStrategy>('all_or_nothing');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  async function previewFile() {
    if (!file) return;
    setBusy(true);
    setMessage('');
    setConfirm(null);
    try {
      setPreview(await uploadRevisions<RevisionPreview>(projectId, 'preview', file));
    } catch (error) {
      setPreview(null);
      setMessage(`预览失败：${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function applySheet() {
    if (!file) return;
    setBusy(true);
    setMessage('');
    try {
      const result = await uploadRevisions<RevisionConfirmResult>(projectId, 'confirm', file, strategy);
      setConfirm(result);
      setPreview(null);
      setMessage(
        result.applied
          ? `已应用 ${result.applied_rows} 行；阻塞 ${result.blocking_rows} 行`
          : result.blocking_rows > 0
            ? '整批未应用：存在阻塞行（全成功策略）'
            : '文件中的值均已应用过，本次确认未重复改值'
      );
      onApplied?.();
    } catch (error) {
      setMessage(`应用被拒绝：${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  const rows = confirm ? confirm.receipts : preview?.rows ?? [];

  return (
    <section className="card">
      <h2>外业测段修订 CSV（先预览，后确认）</h2>
      <p className="hint">
        列：<code>line_code, lock_version, observed_delta_m, distance_m</code>
        （支持中文表头：测段编号/原锁版本/高差/长度）。原观测只经乐观锁修订，旧快照与旧 Job 不变。
      </p>
      <div className="rev-controls">
        <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button onClick={previewFile} disabled={!file || busy}>
          预览校验
        </button>
        <label className="strategy">
          确认策略
          <select value={strategy} onChange={(e) => setStrategy(e.target.value as RevisionStrategy)}>
            <option value="all_or_nothing">全成功（任一行冲突则整批不应用）</option>
            <option value="per_row">逐行结果（合法行应用，冲突行回报）</option>
          </select>
        </label>
        <button
          className="primary"
          onClick={applySheet}
          disabled={!file || busy || (preview !== null && !preview.ready_to_apply && strategy === 'all_or_nothing')}
        >
          确认应用
        </button>
      </div>

      {preview && (
        <div className="rev-summary">
          共 {preview.total_rows} 行：可应用 <b>{preview.applicable_rows}</b>，阻塞 <b>{preview.blocking_rows}</b>
          {preview.blocking_rows === 0 && <span className="rev-ok"> · 可以整批应用</span>}
        </div>
      )}
      {confirm && (
        <div className="rev-summary">
          回执：已应用 <b>{confirm.applied_rows}</b> 行
          {confirm.already_applied_rows !== undefined && <>，已是该值 <b>{confirm.already_applied_rows}</b> 行</>}
          ，阻塞 <b>{confirm.blocking_rows}</b> 行（策略：{confirm.strategy === 'per_row' ? '逐行结果' : '全成功'}）
        </div>
      )}
      {message && <div className="message">{message}</div>}

      {rows.length > 0 && (
        <table className="rev-table">
          <thead>
            <tr>
              <th>CSV 行</th>
              <th>测段稳定 ID</th>
              <th>状态</th>
              <th>原锁版本</th>
              <th>当前版本</th>
              <th>当前高差/长度</th>
              <th>修订高差/长度</th>
              <th>新版本 / 审计</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.row_number} className={STATE_CLASS[row.state]}>
                <td>{row.row_number}</td>
                <td>{row.line_code ?? '—'}</td>
                <td>{STATE_LABEL[row.state]}</td>
                <td>{row.expected_lock_version ?? '—'}</td>
                <td>{row.current_lock_version ?? '—'}</td>
                <td>
                  {fmt(row.current_observed_delta_m)} / {fmt(row.current_distance_m)}
                </td>
                <td>
                  {fmt(row.proposed_observed_delta_m)} / {fmt(row.proposed_distance_m)}
                </td>
                <td>
                  {row.new_lock_version ?? '—'}
                  {row.audit_event_id ? ` / audit#${row.audit_event_id}` : ''}
                </td>
                <td>
                  {row.detail}
                  {row.issues.length > 0 && (
                    <>
                      {' '}
                      ({row.issues.join('，')})
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function fmt(value: number | null): string {
  return value === null ? '—' : String(value);
}
