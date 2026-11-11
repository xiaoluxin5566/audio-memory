import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { fieldTooltipPosition, nullValueLabel } from '../api/databasePresentation.js';

function FieldHeader({ column }) {
  const id = useId(), anchor = useRef(null), tooltip = useRef(null), timer = useRef(null);
  const [open, setOpen] = useState(false), [position, setPosition] = useState({left:12, top:12});
  const cancelHide = () => clearTimeout(timer.current);
  const show = () => { cancelHide(); setOpen(true); };
  const hide = () => {
    cancelHide();
    if (document.activeElement !== anchor.current) timer.current = setTimeout(() => setOpen(false), 120);
  };
  useEffect(() => () => clearTimeout(timer.current), []);
  useLayoutEffect(() => {
    if (!open || !anchor.current || !tooltip.current) return;
    setPosition(fieldTooltipPosition(anchor.current.getBoundingClientRect(),
      {width:window.innerWidth, height:window.innerHeight}, tooltip.current.getBoundingClientRect()));
  }, [open, column.description]);
  useEffect(() => {
    if (!open) return undefined;
    const close = event => {
      if (event.type === 'resize' || !tooltip.current?.contains(event.target)) setOpen(false);
    };
    const outside = event => {
      if (!anchor.current?.contains(event.target) && !tooltip.current?.contains(event.target)) setOpen(false);
    };
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    document.addEventListener('pointerdown', outside);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
      document.removeEventListener('pointerdown', outside);
    };
  }, [open]);
  return <th scope="col" onMouseEnter={column.description ? show : undefined} onMouseLeave={hide}>
    {column.description ? <button ref={anchor} type="button" className="db-field-trigger"
      aria-label={`${column.name} 字段说明`} aria-describedby={open ? id : undefined}
      onFocus={show} onBlur={() => { cancelHide(); setOpen(false); }} onClick={show}
      onKeyDown={event => { if (event.key === 'Escape' && open) { event.preventDefault(); event.stopPropagation(); cancelHide(); setOpen(false); } }}>
      <code>{column.name}</code><span aria-hidden="true" className="db-field-info">ⓘ</span>
    </button> : <code>{column.name}</code>}
    <small>{column.key ? '主键／唯一键 · ' : ''}{column.type || ''}</small>
    {open && column.description && createPortal(<div ref={tooltip} id={id} role="tooltip"
      className="db-field-tooltip" style={position} onMouseEnter={cancelHide} onMouseLeave={hide}>
      <code>{column.name}</code><p>{column.description}</p>
    </div>, document.body)}
  </th>;
}

export function DataValue({ value, columnName }) {
  if (value === null || value === undefined) return <span className="db-null" title="NULL：未提供、未知或未记录">{nullValueLabel(columnName)}</span>;
  const text = typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
  if (text.length <= 100 && !text.includes('\n')) return <span>{text}</span>;
  return <details className="db-value"><summary>{value?.kind === 'binary' ? `${value.byte_length.toLocaleString()} 字节 · ${value.encoding.toUpperCase()}` : `${text.slice(0, 70)}…`}</summary><pre tabIndex="0">{text}</pre></details>;
}

export function DatabaseGrid({ columns = [], rows = [], empty = '这张表暂无记录' }) {
  return <div className="db-grid-scroll" tabIndex="0" role="region" aria-label="数据库表格，可横向滚动"><table className="db-grid" style={columns.every(c => c.width) && columns.length ? {minWidth:columns.reduce((sum,c) => sum+c.width,0)} : undefined}>{columns.some(c => c.width) && <colgroup>{columns.map(c => <col key={c.name} style={{width:c.width}} />)}</colgroup>}<thead><tr>{columns.map(c => <FieldHeader key={c.name} column={c} />)}</tr></thead><tbody>{rows.map((row, i) => <tr key={i}>{columns.map(c => <td key={c.name}>{c.render ? c.render(row[c.name], row) : <DataValue value={row[c.name]} columnName={c.name} />}</td>)}</tr>)}</tbody></table>{!rows.length && <p className="db-empty">{empty}</p>}</div>;
}

export function ChangeRows({ rows }) {
  const [opened, setOpened] = useState(null);
  return <div className="db-change-rows">{rows.map((row, i) => <article key={i} className="db-change-record"><div><code>{JSON.stringify(row.row_key)}</code><button type="button" className="secondary compact" onClick={() => setOpened(opened === i ? null : i)}>{opened === i ? '收起整条记录' : '查看整条记录'}</button></div><DatabaseGrid columns={[{ name: '字段 / Field' }, { name: '原值 / Before' }, { name: '新值 / After' }]} rows={row.fields.map(f => ({ '字段 / Field': f.field, '原值 / Before': f.before, '新值 / After': f.after }))} empty="临时记录已创建并释放，最终未留存。" />{opened === i && <details open><summary>完整记录与写入过程</summary><pre>{JSON.stringify({ before: row.before, after: row.after, steps: row.steps }, null, 2)}</pre></details>}</article>)}</div>;
}
